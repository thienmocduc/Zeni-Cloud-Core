"""
Zeni Voice API — voice/SMS module replacing Stringee voice.

Mounted under /api/v1/voice in main.py. Surface area:

Numbers
  POST   /voice/numbers/rent?ws=                — provision number
  GET    /voice/numbers?ws=                     — list workspace numbers
  DELETE /voice/numbers/{id}?ws=                — release number

Calls
  POST   /voice/calls?ws=                       — initiate outbound
  GET    /voice/calls?ws=&from=&to=&direction=  — list (paginated)
  GET    /voice/calls/{id}?ws=                  — detail
  GET    /voice/calls/{id}/recording?ws=        — signed recording URL
  GET    /voice/calls/{id}/transcript?ws=       — transcript text

Webhooks (public, signed)
  POST   /voice/webhook/inbound/{provider}      — inbound voice → return TwiML
  POST   /voice/webhook/status/{provider}       — call status updates
  POST   /voice/webhook/recording/{provider}    — recording ready

TTS / STT
  POST   /voice/tts?ws=                         — text → audio (mp3 b64)
  POST   /voice/stt?ws=                         — audio file → transcript

IVR
  POST   /voice/ivr/flows?ws=                   — create flow
  GET    /voice/ivr/flows?ws=                   — list flows
  PATCH  /voice/ivr/flows/{id}?ws=              — update
  DELETE /voice/ivr/flows/{id}?ws=              — delete
  POST   /voice/ivr/flows/{id}/test?ws=         — simulate flow

Queues + agents
  POST   /voice/queues?ws=                      — create queue
  GET    /voice/queues?ws=                      — list
  DELETE /voice/queues/{id}?ws=                 — delete
  POST   /voice/agents?ws=                      — create agent
  GET    /voice/agents?ws=                      — list
  PATCH  /voice/agents/{id}?ws=                 — update
  POST   /voice/agents/{id}/status?ws=          — set online/busy/away/offline

Voicemails
  GET    /voice/voicemails?ws=&listened=        — list
  POST   /voice/voicemails/{id}/listen?ws=      — mark listened
  DELETE /voice/voicemails/{id}?ws=             — delete

Conferences
  POST   /voice/conferences?ws=                 — create + dial participants

Analytics
  GET    /voice/analytics/overview?ws=&from=&to=
  GET    /voice/analytics/agent-performance?ws=

Security
  - All non-webhook endpoints: get_current_user + require_workspace_access(ws)
  - PAT scope: 'notify' or 'voice' or 'full' required
  - Webhook endpoints: provider signature verification (e.g. X-Twilio-Signature)
  - audit_push for all state-changing operations
  - billing_push for outbound calls + TTS/STT

DON'T touch main.py — caller wires `include_router(voice_router, prefix="/api/v1")`.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.sms import normalize_phone
from app.services.voice_engine import (
    build_twiml_for_instructions,
    estimate_sentiment,
    execute_ivr_node,
    gen_synthetic_phone_number,
    initiate_outbound_call,
    process_inbound_webhook,
    route_to_queue,
    signed_url_for_recording,
    stt_transcribe,
    tts_generate,
    verify_twilio_signature,
)

log = logging.getLogger("zeni.api.voice")
router = APIRouter(prefix="/voice", tags=["voice"])


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
USD_TO_VND = 25_500
ALLOWED_VOICE_SCOPES = ("voice", "notify", "full")

VALID_DIRECTIONS = ("inbound", "outbound")
VALID_AGENT_STATUSES = ("online", "busy", "away", "offline")
VALID_ROUTING = ("round-robin", "least-busy", "priority")
VALID_OVERFLOW = ("voicemail", "callback", "external")
VALID_NUMBER_PROVIDERS = ("twilio", "viettel", "fpt", "vnpt")
VALID_CAPABILITIES = ("voice", "sms", "mms", "fax")


def _check_scope(me: CurrentUser) -> None:
    """PAT must carry voice|notify|full. JWT users always pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and not (scopes & set(ALLOWED_VOICE_SCOPES)):
        raise HTTPException(
            status_code=403,
            detail="PAT cần scope 'voice' / 'notify' / 'full' để dùng /voice",
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


async def _ensure_call_in_ws(db: AsyncSession, call_id: int, ws: str) -> dict[str, Any]:
    row = (
        await db.execute(
            text("SELECT * FROM voice_calls WHERE id = :id AND workspace_id = :ws"),
            {"id": call_id, "ws": ws},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy cuộc gọi")
    return _row_to_dict(row)


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class NumberRentIn(BaseModel):
    country: str = Field(default="VN", min_length=2, max_length=20)
    area_code: str | None = Field(default=None, max_length=8)
    capabilities: list[str] = Field(default_factory=lambda: ["voice", "sms"])
    provider: str = Field(default="twilio")

    @field_validator("provider")
    @classmethod
    def _v_provider(cls, v: str) -> str:
        if v not in VALID_NUMBER_PROVIDERS:
            raise ValueError(f"provider phải thuộc {VALID_NUMBER_PROVIDERS}")
        return v

    @field_validator("capabilities")
    @classmethod
    def _v_caps(cls, v: list[str]) -> list[str]:
        for c in v:
            if c not in VALID_CAPABILITIES:
                raise ValueError(f"capability không hợp lệ: {c}")
        return v


class CallInitiateIn(BaseModel):
    from_number_id: int = Field(..., gt=0)
    to_number: str = Field(..., min_length=8, max_length=20)
    message_or_flow_id: int | str | None = Field(default=None,
        description="int → IVR flow id; str → TTS text")
    voice: str | None = Field(default="vi-VN-Standard-A", max_length=60)


class TtsIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str = Field(default="vi-VN-Standard-A", max_length=60)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class IvrFlowIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    welcome_message: str | None = None
    nodes: list[dict[str, Any]]
    associated_number_id: int | None = None
    is_active: bool = False

    @field_validator("nodes")
    @classmethod
    def _v_nodes(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(v, list) or len(v) == 0:
            raise ValueError("nodes không được rỗng")
        for n in v:
            if not isinstance(n, dict) or "id" not in n or "type" not in n:
                raise ValueError("Mỗi node phải có {id, type, ...}")
        return v


class IvrFlowPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    welcome_message: str | None = None
    nodes: list[dict[str, Any]] | None = None
    associated_number_id: int | None = None
    is_active: bool | None = None


class IvrTestIn(BaseModel):
    current_node_id: str | None = None
    user_input: str | None = None


class QueueIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    routing_strategy: str = Field(default="round-robin")
    max_wait_seconds: int = Field(default=300, ge=10, le=3600)
    overflow_action: str | None = None
    overflow_target: str | None = None

    @field_validator("routing_strategy")
    @classmethod
    def _v_strat(cls, v: str) -> str:
        if v not in VALID_ROUTING:
            raise ValueError(f"routing_strategy phải thuộc {VALID_ROUTING}")
        return v

    @field_validator("overflow_action")
    @classmethod
    def _v_over(cls, v: str | None) -> str | None:
        if v and v not in VALID_OVERFLOW:
            raise ValueError(f"overflow_action phải thuộc {VALID_OVERFLOW}")
        return v


class AgentIn(BaseModel):
    user_email: str = Field(..., min_length=3, max_length=200)
    extension: str | None = Field(default=None, max_length=10)
    skills: list[str] | None = None
    queue_ids: list[int] | None = None


class AgentPatch(BaseModel):
    extension: str | None = Field(default=None, max_length=10)
    skills: list[str] | None = None
    queue_ids: list[int] | None = None


class AgentStatusIn(BaseModel):
    status: str = Field(...)

    @field_validator("status")
    @classmethod
    def _v_st(cls, v: str) -> str:
        if v not in VALID_AGENT_STATUSES:
            raise ValueError(f"status phải thuộc {VALID_AGENT_STATUSES}")
        return v


class ConferenceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    from_number_id: int = Field(..., gt=0)
    participants: list[str] = Field(..., min_length=1, max_length=20)


# ════════════════════════════════════════════════════════════════════════════
# Numbers
# ════════════════════════════════════════════════════════════════════════════
@router.post("/numbers/rent")
async def rent_number(
    body: NumberRentIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)

    # Provision (stub — real impl calls provider API)
    phone_number = gen_synthetic_phone_number(body.country, body.area_code)
    monthly = 1.00 if body.provider == "twilio" else 2.00

    row = (
        await db.execute(
            text(
                """
                INSERT INTO voice_numbers
                  (workspace_id, phone_number, provider, capabilities,
                   monthly_cost_usd, status)
                VALUES (:ws, :phone, :provider, :caps, :cost, 'active')
                RETURNING id, phone_number, provider, capabilities,
                          monthly_cost_usd, status, created_at
                """
            ),
            {
                "ws": ws,
                "phone": phone_number,
                "provider": body.provider,
                "caps": body.capabilities,
                "cost": monthly,
            },
        )
    ).mappings().first()

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.number.rent", target=phone_number, severity="ok",
        metadata={"provider": body.provider, "monthly_usd": monthly},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/numbers")
async def list_numbers(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (
        await db.execute(
            text(
                "SELECT id, phone_number, provider, capabilities, "
                "monthly_cost_usd, status, created_at "
                "FROM voice_numbers WHERE workspace_id = :ws "
                "ORDER BY created_at DESC"
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.delete("/numbers/{number_id}")
async def release_number(
    number_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "UPDATE voice_numbers SET status='released' "
            "WHERE id = :id AND workspace_id = :ws AND status != 'released' "
            "RETURNING id, phone_number"
        ),
        {"id": number_id, "ws": ws},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy số")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.number.release", target=str(row["phone_number"]),
        severity="warn",
    )
    await db.commit()
    return {"ok": True, "released": _row_to_dict(row)}


# ════════════════════════════════════════════════════════════════════════════
# Calls (outbound + listing)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/calls")
async def initiate_call(
    body: CallInitiateIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)

    # Validate / fetch from-number
    n = (
        await db.execute(
            text(
                "SELECT id, phone_number, provider, status FROM voice_numbers "
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": body.from_number_id, "ws": ws},
        )
    ).first()
    if not n:
        raise HTTPException(404, "from_number_id không thuộc workspace")
    if n[3] != "active":
        raise HTTPException(400, f"Số đã {n[3]}, không thể gọi đi")

    # Normalize destination
    try:
        to_e164 = normalize_phone(body.to_number)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Build instructions
    if isinstance(body.message_or_flow_id, int):
        instructions = {"type": "ivr", "flow_id": body.message_or_flow_id}
    elif isinstance(body.message_or_flow_id, str) and body.message_or_flow_id:
        instructions = {
            "type": "say",
            "text": body.message_or_flow_id,
            "voice": body.voice,
        }
    else:
        instructions = {"type": "say", "text": "Xin chào từ Zeni Cloud."}

    try:
        result = await initiate_outbound_call(
            workspace_id=ws,
            from_number=str(n[1]),
            to_number=to_e164,
            instructions=instructions,
            provider=str(n[2]),
        )
    except RuntimeError as e:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="voice.call.outbound", target=to_e164,
            severity="err", metadata={"error": str(e)[:200]},
        )
        await db.commit()
        raise HTTPException(502, str(e))

    estimated_cost = float(result.get("estimated_cost_usd") or 0.0)

    row = (
        await db.execute(
            text(
                """
                INSERT INTO voice_calls
                  (workspace_id, call_sid, direction, from_number, to_number,
                   status, cost_usd, metadata)
                VALUES (:ws, :sid, 'outbound', :from_n, :to_n, :status,
                        :cost, CAST(:meta AS JSONB))
                RETURNING id, call_sid, status, started_at
                """
            ),
            {
                "ws": ws,
                "sid": result["call_sid"],
                "from_n": str(n[1]),
                "to_n": to_e164,
                "status": result.get("status", "queued"),
                "cost": estimated_cost,
                "meta": json.dumps(
                    {"provider": result.get("provider"),
                     "instructions_type": instructions.get("type")},
                    ensure_ascii=False,
                ),
            },
        )
    ).mappings().first()

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.call.outbound", target=to_e164,
        severity="ok",
        metadata={"sid": result["call_sid"], "cost_usd": estimated_cost},
    )
    await billing_push(
        db, workspace_id=ws, layer="L4",
        action="voice.call.outbound", cost_usd=estimated_cost,
    )
    await db.commit()

    return {
        "ok": True,
        "call_id": row["id"],
        "call_sid": row["call_sid"],
        "status": row["status"],
        "to": to_e164,
        "from": str(n[1]),
        "estimated_cost_usd": estimated_cost,
        "estimated_cost_vnd": int(round(estimated_cost * USD_TO_VND)),
    }


@router.get("/calls")
async def list_calls(
    ws: str = Query(...),
    direction: str | None = Query(default=None),
    from_filter: str | None = Query(default=None, alias="from"),
    to_filter: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    if direction and direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"direction phải thuộc {VALID_DIRECTIONS}")

    sql = (
        "SELECT id, call_sid, direction, from_number, to_number, "
        "duration_seconds, status, sentiment_score, cost_usd, "
        "started_at, ended_at "
        "FROM voice_calls WHERE workspace_id = :ws"
    )
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if direction:
        sql += " AND direction = :dir"
        params["dir"] = direction
    if from_filter:
        sql += " AND from_number = :from_n"
        params["from_n"] = from_filter
    if to_filter:
        sql += " AND to_number = :to_n"
        params["to_n"] = to_filter
    sql += " ORDER BY started_at DESC LIMIT :lim OFFSET :off"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "items": [_row_to_dict(r) for r in rows],
        "count": len(rows),
        "limit": limit,
        "offset": offset,
    }


@router.get("/calls/{call_id}")
async def get_call(
    call_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    return await _ensure_call_in_ws(db, call_id, ws)


@router.get("/calls/{call_id}/recording")
async def get_call_recording(
    call_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    call = await _ensure_call_in_ws(db, call_id, ws)
    raw_url = call.get("recording_url") or ""
    if not raw_url:
        raise HTTPException(404, "Cuộc gọi chưa có recording")
    return {
        "call_id": call_id,
        "url": signed_url_for_recording(raw_url, ttl_seconds=600),
        "expires_in_seconds": 600,
    }


@router.get("/calls/{call_id}/transcript")
async def get_call_transcript(
    call_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    call = await _ensure_call_in_ws(db, call_id, ws)
    return {
        "call_id": call_id,
        "transcript": call.get("transcript") or "",
        "sentiment_score": call.get("sentiment_score"),
    }


# ════════════════════════════════════════════════════════════════════════════
# Webhooks (public; signature-verified)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/webhook/inbound/{provider}")
async def webhook_inbound(
    provider: str,
    request: Request,
    x_twilio_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Provider-agnostic inbound webhook → returns provider control instructions
    (TwiML XML for Twilio).
    """
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}

    if provider.lower() == "twilio":
        full_url = str(request.url)
        if not verify_twilio_signature(full_url, payload, x_twilio_signature):
            log.warning("[voice] inbound webhook bad signature: %s", full_url)
            raise HTTPException(403, "Bad Twilio signature")

    decision = await process_inbound_webhook(provider, payload, db=db)

    # Persist call record (best-effort)
    call_sid = payload.get("CallSid") or payload.get("call_sid")
    if call_sid:
        # Resolve workspace via the dialed number
        to_n = payload.get("To") or ""
        ws_row = (
            await db.execute(
                text("SELECT workspace_id FROM voice_numbers WHERE phone_number = :p"),
                {"p": to_n},
            )
        ).first()
        ws = ws_row[0] if ws_row else None
        if ws:
            try:
                await db.execute(
                    text(
                        """
                        INSERT INTO voice_calls
                          (workspace_id, call_sid, direction, from_number, to_number,
                           status, metadata)
                        VALUES (:ws, :sid, 'inbound', :fn, :tn, :st,
                                CAST(:meta AS JSONB))
                        ON CONFLICT (call_sid) DO NOTHING
                        """
                    ),
                    {
                        "ws": ws,
                        "sid": str(call_sid),
                        "fn": payload.get("From"),
                        "tn": to_n,
                        "st": payload.get("CallStatus", "ringing"),
                        "meta": json.dumps(
                            {"provider": provider}, ensure_ascii=False
                        ),
                    },
                )
                await audit_push(
                    db, actor="webhook", workspace_id=ws,
                    action="voice.call.inbound", target=str(to_n)[:20],
                    severity="info", metadata={"sid": str(call_sid)},
                )
                await db.commit()
            except Exception as e:
                log.warning("[voice] inbound persist failed: %s", e)

    if decision.get("action") == "twiml":
        return Response(content=decision["xml"], media_type="application/xml")
    return Response(content="", media_type="application/xml")


@router.post("/webhook/status/{provider}")
async def webhook_status(
    provider: str,
    request: Request,
    x_twilio_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update call status/duration/cost from provider event."""
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}

    if provider.lower() == "twilio":
        full_url = str(request.url)
        if not verify_twilio_signature(full_url, payload, x_twilio_signature):
            raise HTTPException(403, "Bad Twilio signature")

    sid = payload.get("CallSid")
    status = payload.get("CallStatus")
    duration = payload.get("CallDuration") or payload.get("Duration") or 0
    if sid:
        try:
            await db.execute(
                text(
                    """
                    UPDATE voice_calls
                    SET status = COALESCE(:status, status),
                        duration_seconds = COALESCE(:dur, duration_seconds),
                        ended_at = CASE WHEN :status IN ('completed','no-answer','busy','failed')
                                        THEN NOW() ELSE ended_at END
                    WHERE call_sid = :sid
                    """
                ),
                {
                    "sid": str(sid),
                    "status": str(status) if status else None,
                    "dur": int(duration) if duration else None,
                },
            )
            await db.commit()
        except Exception as e:
            log.warning("[voice] status update failed: %s", e)
    return {"ok": True}


@router.post("/webhook/recording/{provider}")
async def webhook_recording(
    provider: str,
    request: Request,
    x_twilio_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Persist recording URL once provider finishes uploading."""
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}

    if provider.lower() == "twilio":
        full_url = str(request.url)
        if not verify_twilio_signature(full_url, payload, x_twilio_signature):
            raise HTTPException(403, "Bad Twilio signature")

    sid = payload.get("CallSid")
    rec_url = payload.get("RecordingUrl") or payload.get("recording_url")
    if sid and rec_url:
        await db.execute(
            text(
                "UPDATE voice_calls SET recording_url = :u WHERE call_sid = :sid"
            ),
            {"u": str(rec_url), "sid": str(sid)},
        )
        await db.commit()
    return {"ok": True, "stored": bool(rec_url)}


# ════════════════════════════════════════════════════════════════════════════
# TTS / STT
# ════════════════════════════════════════════════════════════════════════════
@router.post("/tts")
async def tts_endpoint(
    body: TtsIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    result = await tts_generate(body.text, voice=body.voice, speed=body.speed)
    await db.execute(
        text(
            """
            INSERT INTO voice_speech_usage
              (workspace_id, operation, text_length, audio_duration_seconds,
               voice, cost_usd)
            VALUES (:ws, 'tts', :len, :dur, :voice, :cost)
            """
        ),
        {
            "ws": ws,
            "len": len(body.text),
            "dur": result.get("duration_s"),
            "voice": result.get("voice"),
            "cost": result.get("cost_usd"),
        },
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.tts", target=result.get("voice", ""),
        severity="ok",
        metadata={"len": len(body.text), "cost_usd": result.get("cost_usd")},
    )
    await billing_push(
        db, workspace_id=ws, layer="L4", action="voice.tts",
        cost_usd=float(result.get("cost_usd") or 0.0),
    )
    await db.commit()
    return result


@router.post("/stt")
async def stt_endpoint(
    ws: str = Query(...),
    audio: UploadFile = File(...),
    language: str = Form(default="vi-VN"),
    encoding: str = Form(default="MP3"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "audio rỗng")
    if len(audio_bytes) > 25 * 1024 * 1024:  # 25MB cap
        raise HTTPException(413, "audio quá lớn (max 25MB)")

    result = await stt_transcribe(audio_bytes, language=language, encoding=encoding)
    await db.execute(
        text(
            """
            INSERT INTO voice_speech_usage
              (workspace_id, operation, text_length, audio_duration_seconds,
               voice, cost_usd)
            VALUES (:ws, 'stt', :len, :dur, :lang, :cost)
            """
        ),
        {
            "ws": ws,
            "len": len(result.get("transcript") or ""),
            "dur": result.get("duration_s"),
            "lang": language,
            "cost": result.get("cost_usd"),
        },
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.stt", target=language,
        severity="ok",
        metadata={"bytes": len(audio_bytes), "cost_usd": result.get("cost_usd")},
    )
    await billing_push(
        db, workspace_id=ws, layer="L4", action="voice.stt",
        cost_usd=float(result.get("cost_usd") or 0.0),
    )
    await db.commit()
    return result


# ════════════════════════════════════════════════════════════════════════════
# IVR flows
# ════════════════════════════════════════════════════════════════════════════
@router.post("/ivr/flows")
async def ivr_create(
    body: IvrFlowIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    if body.associated_number_id is not None:
        n = (
            await db.execute(
                text(
                    "SELECT id FROM voice_numbers "
                    "WHERE id = :id AND workspace_id = :ws"
                ),
                {"id": body.associated_number_id, "ws": ws},
            )
        ).first()
        if not n:
            raise HTTPException(404, "associated_number_id không thuộc workspace")
    row = (
        await db.execute(
            text(
                """
                INSERT INTO voice_ivr_flows
                  (workspace_id, name, welcome_message, nodes,
                   associated_number_id, is_active)
                VALUES (:ws, :name, :wm, CAST(:nodes AS JSONB), :nid, :active)
                RETURNING id, name, is_active, created_at
                """
            ),
            {
                "ws": ws,
                "name": body.name,
                "wm": body.welcome_message,
                "nodes": json.dumps({"nodes": body.nodes}, ensure_ascii=False),
                "nid": body.associated_number_id,
                "active": body.is_active,
            },
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.ivr.create", target=body.name, severity="ok",
        metadata={"nodes": len(body.nodes)},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/ivr/flows")
async def ivr_list(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (
        await db.execute(
            text(
                "SELECT id, name, welcome_message, nodes, "
                "associated_number_id, is_active, created_at "
                "FROM voice_ivr_flows WHERE workspace_id = :ws "
                "ORDER BY created_at DESC"
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.patch("/ivr/flows/{flow_id}")
async def ivr_update(
    flow_id: int,
    body: IvrFlowPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    fields: list[str] = []
    params: dict[str, Any] = {"id": flow_id, "ws": ws}
    if body.name is not None:
        fields.append("name = :name"); params["name"] = body.name
    if body.welcome_message is not None:
        fields.append("welcome_message = :wm"); params["wm"] = body.welcome_message
    if body.nodes is not None:
        fields.append("nodes = CAST(:nodes AS JSONB)")
        params["nodes"] = json.dumps({"nodes": body.nodes}, ensure_ascii=False)
    if body.associated_number_id is not None:
        fields.append("associated_number_id = :nid"); params["nid"] = body.associated_number_id
    if body.is_active is not None:
        fields.append("is_active = :act"); params["act"] = body.is_active
    if not fields:
        raise HTTPException(400, "Không có field cập nhật")
    sql = (
        f"UPDATE voice_ivr_flows SET {', '.join(fields)} "
        "WHERE id = :id AND workspace_id = :ws "
        "RETURNING id, name, is_active"
    )
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy flow")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.ivr.update", target=str(flow_id), severity="ok",
    )
    await db.commit()
    return _row_to_dict(row)


@router.delete("/ivr/flows/{flow_id}")
async def ivr_delete(
    flow_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "DELETE FROM voice_ivr_flows WHERE id = :id AND workspace_id = :ws "
            "RETURNING id"
        ),
        {"id": flow_id, "ws": ws},
    )
    row = res.first()
    if not row:
        raise HTTPException(404, "Không tìm thấy flow")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.ivr.delete", target=str(flow_id), severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted_id": row[0]}


@router.post("/ivr/flows/{flow_id}/test")
async def ivr_test(
    flow_id: int,
    body: IvrTestIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (
        await db.execute(
            text(
                "SELECT nodes FROM voice_ivr_flows "
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": flow_id, "ws": ws},
        )
    ).first()
    if not row:
        raise HTTPException(404, "Không tìm thấy flow")
    nodes_blob = row[0]
    if isinstance(nodes_blob, str):
        try:
            nodes_blob = json.loads(nodes_blob)
        except Exception:
            nodes_blob = {"nodes": []}
    flow = {"nodes": nodes_blob.get("nodes", []) if isinstance(nodes_blob, dict) else []}
    decision = execute_ivr_node(flow, body.current_node_id, body.user_input)
    return {"flow_id": flow_id, "result": decision}


# ════════════════════════════════════════════════════════════════════════════
# Queues + Agents
# ════════════════════════════════════════════════════════════════════════════
@router.post("/queues")
async def queue_create(
    body: QueueIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO voice_queues
                  (workspace_id, name, routing_strategy, max_wait_seconds,
                   overflow_action, overflow_target)
                VALUES (:ws, :name, :strat, :wait, :oa, :ot)
                RETURNING id, name, routing_strategy, max_wait_seconds,
                          overflow_action, overflow_target, created_at
                """
            ),
            {
                "ws": ws,
                "name": body.name,
                "strat": body.routing_strategy,
                "wait": body.max_wait_seconds,
                "oa": body.overflow_action,
                "ot": body.overflow_target,
            },
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.queue.create", target=body.name, severity="ok",
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/queues")
async def queue_list(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (
        await db.execute(
            text(
                "SELECT * FROM voice_queues WHERE workspace_id = :ws "
                "ORDER BY created_at DESC"
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.delete("/queues/{queue_id}")
async def queue_delete(
    queue_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "DELETE FROM voice_queues WHERE id = :id AND workspace_id = :ws "
            "RETURNING id"
        ),
        {"id": queue_id, "ws": ws},
    )
    if not res.first():
        raise HTTPException(404, "Không tìm thấy queue")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.queue.delete", target=str(queue_id), severity="warn",
    )
    await db.commit()
    return {"ok": True}


@router.post("/agents")
async def agent_create(
    body: AgentIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO voice_agents
                  (workspace_id, user_email, extension, skills, queue_ids,
                   status, last_active_at)
                VALUES (:ws, :email, :ext, :skills, :qids, 'offline', NOW())
                ON CONFLICT (workspace_id, user_email) DO UPDATE
                  SET extension = EXCLUDED.extension,
                      skills    = EXCLUDED.skills,
                      queue_ids = EXCLUDED.queue_ids
                RETURNING id, user_email, extension, skills, queue_ids, status
                """
            ),
            {
                "ws": ws,
                "email": body.user_email,
                "ext": body.extension,
                "skills": body.skills,
                "qids": body.queue_ids,
            },
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.agent.upsert", target=body.user_email, severity="ok",
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/agents")
async def agent_list(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (
        await db.execute(
            text(
                "SELECT id, user_email, extension, skills, queue_ids, status, "
                "last_active_at FROM voice_agents "
                "WHERE workspace_id = :ws ORDER BY id"
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.patch("/agents/{agent_id}")
async def agent_update(
    agent_id: int,
    body: AgentPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    fields: list[str] = []
    params: dict[str, Any] = {"id": agent_id, "ws": ws}
    if body.extension is not None:
        fields.append("extension = :ext"); params["ext"] = body.extension
    if body.skills is not None:
        fields.append("skills = :skills"); params["skills"] = body.skills
    if body.queue_ids is not None:
        fields.append("queue_ids = :qids"); params["qids"] = body.queue_ids
    if not fields:
        raise HTTPException(400, "Không có field cập nhật")
    sql = (
        f"UPDATE voice_agents SET {', '.join(fields)} "
        "WHERE id = :id AND workspace_id = :ws RETURNING id, user_email, status"
    )
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy agent")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.agent.update", target=str(agent_id), severity="ok",
    )
    await db.commit()
    return _row_to_dict(row)


@router.post("/agents/{agent_id}/status")
async def agent_set_status(
    agent_id: int,
    body: AgentStatusIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "UPDATE voice_agents SET status = :st, last_active_at = NOW() "
            "WHERE id = :id AND workspace_id = :ws "
            "RETURNING id, user_email, status"
        ),
        {"id": agent_id, "ws": ws, "st": body.status},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy agent")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.agent.status", target=str(agent_id),
        severity="info", metadata={"status": body.status},
    )
    await db.commit()
    return _row_to_dict(row)


# ════════════════════════════════════════════════════════════════════════════
# Voicemails
# ════════════════════════════════════════════════════════════════════════════
@router.get("/voicemails")
async def vm_list(
    ws: str = Query(...),
    listened: bool | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    sql = (
        "SELECT id, call_id, from_number, to_number, audio_url, transcript, "
        "duration_seconds, listened, listened_by, listened_at, received_at "
        "FROM voice_voicemails WHERE workspace_id = :ws"
    )
    params: dict[str, Any] = {"ws": ws}
    if listened is not None:
        sql += " AND listened = :l"
        params["l"] = listened
    sql += " ORDER BY received_at DESC LIMIT 200"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.post("/voicemails/{vm_id}/listen")
async def vm_listen(
    vm_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "UPDATE voice_voicemails "
            "SET listened = TRUE, listened_by = :u, listened_at = NOW() "
            "WHERE id = :id AND workspace_id = :ws "
            "RETURNING id"
        ),
        {"id": vm_id, "ws": ws, "u": me.email},
    )
    if not res.first():
        raise HTTPException(404, "Không tìm thấy voicemail")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.vm.listen", target=str(vm_id), severity="info",
    )
    await db.commit()
    return {"ok": True}


@router.delete("/voicemails/{vm_id}")
async def vm_delete(
    vm_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(
        text(
            "DELETE FROM voice_voicemails "
            "WHERE id = :id AND workspace_id = :ws RETURNING id"
        ),
        {"id": vm_id, "ws": ws},
    )
    if not res.first():
        raise HTTPException(404, "Không tìm thấy voicemail")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.vm.delete", target=str(vm_id), severity="warn",
    )
    await db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# Conferences
# ════════════════════════════════════════════════════════════════════════════
@router.post("/conferences")
async def conference_create(
    body: ConferenceIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    n = (
        await db.execute(
            text(
                "SELECT id, phone_number, provider FROM voice_numbers "
                "WHERE id = :id AND workspace_id = :ws AND status = 'active'"
            ),
            {"id": body.from_number_id, "ws": ws},
        )
    ).first()
    if not n:
        raise HTTPException(404, "from_number không khả dụng")

    instructions = {"type": "say", "text": f"Bắt đầu hội thảo: {body.name}"}
    dial_results: list[dict[str, Any]] = []
    total_cost = 0.0
    for participant in body.participants:
        try:
            to_e164 = normalize_phone(participant)
        except ValueError:
            dial_results.append({"to": participant, "ok": False, "error": "phone_invalid"})
            continue
        try:
            res = await initiate_outbound_call(
                workspace_id=ws,
                from_number=str(n[1]),
                to_number=to_e164,
                instructions=instructions,
                provider=str(n[2]),
            )
            cost = float(res.get("estimated_cost_usd") or 0.0)
            total_cost += cost
            dial_results.append({
                "to": to_e164,
                "ok": True,
                "call_sid": res.get("call_sid"),
                "estimated_cost_usd": cost,
            })
            # Insert call record
            await db.execute(
                text(
                    """
                    INSERT INTO voice_calls
                      (workspace_id, call_sid, direction, from_number, to_number,
                       status, cost_usd, metadata)
                    VALUES (:ws, :sid, 'outbound', :fn, :tn, :st, :c,
                            CAST(:meta AS JSONB))
                    """
                ),
                {
                    "ws": ws,
                    "sid": res["call_sid"],
                    "fn": str(n[1]),
                    "tn": to_e164,
                    "st": res.get("status", "queued"),
                    "c": cost,
                    "meta": json.dumps(
                        {"conference": body.name}, ensure_ascii=False
                    ),
                },
            )
        except Exception as e:
            dial_results.append({"to": participant, "ok": False, "error": str(e)[:100]})

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="voice.conference.create", target=body.name,
        severity="ok", metadata={"participants": len(body.participants)},
    )
    if total_cost > 0:
        await billing_push(
            db, workspace_id=ws, layer="L4",
            action="voice.conference", cost_usd=total_cost,
        )
    await db.commit()
    return {
        "ok": True,
        "conference_name": body.name,
        "participants": dial_results,
        "estimated_total_cost_usd": round(total_cost, 4),
    }


# ════════════════════════════════════════════════════════════════════════════
# Analytics
# ════════════════════════════════════════════════════════════════════════════
def _parse_date(s: str | None, default_offset_days: int) -> datetime:
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"Sai định dạng date: {s}")
    return datetime.now(timezone.utc) - timedelta(days=default_offset_days)


@router.get("/analytics/overview")
async def analytics_overview(
    ws: str = Query(...),
    from_param: str | None = Query(default=None, alias="from"),
    to_param: str | None = Query(default=None, alias="to"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    dt_from = _parse_date(from_param, 30)
    dt_to = _parse_date(to_param, 0)
    if dt_to <= dt_from:
        dt_to = dt_from + timedelta(days=1)

    row = (
        await db.execute(
            text(
                """
                SELECT
                  COUNT(*)                                              AS total_calls,
                  COUNT(*) FILTER (WHERE direction = 'inbound')        AS inbound,
                  COUNT(*) FILTER (WHERE direction = 'outbound')       AS outbound,
                  COUNT(*) FILTER (WHERE status IN ('no-answer','busy','failed'))
                                                                       AS missed,
                  COALESCE(AVG(duration_seconds), 0)::float             AS avg_duration_s,
                  COALESCE(SUM(cost_usd), 0)::float                     AS total_cost_usd,
                  COALESCE(AVG(sentiment_score), 0)::float              AS avg_sentiment
                FROM voice_calls
                WHERE workspace_id = :ws
                  AND started_at >= :dt_from AND started_at < :dt_to
                """
            ),
            {"ws": ws, "dt_from": dt_from, "dt_to": dt_to},
        )
    ).mappings().first()
    out = _row_to_dict(row) if row else {}
    total = int(out.get("total_calls") or 0)
    missed = int(out.get("missed") or 0)
    out["missed_rate"] = round(missed / total, 4) if total else 0.0
    out["from"] = dt_from.isoformat()
    out["to"] = dt_to.isoformat()
    return out


@router.get("/analytics/agent-performance")
async def analytics_agent_performance(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (
        await db.execute(
            text(
                """
                SELECT a.id, a.user_email, a.status, a.last_active_at,
                       COUNT(c.id)                          AS total_calls,
                       COALESCE(AVG(c.duration_seconds),0)::float AS avg_duration_s,
                       COALESCE(AVG(c.sentiment_score),0)::float  AS avg_sentiment
                FROM voice_agents a
                LEFT JOIN voice_calls c
                  ON c.workspace_id = a.workspace_id
                 AND c.started_at > NOW() - INTERVAL '30 days'
                 AND c.metadata ? 'agent_id'
                 AND (c.metadata->>'agent_id')::bigint = a.id
                WHERE a.workspace_id = :ws
                GROUP BY a.id, a.user_email, a.status, a.last_active_at
                ORDER BY total_calls DESC
                """
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return {"agents": [_row_to_dict(r) for r in rows], "count": len(rows)}
