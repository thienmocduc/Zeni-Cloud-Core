"""
ZENI CLOUD CORE — Translation service (Stream A3).

GCP Cloud Translation API v3 wrapper — REST API qua httpx + Service Account OAuth2 token.
Re-uses _get_gcp_token() từ app.services.ocr (cùng SA + scope cloud-platform).
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.services.ocr import _get_gcp_token  # share token cache

log = logging.getLogger("zeni.translate")

HTTP_TIMEOUT = 60.0

# ISO-639 language codes — bao gồm BCP-47 region tags Google support (vi, en, ja, zh-CN, zh-TW, ko, fr, ...)
# Pattern: 2-3 chữ + optional '-' + 2-4 chữ (region)
_LANG_CODE_RE = re.compile(r"^[a-zA-Z]{2,3}(-[A-Za-z0-9]{2,4})?$")


def _validate_lang_code(code: str) -> bool:
    """Check ISO-639 / BCP-47 valid (vi, en, ja, zh-CN, ko, fr, ...)."""
    if not code or not isinstance(code, str):
        return False
    return bool(_LANG_CODE_RE.match(code))


def _project_endpoint() -> str:
    project_id = settings.gcp_project_id or ""
    if not project_id:
        raise RuntimeError("GCP_PROJECT_ID chưa cấu hình")
    return (
        f"https://translation.googleapis.com/v3/projects/{project_id}"
        f"/locations/global:translateText"
    )


async def translate_text(
    text: str,
    target_lang: str,
    source_lang: str | None = None,
) -> dict[str, Any]:
    """
    Translate plain text. Tự động detect source nếu source_lang=None.

    Return: {translated_text, source_lang_detected, char_count, target_lang}
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text không được rỗng")
    if not _validate_lang_code(target_lang):
        raise ValueError("Ngôn ngữ đích không hợp lệ")
    if source_lang is not None and not _validate_lang_code(source_lang):
        raise ValueError("Ngôn ngữ nguồn không hợp lệ")

    body: dict[str, Any] = {
        "contents": [text],
        "targetLanguageCode": target_lang,
        "mimeType": "text/plain",
    }
    if source_lang:
        body["sourceLanguageCode"] = source_lang

    token = await _get_gcp_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-user-project": settings.gcp_project_id or "",
    }
    endpoint = _project_endpoint()

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
    except httpx.TimeoutException as e:
        log.warning("Translation API timeout: %s", e)
        raise RuntimeError("Lỗi gọi Cloud Translation API: timeout sau 60s") from e
    except httpx.HTTPError as e:
        log.warning("Translation API network error: %s", e)
        raise RuntimeError(f"Lỗi gọi Cloud Translation API: {e}") from e

    if resp.status_code != 200:
        log.error("Translation API HTTP %s: %s", resp.status_code, resp.text[:500])
        msg = "Lỗi gọi Cloud Translation API"
        try:
            j = resp.json()
            api_err = (j.get("error") or {}).get("message")
            if api_err:
                msg = f"Lỗi gọi Cloud Translation API: {api_err[:200]}"
        except Exception:
            pass
        raise RuntimeError(msg)

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError("Lỗi gọi Cloud Translation API: response không hợp lệ") from e

    translations = data.get("translations") or []
    if not translations:
        raise RuntimeError("Cloud Translation API trả về rỗng")

    t0 = translations[0]
    return {
        "translated_text": t0.get("translatedText", ""),
        "source_lang_detected": t0.get("detectedLanguageCode") or source_lang,
        "char_count": len(text),
        "target_lang": target_lang,
    }
