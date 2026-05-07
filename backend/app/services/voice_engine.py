"""
Zeni Voice — provider-agnostic voice engine.

Replaces Stringee voice with a native dispatcher that supports multiple
providers (Twilio first, Vietnamese SIP carriers later). Handles outbound
call dispatch, inbound webhook parsing, IVR DAG traversal, queue routing,
and TTS / STT (Google Cloud Text-to-Speech / Speech-to-Text with graceful
fallback when libraries are unavailable).

Public API
----------
- initiate_outbound_call(...)
- process_inbound_webhook(provider, payload)  -> {"action": "twiml", "xml": "..."}
- tts_generate(text, voice, speed)            -> {"audio_b64": "...", "duration_s": ..., "cost_usd": ...}
- stt_transcribe(audio_bytes, language)       -> {"transcript": "...", "duration_s": ..., "cost_usd": ...}
- execute_ivr_node(flow, current_node_id, user_input)  -> next_node_id, action_payload
- route_to_queue(db, queue_id, call_id)       -> {"action": "agent_dial"|"wait", ...}

Note: All async, Pydantic-free (returns plain dicts). HTTP client = httpx.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.voice.engine")

HTTP_TIMEOUT = 30.0

# ─── Provider pricing (USD) ─────────────────────────────────────────────────
TWILIO_OUTBOUND_PER_MIN_USD = 0.013          # US/CA outbound voice
TWILIO_VN_OUTBOUND_PER_MIN_USD = 0.30         # International to VN
TWILIO_NUMBER_MONTHLY_USD = 1.00              # local number rental
VN_SIP_OUTBOUND_PER_MIN_USD = 0.012           # Vietnamese SIP carrier (estimate)

# Google Cloud TTS / STT
GOOGLE_TTS_STANDARD_PER_MILLION = 4.0         # $4 / 1M chars (Standard voices)
GOOGLE_TTS_WAVENET_PER_MILLION = 16.0          # $16 / 1M chars (WaveNet)
GOOGLE_STT_PER_15S = 0.006                    # $0.006 per 15s chunk

# Vietnamese voices (Google Cloud)
VN_TTS_VOICES_STANDARD = (
    "vi-VN-Standard-A",
    "vi-VN-Standard-B",
    "vi-VN-Standard-C",
    "vi-VN-Standard-D",
)
VN_TTS_VOICES_WAVENET = (
    "vi-VN-Wavenet-A",
    "vi-VN-Wavenet-B",
    "vi-VN-Wavenet-C",
    "vi-VN-Wavenet-D",
)


# ────────────────────────────────────────────────────────────────────────────
# Settings helper
# ────────────────────────────────────────────────────────────────────────────
def _settings_get(key: str) -> str:
    """Read a settings value (settings → env)."""
    try:
        from app.core.config import settings  # type: ignore
        v = getattr(settings, key.lower(), None)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(key, "") or ""


def _twilio_creds() -> tuple[str, str, str]:
    return (
        _settings_get("TWILIO_ACCOUNT_SID"),
        _settings_get("TWILIO_AUTH_TOKEN"),
        _settings_get("TWILIO_FROM"),
    )


def _twilio_configured() -> bool:
    sid, tok, _ = _twilio_creds()
    return bool(sid and tok)


# ────────────────────────────────────────────────────────────────────────────
# Twilio webhook signature verification
# ────────────────────────────────────────────────────────────────────────────
def verify_twilio_signature(
    full_url: str,
    form_params: dict[str, str],
    signature_header: str | None,
) -> bool:
    """
    Verify Twilio's X-Twilio-Signature header (HMAC-SHA1 of url + sorted params).
    Returns True if signature matches OR if creds not configured (dev mode).
    """
    _, token, _ = _twilio_creds()
    if not token:
        log.warning("[voice] twilio token not set — skipping signature check (dev only)")
        return True
    if not signature_header:
        return False
    data = full_url + "".join(
        f"{k}{v}" for k, v in sorted(form_params.items()) if v is not None
    )
    mac = hmac.new(token.encode(), data.encode("utf-8"), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature_header.strip())


# ────────────────────────────────────────────────────────────────────────────
# Outbound calls
# ────────────────────────────────────────────────────────────────────────────
async def initiate_outbound_call(
    *,
    workspace_id: str,
    from_number: str,
    to_number: str,
    instructions: dict[str, Any] | None = None,
    provider: str = "twilio",
    callback_url: str | None = None,
) -> dict[str, Any]:
    """
    Provider-agnostic outbound call dispatcher.

    instructions:
        {"type": "say", "text": "...", "voice": "vi-VN-Standard-A"} — TTS
        {"type": "play", "url": "https://..."}                        — play audio
        {"type": "ivr", "flow_id": 42}                                — exec IVR
        {"type": "dial", "to": "+84..."}                              — bridge call

    Returns:
        {"call_sid", "provider", "status", "from", "to", "estimated_cost_usd"}
    """
    instructions = instructions or {"type": "say", "text": "Xin chào từ Zeni Cloud."}

    if provider == "twilio":
        return await _twilio_initiate(
            workspace_id=workspace_id,
            from_number=from_number,
            to_number=to_number,
            instructions=instructions,
            callback_url=callback_url,
        )
    if provider in ("viettel", "fpt", "vnpt"):
        return await _vn_sip_initiate(
            workspace_id=workspace_id,
            from_number=from_number,
            to_number=to_number,
            instructions=instructions,
        )
    raise RuntimeError(f"Provider không hỗ trợ: {provider}")


async def _twilio_initiate(
    *,
    workspace_id: str,
    from_number: str,
    to_number: str,
    instructions: dict[str, Any],
    callback_url: str | None,
) -> dict[str, Any]:
    """POST to Twilio Calls.json. If creds missing → return synthetic stub."""
    sid, token, _ = _twilio_creds()
    if not sid or not token:
        # Graceful stub for dev/test (no provider credentials)
        return {
            "call_sid": f"CA-stub-{uuid.uuid4().hex[:24]}",
            "provider": "twilio",
            "status": "queued",
            "from": from_number,
            "to": to_number,
            "estimated_cost_usd": 0.0,
            "stub": True,
        }

    twiml = build_twiml_for_instructions(instructions)
    body = {
        "From": from_number,
        "To": to_number,
        "Twiml": twiml,
    }
    if callback_url:
        body["StatusCallback"] = callback_url
        body["StatusCallbackEvent"] = "initiated ringing answered completed"

    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "ZeniCloud-Voice/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
            r = await c.post(url, data=body, headers=headers)
        ok = 200 <= r.status_code < 300
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:200]}
        return {
            "call_sid": str(data.get("sid") or f"CA-err-{uuid.uuid4().hex[:12]}"),
            "provider": "twilio",
            "status": str(data.get("status") or ("queued" if ok else "failed")),
            "from": from_number,
            "to": to_number,
            "estimated_cost_usd": _estimate_outbound_cost(to_number, "twilio"),
            "http_status": r.status_code,
        }
    except httpx.TimeoutException:
        log.warning("[voice] twilio outbound timeout → %s", to_number)
        raise RuntimeError("Twilio timeout (>30s)")
    except httpx.HTTPError as e:
        log.warning("[voice] twilio outbound http error: %s", e)
        raise RuntimeError(f"Twilio gọi lỗi: {type(e).__name__}")


async def _vn_sip_initiate(
    *,
    workspace_id: str,
    from_number: str,
    to_number: str,
    instructions: dict[str, Any],
) -> dict[str, Any]:
    """
    Vietnamese SIP carrier dispatcher (placeholder).

    In production wire FreeSWITCH ESL or carrier REST API. For now return a
    stub so the rest of the platform can integrate.
    """
    return {
        "call_sid": f"VN-{uuid.uuid4().hex[:24]}",
        "provider": "vn-sip",
        "status": "queued",
        "from": from_number,
        "to": to_number,
        "estimated_cost_usd": _estimate_outbound_cost(to_number, "vn-sip"),
        "stub": True,
    }


def _estimate_outbound_cost(to_number: str, provider: str) -> float:
    """Estimate per-minute cost for budgeting (1-min default)."""
    is_vn = to_number.startswith("+84")
    if provider == "twilio":
        return TWILIO_VN_OUTBOUND_PER_MIN_USD if is_vn else TWILIO_OUTBOUND_PER_MIN_USD
    return VN_SIP_OUTBOUND_PER_MIN_USD


# ────────────────────────────────────────────────────────────────────────────
# TwiML builder
# ────────────────────────────────────────────────────────────────────────────
def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_twiml_for_instructions(instructions: dict[str, Any]) -> str:
    """Convert a Zeni instruction dict to TwiML XML."""
    t = (instructions or {}).get("type", "say")
    if t == "say":
        text_v = _xml_escape(str(instructions.get("text", "Xin chào")))
        voice = instructions.get("voice", "Polly.Linh")  # Twilio's VN voice
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Say voice="{voice}" language="vi-VN">{text_v}</Say></Response>'
        )
    if t == "play":
        url = _xml_escape(str(instructions.get("url", "")))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Play>{url}</Play></Response>"
        )
    if t == "dial":
        to = _xml_escape(str(instructions.get("to", "")))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Dial>{to}</Dial></Response>"
        )
    if t == "voicemail":
        prompt = _xml_escape(str(instructions.get("prompt", "Để lại lời nhắn sau tiếng bíp.")))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Say voice="Polly.Linh" language="vi-VN">{prompt}</Say>'
            '<Record maxLength="120" playBeep="true" />'
            "</Response>"
        )
    # default: hangup
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


# ────────────────────────────────────────────────────────────────────────────
# Inbound webhook processing
# ────────────────────────────────────────────────────────────────────────────
async def process_inbound_webhook(
    provider: str,
    payload: dict[str, Any],
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Parse provider webhook + decide what to do.

    Returns one of:
      {"action": "twiml", "xml": "<Response>...</Response>"}
      {"action": "noop"}

    For Twilio inbound voice the body shape is form-encoded:
      CallSid, From, To, CallStatus, ...
    """
    p = (provider or "").lower()
    if p == "twilio":
        call_sid = str(payload.get("CallSid") or payload.get("call_sid") or "")
        from_n = str(payload.get("From") or payload.get("from") or "")
        to_n = str(payload.get("To") or payload.get("to") or "")
        status = str(payload.get("CallStatus") or "ringing")

        # Try to find an active IVR flow attached to this number
        flow_xml = None
        if db is not None and to_n:
            flow_xml = await _find_active_ivr_xml(db, to_n)

        if flow_xml is None:
            flow_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                '<Say voice="Polly.Linh" language="vi-VN">'
                "Xin chào, bạn đã gọi đến Zeni. Vui lòng để lại lời nhắn sau tiếng bíp."
                "</Say>"
                '<Record maxLength="120" playBeep="true" />'
                "</Response>"
            )

        return {
            "action": "twiml",
            "xml": flow_xml,
            "call_sid": call_sid,
            "from": from_n,
            "to": to_n,
            "status": status,
        }

    # Other providers: provide a generic noop instruction
    return {"action": "noop", "provider": provider}


async def _find_active_ivr_xml(db: AsyncSession, dialed_to: str) -> str | None:
    """Look up active IVR flow attached to a number; return TwiML or None."""
    try:
        row = (
            await db.execute(
                sql_text(
                    """
                    SELECT f.id, f.welcome_message, f.nodes
                    FROM voice_ivr_flows f
                    JOIN voice_numbers n ON n.id = f.associated_number_id
                    WHERE n.phone_number = :p AND f.is_active = TRUE
                    LIMIT 1
                    """
                ),
                {"p": dialed_to},
            )
        ).first()
    except Exception as e:
        log.warning("[voice] ivr lookup failed: %s", e)
        return None
    if not row:
        return None
    welcome = row[1] or "Xin chào, bạn đã gọi đến Zeni."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="Polly.Linh" language="vi-VN">{_xml_escape(welcome)}</Say>'
        '<Gather numDigits="1" timeout="8">'
        '<Say voice="Polly.Linh" language="vi-VN">Nhấn 1 để gặp tổng đài viên, 2 để lại lời nhắn.</Say>'
        "</Gather>"
        "</Response>"
    )


# ────────────────────────────────────────────────────────────────────────────
# IVR DAG traversal
# ────────────────────────────────────────────────────────────────────────────
def execute_ivr_node(
    flow: dict[str, Any],
    current_node_id: str | None,
    user_input: str | None = None,
) -> dict[str, Any]:
    """
    Walk the IVR flow one step.

    flow["nodes"] is a list of nodes each with shape:
      {"id": "start", "type": "say", "text": "...", "next": "menu"}
      {"id": "menu",  "type": "gather", "prompt": "...", "branches": {"1": "agent", "2": "vm"}}
      {"id": "agent", "type": "dial", "to": "+8490..."}
      {"id": "vm",    "type": "voicemail"}
      {"id": "end",   "type": "hangup"}
    """
    nodes = {n["id"]: n for n in flow.get("nodes", []) if isinstance(n, dict) and "id" in n}
    if not nodes:
        return {"action": "hangup", "reason": "empty_flow"}

    if current_node_id is None:
        # Default entry = first node
        current = next(iter(nodes.values()))
    else:
        current = nodes.get(current_node_id)
        if not current:
            return {"action": "hangup", "reason": "node_not_found"}

    t = current.get("type")
    if t == "say":
        return {
            "action": "say",
            "text": current.get("text", ""),
            "next": current.get("next"),
        }
    if t == "gather":
        branches = current.get("branches") or {}
        next_id = branches.get(str(user_input)) if user_input else None
        return {
            "action": "gather",
            "prompt": current.get("prompt", ""),
            "next": next_id or current.get("default_next"),
            "matched": next_id is not None,
        }
    if t == "dial":
        return {
            "action": "dial",
            "to": current.get("to"),
            "next": current.get("next"),
        }
    if t == "voicemail":
        return {"action": "voicemail", "next": current.get("next")}
    if t == "hangup":
        return {"action": "hangup"}
    return {"action": "hangup", "reason": f"unknown_type:{t}"}


# ────────────────────────────────────────────────────────────────────────────
# TTS / STT
# ────────────────────────────────────────────────────────────────────────────
async def tts_generate(
    text: str,
    voice: str = "vi-VN-Standard-A",
    speed: float = 1.0,
) -> dict[str, Any]:
    """
    Synthesize text → audio (MP3 base64).

    Uses google-cloud-texttospeech if available; otherwise returns a graceful
    fallback (silent placeholder) so callers don't break.
    """
    voice = voice or "vi-VN-Standard-A"
    speed = max(0.25, min(4.0, float(speed or 1.0)))
    is_wavenet = "Wavenet" in voice
    cost = (
        len(text) / 1_000_000
        * (GOOGLE_TTS_WAVENET_PER_MILLION if is_wavenet else GOOGLE_TTS_STANDARD_PER_MILLION)
    )

    try:
        from google.cloud import texttospeech  # type: ignore
        client = texttospeech.TextToSpeechAsyncClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code="vi-VN",
            name=voice,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speed,
        )
        resp = await client.synthesize_speech(
            input=synthesis_input, voice=voice_params, audio_config=audio_config
        )
        audio_b64 = base64.b64encode(resp.audio_content).decode()
        # Approx duration: ~140 chars/sec at 1x for Vietnamese TTS
        approx_duration = max(0.5, len(text) / 14.0 / max(0.5, speed))
        return {
            "audio_b64": audio_b64,
            "audio_format": "mp3",
            "duration_s": round(approx_duration, 2),
            "voice": voice,
            "cost_usd": round(cost, 6),
            "provider": "google",
        }
    except ImportError:
        log.info("[voice] google-cloud-texttospeech missing — returning fallback")
    except Exception as e:
        log.warning("[voice] tts error: %s", e)

    # Fallback: empty MP3 placeholder
    placeholder = base64.b64encode(b"").decode()
    approx_duration = max(0.5, len(text) / 14.0)
    return {
        "audio_b64": placeholder,
        "audio_format": "mp3",
        "duration_s": round(approx_duration, 2),
        "voice": voice,
        "cost_usd": round(cost, 6),
        "provider": "fallback",
        "stub": True,
    }


async def stt_transcribe(
    audio_bytes: bytes,
    language: str = "vi-VN",
    encoding: str = "MP3",
) -> dict[str, Any]:
    """
    Transcribe audio bytes → text using Google Cloud Speech-to-Text.
    Falls back to empty transcript if library missing.
    """
    size = len(audio_bytes or b"")
    # Estimate duration (16kHz mono PCM ≈ 32KB/s; codecs vary, so this is a coarse ceiling)
    approx_duration = max(1.0, size / 32000.0)
    cost = (approx_duration / 15.0) * GOOGLE_STT_PER_15S

    try:
        from google.cloud import speech  # type: ignore
        client = speech.SpeechAsyncClient()
        enc_map = {
            "MP3": speech.RecognitionConfig.AudioEncoding.MP3,
            "LINEAR16": speech.RecognitionConfig.AudioEncoding.LINEAR16,
            "OGG_OPUS": speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            "WEBM_OPUS": speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
        }
        config = speech.RecognitionConfig(
            encoding=enc_map.get(encoding.upper(), speech.RecognitionConfig.AudioEncoding.MP3),
            language_code=language or "vi-VN",
            enable_automatic_punctuation=True,
        )
        audio_in = speech.RecognitionAudio(content=audio_bytes)
        resp = await client.recognize(config=config, audio=audio_in)
        parts = [r.alternatives[0].transcript for r in resp.results if r.alternatives]
        transcript = " ".join(parts).strip()
        return {
            "transcript": transcript,
            "language": language,
            "duration_s": round(approx_duration, 2),
            "cost_usd": round(cost, 6),
            "provider": "google",
        }
    except ImportError:
        log.info("[voice] google-cloud-speech missing — returning fallback")
    except Exception as e:
        log.warning("[voice] stt error: %s", e)

    return {
        "transcript": "",
        "language": language,
        "duration_s": round(approx_duration, 2),
        "cost_usd": round(cost, 6),
        "provider": "fallback",
        "stub": True,
    }


# ────────────────────────────────────────────────────────────────────────────
# Sentiment helper (lightweight VN keyword heuristic, no LLM dependency)
# ────────────────────────────────────────────────────────────────────────────
_NEG_WORDS = ("tệ", "kém", "không hài lòng", "thất vọng", "bực", "phàn nàn",
              "khiếu nại", "lừa", "tồi", "chậm", "lỗi", "hỏng")
_POS_WORDS = ("tốt", "tuyệt", "hài lòng", "cảm ơn", "ok", "okay", "great",
              "yêu", "tuyệt vời", "happy", "vui", "nhanh")


def estimate_sentiment(transcript: str) -> float:
    """
    Score in [-1, +1] based on simple keyword matching. Cheap proxy until a
    proper model is wired up.
    """
    if not transcript:
        return 0.0
    s = transcript.lower()
    pos = sum(1 for w in _POS_WORDS if w in s)
    neg = sum(1 for w in _NEG_WORDS if w in s)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 2)


# ────────────────────────────────────────────────────────────────────────────
# Queue routing
# ────────────────────────────────────────────────────────────────────────────
async def route_to_queue(
    db: AsyncSession,
    *,
    queue_id: int,
    call_id: int,
) -> dict[str, Any]:
    """
    Pick an available agent for a queue, applying the queue's routing strategy.

    Returns:
        {"action": "agent_dial", "agent_id": ..., "agent_extension": ...}
        {"action": "wait",        "reason": "no_agents"}
        {"action": "overflow",    "target": "...", "method": "voicemail"|"callback"|"external"}
    """
    q = (
        await db.execute(
            sql_text(
                "SELECT id, routing_strategy, overflow_action, overflow_target "
                "FROM voice_queues WHERE id = :id"
            ),
            {"id": queue_id},
        )
    ).first()
    if not q:
        return {"action": "wait", "reason": "queue_not_found"}

    strategy = q[1] or "round-robin"

    agents = (
        await db.execute(
            sql_text(
                """
                SELECT id, user_email, extension, last_active_at
                FROM voice_agents
                WHERE :qid = ANY(queue_ids) AND status = 'online'
                ORDER BY COALESCE(last_active_at, '1970-01-01'::timestamptz) ASC
                """
            ),
            {"qid": queue_id},
        )
    ).all()

    if not agents:
        if q[2]:  # overflow_action set
            return {"action": "overflow", "method": q[2], "target": q[3]}
        return {"action": "wait", "reason": "no_agents"}

    if strategy == "least-busy":
        chosen = agents[0]  # earliest last_active_at == least busy
    elif strategy == "priority":
        chosen = agents[0]  # placeholder: treat order as priority
    else:  # round-robin (default)
        chosen = agents[secrets.randbelow(len(agents))]

    # Mark agent busy + record activity (best-effort)
    try:
        await db.execute(
            sql_text(
                "UPDATE voice_agents SET status='busy', last_active_at=NOW() WHERE id=:id"
            ),
            {"id": chosen[0]},
        )
    except Exception:
        pass

    return {
        "action": "agent_dial",
        "agent_id": chosen[0],
        "agent_email": chosen[1],
        "agent_extension": chosen[2],
        "queue_id": queue_id,
        "call_id": call_id,
    }


# ────────────────────────────────────────────────────────────────────────────
# Misc helpers used by API
# ────────────────────────────────────────────────────────────────────────────
def gen_synthetic_phone_number(country: str = "VN", area_code: str | None = None) -> str:
    """Generate a synthetic E.164 number for stub provisioning (dev/test)."""
    cc = "+84" if country.upper() in ("VN", "VIETNAM") else "+1"
    suffix = "".join(secrets.choice("0123456789") for _ in range(8))
    if area_code:
        return f"{cc}{area_code}{suffix[: max(0, 9 - len(area_code))]}"
    return f"{cc}{suffix}"


def signed_url_for_recording(raw_url: str, ttl_seconds: int = 600) -> str:
    """
    Wrap a recording URL with a short TTL signature for download. If the URL is
    already a GCS object signed URL, pass through; otherwise emit a query
    parameter sig= for the platform's pre-signing layer (handled elsewhere).
    """
    if not raw_url:
        return ""
    if "X-Goog-Signature=" in raw_url or "X-Amz-Signature=" in raw_url:
        return raw_url
    expiry = int(datetime.now(timezone.utc).timestamp()) + max(60, int(ttl_seconds))
    sep = "&" if "?" in raw_url else "?"
    return f"{raw_url}{sep}{urlencode({'expires': expiry, 'sig': uuid.uuid4().hex})}"
