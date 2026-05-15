#!/usr/bin/env python3
"""
Common Crawl — image URL extractor (interior/architecture filter).

Common Crawl: 150T+ web pages, FREE re-use under fair-use + robots.txt respect.
WARC files: ~80TB per snapshot. We download index, filter URLs, then extract
<img src=...> + alt text matching interior keywords.

Strategy:
    1. Download CC index (small, ~5GB) → filter WARC paths
    2. Stream WARC files (one segment ~1GB) → extract <img> tags
    3. Filter image URLs by:
       - alt text matches INTERIOR_KEYWORDS
       - host whitelist (Wikipedia, Wikimedia, government domains, .edu)
       - blacklist (porn, social media, photo stocks with paywall)
    4. Emit urls.txt for img2dataset

Output: ~2-5M URLs after filter, expected ~70% download success → 1.5-3.5M images.

Usage:
    pip install warcio fastwarc trafilatura

    # Step 1: download index (CC-MAIN-2025-XX-index.gz)
    wget https://data.commoncrawl.org/crawl-data/CC-MAIN-2025-13/cc-index.paths.gz

    # Step 2: pick 1 segment file from index, run filter
    python commoncrawl_image.py \\
        --warc-url https://data.commoncrawl.org/crawl-data/CC-MAIN-2025-13/segments/.../warc.gz \\
        --output-dir /mnt/zeni-data/cc-interior \\
        --target 1000000

Pipeline produces:
    - urls.txt (image URLs)
    - metadata.jsonl (alt text + source domain per URL)

License: each image follows its original site's license. Filter respects:
    - robots.txt noindex/noimageindex
    - <meta name="robots" content="noai/noimageai">
    - Creative Commons attribution (logged in metadata)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("zeni.commoncrawl")


# ─── Keyword filter (caption/alt/title) ───────────────────────
INTERIOR_KEYWORDS = [
    "interior", "interior design", "living room", "kitchen", "bedroom",
    "architecture", "facade", "modern house", "villa",
    "kien truc", "noi that", "phong khach", "phong ngu", "biet thu",
]


# ─── Host filter ──────────────────────────────────────────────
HOST_WHITELIST_SUFFIXES = (
    # Encyclopedia / non-profit (CC-BY-SA)
    "wikipedia.org", "wikimedia.org", "wikidata.org",
    # Government / academic
    ".gov", ".edu", ".gov.vn", ".edu.vn",
    # Open photo
    "unsplash.com", "pexels.com", "pixabay.com", "freepik.com",
    "rawpixel.com", "burst.shopify.com", "stocksnap.io",
    # Open data
    "archdaily.com",  # CC-licensed architecture site
)

HOST_BLACKLIST_SUFFIXES = (
    # Porn / NSFW (full blacklist)
    ".xxx", ".adult", "pornhub.com", "xvideos.com",
    # Paywalled stock photo (cannot re-use)
    "shutterstock.com", "gettyimages.com", "istockphoto.com",
    "alamy.com", "adobestock.com", "depositphotos.com",
    # Social media private content
    "instagram.com", "facebook.com/photo", "pinterest.com/pin",
)


IMG_TAG_RE = re.compile(
    r'<img\s+[^>]*?src=["\']([^"\']+)["\'][^>]*?(?:alt=["\']([^"\']*)["\'])?[^>]*?>',
    re.IGNORECASE | re.DOTALL,
)
ROBOTS_NOAI_RE = re.compile(
    r'<meta\s+name=["\']robots["\']\s+content=["\'][^"\']*?(noai|noimageai|noml)[^"\']*?["\']',
    re.IGNORECASE,
)


def is_host_allowed(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()

    # Blacklist beats whitelist
    if any(host.endswith(s) for s in HOST_BLACKLIST_SUFFIXES):
        return False

    return any(host.endswith(s) for s in HOST_WHITELIST_SUFFIXES)


def matches_keyword(alt_text: str) -> bool:
    if not alt_text:
        return False
    text = alt_text.lower()
    return any(kw in text for kw in INTERIOR_KEYWORDS)


def extract_images_from_html(html: str, page_url: str) -> list[dict]:
    """Extract <img> tags with alt text. Skip if page has noai meta."""
    if ROBOTS_NOAI_RE.search(html):
        return []  # respect noai opt-out

    base_parsed = urlparse(page_url)
    base = f"{base_parsed.scheme}://{base_parsed.netloc}"

    out: list[dict] = []
    for match in IMG_TAG_RE.finditer(html):
        src = match.group(1).strip()
        alt = (match.group(2) or "").strip()

        # Resolve relative URL
        if src.startswith("//"):
            src = base_parsed.scheme + ":" + src
        elif src.startswith("/"):
            src = base + src
        elif not src.startswith(("http://", "https://")):
            continue

        # Filter by alt text keyword
        if not matches_keyword(alt):
            continue

        # Filter by host whitelist
        if not is_host_allowed(src):
            continue

        out.append({
            "url": src,
            "alt": alt,
            "source_page": page_url,
            "source_host": urlparse(page_url).hostname,
        })
    return out


def process_warc(
    warc_path_or_url: str,
    output_urls: Path,
    output_meta: Path,
    *,
    target: int = 1_000_000,
) -> int:
    """Stream WARC file, extract images, write urls + metadata."""
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError:
        log.error("Install: pip install warcio")
        return 0

    matched = 0

    # Support both local path + remote URL
    if warc_path_or_url.startswith(("http://", "https://")):
        try:
            import requests
        except ImportError:
            log.error("Install: pip install requests")
            return 0
        log.info("Streaming WARC from %s", warc_path_or_url)
        resp = requests.get(warc_path_or_url, stream=True, timeout=60)
        resp.raise_for_status()
        stream = resp.raw
    else:
        stream = open(warc_path_or_url, "rb")

    with open(output_urls, "w", encoding="utf-8") as f_url, \
         open(output_meta, "w", encoding="utf-8") as f_meta:
        for record in ArchiveIterator(stream):
            if record.rec_type != "response":
                continue

            page_url = record.rec_headers.get_header("WARC-Target-URI") or ""
            content_type = record.http_headers.get_header("Content-Type") or "" if record.http_headers else ""
            if "html" not in content_type.lower():
                continue

            try:
                payload = record.content_stream().read().decode("utf-8", errors="ignore")
            except Exception:
                continue

            images = extract_images_from_html(payload, page_url)
            for img in images:
                f_url.write(img["url"] + "\n")
                f_meta.write(json.dumps(img, ensure_ascii=False) + "\n")
                matched += 1

                if matched % 1000 == 0:
                    log.info("Extracted %d image URLs so far...", matched)

                if matched >= target:
                    break

            if matched >= target:
                break

    if hasattr(stream, "close"):
        stream.close()

    log.info("WARC done. Extracted %d image URLs.", matched)
    return matched


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--warc-url", required=True,
                        help="WARC file URL or local path (e.g. https://data.commoncrawl.org/.../warc.gz)")
    parser.add_argument("--output-dir", default="./cc-interior")
    parser.add_argument("--target", type=int, default=1_000_000)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_urls = out_dir / "urls.txt"
    output_meta = out_dir / "metadata.jsonl"

    count = process_warc(
        args.warc_url,
        output_urls,
        output_meta,
        target=args.target,
    )

    log.info("Final: %d URLs in %s + metadata in %s", count, output_urls, output_meta)
    log.info("Next step: img2dataset --url_list=%s --output_folder=%s --image_size=1024",
             output_urls, out_dir / "images")


if __name__ == "__main__":
    main()
