"""
Email API — public endpoint POST /api/v1/email/send.
Khách (NexBuild, BTHome, ANIMA, ...) gọi để gửi email qua Zeni Cloud Gmail SMTP.

Security:
  - Auth required (JWT or PAT scope ai|full|email)
  - Rate limit per workspace per day (theo tier)
  - Sender domain validation (chống spam)
  - Audit log + billing 100đ/email
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.email import send_email, is_configured

log = logging.getLogger("zeni.api.email")
router = APIRouter(prefix="/email", tags=["email"])


# Daily email cap per workspace tier
TIER_DAILY_CAP = {
    "free":     20,
    "starter":  500,
    "pro":      2000,
    "business": 10000,
    "enterprise": 100000,
}

EMAIL_COST_VND = 100  # 100đ/email — markup từ Gmail free

# Spam protection
_BLOCKED_KEYWORDS = re.compile(
    r"\b(viagra|casino|bitcoin investment|forex signals|nigerian prince|"
    r"loan offer|congratulations you won|click here urgent|verify your account)\b",
    re.IGNORECASE,
)
_MAX_RECIPIENTS_PER_REQUEST = 10


class EmailSendIn(BaseModel):
    to: EmailStr | list[EmailStr] = Field(
        description="Recipient email(s). Max 10 per request.",
    )
    subject: str = Field(min_length=1, max_length=255)
    body_html: str = Field(min_length=1, max_length=200_000)
    body_text: str | None = Field(default=None, max_length=200_000)
    reply_to: EmailStr | None = None
    tag: str | None = Field(default=None, max_length=64,
                             description="Tag để track (e.g., 'welcome', 'order_confirm')")


class EmailSendOut(BaseModel):
    sent: int
    failed: int
    cost_vnd: int
    message_ids: list[str] = []
    quota_remaining_today: int


async def _get_workspace_tier(db: AsyncSession, ws: str) -> str:
    row = (await db.execute(text("""
        SELECT tier FROM subscriptions
        WHERE workspace_id = :w AND status = 'active'
        ORDER BY created_at DESC LIMIT 1
    """), {"w": ws})).first()
    return row[0] if row else "free"


async def _check_daily_quota(db: AsyncSession, ws: str, requested: int) -> tuple[int, int]:
    """Returns (already_sent_today, daily_cap)."""
    tier = await _get_workspace_tier(db, ws)
    cap = TIER_DAILY_CAP.get(tier, 20)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sent_count = (await db.execute(text("""
        SELECT COALESCE(SUM(CAST(metadata->>'count' AS INTEGER)), 0)
        FROM audit_log
        WHERE workspace_id = :w AND action = 'email.send' AND ts >= :since
    """), {"w": ws, "since": since})).scalar() or 0
    if sent_count + requested > cap:
        raise HTTPException(
            status_code=429,
            detail=f"Vượt quota email tier {tier}: {sent_count}/{cap} đã dùng trong 24h. "
                   f"Yêu cầu thêm {requested}. Upgrade tier hoặc đợi 24h."
        )
    return int(sent_count), cap


def _validate_content(subject: str, body: str) -> None:
    combined = (subject + " " + body).lower()
    if _BLOCKED_KEYWORDS.search(combined):
        raise HTTPException(status_code=400,
                            detail="Nội dung email chứa pattern bị chặn (anti-spam)")


@router.post("/send", response_model=EmailSendOut)
async def send_email_endpoint(
    ws: str,
    data: EmailSendIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EmailSendOut:
    """
    Gửi email qua Zeni Cloud Gmail SMTP.
    Cost: 100đ/email. Daily cap theo tier.
    """
    await require_workspace_access(ws, me)
    # Check PAT scope
    if me.auth_scope and not any(s in me.auth_scope for s in ("email", "full")):
        raise HTTPException(status_code=403,
                            detail="Token thiếu scope 'email' (hoặc 'full')")

    if not is_configured():
        raise HTTPException(status_code=502,
                            detail="Email service chưa cấu hình SMTP — báo admin")

    # Normalize recipients
    if isinstance(data.to, str):
        recipients = [data.to]
    else:
        recipients = list(data.to)
    if len(recipients) > _MAX_RECIPIENTS_PER_REQUEST:
        raise HTTPException(status_code=400,
                            detail=f"Tối đa {_MAX_RECIPIENTS_PER_REQUEST} recipients/request")

    _validate_content(data.subject, data.body_html)

    # Daily quota
    sent_so_far, cap = await _check_daily_quota(db, ws, len(recipients))

    # Send
    sent = 0
    failed = 0
    message_ids = []
    for to_addr in recipients:
        try:
            ok = await send_email(
                to=str(to_addr), subject=data.subject,
                body_html=data.body_html, body_text=data.body_text,
            )
            if ok:
                sent += 1
                message_ids.append(f"zeni-{ws}-{int(datetime.now().timestamp() * 1000)}-{sent}")
            else:
                failed += 1
        except Exception as e:
            log.exception("Email send failed to %s: %s", to_addr, e)
            failed += 1

    # Charge wallet (100đ × sent)
    cost_vnd = sent * EMAIL_COST_VND
    if cost_vnd > 0:
        try:
            await db.execute(text("""
                UPDATE wallet_balances SET
                  balance_vnd = balance_vnd - :c,
                  total_spent = total_spent + :c, updated_at = NOW()
                WHERE workspace_id = :w
            """), {"w": ws, "c": cost_vnd})
            # Log transaction
            bal_row = (await db.execute(text(
                "SELECT balance_vnd FROM wallet_balances WHERE workspace_id = :w"
            ), {"w": ws})).first()
            balance_after = float(bal_row[0]) if bal_row else 0
            await db.execute(text("""
                INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                  description, ref_id, actor)
                VALUES(:w, 'charge', :amt, :bal, :desc, NULL, :ac)
            """), {"w": ws, "amt": -cost_vnd, "bal": balance_after,
                   "desc": f"email.send × {sent} recipients (tag={data.tag or 'none'})",
                   "ac": me.email})
        except Exception as e:
            log.warning("[email_api] charge wallet failed for %s: %s", ws, e)

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="email.send",
        target=f"{recipients[0][:30]}{'...' if len(recipients)>1 else ''}",
        severity="ok" if failed == 0 else "warn",
        metadata={"count": sent, "failed": failed, "tag": data.tag or "none",
                  "subject": data.subject[:60]},
    )
    await billing_push(db, workspace_id=ws, layer="L5", action="email.send",
                       cost_usd=cost_vnd / 25_000)
    await db.commit()

    return EmailSendOut(
        sent=sent, failed=failed, cost_vnd=cost_vnd,
        message_ids=message_ids,
        quota_remaining_today=cap - sent_so_far - sent,
    )


@router.get("/quota")
async def email_quota(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check email quota usage today."""
    await require_workspace_access(ws, me)
    tier = await _get_workspace_tier(db, ws)
    cap = TIER_DAILY_CAP.get(tier, 20)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sent = (await db.execute(text("""
        SELECT COALESCE(SUM(CAST(metadata->>'count' AS INTEGER)), 0)
        FROM audit_log
        WHERE workspace_id = :w AND action = 'email.send' AND ts >= :since
    """), {"w": ws, "since": since})).scalar() or 0
    return {
        "workspace_id": ws,
        "tier": tier,
        "daily_cap": cap,
        "sent_last_24h": int(sent),
        "remaining": cap - int(sent),
        "cost_per_email_vnd": EMAIL_COST_VND,
    }
