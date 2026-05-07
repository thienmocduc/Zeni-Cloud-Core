"""
SMS API — public endpoint POST /api/v1/sms/send.

Routes by destination phone:
- VN (+84 / 0xxx) → Stringee ($0.005/SMS)
- International   → Twilio   ($0.05/SMS)

Security:
  - Auth required (JWT or PAT scope notify|full)
  - require_workspace_access(ws, me)
  - 503 if provider not configured
  - audit_push(action="notify.sms")
  - billing_push(layer="L4", action="notify.sms")
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.sms import (
    detect_provider,
    is_configured,
    normalize_phone,
    send_sms,
)

log = logging.getLogger("zeni.api.sms")
router = APIRouter(prefix="/sms", tags=["sms"])

# USD → VND conversion (consistent with billing module)
USD_TO_VND = 25_500


class SmsSendIn(BaseModel):
    to: str = Field(min_length=8, max_length=20,
                    description="Phone number, E.164 (+84xxx) or VN local (0xxx).")
    text: str = Field(min_length=1, max_length=1600,
                      description="SMS body. Multi-segment auto (>160 chars splits).")


class SmsSendOut(BaseModel):
    ok: bool
    provider: str
    message_id: str
    to: str
    cost_usd: float
    cost_vnd: int
    status: str


@router.post("/send", response_model=SmsSendOut)
async def send_sms_endpoint(
    ws: str,
    data: SmsSendIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SmsSendOut:
    """
    Send a single SMS.

    Routing:
      - `to` starts +84 / 0xxx → Stringee (VN, ~125đ)
      - else → Twilio ($0.05 = ~1275đ)

    Returns 503 if the chosen provider is not configured.
    """
    await require_workspace_access(ws, me)

    # PAT scope check (notify | full)
    if me.auth_scope and not any(s in me.auth_scope for s in ("notify", "full")):
        raise HTTPException(
            status_code=403,
            detail="Token thiếu scope 'notify' (hoặc 'full')",
        )

    # Phone normalization (raises ValueError → 400)
    try:
        to_e164 = normalize_phone(data.to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    provider = detect_provider(to_e164)
    if not is_configured(provider):
        # Audit even the rejection so workspace owners see something happened
        await audit_push(
            db, actor=me.email, workspace_id=ws, action="notify.sms",
            target=f"{provider}:not_configured", severity="err",
            metadata={"provider": provider, "to_prefix": to_e164[:4]},
        )
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Provider chưa được cấu hình ({provider})",
        )

    # Send
    try:
        result = await send_sms(to=to_e164, text=data.text)
    except RuntimeError as e:
        # Includes both "Provider chưa được cấu hình" (defense-in-depth) and upstream errors
        msg = str(e)
        status_code = 503 if "chưa được cấu hình" in msg else 502
        await audit_push(
            db, actor=me.email, workspace_id=ws, action="notify.sms",
            target=f"{provider}:error", severity="err",
            metadata={"provider": provider, "error": msg[:200]},
        )
        await db.commit()
        raise HTTPException(status_code=status_code, detail=msg)

    cost_usd = float(result.get("cost_usd") or 0.0)
    cost_vnd = int(round(cost_usd * USD_TO_VND))
    ok = result.get("status") not in ("failed",) and result.get("http_status", 200) < 400

    # Audit + billing
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="notify.sms",
        target=f"{result['provider']}:{to_e164[:6]}xxx",
        severity="ok" if ok else "warn",
        metadata={
            "provider": result["provider"],
            "message_id": result.get("message_id", ""),
            "len_text": len(data.text),
            "cost_vnd": cost_vnd,
            "status": result.get("status", ""),
        },
    )
    await billing_push(
        db, workspace_id=ws, layer="L4", action="notify.sms",
        cost_usd=cost_usd,
    )
    await db.commit()

    return SmsSendOut(
        ok=ok,
        provider=result["provider"],
        message_id=result.get("message_id", ""),
        to=to_e164,
        cost_usd=cost_usd,
        cost_vnd=cost_vnd,
        status=result.get("status", "unknown"),
    )
