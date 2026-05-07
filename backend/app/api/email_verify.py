"""
Zeni Cloud Core — L5 Identity: Email Verification.

Flow:
  1. Authenticated user → POST /auth/email/send-verification
     Generates a 32-byte URL-safe token, stores it in `email_verifications`,
     sends an email with link `{ZENI_BASE_URL}/api/v1/auth/email/verify?token=...`.
     Rate-limited: max 3 sends/hour per user (DB-tracked via `sent_at`).

  2. User clicks link → GET /auth/email/verify?token=...
     Validates token (not consumed, not expired, ≤5 attempts).
     On success: marks `users.email_verified_at = NOW()`,
     marks the row `verified_at = NOW()`, redirects → `{ZENI_BASE_URL}/app#email-verified`.

  3. GET /auth/email/status — current user's verification state.

Security:
  - Tokens are 32-byte secrets.token_urlsafe() (~43 chars, 256-bit entropy).
  - One-time use: rows with verified_at IS NOT NULL cannot be reused.
  - 24h expiry. Per-token attempt counter.
  - Audit pushed for all sends + verifications.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings  # noqa: F401
from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db
from app.db.models import User
from app.services.audit import audit_push
from app.services.email import send_email

log = logging.getLogger("zeni.api.email_verify")
router = APIRouter(prefix="/auth/email", tags=["auth", "email"])

# ── Config helpers ────────────────────────────────────────────
def _base_url() -> str:
    """Public base URL used for verification links."""
    val = getattr(settings, "zeni_base_url", None) or os.environ.get("ZENI_BASE_URL", "")
    return (val or "https://zenicloud.io").rstrip("/")


# ── Pydantic schemas ──────────────────────────────────────────
class SendVerificationIn(BaseModel):
    email: EmailStr | None = Field(
        default=None,
        description="Optional override; defaults to current user's email.",
    )


class VerificationStatusOut(BaseModel):
    email: str
    email_verified: bool
    email_verified_at: datetime | None
    last_sent_at: datetime | None
    pending_token: bool


class SendOk(BaseModel):
    ok: bool
    sent_at: datetime
    expires_at: datetime
    message: str


# ── Email template ────────────────────────────────────────────
def _render_verification_email(*, name: str, verify_url: str) -> tuple[str, str]:
    subject = "[Zeni Cloud] Xac nhan dia chi email"
    safe_name = (name or "ban").replace("<", "&lt;").replace(">", "&gt;")
    safe_url = verify_url.replace("<", "%3C").replace(">", "%3E")
    html = f"""
<!DOCTYPE html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; background: #fafafa; color: #1a1a1a;">
  <div style="background: #08051F; padding: 28px; border-radius: 12px; text-align: center;">
    <div style="display:inline-block;width:60px;height:60px;border-radius:14px;background:linear-gradient(135deg,#FDE68A,#A855F7);color:#1a0938;font-weight:900;font-size:32px;line-height:60px;">Z</div>
    <h1 style="color:#FAF5FF;margin:16px 0 4px;font-size:24px;">Zeni Cloud</h1>
    <p style="color:#C4B5FD;margin:0;font-size:12px;letter-spacing:0.1em;">UNIFIED CLOUD OS</p>
  </div>
  <div style="background:white;padding:32px;border-radius:12px;margin-top:16px;">
    <h2 style="color:#1a0938;margin:0 0 16px;">Xac nhan email cua ban</h2>
    <p>Xin chao <strong>{safe_name}</strong>,</p>
    <p>Click nut ben duoi de xac nhan dia chi email cho tai khoan Zeni Cloud cua ban.
       Lien ket nay het han sau 24 gio.</p>
    <div style="text-align:center;margin:24px 0;">
      <a href="{safe_url}" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#FDE68A,#F59E0B);color:#1a0938;text-decoration:none;font-weight:700;border-radius:8px;">Xac nhan email</a>
    </div>
    <p style="color:#666;font-size:13px;">Hoac copy link: <a href="{safe_url}">{safe_url}</a></p>
    <p style="color:#999;font-size:12px;margin-top:24px;">Neu khong phai ban yeu cau, bo qua email nay — khong co thay doi nao duoc thuc hien.</p>
  </div>
  <div style="text-align:center;margin-top:24px;color:#999;font-size:11px;">
    San pham cua <strong>Zeni Holdings</strong> · zenicloud.io
  </div>
</body></html>
"""
    return subject, html


# ─────────────────────────────────────────────────────────────
# 1. POST /auth/email/send-verification
# ─────────────────────────────────────────────────────────────
@router.post("/send-verification", response_model=SendOk)
async def send_verification(
    data: SendVerificationIn,
    request: Request,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SendOk:
    """
    Send a verification email to the current user.

    Rate limit: max 3 sends per hour per user (counted from `email_verifications.sent_at`).
    """
    user = me.user

    # Resolve target email — default = user's, override only if same
    target_email = (str(data.email).lower().strip() if data.email else user.email).lower()
    if target_email != user.email.lower():
        raise HTTPException(
            status_code=400,
            detail="Email override khong khop voi tai khoan. Lien he support de doi email.",
        )

    if user.email_verified_at is not None:
        raise HTTPException(status_code=400, detail="Email da xac nhan truoc do.")

    # Rate limit: 3 sends/hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = (await db.execute(_sql("""
        SELECT COUNT(*) FROM email_verifications
        WHERE user_id = :uid AND sent_at >= :since
    """), {"uid": str(user.id), "since": one_hour_ago})).scalar() or 0
    if recent_count >= 3:
        raise HTTPException(
            status_code=429,
            detail="Da gui 3 lan trong 1 gio. Thu lai sau.",
        )

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=24)

    await db.execute(_sql("""
        INSERT INTO email_verifications (user_id, token, email, expires_at, sent_at)
        VALUES (:uid, :tok, :em, :exp, :now)
    """), {"uid": str(user.id), "tok": token, "em": target_email, "exp": exp, "now": now})

    verify_url = f"{_base_url()}/api/v1/auth/email/verify?token={token}"
    subject, html = _render_verification_email(name=user.name, verify_url=verify_url)
    sent = await send_email(to=target_email, subject=subject, body_html=html)
    if not sent:
        log.warning("[email_verify] send_email returned False for %s — token persisted anyway", target_email)

    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else "unknown")
    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.email.verification_sent",
        target=target_email, severity="info",
        metadata={"smtp_sent": bool(sent), "expires_at": exp.isoformat(),
                  "ip_hint": client_ip[:64]},
    )
    await db.commit()

    return SendOk(
        ok=True,
        sent_at=now,
        expires_at=exp,
        message="Da gui email xac nhan. Kiem tra hop thu (cha ca thu mucSPAM).",
    )


# ─────────────────────────────────────────────────────────────
# 2. GET /auth/email/verify?token=...
# ─────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify_email(
    token: str = Query(min_length=8, max_length=128),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Public callback. Validates token, marks email verified, redirects to /app.
    """
    base = _base_url()

    # Look up
    row = (await db.execute(_sql("""
        SELECT id, user_id, email, expires_at, verified_at, attempts
          FROM email_verifications
         WHERE token = :tok
    """), {"tok": token})).mappings().first()

    if row is None:
        return RedirectResponse(
            url=f"{base}/app#email-verify-error=invalid",
            status_code=302,
        )

    # Always bump attempts (audit-grade)
    await db.execute(
        _sql("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = :id"),
        {"id": row["id"]},
    )

    if row["verified_at"] is not None:
        await db.commit()
        return RedirectResponse(url=f"{base}/app#email-already-verified", status_code=302)

    if int(row["attempts"]) >= 5:
        await db.commit()
        return RedirectResponse(url=f"{base}/app#email-verify-error=too_many_attempts",
                                status_code=302)

    if row["expires_at"] < datetime.now(timezone.utc):
        await db.commit()
        return RedirectResponse(url=f"{base}/app#email-verify-error=expired",
                                status_code=302)

    # Mark user + token verified
    user = (await db.execute(select(User).where(User.id == row["user_id"]))).scalar_one_or_none()
    if user is None:
        await db.commit()
        return RedirectResponse(url=f"{base}/app#email-verify-error=user_missing",
                                status_code=302)

    now = datetime.now(timezone.utc)
    await db.execute(_sql("""
        UPDATE email_verifications SET verified_at = :now WHERE id = :id
    """), {"now": now, "id": row["id"]})
    user.email_verified_at = now

    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.email.verified",
        target=user.email, severity="ok",
        metadata={"token_id": int(row["id"])},
    )
    await db.commit()

    return RedirectResponse(url=f"{base}/app#email-verified", status_code=302)


# ─────────────────────────────────────────────────────────────
# 3. GET /auth/email/status
# ─────────────────────────────────────────────────────────────
@router.get("/status", response_model=VerificationStatusOut)
async def verification_status(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VerificationStatusOut:
    """Return current user's verification status + last send + pending token presence."""
    user = me.user
    last = (await db.execute(_sql("""
        SELECT sent_at, verified_at, expires_at
          FROM email_verifications
         WHERE user_id = :uid
         ORDER BY sent_at DESC
         LIMIT 1
    """), {"uid": str(user.id)})).mappings().first()

    pending = False
    last_sent = None
    if last is not None:
        last_sent = last["sent_at"]
        pending = (
            last["verified_at"] is None
            and last["expires_at"] >= datetime.now(timezone.utc)
        )

    return VerificationStatusOut(
        email=user.email,
        email_verified=bool(user.email_verified_at),
        email_verified_at=user.email_verified_at,
        last_sent_at=last_sent,
        pending_token=pending,
    )
