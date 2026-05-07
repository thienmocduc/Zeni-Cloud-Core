"""
Zeni Cloud Core — SMS provider abstraction (Stream A4).

Routes SMS by destination phone number:
- Vietnam (+84 / 0xxx) → Stringee (REST API, $0.005/SMS)
- International (+xx)  → Twilio  (REST API, $0.05/SMS)

Credentials read from settings if available, fallback to os.environ:
- STRINGEE_API_KEY, STRINGEE_FROM
- TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM

Usage:
    from app.services.sms import send_sms
    res = await send_sms(to="+84901234567", text="Xin chào")
    # → {"provider": "stringee", "message_id": "...", "to": "+84901234567",
    #    "cost_usd": 0.005, "status": "queued"}
"""
from __future__ import annotations

import base64
import logging
import os
import re
import uuid
from typing import Any

import httpx

log = logging.getLogger("zeni.sms")

# ─── Pricing constants ──────────────────────────────────────
STRINGEE_COST_USD = 0.005   # $0.005/SMS (VN domestic)
TWILIO_COST_USD = 0.05      # $0.05/SMS (international avg)

# ─── HTTP timeout ───────────────────────────────────────────
HTTP_TIMEOUT = 30.0

# ─── Phone-format helpers ───────────────────────────────────
_PHONE_DIGITS_RE = re.compile(r"^\+?\d{10,15}$")


def _settings_get(key: str) -> str:
    """Get config value: try app.core.config.settings first, then os.environ."""
    try:
        from app.core.config import settings  # type: ignore
        val = getattr(settings, key.lower(), None)
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(key, "") or ""


def _mask(secret: str, show: int = 8) -> str:
    """Mask a secret/token for safe logging — show first N chars only."""
    if not secret:
        return "<empty>"
    if len(secret) <= show:
        return f"{secret[:2]}..."
    return f"{secret[:show]}..."


def is_configured(provider: str) -> bool:
    """Check whether credentials are present for the given provider."""
    p = provider.lower()
    if p == "stringee":
        return bool(_settings_get("STRINGEE_API_KEY") and _settings_get("STRINGEE_FROM"))
    if p == "twilio":
        return bool(
            _settings_get("TWILIO_ACCOUNT_SID")
            and _settings_get("TWILIO_AUTH_TOKEN")
            and _settings_get("TWILIO_FROM")
        )
    return False


def normalize_phone(to: str) -> str:
    """
    Normalize phone number to E.164:
    - "0901234567"   → "+84901234567"
    - "+84901234567" → "+84901234567"
    - other          → ValueError
    """
    if not to:
        raise ValueError("Số điện thoại không hợp lệ")
    s = re.sub(r"[\s\-()]", "", to.strip())
    if s.startswith("0"):
        s = "+84" + s[1:]
    elif not s.startswith("+"):
        raise ValueError("Số điện thoại không hợp lệ")
    if not _PHONE_DIGITS_RE.match(s):
        raise ValueError("Số điện thoại không hợp lệ")
    return s


def detect_provider(to_e164: str) -> str:
    """Return 'stringee' if VN, else 'twilio'."""
    return "stringee" if to_e164.startswith("+84") else "twilio"


# ─── Public API ─────────────────────────────────────────────
async def send_sms(to: str, text: str) -> dict[str, Any]:
    """
    Send an SMS. Routes to Stringee (VN) or Twilio (international).

    Args:
        to: phone number (accepts "0xxx" or "+xxx").
        text: SMS body (1-1600 chars; multi-segment allowed).

    Returns:
        {provider, message_id, to, cost_usd, status}

    Raises:
        ValueError: invalid phone format.
        RuntimeError: provider not configured (caller maps to HTTP 503).
        httpx.HTTPError: upstream call failed.
    """
    to_e164 = normalize_phone(to)
    provider = detect_provider(to_e164)

    if not is_configured(provider):
        raise RuntimeError("Provider chưa được cấu hình")

    if provider == "stringee":
        return await _send_via_stringee(to_e164, text)
    return await _send_via_twilio(to_e164, text)


async def _send_via_stringee(to: str, text: str) -> dict[str, Any]:
    """
    Send SMS via Stringee (VN provider).
    Endpoint: POST https://api.stringee.com/v1/sms
    Auth header: X-STRINGEE-AUTH: <STRINGEE_API_KEY>
    Body: {"from": STRINGEE_FROM, "to": "<E.164>", "sms": "<text>"}
    """
    api_key = _settings_get("STRINGEE_API_KEY")
    from_ = _settings_get("STRINGEE_FROM")

    log.info(
        "[sms] stringee → to=%s len=%d from=%s key=%s",
        to, len(text), from_, _mask(api_key),
    )

    payload = {"from": from_, "to": to, "sms": text}
    headers = {
        "X-STRINGEE-AUTH": api_key,
        "Content-Type": "application/json",
        "User-Agent": "ZeniCloud/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(
                "https://api.stringee.com/v1/sms",
                json=payload, headers=headers,
            )
        ok = 200 <= r.status_code < 300
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        message_id = (
            body.get("sms_id")
            or body.get("id")
            or body.get("message_id")
            or f"stringee-{uuid.uuid4().hex[:12]}"
        )
        status = "sent" if ok else "failed"
        if not ok:
            log.warning("[sms] stringee non-2xx status=%s body=%s", r.status_code, str(body)[:200])
        return {
            "provider": "stringee",
            "message_id": str(message_id),
            "to": to,
            "cost_usd": STRINGEE_COST_USD if ok else 0.0,
            "status": status,
            "http_status": r.status_code,
        }
    except httpx.TimeoutException:
        log.warning("[sms] stringee timeout to=%s", to)
        raise RuntimeError("Stringee timeout (>30s)")
    except httpx.HTTPError as e:
        log.warning("[sms] stringee http error to=%s: %s", to, e)
        raise RuntimeError(f"Stringee gọi lỗi: {type(e).__name__}")


async def _send_via_twilio(to: str, text: str) -> dict[str, Any]:
    """
    Send SMS via Twilio (international).
    Endpoint: POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json
    Auth: HTTP Basic (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    Body (form-encoded): From=..., To=..., Body=...
    """
    sid = _settings_get("TWILIO_ACCOUNT_SID")
    token = _settings_get("TWILIO_AUTH_TOKEN")
    from_ = _settings_get("TWILIO_FROM")

    log.info(
        "[sms] twilio → to=%s len=%d from=%s sid=%s",
        to, len(text), from_, _mask(sid),
    )

    auth_str = base64.b64encode(f"{sid}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "ZeniCloud/1.0",
    }
    data = {"From": from_, "To": to, "Body": text}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post(url, data=data, headers=headers)
        ok = 200 <= r.status_code < 300
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        message_id = (
            body.get("sid")
            or body.get("message_id")
            or f"twilio-{uuid.uuid4().hex[:12]}"
        )
        status = body.get("status") if ok else "failed"
        if not ok:
            log.warning("[sms] twilio non-2xx status=%s body=%s", r.status_code, str(body)[:200])
        return {
            "provider": "twilio",
            "message_id": str(message_id),
            "to": to,
            "cost_usd": TWILIO_COST_USD if ok else 0.0,
            "status": str(status or ("sent" if ok else "failed")),
            "http_status": r.status_code,
        }
    except httpx.TimeoutException:
        log.warning("[sms] twilio timeout to=%s", to)
        raise RuntimeError("Twilio timeout (>30s)")
    except httpx.HTTPError as e:
        log.warning("[sms] twilio http error to=%s: %s", to, e)
        raise RuntimeError(f"Twilio gọi lỗi: {type(e).__name__}")
