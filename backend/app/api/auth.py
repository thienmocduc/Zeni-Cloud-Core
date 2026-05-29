from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.api.auth")

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.security import (
    decode_token,
    hash_password,
    make_access_token,
    make_refresh_token,
    verify_password,
)
from app.db.base import get_db
from app.db.models import RefreshToken, User, UserWorkspace, Workspace
from app.schemas.auth import LoginIn, MeOut, RefreshIn, RegisterIn, TokenPair, UserOut
from app.services.audit import audit_push

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    data: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Login flow:
      - Password OK + MFA off → return JWT pair immediately
      - Password OK + MFA on  → return {mfa_required: true, pre_token}
        Client must POST /auth/mfa/login with pre_token + code to get JWT.

    Security:
      - Rate limit 10 fail/IP/15min
      - Brute force lockout per email: 5 fail in 10min → temporary block
    """
    # IP-based rate limit
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else "unknown")
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:32]
    fifteen_min_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
    fail_count = (await db.execute(text("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'auth.login.fail' AND ts >= :since
              AND metadata->>'ip_hash' = :iph
    """), {"since": fifteen_min_ago, "iph": ip_hash})).scalar() or 0
    # Tighter rate limit (security hardening): 5 fail/15min/IP (was 10)
    if fail_count >= 5:
        raise HTTPException(status_code=429,
            detail="Quá nhiều lần đăng nhập sai từ IP này. Thử lại sau 15 phút hoặc dùng MFA / OAuth.")

    user = (await db.execute(select(User).where(User.email == str(data.email).lower()))).scalar_one_or_none()
    auth_ok = user is not None and not user.disabled and user.password_hash \
              and verify_password(data.password, user.password_hash)
    if not auth_ok:
        # Log failed attempt for rate limit + audit
        await audit_push(db, actor=str(data.email).lower(), workspace_id=None,
                          action="auth.login.fail", target=str(data.email).lower()[:80],
                          severity="warn", metadata={"ip_hash": ip_hash})
        await db.commit()
        raise HTTPException(status_code=401, detail="Email hoặc mật khẩu không đúng")

    # If MFA enabled → issue short-lived pre-token; client must verify TOTP next
    if user.mfa_enabled and user.mfa_secret_enc:
        from app.api.mfa import issue_mfa_pre_token
        pre_token = await issue_mfa_pre_token(db, user.id)
        await audit_push(db, actor=user.email, workspace_id=None, action="auth.login.mfa_required",
                         target=user.email, severity="info")
        await db.commit()
        return {"mfa_required": True, "pre_token": pre_token, "expires_in": 300,
                "message": "Nhập mã TOTP từ Google Authenticator / Authy"}

    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = datetime.now(timezone.utc)
    await audit_push(db, actor=user.email, workspace_id=None, action="auth.login", target=user.email, severity="ok")
    await db.commit()

    # Fetch user's workspaces (Owner sees ALL, others see linked workspaces only)
    if user.role == "Owner":
        ws_rows = (await db.execute(text("SELECT id, name FROM workspaces ORDER BY id"))).all()
    else:
        ws_rows = (await db.execute(
            text("""SELECT w.id, w.name FROM workspaces w
                    JOIN user_workspaces uw ON uw.workspace_id = w.id
                    WHERE uw.user_id = :uid ORDER BY w.id"""),
            {"uid": user.id}
        )).all()
    workspaces = [{"id": r[0], "name": r[1]} for r in ws_rows]
    workspace_ids = [r[0] for r in ws_rows]

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.jwt_access_ttl,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.name or user.email.split("@")[0],
            "role": user.role,
            "workspaces": workspace_ids,        # Array of workspace ID strings (for frontend state)
            "workspace_details": workspaces,    # Full objects with id+name
        }
    }


@router.post("/refresh", response_model=TokenPair)
async def refresh_session(data: RefreshIn, db: AsyncSession = Depends(get_db)) -> TokenPair:
    try:
        payload = decode_token(data.refresh_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    if payload.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="wrong token type")

    jti = payload.get("jti")
    sub = payload.get("sub")
    if not jti or not sub:
        raise HTTPException(status_code=401, detail="invalid refresh payload")

    rt = (await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))).scalar_one_or_none()
    if rt is None or rt.revoked:
        raise HTTPException(status_code=401, detail="refresh token revoked or unknown")

    if rt.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="refresh token expired")

    try:
        uid = UUID(sub)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad subject")

    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None or user.disabled:
        raise HTTPException(status_code=401, detail="user disabled")

    # Rotate: revoke old, issue new pair
    rt.revoked = True
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    new_refresh, new_jti, new_exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=new_jti, user_id=user.id, expires_at=new_exp))
    await db.commit()

    return TokenPair(access_token=access, refresh_token=new_refresh, expires_in=settings.jwt_access_ttl)


@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    data: RefreshIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        payload = decode_token(data.refresh_token)
        jti = payload.get("jti")
        if jti:
            rt = (await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))).scalar_one_or_none()
            if rt is not None:
                rt.revoked = True
    except ValueError:
        pass
    await audit_push(db, actor=me.email, workspace_id=None, action="auth.logout", target=me.email, severity="info")
    await db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=MeOut)
async def me(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MeOut:
    # Owner → all workspaces
    if current.role == "Owner":
        ws = (await db.execute(select(Workspace.id))).scalars().all()
        workspaces = list(ws)
    else:
        workspaces = current.workspaces

    user = current.user
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        avatar=user.avatar,
        mfa_enabled=user.mfa_enabled,
        last_login=user.last_login,
        workspaces=workspaces,
    )


import re as _re_pwd
_PWD_RULES = (
    (lambda s: len(s) >= 8, "min 8 chars"),
    (lambda s: bool(_re_pwd.search(r"[a-z]", s)), "1 chữ thường"),
    (lambda s: bool(_re_pwd.search(r"[A-Z0-9]", s)), "1 chữ HOA hoặc số"),
)
_BLOCKED_EMAIL_DOMAINS = {
    "tempmail.com", "guerrillamail.com", "10minutemail.com", "mailinator.com",
    "throwaway.email", "trashmail.com", "yopmail.com", "fakeinbox.com",
}
_RESERVED_WS_IDS = {
    "admin", "api", "auth", "login", "signup", "system", "internal",
    "_admin", "root", "zeni", "zenicloud", "test", "demo", "public", "private",
    "billing", "wallet", "settings", "owner", "support",
}


class SignupIn(BaseModel):
    """Public signup endpoint for landing page form (full self-service)."""
    full_name: str = Field(min_length=2, max_length=128, pattern=r"^[\w\sÀ-ɏḀ-ỿ.,'-]{2,128}$")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    company_name: str = Field(min_length=2, max_length=64)
    workspace_id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")

    def validate_password_strength(self) -> str | None:
        """Returns error message if weak, None if OK."""
        for check, label in _PWD_RULES:
            if not check(self.password):
                return f"Mật khẩu yếu — cần {label}"
        # Block top-100 most common passwords
        common = {"password", "12345678", "qwerty123", "admin123", "letmein123",
                  "welcome123", "iloveyou", "password1", "abc12345", "qwertyuiop"}
        if self.password.lower() in common:
            return "Mật khẩu quá phổ biến — chọn mật khẩu khác"
        return None

    def validate_workspace_id(self) -> str | None:
        if self.workspace_id in _RESERVED_WS_IDS:
            return f"Workspace ID '{self.workspace_id}' là từ bảo lưu — chọn tên khác"
        return None

    def validate_email_domain(self) -> str | None:
        domain = str(self.email).split("@")[-1].lower()
        if domain in _BLOCKED_EMAIL_DOMAINS:
            return f"Email domain '{domain}' không được chấp nhận (disposable email)"
        return None


@router.post("/signup")
async def public_signup(
    data: SignupIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Self-service signup từ landing /signup form.
    Security: rate limit 3/IP/hour, strong password, blocked disposable emails,
    reserved workspace IDs blocked.
    """
    # Strong password
    err = data.validate_password_strength()
    if err:
        raise HTTPException(status_code=400, detail=err)
    err = data.validate_workspace_id()
    if err:
        raise HTTPException(status_code=400, detail=err)
    err = data.validate_email_domain()
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Rate limit signup per IP (3/hour) — prevent abuse
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
                (request.client.host if request.client else "unknown")
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:32]
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    rate_count = (await db.execute(text("""
        SELECT COUNT(*) FROM audit_log
        WHERE action = 'auth.signup' AND ts >= :since
              AND metadata->>'ip_hash' = :iph
    """), {"since": one_hour_ago, "iph": ip_hash})).scalar() or 0
    if rate_count >= 3:
        raise HTTPException(status_code=429, detail="Quá nhiều lần signup từ IP này. Thử lại sau 1 giờ.")

    email = str(data.email).lower().strip()
    # Check existing
    existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing_user:
        raise HTTPException(status_code=409, detail="Email đã đăng ký. Vui lòng đăng nhập.")
    existing_ws = (await db.execute(select(Workspace).where(Workspace.id == data.workspace_id))).scalar_one_or_none()
    if existing_ws:
        raise HTTPException(status_code=409, detail=f"Workspace ID '{data.workspace_id}' đã được dùng. Chọn tên khác.")

    # Create workspace — generate UNIQUE code (avoid collisions with existing workspaces)
    # workspaces.code has UNIQUE constraint, so first-8-uppercase can collide.
    # Try base code first, fall back to random suffix.
    import secrets as _secrets
    base_code = data.workspace_id[:8].upper()
    final_code = base_code
    for _ in range(5):
        existing_code = (await db.execute(
            select(Workspace).where(Workspace.code == final_code)
        )).scalar_one_or_none()
        if existing_code is None:
            break
        # Collision → 4 prefix chars + 4 random hex = 8 chars (fits varchar(8))
        final_code = (base_code[:4] + _secrets.token_hex(2).upper())[:8]

    ws = Workspace(
        id=data.workspace_id,
        code=final_code,
        name=data.company_name[:128],
        tagline="Self-service signup",
        color="var(--crown)",
    )
    db.add(ws)
    await db.flush()

    # Create user as Admin (workspace-scoped, NOT super-admin "Owner")
    # "Owner" role is reserved for system super-admin (admin email seeded by main.py).
    # Self-signup users get "Admin" — full power within their own workspace only.
    user = User(
        email=email,
        password_hash=hash_password(data.password),
        name=data.full_name,
        role="Admin",
    )
    db.add(user)
    await db.flush()
    db.add(UserWorkspace(user_id=user.id, workspace_id=ws.id, role="Owner"))

    # Auto-create wallet with 50K free credit
    from sqlalchemy import text as sql_text
    await db.execute(sql_text(
        "INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up) "
        "VALUES(:w, 50000, 50000) ON CONFLICT DO NOTHING"
    ), {"w": ws.id})

    # Issue tokens immediately
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = datetime.now(timezone.utc)

    await audit_push(db, actor=email, workspace_id=ws.id, action="auth.signup",
                     target=email, severity="ok",
                     metadata={"company": data.company_name, "self_service": True,
                               "ip_hash": ip_hash})
    await db.commit()

    log.info("[signup] new user %s + workspace %s created", email, ws.id)

    return {
        "ok": True,
        "user_id": str(user.id),
        "email": user.email,
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.jwt_access_ttl,
        "free_credit_vnd": 50000,
        "message": "Tài khoản tạo thành công. Đăng nhập ngay tại /app",
    }


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)) -> UserOut:
    existing = (await db.execute(select(User).where(User.email == str(data.email).lower()))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Email đã đăng ký")

    user = User(
        email=str(data.email).lower(),
        password_hash=hash_password(data.password),
        name=data.name,
        role="Developer",
    )
    db.add(user)
    await db.flush()

    # Auto-create personal workspace (Vercel pattern)
    import re as _re
    ws_id_slug = _re.sub(r'[^a-z0-9_-]+', '-', user.email.split('@')[0].lower()).strip('-')[:60]
    if not ws_id_slug or ws_id_slug in _RESERVED_WS_IDS:
        ws_id_slug = f"user-{str(user.id)[:8]}"
    ws_code_slug = _re.sub(r'[^A-Z0-9]', '', user.email.split('@')[0].upper())[:8] or "USER"

    # Check if slug exists, append suffix if needed
    exists = (await db.execute(text("SELECT id FROM workspaces WHERE id = :id"), {"id": ws_id_slug})).first()
    if exists:
        ws_id_slug = f"{ws_id_slug}-{str(user.id)[:6]}"

    final_ws_id = ws_id_slug
    try:
        await db.execute(text(
            "INSERT INTO workspaces (id, code, name) VALUES (:id, :code, :name) ON CONFLICT (id) DO NOTHING"
        ), {"id": ws_id_slug, "code": ws_code_slug, "name": user.name or user.email.split('@')[0]})
        await db.execute(text(
            "INSERT INTO user_workspaces (user_id, workspace_id, role) VALUES (:uid, :ws, 'Owner') ON CONFLICT DO NOTHING"
        ), {"uid": str(user.id), "ws": ws_id_slug})
        log.info("Auto-created personal workspace %s for user %s", ws_id_slug, user.email)
    except Exception as e:
        log.warning("Auto-create workspace failed for %s: %s", user.email, e)
        final_ws_id = None

    await audit_push(db, actor=user.email, workspace_id=None, action="auth.register", target=user.email, severity="ok")
    await db.commit()
    return UserOut(
        id=user.id, email=user.email, name=user.name, role=user.role,
        avatar=None, mfa_enabled=False, last_login=None,
        workspaces=[final_ws_id] if final_ws_id else [],
    )
