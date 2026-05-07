"""
Zeni Cloud Core — L5 Identity: Phone OTP (signup / login / reset / add-phone).

Endpoints:
  POST /auth/phone/send-otp
  POST /auth/phone/verify-otp   (generic verify — for add_phone, reset, step_up)
  POST /auth/phone/login        (phone + OTP → JWT, with optional 2FA gate)
  POST /auth/phone/signup       (phone + OTP + password + name + workspace → user)

Security:
  - 6-digit OTP, randomly generated. Stored only as bcrypt hash.
  - 10-minute expiry. Max 5 verify attempts per OTP — then row marked verified=NULL+attempts=99.
  - Rate limits:
      * Per phone: 3 sends / 15 min
      * Per IP   : 10 sends / day
  - Phone normalized to E.164 (uses services.sms.normalize_phone; +84 / 0xxx → +84xxx).
  - Audit pushed for sends + verifies + signups + logins.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select, text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.security import (
    hash_password,
    make_access_token,
    make_refresh_token,
)
from app.db.base import get_db
from app.db.models import RefreshToken, User, UserWorkspace, Workspace
from app.services.audit import audit_push

# Phone helpers — reuse SMS module
try:
    from app.services.sms import is_configured as _sms_is_configured
    from app.services.sms import normalize_phone as _normalize_phone
    from app.services.sms import send_sms as _send_sms_real
except Exception:  # pragma: no cover — keeps module importable if sms missing
    _normalize_phone = None  # type: ignore
    _sms_is_configured = None  # type: ignore
    _send_sms_real = None  # type: ignore

log = logging.getLogger("zeni.api.phone_otp")
router = APIRouter(prefix="/auth/phone", tags=["auth", "phone"])

# ── Constants ─────────────────────────────────────────────────
OTP_TTL_MIN = 10
MAX_ATTEMPTS = 5
PHONE_RATE_PER_15MIN = 3
IP_RATE_PER_DAY = 10
PURPOSES = ("signup", "login", "reset", "add_phone", "step_up")

_RESERVED_WS_IDS = {
    "admin", "api", "auth", "login", "signup", "system", "internal",
    "_admin", "root", "zeni", "zenicloud", "test", "demo", "public", "private",
    "billing", "wallet", "settings", "owner", "support",
}

# ── Helpers ───────────────────────────────────────────────────
def _normalize_phone_safe(raw: str) -> str:
    """E.164 normalize. Falls back to a local impl if services.sms is unavailable."""
    if _normalize_phone is not None:
        return _normalize_phone(raw)
    if not raw:
        raise ValueError("So dien thoai khong hop le")
    s = re.sub(r"[\s\-()]", "", raw.strip())
    if s.startswith("0"):
        s = "+84" + s[1:]
    elif not s.startswith("+"):
        raise ValueError("So dien thoai khong hop le")
    if not re.match(r"^\+?\d{10,15}$", s):
        raise ValueError("So dien thoai khong hop le")
    return s


def _gen_otp() -> str:
    """6-digit code, zero-padded. Uses secrets.randbelow for cryptographic strength."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_otp(code: str) -> str:
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def _verify_otp(code: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(code.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
           (request.client.host if request.client else "unknown")


# ── Schemas ───────────────────────────────────────────────────
class SendOtpIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    purpose: Literal["signup", "login", "reset", "add_phone", "step_up"] = "login"


class SendOtpOut(BaseModel):
    ok: bool
    phone: str
    purpose: str
    expires_at: datetime
    message: str


class VerifyOtpIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    purpose: Literal["signup", "login", "reset", "add_phone", "step_up"] = "login"


class PhoneLoginIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PhoneSignupIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=2, max_length=128)
    company: str = Field(min_length=2, max_length=64)
    email: EmailStr
    workspace_id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")

    @field_validator("workspace_id")
    @classmethod
    def _ws_not_reserved(cls, v: str) -> str:
        if v in _RESERVED_WS_IDS:
            raise ValueError(f"Workspace ID '{v}' la tu bao luu — chon ten khac")
        return v


# ─────────────────────────────────────────────────────────────
# Internal: send the SMS (provider-agnostic; gracefully degrades)
# ─────────────────────────────────────────────────────────────
async def _dispatch_sms(phone: str, code: str, purpose: str) -> bool:
    """
    Send the OTP SMS. Prefers services.sms.send_sms; if unavailable / not configured,
    logs and returns False — caller still persists the OTP so user can retry once
    SMS becomes available, but flow can also be dev-tested via DB inspection.
    """
    body = f"[Zeni Cloud] Ma xac thuc cua ban: {code}. Het han sau {OTP_TTL_MIN} phut. Khong chia se ma nay."
    try:
        if _send_sms_real is None:
            log.warning("[phone_otp] services.sms unavailable — OTP persisted but not sent")
            return False
        # Preflight check: fail fast if no provider creds
        if _sms_is_configured is not None:
            provider = "stringee" if phone.startswith("+84") else "twilio"
            if not _sms_is_configured(provider):
                log.warning("[phone_otp] provider %s not configured — OTP persisted only", provider)
                return False
        result = await _send_sms_real(to=phone, text=body)
        return bool(result and result.get("status") not in ("failed",))
    except Exception as e:
        log.warning("[phone_otp] SMS send failed for %s: %s", phone[:6] + "xxx", e)
        return False


# ─────────────────────────────────────────────────────────────
# 1. POST /auth/phone/send-otp
# ─────────────────────────────────────────────────────────────
@router.post("/send-otp", response_model=SendOtpOut)
async def send_otp(
    data: SendOtpIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SendOtpOut:
    """Generate + send a 6-digit OTP. Rate limited per phone (3/15min) and per IP (10/day)."""
    if data.purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail=f"purpose phai trong {PURPOSES}")

    try:
        phone = _normalize_phone_safe(data.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ip = _client_ip(request)
    now = datetime.now(timezone.utc)
    fifteen_min_ago = now - timedelta(minutes=15)
    one_day_ago = now - timedelta(days=1)

    # Per-phone rate limit
    phone_count = (await db.execute(_sql("""
        SELECT COUNT(*) FROM phone_otps WHERE phone = :p AND sent_at >= :since
    """), {"p": phone, "since": fifteen_min_ago})).scalar() or 0
    if phone_count >= PHONE_RATE_PER_15MIN:
        raise HTTPException(
            status_code=429,
            detail=f"Da gui {PHONE_RATE_PER_15MIN} OTP cho so nay trong 15 phut. Thu lai sau.",
        )

    # Per-IP rate limit
    ip_count = (await db.execute(_sql("""
        SELECT COUNT(*) FROM phone_otps WHERE ip = :ip AND sent_at >= :since
    """), {"ip": ip, "since": one_day_ago})).scalar() or 0
    if ip_count >= IP_RATE_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"IP nay da gui {IP_RATE_PER_DAY} OTP trong 24 gio. Thu lai sau.",
        )

    code = _gen_otp()
    code_hash = _hash_otp(code)
    exp = now + timedelta(minutes=OTP_TTL_MIN)

    # Resolve user_id if signup-known phone or login flow
    user_id: UUID | None = None
    if data.purpose in ("login", "reset", "add_phone", "step_up"):
        u = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if u is not None:
            user_id = u.id
        elif data.purpose == "login":
            # Don't enumerate — still send (consume a slot) but tag without user
            log.info("[phone_otp] login OTP for unregistered phone %s***", phone[:6])

    await db.execute(_sql("""
        INSERT INTO phone_otps (phone, code_hash, purpose, user_id, expires_at, sent_at, ip)
        VALUES (:p, :h, :pu, :uid, :exp, :now, :ip)
    """), {
        "p": phone, "h": code_hash, "pu": data.purpose,
        "uid": str(user_id) if user_id else None,
        "exp": exp, "now": now, "ip": ip[:128],
    })

    # Send SMS (best-effort)
    sent_ok = await _dispatch_sms(phone, code, data.purpose)

    await audit_push(
        db, actor=phone, workspace_id=None,
        action="auth.phone.otp_sent",
        target=phone[:6] + "xxx", severity="info",
        metadata={"purpose": data.purpose, "smtp_sent": False,
                  "sms_sent": sent_ok, "ip_hint": ip[:64]},
    )
    await db.commit()

    return SendOtpOut(
        ok=True, phone=phone, purpose=data.purpose, expires_at=exp,
        message=f"OTP da duoc gui den {phone}. Het han sau {OTP_TTL_MIN} phut.",
    )


# ─────────────────────────────────────────────────────────────
# Internal: consume the latest valid OTP for (phone, purpose)
# Returns (ok, otp_id, user_id_from_otp). On wrong code, increments attempts.
# ─────────────────────────────────────────────────────────────
async def _consume_otp(
    db: AsyncSession, phone: str, code: str, purpose: str
) -> tuple[bool, int | None, UUID | None]:
    row = (await db.execute(_sql("""
        SELECT id, code_hash, user_id, expires_at, verified_at, attempts
          FROM phone_otps
         WHERE phone = :p AND purpose = :pu AND verified_at IS NULL
         ORDER BY sent_at DESC
         LIMIT 1
    """), {"p": phone, "pu": purpose})).mappings().first()

    if row is None:
        return False, None, None

    if int(row["attempts"]) >= MAX_ATTEMPTS:
        return False, int(row["id"]), None

    if row["expires_at"] < datetime.now(timezone.utc):
        return False, int(row["id"]), None

    if not _verify_otp(code, row["code_hash"]):
        await db.execute(_sql("UPDATE phone_otps SET attempts = attempts + 1 WHERE id = :id"),
                         {"id": int(row["id"])})
        return False, int(row["id"]), None

    # Success — mark verified
    now = datetime.now(timezone.utc)
    await db.execute(_sql("""
        UPDATE phone_otps SET verified_at = :now, attempts = attempts + 1 WHERE id = :id
    """), {"now": now, "id": int(row["id"])})

    uid: UUID | None = None
    if row["user_id"]:
        try:
            uid = UUID(str(row["user_id"]))
        except Exception:
            uid = None
    return True, int(row["id"]), uid


# ─────────────────────────────────────────────────────────────
# 2. POST /auth/phone/verify-otp  (generic — add_phone / step_up / reset)
# ─────────────────────────────────────────────────────────────
@router.post("/verify-otp")
async def verify_otp(
    data: VerifyOtpIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Generic verify endpoint.
      - signup → caller should use /auth/phone/signup instead (this returns 400 hint).
      - login  → caller should use /auth/phone/login instead.
      - add_phone → must be authenticated; links phone to current user.
      - reset / step_up → returns one-shot challenge token good for 10 min.
    """
    if data.purpose == "signup":
        raise HTTPException(status_code=400, detail="Dung /auth/phone/signup cho purpose=signup")
    if data.purpose == "login":
        raise HTTPException(status_code=400, detail="Dung /auth/phone/login cho purpose=login")

    try:
        phone = _normalize_phone_safe(data.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if data.purpose == "add_phone":
        # Requires auth; we re-resolve user via header inside the handler
        # Simpler: require Authorization header & decode manually here
        from app.core.deps import get_current_user as _gcu
        try:
            me = await _gcu(authorization=request.headers.get("authorization"), db=db)
        except HTTPException as e:
            raise e

        ok, otp_id, _ = await _consume_otp(db, phone, data.code, "add_phone")
        if not ok:
            await db.commit()
            raise HTTPException(status_code=401, detail="OTP sai hoac het han")

        # Make sure no other user is using this phone
        existing = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if existing and existing.id != me.user.id:
            raise HTTPException(status_code=409, detail="So dien thoai da duoc dung boi tai khoan khac")

        me.user.phone = phone
        me.user.phone_verified_at = datetime.now(timezone.utc)
        await audit_push(
            db, actor=me.user.email, workspace_id=None,
            action="auth.phone.added", target=phone[:6] + "xxx", severity="ok",
            metadata={"otp_id": otp_id},
        )
        await db.commit()
        return {"ok": True, "phone_verified": True, "phone": phone}

    # reset / step_up — issue short-lived challenge token, no JWT
    ok, otp_id, uid = await _consume_otp(db, phone, data.code, data.purpose)
    if not ok:
        await db.commit()
        raise HTTPException(status_code=401, detail="OTP sai hoac het han")

    challenge = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    if uid is not None:
        await db.execute(_sql("""
            INSERT INTO login_challenges (challenge_token, user_id, method, expires_at)
            VALUES (:t, :u, 'sms_otp', :e)
        """), {"t": challenge, "u": str(uid), "e": exp})

    await audit_push(
        db, actor=phone[:6] + "xxx", workspace_id=None,
        action=f"auth.phone.otp_verified.{data.purpose}",
        target=phone[:6] + "xxx", severity="info",
        metadata={"otp_id": otp_id, "user_resolved": bool(uid)},
    )
    await db.commit()

    return {
        "ok": True,
        "purpose": data.purpose,
        "challenge_token": challenge if uid else None,
        "expires_at": exp.isoformat() if uid else None,
    }


# ─────────────────────────────────────────────────────────────
# 3. POST /auth/phone/login  (phone + OTP → JWT pair, with 2FA gate)
# ─────────────────────────────────────────────────────────────
@router.post("/login")
async def phone_login(
    data: PhoneLoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Login with phone + OTP. Returns JWT pair, or 2FA challenge if mfa_required."""
    try:
        phone = _normalize_phone_safe(data.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ok, otp_id, uid = await _consume_otp(db, phone, data.code, "login")
    if not ok:
        await audit_push(
            db, actor=phone[:6] + "xxx", workspace_id=None,
            action="auth.phone.login.fail", target=phone[:6] + "xxx",
            severity="warn", metadata={"reason": "bad_otp_or_expired"},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="OTP sai hoac het han")

    # If OTP wasn't tied to a user, look up by phone
    user: User | None = None
    if uid:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None:
        user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if user is None or user.disabled:
        await audit_push(
            db, actor=phone[:6] + "xxx", workspace_id=None,
            action="auth.phone.login.fail", target=phone[:6] + "xxx",
            severity="warn", metadata={"reason": "user_missing"},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="Tai khoan khong ton tai cho so nay")

    # 2FA gate — if user has TOTP/MFA required, return challenge instead of JWT
    if user.mfa_required and user.mfa_enabled and user.mfa_secret_enc:
        challenge = secrets.token_urlsafe(32)
        exp = datetime.now(timezone.utc) + timedelta(minutes=5)
        await db.execute(_sql("""
            INSERT INTO login_challenges (challenge_token, user_id, method, expires_at)
            VALUES (:t, :u, 'totp', :e)
        """), {"t": challenge, "u": str(user.id), "e": exp})
        await audit_push(
            db, actor=user.email, workspace_id=None,
            action="auth.phone.login.mfa_required",
            target=user.email, severity="info",
            metadata={"otp_id": otp_id},
        )
        await db.commit()
        return {
            "mfa_required": True,
            "challenge_token": challenge,
            "methods": ["totp"],
            "expires_in": 300,
            "message": "Nhap ma TOTP de hoan tat dang nhap",
        }

    # Issue JWT pair
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = datetime.now(timezone.utc)

    await audit_push(
        db, actor=user.email, workspace_id=None,
        action="auth.phone.login", target=user.email, severity="ok",
        metadata={"otp_id": otp_id},
    )
    await db.commit()

    return {
        "access_token": access, "refresh_token": refresh,
        "token_type": "Bearer", "expires_in": settings.jwt_access_ttl,
    }


# ─────────────────────────────────────────────────────────────
# 4. POST /auth/phone/signup  (phone + OTP + password + workspace)
# ─────────────────────────────────────────────────────────────
@router.post("/signup")
async def phone_signup(
    data: PhoneSignupIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Signup: verify phone OTP, then create user + workspace. Issues JWT pair on success.
    Phone is verified on creation. Email is set but unverified (use email_verify flow).
    """
    try:
        phone = _normalize_phone_safe(data.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Verify OTP first
    ok, otp_id, _ = await _consume_otp(db, phone, data.code, "signup")
    if not ok:
        await audit_push(
            db, actor=phone[:6] + "xxx", workspace_id=None,
            action="auth.phone.signup.fail", target=phone[:6] + "xxx",
            severity="warn", metadata={"reason": "bad_otp_or_expired"},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="OTP sai hoac het han")

    email = str(data.email).lower().strip()
    # Uniqueness checks
    if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email da duoc dang ky")
    if (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="So dien thoai da duoc dang ky")
    if (await db.execute(select(Workspace).where(Workspace.id == data.workspace_id))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Workspace ID '{data.workspace_id}' da ton tai")

    # Create workspace
    ws = Workspace(
        id=data.workspace_id,
        code=data.workspace_id[:8].upper(),
        name=data.company[:128],
        tagline="Phone signup",
        color="var(--crown)",
    )
    db.add(ws)
    await db.flush()

    now = datetime.now(timezone.utc)
    user = User(
        email=email,
        password_hash=hash_password(data.password),
        name=data.name,
        role="Owner",
        phone=phone,
        phone_verified_at=now,
    )
    db.add(user)
    await db.flush()
    db.add(UserWorkspace(user_id=user.id, workspace_id=ws.id, role="Owner"))

    # Free credit (matches /auth/signup pattern)
    await db.execute(_sql(
        "INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up) "
        "VALUES(:w, 50000, 50000) ON CONFLICT DO NOTHING"
    ), {"w": ws.id})

    # Issue JWT pair
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = now

    await audit_push(
        db, actor=email, workspace_id=ws.id,
        action="auth.phone.signup", target=email, severity="ok",
        metadata={"phone_prefix": phone[:6] + "xxx", "company": data.company,
                  "otp_id": otp_id},
    )
    await db.commit()

    return {
        "ok": True,
        "user_id": str(user.id),
        "email": user.email,
        "phone": phone,
        "workspace_id": ws.id,
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.jwt_access_ttl,
        "free_credit_vnd": 50000,
        "message": "Tai khoan tao thanh cong qua so dien thoai.",
    }
