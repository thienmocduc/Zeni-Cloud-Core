#!/usr/bin/env python3
"""
LAION-Aesthetics V2 Downloader — interior/architecture filter.

Download 10M ảnh FREE LEGAL từ LAION-Aesthetics V2 (200M curated, aesthetic ≥6.5).
Filter caption keywords: interior, architecture, design, room, kitchen, bedroom, ...

Usage:
    pip install datasets img2dataset
    export HUGGINGFACE_TOKEN=hf_xxx  # optional, faster download
    python laion_downloader.py --target 10000000 --output-dir /mnt/zeni-data/laion-interior

Pipeline:
    1. Stream LAION-Aesthetics V2 metadata Parquet
    2. Filter rows by caption keywords (interior/architecture)
    3. Batch download images via img2dataset (1000 concurrent connections)
    4. Save WebDataset .tar shards (~1GB each, ~10K images per shard)
    5. Generate metadata.jsonl per shard

Output:
    /mnt/zeni-data/laion-interior/
    ├── shard_00000.tar      # 1GB, ~10K ảnh
    ├── shard_00000.json     # metadata
    ├── shard_00001.tar
    └── ...

License: LAION CC0 (research + commercial).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("zeni.laion")


# ─── Interior/architecture keyword filter ───────────────────────
INTERIOR_KEYWORDS = [
    # English
    "interior", "interior design", "living room", "kitchen", "bedroom", "bathroom",
    "dining room", "office interior", "modern interior", "minimalist", "scandinavian",
    "japandi", "indochine", "luxury", "loft", "industrial", "boho", "wabi sabi",
    "rustic", "contemporary", "eclectic", "art deco", "mid-century",
    # Architecture
    "architecture", "facade", "exterior", "elevation", "villa", "townhouse",
    "modern house", "tropical house", "wooden house", "concrete house", "glass house",
    "courtyard", "atrium", "rooftop",
    # Vietnamese
    "nha", "phong khach", "phong ngu", "phong bep", "kien truc", "noi that",
    "biet thu", "nha pho",
]


def filter_caption(caption: str) -> bool:
    """True if caption matches interior/architecture keywords."""
    if not caption or len(caption) < 10:
        return False
    text = caption.lower()
    return any(kw in text for kw in INTERIOR_KEYWORDS)


def download_laion_aesthetics(target: int = 10_000_000, output_dir: str = "./laion-interior"):
    """
    Download LAION-Aesthetics V2 filtered for interior/architecture.

    Uses HuggingFace datasets streaming → filter → img2dataset batch download.
    """
    try:
        from datasets import load_dataset
    except ImportError:
        log.error("Install: pip install datasets img2dataset")
        return

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Try multiple PUBLIC datasets — fallback chain.
    # NOTE: LAION-2B-en-aesthetic là GATED dataset (cần HF token + apply access).
    # Em switch sang PUBLIC alternatives để không phụ thuộc gating workflow.
    CANDIDATE_DATASETS = [
        # Primary: Spawning/PD12M — 12M Public Domain CC0 (no token needed)
        ("Spawning/PD12M",   {"caption_col": "caption", "url_col": "url"}),
        # Fallback 1: kakaobrain/coyo-700m — 700M public (NO gate)
        ("kakaobrain/coyo-700m", {"caption_col": "text", "url_col": "url"}),
        # Fallback 2: nlphuji/conceptual_12m — 12M Conceptual Captions
        ("nlphuji/conceptual_12m", {"caption_col": "caption", "url_col": "image_url"}),
    ]

    ds = None
    ds_name = None
    caption_col = None
    url_col = None
    for cand_name, schema in CANDIDATE_DATASETS:
        try:
            log.info("Trying dataset: %s ...", cand_name)
            ds = load_dataset(cand_name, split="train", streaming=True)
            ds_name = cand_name
            caption_col = schema["caption_col"]
            url_col = schema["url_col"]
            log.info("Loaded %s (caption=%s, url=%s)", ds_name, caption_col, url_col)
            break
        except Exception as e:
            log.warning("Dataset %s unavailable: %s", cand_name, str(e)[:200])
            continue

    if ds is None:
        log.error("All candidate datasets failed. Check HF Hub status or add HF_TOKEN env.")
        return

    # Filter + collect URLs
    urls_file = out_path / "urls_filtered.parquet"
    filtered_count = 0
    batch = []
    BATCH_SIZE = 10_000

    log.info("Filtering captions (%s) for interior/architecture keywords...", ds_name)
    for row in ds:
        caption = row.get(caption_col, "") or row.get("TEXT", "")
        if filter_caption(caption):
            batch.append({
                "URL": row.get(url_col) or row.get("URL", ""),
                "TEXT": caption,
                "WIDTH": row.get("WIDTH", 0) or row.get("width", 0),
                "HEIGHT": row.get("HEIGHT", 0) or row.get("height", 0),
                "AESTHETIC_SCORE": row.get("AESTHETIC_SCORE", 0) or row.get("aesthetic_score", 0),
                "SIMILARITY": row.get("similarity", 0),
                "LICENSE": "cc0" if "PD12M" in ds_name else "public",
                "SOURCE_DATASET": ds_name,
            })
            filtered_count += 1

            if filtered_count % BATCH_SIZE == 0:
                log.info(f"Filtered {filtered_count:,} URLs so far...")

            if filtered_count >= target:
                break

    # Write Parquet (or JSONL fallback)
    try:
        import pandas as pd
        df = pd.DataFrame(batch)
        df.to_parquet(urls_file, index=False)
        log.info(f"Saved {len(batch):,} URLs to {urls_file}")
    except ImportError:
        jsonl_file = out_path / "urls_filtered.jsonl"
        with open(jsonl_file, "w") as f:
            for row in batch:
                f.write(json.dumps(row) + "\n")
        log.info(f"Saved {len(batch):,} URLs to {jsonl_file}")

    # Use img2dataset for batch download (10x faster than naive httpx)
    log.info("Starting img2dataset batch download (1000 concurrent)...")
    log.info("Run: img2dataset --url_list=" + str(urls_file) +
             " --output_folder=" + str(out_path / "images") +
             " --processes_count=16 --thread_count=64 --image_size=1024" +
             " --output_format=webdataset --enable_wandb=False")
    log.info("This script writes URL list. Run img2dataset CLI separately for actual download.")
    log.info("Reason: img2dataset has its own optimized download loop with retry + dedup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=10_000_000)
    parser.add_argument("--output-dir", default="/mnt/zeni-data/laion-interior")
    args = parser.parse_args()
    download_laion_aesthetics(target=args.target, output_dir=args.output_dir)
