"""
Zeni Cloud Core — L5 Identity: Password change + reset flow.

Endpoints (mounted at /api/v1/auth/password/*):
  POST   /auth/password/change         — JWT-protected. Body: {old_password, new_password}
  POST   /auth/password/forgot/init    — Body: {email}. Email a reset link.
  POST   /auth/password/forgot/verify  — Body: {token, new_password}. Finalize.
  GET    /auth/password/forgot/status  — ?token=... → check still valid.

Security:
  - Reset tokens stored as SHA-256 hash only (token text emailed, never persisted).
  - TTL 1 hour. Single-use (used_at set on success).
  - IP hash stored for forensics.
  - Strong password rules (≥10 chars, mix upper/lower/digit/symbol).
  - Returns generic 200 OK even when email not found (no enumeration).
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.security import hash_password, verify_password
from app.db.base import get_db
from app.db.models import User
from app.services.audit import audit_push
from app.services.email import send_email

log = logging.getLogger("zeni.api.password")
router = APIRouter(prefix="/auth/password", tags=["auth"])


_RESET_TTL = timedelta(hours=1)
_TOKEN_BYTES = 32

# Strong password rules — yêu cầu mạnh hơn signup (≥10 + symbol)
_STRONG_PWD_RULES = (
    (lambda s: len(s) >= 10, "≥ 10 ký tự"),
    (lambda s: bool(re.search(r"[a-z]", s)), "1 chữ thường"),
    (lambda s: bool(re.search(r"[A-Z]", s)), "1 chữ HOA"),
    (lambda s: bool(re.search(r"[0-9]", s)), "1 chữ số"),
    (lambda s: bool(re.search(r"[^A-Za-z0-9]", s)), "1 ký tự đặc biệt"),
)


def _validate_strong_password(pw: str) -> str | None:
    """Returns error message if weak, None if OK."""
    for check, label in _STRONG_PWD_RULES:
        if not check(pw):
            return f"Mật khẩu yếu — cần {label}"
    return None


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_ip(request: Request) -> str:
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else "unknown")
    return hashlib.sha256(client_ip.encode()).hexdigest()[:64]


def _reset_link(token: str) -> str:
    base = "https://zenicloud.io"
    try:
        cors = settings.cors_origins_list
        if cors:
            base = cors[0].rstrip("/")
    except Exception:
        pass
    return f"{base}/forgot-password.html?token={token}"


# ─── Schemas ────────────────────────────────────────────────
class PasswordChangeIn(BaseModel):
    old_password: str = Field(min_length=6, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class ForgotInitIn(BaseModel):
    email: EmailStr


class ForgotVerifyIn(BaseModel):
    token: str = Field(min_length=20, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


# ─── Endpoints ──────────────────────────────────────────────
@router.post("/change")
async def change_password(
    data: PasswordChangeIn,
    request: Request,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """JWT-protected — change own password by proving knowledge of old one."""
    user = me.user
    if not user.password_hash:
        raise HTTPException(status_code=400,
                            detail="Tài khoản OAuth — không có password local. Đặt qua /forgot.")
    if not verify_password(data.old_password, user.password_hash):
        await audit_push(db, actor=user.email, workspace_id=None,
                         action="auth.password.change.fail", target=user.email,
                         severity="warn", metadata={"ip_hash": _hash_ip(request)})
        await db.commit()
        raise HTTPException(status_code=401, detail="Mật khẩu cũ không đúng")

    err = _validate_strong_password(data.new_password)
    if err:
        raise HTTPException(status_code=422, detail=err)
    if data.new_password == data.old_password:
        raise HTTPException(status_code=422,
                            detail="Mật khẩu mới phải khác mật khẩu cũ")

    user.password_hash = hash_password(data.new_password)
    await audit_push(db, actor=user.email, workspace_id=None,
                     action="auth.password.change", target=user.email,
                     severity="ok", metadata={"ip_hash": _hash_ip(request)})
    await db.commit()
    log.info("[password.change] user=%s updated", user.email)
    return {"ok": True, "message": "Đổi mật khẩu thành công"}


@router.post("/forgot/init")
async def forgot_init(
    data: ForgotInitIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Send reset link via email. Returns generic 200 to prevent email enumeration.

    Rate limit: 3 requests / 15 min / IP (best effort via audit log lookup).
    """
    email_norm = str(data.email).lower().strip()
    ip_hash = _hash_ip(request)

    # Rate limit: 3 init / 15min / IP
    fifteen_min_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
    rate_count = (await db.execute(text("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'auth.password.forgot.init' AND ts >= :since
              AND metadata->>'ip_hash' = :iph
    """), {"since": fifteen_min_ago, "iph": ip_hash})).scalar() or 0
    if rate_count >= 3:
        raise HTTPException(status_code=429,
                            detail="Quá nhiều yêu cầu reset password. Thử lại sau 15 phút.")

    user_row = (await db.execute(text(
        "SELECT id, email, name FROM users WHERE email = :e AND disabled = false"
    ), {"e": email_norm})).first()

    # Always audit-log the request (success path or not); response is generic
    if user_row is not None:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        token_hash = _hash_reset_token(token)
        expires_at = datetime.now(timezone.utc) + _RESET_TTL

        # Invalidate any previous unused tokens for this user (best-effort)
        try:
            await db.execute(text("""
                UPDATE password_resets SET used_at = NOW()
                WHERE user_id = :uid AND used_at IS NULL AND expires_at > NOW()
            """), {"uid": user_row[0]})
        except Exception:
            pass  # table may not exist yet on first deploy

        await db.execute(text("""
            INSERT INTO password_resets (user_id, token_hash, expires_at, ip_hash)
            VALUES (:uid, :th, :exp, :iph)
        """), {"uid": user_row[0], "th": token_hash, "exp": expires_at, "iph": ip_hash})

        # Send email
        link = _reset_link(token)
        subject = "[Zeni Cloud] Reset mật khẩu — link có hiệu lực 1 giờ"
        html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#fafafa;">
  <div style="background:#08051F;padding:28px;border-radius:12px;text-align:center;">
    <h1 style="color:#FAF5FF;margin:0;">Zeni Cloud</h1>
    <p style="color:#C4B5FD;margin:6px 0 0;font-size:12px;letter-spacing:0.1em;">RESET PASSWORD</p>
  </div>
  <div style="background:white;padding:32px;border-radius:12px;margin-top:16px;">
    <h2 style="color:#1a0938;margin:0 0 16px;">Xin chào {user_row[2] or email_norm.split('@')[0]},</h2>
    <p>Bạn (hoặc ai đó) đã yêu cầu đặt lại mật khẩu cho tài khoản <strong>{email_norm}</strong>.</p>
    <p>Click nút dưới đây để đặt mật khẩu mới (link hết hạn sau <strong>1 giờ</strong>):</p>
    <div style="text-align:center;margin:24px 0;">
      <a href="{link}" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#FDE68A,#F59E0B);color:#1a0938;text-decoration:none;font-weight:700;border-radius:8px;">Đặt lại mật khẩu</a>
    </div>
    <p style="color:#666;font-size:13px;">Hoặc copy link: <a href="{link}">{link}</a></p>
    <p style="color:#999;font-size:12px;margin-top:24px;">Không phải bạn? Bỏ qua email này — mật khẩu vẫn an toàn.</p>
  </div>
  <div style="text-align:center;margin-top:24px;color:#999;font-size:11px;">
    Zeni Holdings · zenicloud.io
  </div>
</body></html>"""
        try:
            await send_email(to=email_norm, subject=subject, body_html=html)
        except Exception as e:
            log.exception("[forgot.init] email send failed: %s", e)

        await audit_push(db, actor=email_norm, workspace_id=None,
                         action="auth.password.forgot.init", target=email_norm,
                         severity="info",
                         metadata={"ip_hash": ip_hash, "found": True})
    else:
        # Audit unfound emails — still rate limit them
        await audit_push(db, actor=email_norm, workspace_id=None,
                         action="auth.password.forgot.init", target=email_norm,
                         severity="info",
                         metadata={"ip_hash": ip_hash, "found": False})

    await db.commit()
    return {"ok": True,
            "message": "Nếu email tồn tại, link reset đã được gửi. Vui lòng kiểm tra hộp thư."}


@router.post("/forgot/verify")
async def forgot_verify(
    data: ForgotVerifyIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate token + set new password. Marks token used."""
    err = _validate_strong_password(data.new_password)
    if err:
        raise HTTPException(status_code=422, detail=err)

    token_hash = _hash_reset_token(data.token)
    row = (await db.execute(text("""
        SELECT id, user_id, expires_at, used_at
        FROM password_resets WHERE token_hash = :th
    """), {"th": token_hash})).first()
    if row is None:
        raise HTTPException(status_code=400, detail="Token không hợp lệ")
    if row[3] is not None:
        raise HTTPException(status_code=400, detail="Token đã được sử dụng")
    if row[2] < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Token đã hết hạn — yêu cầu reset lại")

    user_row = (await db.execute(text(
        "SELECT id, email FROM users WHERE id = :uid AND disabled = false"
    ), {"uid": row[1]})).first()
    if user_row is None:
        raise HTTPException(status_code=400, detail="Tài khoản không tồn tại hoặc đã bị disable")

    # Update password + mark token used
    await db.execute(text(
        "UPDATE users SET password_hash = :h WHERE id = :uid"
    ), {"h": hash_password(data.new_password), "uid": row[1]})
    await db.execute(text(
        "UPDATE password_resets SET used_at = NOW() WHERE id = :rid"
    ), {"rid": row[0]})

    await audit_push(db, actor=user_row[1], workspace_id=None,
                     action="auth.password.forgot.complete", target=user_row[1],
                     severity="ok", metadata={"ip_hash": _hash_ip(request)})
    await db.commit()
    log.info("[password.forgot.verify] user=%s password reset", user_row[1])
    return {"ok": True, "message": "Mật khẩu đã được đặt lại. Vui lòng đăng nhập."}


@router.get("/forgot/status")
async def forgot_status(
    token: str = Query(..., min_length=20, max_length=128),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check token validity (used by reset page to show 'expired' state)."""
    token_hash = _hash_reset_token(token)
    row = (await db.execute(text("""
        SELECT expires_at, used_at FROM password_resets WHERE token_hash = :th
    """), {"th": token_hash})).first()
    if row is None:
        return {"valid": False, "reason": "not_found"}
    if row[1] is not None:
        return {"valid": False, "reason": "used"}
    if row[0] < datetime.now(timezone.utc):
        return {"valid": False, "reason": "expired"}
    return {"valid": True, "expires_at": row[0].isoformat()}
