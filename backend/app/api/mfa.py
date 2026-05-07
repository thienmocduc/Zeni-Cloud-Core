"""
Zeni Cloud Core — L5 Identity: MFA TOTP (Time-based One-Time Password).

Flow:
  1. User logged in (no MFA yet) → POST /auth/mfa/setup
     Returns base32 secret + QR data URI (scan trong Google Authenticator / Authy)
  2. User scans QR, gets 6-digit code → POST /auth/mfa/verify {code}
     Server verifies; if OK, mfa_enabled=true, secret stored encrypted
  3. Future logins with MFA: POST /auth/login → returns mfa_required=true + pre_token
     Then POST /auth/mfa/login {pre_token, code} → real JWT pair
  4. Disable: POST /auth/mfa/disable {password} (requires fresh password proof)
"""
from __future__ import annotations

import base64
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.security import make_access_token, make_refresh_token, verify_password
from app.core.vault import decrypt, encrypt
from app.db.base import get_db
from app.db.models import RefreshToken, User
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.mfa")
router = APIRouter(prefix="/auth/mfa", tags=["auth", "mfa"])


class SetupOut(BaseModel):
    secret: str
    qr_data_uri: str
    issuer: str = "Zeni Cloud"
    label: str


class VerifyIn(BaseModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class DisableIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class MfaLoginIn(BaseModel):
    pre_token: str
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


def _qr_data_uri(uri: str) -> str:
    """Generate QR code as data URI for inline display."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.post("/setup", response_model=SetupOut)
async def setup_mfa(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SetupOut:
    """Generate fresh TOTP secret + QR for current user. Doesn't enable until /verify."""
    user = me.user
    if user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA đã bật. Disable trước khi setup lại.")

    secret = pyotp.random_base32()
    # Store encrypted (won't be enabled until verify)
    user.mfa_secret_enc = encrypt(secret)
    await db.commit()

    issuer = "Zeni Cloud"
    label = user.email
    uri = pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)
    qr = _qr_data_uri(uri)

    return SetupOut(secret=secret, qr_data_uri=qr, issuer=issuer, label=label)


@router.post("/verify")
async def verify_mfa(
    data: VerifyIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify TOTP code + finalize MFA enable."""
    user = me.user
    if not user.mfa_secret_enc:
        raise HTTPException(status_code=400, detail="Chưa setup MFA. Gọi /setup trước.")
    try:
        secret = decrypt(user.mfa_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Không giải mã được MFA secret")

    totp = pyotp.TOTP(secret)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Mã không khớp hoặc hết hạn")

    user.mfa_enabled = True
    await audit_push(db, actor=user.email, workspace_id=None, action="mfa.enabled",
                     target=user.email, severity="ok")
    await db.commit()
    return {"ok": True, "mfa_enabled": True, "message": "MFA đã được kích hoạt"}


@router.post("/disable")
async def disable_mfa(
    data: DisableIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disable MFA. Requires both fresh password + current TOTP code (defense in depth)."""
    user = me.user
    if not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA chưa được bật")
    if not user.password_hash or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=403, detail="Mật khẩu không đúng")
    try:
        secret = decrypt(user.mfa_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Không giải mã được MFA secret")
    if not pyotp.TOTP(secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Mã TOTP không khớp")

    user.mfa_enabled = False
    user.mfa_secret_enc = None
    await audit_push(db, actor=user.email, workspace_id=None, action="mfa.disabled",
                     target=user.email, severity="warn")
    await db.commit()
    return {"ok": True, "mfa_enabled": False}


@router.post("/login")
async def mfa_login(data: MfaLoginIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Exchange MFA pre-token + TOTP code → JWT pair (used after first /auth/login if MFA on)."""
    # Look up pending pre-token
    row = (await db.execute(text(
        "SELECT user_id FROM mfa_pending WHERE token = :t AND expires_at > now()"
    ), {"t": data.pre_token})).first()
    if row is None:
        raise HTTPException(status_code=401, detail="pre_token không hợp lệ hoặc hết hạn")
    user_id = row[0]

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.disabled or not user.mfa_enabled or not user.mfa_secret_enc:
        raise HTTPException(status_code=401, detail="user/MFA không hợp lệ")

    secret = decrypt(user.mfa_secret_enc)
    if not pyotp.TOTP(secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Mã TOTP sai")

    # Consume pre-token
    await db.execute(text("DELETE FROM mfa_pending WHERE token = :t"), {"t": data.pre_token})

    # Issue JWT pair
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = datetime.now(timezone.utc)
    await audit_push(db, actor=user.email, workspace_id=None, action="auth.login.mfa",
                     target=user.email, severity="ok")
    await db.commit()

    return {
        "access_token": access, "refresh_token": refresh,
        "token_type": "Bearer", "expires_in": settings.jwt_access_ttl,
    }


# ─── Helper used by /auth/login to gate users with MFA ─────────
async def issue_mfa_pre_token(db: AsyncSession, user_id) -> str:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.execute(text(
        "INSERT INTO mfa_pending (token, user_id, expires_at) VALUES (:t, :u, :e)"
    ), {"t": token, "u": str(user_id), "e": exp})
    return token
