"""
ZENI CLOUD CORE — OCR service (Stream A3).

GCP Cloud Vision API wrapper — REST API qua httpx + Service Account OAuth2 token.

  - ocr_image(): annotate 1 ảnh từ GCS URI / public URL / base64
  - ocr_pdf():   annotate PDF (sync mode, max 5 pages — Vision API limit)

Auth pattern: service_account.Credentials.from_service_account_file() + Request()
              fall back to google.auth.default() (ADC trên Cloud Run).
Token cached 50 phút (token thực sống 60 phút).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings

log = logging.getLogger("zeni.ocr")

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
VISION_FILES_ENDPOINT = "https://vision.googleapis.com/v1/files:annotate"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
HTTP_TIMEOUT = 60.0

# Token cache (50 phút, dù token sống 60 phút — refresh sớm)
_token_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}
_TOKEN_TTL = 50 * 60  # seconds

# GCS URI regex: gs://bucket-name/path/to/file
_GCS_URI_RE = re.compile(r"^gs://[a-z0-9][a-z0-9._-]*[a-z0-9]/.+$")


def _validate_gcs_uri(uri: str) -> None:
    """Raise ValueError nếu uri không phải GCS URI hợp lệ."""
    if not uri or not isinstance(uri, str):
        raise ValueError("GCS URI không được rỗng")
    if not _GCS_URI_RE.match(uri):
        raise ValueError(f"GCS URI không hợp lệ: phải có dạng gs://bucket/path")


def _is_base64(s: str) -> bool:
    """Detect base64 string (kể cả có data URI prefix)."""
    if not isinstance(s, str):
        return False
    # Strip data URI prefix nếu có
    if s.startswith("data:"):
        if "," not in s:
            return False
        s = s.split(",", 1)[1]
    # Min length & valid charset check
    if len(s) < 16:
        return False
    try:
        base64.b64decode(s, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


def _strip_data_uri(s: str) -> str:
    """Return raw base64 from 'data:image/png;base64,...' or the input itself."""
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1]
    return s


async def _get_gcp_token() -> str:
    """
    Lấy SA OAuth2 access token, cache 50 phút.

    Order:
      1. Service account JSON từ GOOGLE_APPLICATION_CREDENTIALS
      2. Application Default Credentials (ADC) — Cloud Run metadata server
    """
    now = time.time()
    cached = _token_cache.get("value")
    expires_at = _token_cache.get("expires_at", 0.0)
    if cached and now < expires_at:
        return cached

    def _refresh() -> str:
        from google.auth.transport.requests import Request
        try:
            if settings.google_application_credentials:
                from google.oauth2 import service_account
                creds = service_account.Credentials.from_service_account_file(
                    settings.google_application_credentials,
                    scopes=[SCOPE],
                )
            else:
                import google.auth
                creds, _project = google.auth.default(scopes=[SCOPE])
        except Exception as e:
            log.exception("Không lấy được GCP credentials")
            raise RuntimeError(f"Không lấy được GCP credentials: {e}") from e

        creds.refresh(Request())
        if not creds.token:
            raise RuntimeError("GCP trả về token rỗng")
        return creds.token

    token = await asyncio.to_thread(_refresh)
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + _TOKEN_TTL
    return token


def _build_image_payload(image_source: str | bytes) -> dict[str, Any]:
    """
    Convert input → Vision API image payload.

    image_source có thể là:
      - GCS URI: 'gs://bucket/path' → {"source": {"gcsImageUri": ...}}
      - Public HTTP(S) URL          → {"source": {"imageUri": ...}}
      - Bytes                       → {"content": base64(bytes)}
      - Base64 string / data URI    → {"content": ...}
    """
    if isinstance(image_source, bytes):
        return {"content": base64.b64encode(image_source).decode("ascii")}

    if not isinstance(image_source, str) or not image_source.strip():
        raise ValueError("image_source phải là chuỗi GCS URI / URL / base64 hoặc bytes")

    s = image_source.strip()

    if s.startswith("gs://"):
        _validate_gcs_uri(s)
        return {"source": {"gcsImageUri": s}}

    if s.startswith("http://") or s.startswith("https://"):
        return {"source": {"imageUri": s}}

    # Treat as base64 (with/without data URI prefix)
    raw = _strip_data_uri(s)
    if not _is_base64(raw):
        raise ValueError("image_source không nhận dạng được — cần GCS URI, HTTP URL, hoặc base64")
    return {"content": raw}


async def _vision_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST → Vision API, raise RuntimeError với message tiếng Việt khi fail."""
    token = await _get_gcp_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-user-project": settings.gcp_project_id or "",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
    except httpx.TimeoutException as e:
        log.warning("Vision API timeout: %s", e)
        raise RuntimeError("Lỗi gọi Cloud Vision API: timeout sau 60s") from e
    except httpx.HTTPError as e:
        log.warning("Vision API network error: %s", e)
        raise RuntimeError(f"Lỗi gọi Cloud Vision API: {e}") from e

    if resp.status_code != 200:
        # Don't leak full response — log nó, return short message
        log.error("Vision API HTTP %s: %s", resp.status_code, resp.text[:500])
        # Try to surface API error message in Vietnamese-context
        msg = "Lỗi gọi Cloud Vision API"
        try:
            j = resp.json()
            api_err = (j.get("error") or {}).get("message")
            if api_err:
                msg = f"Lỗi gọi Cloud Vision API: {api_err[:200]}"
        except Exception:
            pass
        raise RuntimeError(msg)

    try:
        return resp.json()
    except Exception as e:
        log.exception("Vision API trả response không parse được")
        raise RuntimeError("Lỗi gọi Cloud Vision API: response không hợp lệ") from e


def _detect_language_from_response(annotation: dict[str, Any]) -> str | None:
    """Pick the most common detected language code from fullTextAnnotation."""
    pages = annotation.get("pages") or []
    counts: dict[str, int] = {}
    for page in pages:
        for prop_lang in (page.get("property") or {}).get("detectedLanguages", []) or []:
            code = prop_lang.get("languageCode")
            conf = prop_lang.get("confidence", 0)
            if code:
                counts[code] = counts.get(code, 0) + (1 if conf == 0 else int(conf * 100))
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _avg_confidence(pages_data: list[dict[str, Any]]) -> float:
    confs = [p.get("confidence") for p in pages_data if p.get("confidence") is not None]
    if not confs:
        return 0.0
    return round(sum(confs) / len(confs), 4)


# ─── PUBLIC API ──────────────────────────────────────────────────


async def ocr_image(image_source: str | bytes) -> dict[str, Any]:
    """
    OCR 1 image (sync). image_source có thể là GCS URI, public URL, base64 string hoặc bytes.

    Return: {text, pages, confidence, language, raw_text_length}
    """
    try:
        image_payload = _build_image_payload(image_source)
    except ValueError as e:
        raise ValueError(str(e))

    body = {
        "requests": [
            {
                "image": image_payload,
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }

    data = await _vision_post(VISION_ENDPOINT, body)

    responses = data.get("responses") or []
    if not responses:
        raise RuntimeError("Không thể OCR ảnh: Vision API trả về rỗng")
    r0 = responses[0]
    if "error" in r0:
        msg = (r0.get("error") or {}).get("message") or "unknown"
        raise RuntimeError(f"Không thể OCR ảnh: {msg[:200]}")

    full = r0.get("fullTextAnnotation") or {}
    text = full.get("text", "") or ""
    page_list = full.get("pages") or []

    pages_out: list[dict[str, Any]] = []
    for idx, page in enumerate(page_list, start=1):
        page_text_parts: list[str] = []
        for block in page.get("blocks", []) or []:
            for paragraph in block.get("paragraphs", []) or []:
                for word in paragraph.get("words", []) or []:
                    word_text = "".join(s.get("text", "") for s in (word.get("symbols") or []))
                    if word_text:
                        page_text_parts.append(word_text)
        pages_out.append({
            "page_number": idx,
            "text": " ".join(page_text_parts),
            "confidence": page.get("confidence"),
            "width": page.get("width"),
            "height": page.get("height"),
        })

    return {
        "text": text,
        "pages": pages_out,
        "confidence": _avg_confidence(pages_out),
        "language": _detect_language_from_response(full),
        "raw_text_length": len(text),
    }


async def ocr_pdf(gcs_uri: str, max_pages: int = 5) -> dict[str, Any]:
    """
    OCR PDF lưu trên GCS, sync mode (max 5 pages — Vision API hard-limit cho files:annotate sync).

    Return: {pages: [{page_number, text, confidence}], total_pages, total_chars}
    """
    _validate_gcs_uri(gcs_uri)
    if max_pages < 1 or max_pages > 5:
        raise ValueError("max_pages phải nằm trong khoảng 1..5 (Vision API sync limit)")

    body = {
        "requests": [
            {
                "inputConfig": {
                    "gcsSource": {"uri": gcs_uri},
                    "mimeType": "application/pdf",
                },
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "pages": list(range(1, max_pages + 1)),
            }
        ]
    }

    data = await _vision_post(VISION_FILES_ENDPOINT, body)

    responses = data.get("responses") or []
    if not responses:
        raise RuntimeError("Không thể OCR PDF: Vision API trả về rỗng")

    file_resp = responses[0]
    if "error" in file_resp:
        msg = (file_resp.get("error") or {}).get("message") or "unknown"
        raise RuntimeError(f"Không thể OCR PDF: {msg[:200]}")

    page_responses = file_resp.get("responses") or []
    pages_out: list[dict[str, Any]] = []
    total_chars = 0
    for page_resp in page_responses:
        if "error" in page_resp:
            log.warning("OCR PDF page error: %s", page_resp["error"])
            continue
        full = page_resp.get("fullTextAnnotation") or {}
        text = full.get("text", "") or ""
        page_meta = (full.get("pages") or [{}])[0]
        page_number = page_resp.get("context", {}).get("pageNumber") or (len(pages_out) + 1)
        pages_out.append({
            "page_number": page_number,
            "text": text,
            "confidence": page_meta.get("confidence"),
        })
        total_chars += len(text)

    return {
        "pages": pages_out,
        "total_pages": len(pages_out),
        "total_chars": total_chars,
    }
