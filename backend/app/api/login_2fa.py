"""
Zeni Cloud Core — L5 Identity: 2FA Login Wrapper.

Adds an MFA gate on top of the existing /auth/login flow.

Endpoints:
  POST /auth/login-2fa            — drop-in /auth/login with mfa_required gate
  POST /auth/login-2fa/verify     — exchange challenge_token + TOTP → JWT pair
  POST /auth/mfa/enable           — turn on user.mfa_required (must already have TOTP enrolled)
  POST /auth/mfa/disable          — turn off mfa_required (verifies TOTP first)

Reuses existing TOTP logic — does not duplicate. The TOTP secret is stored on
`users.mfa_secret_enc` (Fernet-encrypted) and managed via /auth/mfa/setup +
/auth/mfa/verify in app.api.mfa.

Security:
  - IP rate limit: 10 failed/15min (mirrors /auth/login).
  - Challenge tokens are 32-byte url-safe; expire in 5 min; one-shot (consumed=true).
  - On TOTP verify failure, audit + counted toward rate limit.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.security import (
    make_access_token,
    make_refresh_token,
    verify_password,
)
from app.core.vault import decrypt
from app.db.base import get_db
from app.db.models import RefreshToken, User
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.login_2fa")
router = APIRouter(prefix="/auth", tags=["auth", "mfa"])


# ── Schemas ───────────────────────────────────────────────────
class Login2FAIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class Login2FAVerifyIn(BaseModel):
    challenge_token: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")
    method: str = Field(default="totp", pattern=r"^(totp|sms_otp|email_otp)$")


class MfaEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$",
                      description="Current TOTP code from authenticator app")


class MfaDisableIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


# ── Helpers ───────────────────────────────────────────────────
def _ip_hash(request: Request) -> tuple[str, str]:
    raw = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
          (request.client.host if request.client else "unknown")
    return raw, hashlib.sha256(raw.encode()).hexdigest()[:32]


async def _ip_fail_count(db: AsyncSession, ip_hash: str, since: datetime) -> int:
    n = (await db.execute(_sql("""
        SELECT COUNT(*) FROM audit_log
         WHERE action IN ('auth.login.fail', 'auth.login_2fa.fail')
           AND ts >= :since
           AND metadata->>'ip_hash' = :iph
    """), {"since": since, "iph": ip_hash})).scalar() or 0
    return int(n)


# ─────────────────────────────────────────────────────────────
# 1. POST /auth/login-2fa  (drop-in replacement for /auth/login)
# ─────────────────────────────────────────────────────────────
@router.post("/login-2fa")
async def login_2fa(
    data: Login2FAIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Password OK + mfa_required=False → return JWT pair immediately.
    Password OK + mfa_required=True  → return {challenge_token, methods}.
    """
    raw_ip, iph = _ip_hash(request)
    fifteen_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
    if await _ip_fail_count(db, iph, fifteen_ago) >= 10:
        raise HTTPException(status_code=429,
                            detail="Qua nhieu lan dang nhap sai. Thu lai sau 15 phut.")

    user = (await db.execute(
        select(User).where(User.email == str(data.email).lower())
    )).scalar_one_or_none()

    auth_ok = (
        user is not None
        and not user.disabled
        and user.password_hash
        and verify_password(data.password, user.password_hash)
    )
    if not auth_ok:
        await audit_push(
            db, actor=str(data.email).lower(), workspace_id=None,
            action="auth.login_2fa.fail",
            target=str(data.email).lower()[:80],
            severity="warn", metadata={"ip_hash": iph},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="Email hoac mat khau khong dung")

    # Determine if MFA gate triggers
    mfa_gate = bool(user.mfa_required and user.mfa_enabled and user.mfa_secret_enc)

    if mfa_gate:
        challenge = secrets.token_urlsafe(32)
        exp = datetime.now(timezone.utc) + timedelta(minutes=5)
        await db.execute(_sql("""
            INSERT INTO login_challenges (challenge_token, user_id, method, expires_at)
            VALUES (:t, :u, 'totp', :e)
        """), {"t": challenge, "u": str(user.id), "e": exp})
        await audit_push(
            db, actor=user.email, workspace_id=None,
            action="auth.login_2fa.challenge_issued",
            target=user.email, severity="info",
            metadata={"ip_hash": iph, "method": "totp"},
        )
        await db.commit()
        return {
            "mfa_required": True,
            "challenge_token": challenge,
            "methods": ["totp"],
            "expires_in": 300,
            "message": "Nhap ma TOTP de hoan tat dang nhap",
        }

    # No MFA → issue JWT pair
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp_at = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp_at))
    user.last_login = datetime.now(timezone.utc)
    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.login_2fa", target=user.email, severity="ok",
        metadata={"ip_hash": iph, "mfa": False},
    )
    await db.commit()
    return {
        "access_token": access, "refresh_token": refresh,
        "token_type": "Bearer", "expires_in": settings.jwt_access_ttl,
    }


# ─────────────────────────────────────────────────────────────
# 2. POST /auth/login-2fa/verify
# ─────────────────────────────────────────────────────────────
@router.post("/login-2fa/verify")
async def login_2fa_verify(
    data: Login2FAVerifyIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify challenge_token + TOTP code → issue JWT pair."""
    raw_ip, iph = _ip_hash(request)
    fifteen_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
    if await _ip_fail_count(db, iph, fifteen_ago) >= 10:
        raise HTTPException(status_code=429,
                            detail="Qua nhieu lan dang nhap sai. Thu lai sau 15 phut.")

    row = (await db.execute(_sql("""
        SELECT id, user_id, method, expires_at, consumed
          FROM login_challenges
         WHERE challenge_token = :t
    """), {"t": data.challenge_token})).mappings().first()

    if row is None or row["consumed"]:
        await audit_push(
            db, actor="login_2fa", workspace_id=None,
            action="auth.login_2fa.fail", target="unknown",
            severity="warn", metadata={"ip_hash": iph, "reason": "bad_challenge"},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="challenge_token khong hop le hoac da dung")

    if row["expires_at"] < datetime.now(timezone.utc):
        await db.commit()
        raise HTTPException(status_code=401, detail="challenge_token het han")

    if row["method"] != data.method:
        raise HTTPException(status_code=400,
                            detail=f"method khong khop (challenge={row['method']})")

    user = (await db.execute(
        select(User).where(User.id == row["user_id"])
    )).scalar_one_or_none()
    if user is None or user.disabled or not user.mfa_enabled or not user.mfa_secret_enc:
        await db.commit()
        raise HTTPException(status_code=401, detail="user / MFA khong hop le")

    # Verify TOTP — reuse decrypt + pyotp logic from existing mfa.py
    try:
        secret = decrypt(user.mfa_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Khong giai ma duoc MFA secret")

    if not pyotp.TOTP(secret).verify(data.code, valid_window=1):
        await audit_push(
            db, actor=user.email, workspace_id=None,
            action="auth.login_2fa.fail", target=user.email,
            severity="warn", metadata={"ip_hash": iph, "reason": "bad_totp"},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="Ma TOTP sai")

    # Consume the challenge
    await db.execute(
        _sql("UPDATE login_challenges SET consumed = TRUE WHERE id = :id"),
        {"id": int(row["id"])},
    )

    # Issue JWT pair
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp_at = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp_at))
    user.last_login = datetime.now(timezone.utc)

    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.login_2fa.mfa_ok", target=user.email, severity="ok",
        metadata={"ip_hash": iph, "challenge_id": int(row["id"])},
    )
    await db.commit()
    return {
        "access_token": access, "refresh_token": refresh,
        "token_type": "Bearer", "expires_in": settings.jwt_access_ttl,
    }


# ─────────────────────────────────────────────────────────────
# 3. POST /auth/mfa/enable    (turn on mfa_required)
# ─────────────────────────────────────────────────────────────
@router.post("/mfa/enable")
async def enable_mfa_required(
    data: MfaEnableIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Turn on `users.mfa_required = TRUE`. User MUST already have TOTP enrolled
    (mfa_enabled + mfa_secret_enc) — call /auth/mfa/setup + /auth/mfa/verify first.
    Verifies a fresh TOTP code as proof-of-possession before flipping the bit.
    """
    user = me.user
    if not user.mfa_enabled or not user.mfa_secret_enc:
        raise HTTPException(
            status_code=400,
            detail="Chua enroll TOTP. Goi /auth/mfa/setup + /auth/mfa/verify truoc.",
        )
    if user.mfa_required:
        return {"ok": True, "mfa_required": True, "message": "MFA da bat truoc do"}

    try:
        secret = decrypt(user.mfa_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Khong giai ma duoc MFA secret")

    if not pyotp.TOTP(secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Ma TOTP sai")

    user.mfa_required = True
    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.mfa.required.enabled", target=user.email, severity="ok",
    )
    await db.commit()
    return {"ok": True, "mfa_required": True, "message": "Da bat MFA bat buoc cho moi lan dang nhap"}


# ─────────────────────────────────────────────────────────────
# 4. POST /auth/mfa/disable  (turn off mfa_required)
# ─────────────────────────────────────────────────────────────
@router.post("/mfa/disable")
async def disable_mfa_required(
    data: MfaDisableIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Turn off `users.mfa_required = FALSE`. Defense-in-depth: requires fresh
    password + TOTP code. Does NOT remove the TOTP secret — call /auth/mfa/disable
    in app.api.mfa for full removal.
    """
    user = me.user
    if not user.mfa_required:
        return {"ok": True, "mfa_required": False, "message": "MFA chua bat"}

    if not user.password_hash or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=403, detail="Mat khau khong dung")

    if not user.mfa_secret_enc:
        raise HTTPException(status_code=500, detail="Tai khoan thieu MFA secret — lien he support")

    try:
        secret = decrypt(user.mfa_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Khong giai ma duoc MFA secret")

    if not pyotp.TOTP(secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Ma TOTP sai")

    user.mfa_required = False
    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.mfa.required.disabled", target=user.email, severity="warn",
    )
    await db.commit()
    return {"ok": True, "mfa_required": False, "message": "Da tat MFA bat buoc"}
