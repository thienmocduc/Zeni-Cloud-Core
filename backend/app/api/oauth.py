"""
OAuth2 framework — Zeni Cloud as Consumer (Google, GitHub, Zeni Digital SSO).

Flow:
  1. Frontend → GET /auth/oauth/{provider}/authorize → redirects to provider OAuth URL
  2. Provider → GET /auth/oauth/{provider}/callback?code=... → backend exchanges code for token
  3. Backend → fetch user info → create/link user → issue Zeni JWT → redirect to /app

Providers supported:
  - google      : Google OAuth 2.0 (popular)
  - github      : GitHub OAuth (developers)
  - zenidigital : Zeni Holdings SSO (custom, internal apps)

Pre-requisites (chairman setup):
  - Google: console.cloud.google.com → APIs & Services → Credentials → OAuth Client ID
    Authorized redirect: https://zenicloud.io/api/v1/auth/oauth/google/callback
    → Save GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET to Secret Manager

  - GitHub: github.com/settings/developers → OAuth Apps → New
    Authorization callback URL: https://zenicloud.io/api/v1/auth/oauth/github/callback
    → Save GITHUB_OAUTH_CLIENT_ID + GITHUB_OAUTH_CLIENT_SECRET to Secret Manager

  - Zeni Digital: chairman tự define (Zeni master account hệ sinh thái)
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, make_access_token, make_refresh_token
from app.db.base import get_db
from app.db.models import RefreshToken, User, UserWorkspace, Workspace
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.oauth")
router = APIRouter(prefix="/auth/oauth", tags=["auth"])


# ─── Provider configurations ─────────────────────────────────
PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url":     "https://oauth2.googleapis.com/token",
        "userinfo_url":  "https://www.googleapis.com/oauth2/v3/userinfo",
        "scopes":        ["openid", "email", "profile"],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url":     "https://github.com/login/oauth/access_token",
        "userinfo_url":  "https://api.github.com/user",
        "userinfo_emails_url": "https://api.github.com/user/emails",
        "scopes":        ["user:email", "read:user"],
        "client_id_env": "GITHUB_OAUTH_CLIENT_ID",
        "client_secret_env": "GITHUB_OAUTH_CLIENT_SECRET",
    },
    "zenidigital": {
        # Zeni Holdings master SSO — sẽ host trên zenidigital.com
        # Phase 1: stub (chairman cấu hình sau)
        "authorize_url": "https://zenidigital.com/oauth/authorize",
        "token_url":     "https://zenidigital.com/oauth/token",
        "userinfo_url":  "https://zenidigital.com/oauth/userinfo",
        "scopes":        ["openid", "email", "profile", "zeni:workspaces"],
        "client_id_env": "ZENIDIGITAL_OAUTH_CLIENT_ID",
        "client_secret_env": "ZENIDIGITAL_OAUTH_CLIENT_SECRET",
    },
}


def _get_provider_config(provider: str) -> dict[str, Any]:
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"OAuth provider không hỗ trợ: {provider}")
    client_id = os.environ.get(cfg["client_id_env"])
    client_secret = os.environ.get(cfg["client_secret_env"])
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=f"OAuth provider '{provider}' chưa cấu hình. "
                   f"Chairman cần set {cfg['client_id_env']} + {cfg['client_secret_env']} vào Secret Manager."
        )
    return {**cfg, "client_id": client_id, "client_secret": client_secret}


def _redirect_uri(provider: str) -> str:
    base = settings.app_base_url.rstrip("/") if hasattr(settings, "app_base_url") else "https://zenicloud.io"
    return f"{base}/api/v1/auth/oauth/{provider}/callback"


# ─── Authorize: redirect user to provider login ────────────
@router.get("/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    return_to: str = "/app",
):
    """Redirect user → provider OAuth login screen."""
    cfg = _get_provider_config(provider)

    # CSRF state token (10 min TTL)
    state = _secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    await db.execute(text(
        "INSERT INTO oauth_states(state, provider, redirect, expires_at) VALUES(:s,:p,:r,:e)"
    ), {"s": state, "p": provider, "r": return_to[:255], "e": expires})
    await db.commit()

    params = {
        "client_id":     cfg["client_id"],
        "redirect_uri":  _redirect_uri(provider),
        "response_type": "code",
        "scope":         " ".join(cfg["scopes"]),
        "state":         state,
        "access_type":   "offline",  # Google: get refresh token
        "prompt":        "consent",
    }
    return RedirectResponse(url=f"{cfg['authorize_url']}?{urlencode(params)}", status_code=302)


# ─── Callback: exchange code → fetch user → issue Zeni JWT ──
@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return _redirect_with_error(f"OAuth từ chối: {error}")
    if not code or not state:
        return _redirect_with_error("Thiếu code hoặc state")

    cfg = _get_provider_config(provider)

    # Verify state
    state_row = (await db.execute(text(
        "SELECT redirect FROM oauth_states WHERE state=:s AND provider=:p AND expires_at > now()"
    ), {"s": state, "p": provider})).first()
    if state_row is None:
        return _redirect_with_error("State không hợp lệ hoặc hết hạn")
    return_to = state_row[0] or "/app"
    # Consume state
    await db.execute(text("DELETE FROM oauth_states WHERE state=:s"), {"s": state})

    # Exchange code → access token
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_payload = {
                "client_id":     cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code":          code,
                "redirect_uri":  _redirect_uri(provider),
                "grant_type":    "authorization_code",
            }
            r = await client.post(cfg["token_url"], data=token_payload,
                                  headers={"Accept": "application/json"})
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Token exchange thất bại: {r.text[:200]}")
            tok = r.json()
            access_token = tok.get("access_token")
            if not access_token:
                raise HTTPException(status_code=502, detail="No access_token trong response")

            # Fetch user info
            ui = await client.get(cfg["userinfo_url"],
                                   headers={"Authorization": f"Bearer {access_token}",
                                            "Accept": "application/json"})
            if ui.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Userinfo fetch fail: {ui.text[:200]}")
            user_info = ui.json()

            # GitHub: email might be private → fetch /user/emails
            if provider == "github" and not user_info.get("email"):
                em = await client.get(cfg["userinfo_emails_url"],
                                       headers={"Authorization": f"Bearer {access_token}",
                                                "Accept": "application/json"})
                emails = em.json() if em.status_code < 400 else []
                primary = next((e for e in emails if e.get("primary")), None)
                if primary:
                    user_info["email"] = primary["email"]
    except HTTPException:
        raise
    except Exception as e:
        log.exception("OAuth callback error")
        return _redirect_with_error(f"OAuth lỗi: {type(e).__name__}")

    # Normalize user info
    email = (user_info.get("email") or "").lower().strip()
    if not email:
        return _redirect_with_error("Không lấy được email từ provider")
    name = user_info.get("name") or user_info.get("login") or email.split("@")[0]
    provider_user_id = str(user_info.get("sub") or user_info.get("id") or email)
    avatar = user_info.get("picture") or user_info.get("avatar_url")

    # Find or create user
    user = (await db.execute(select(User).where(
        (User.email == email) | ((User.oauth_provider == provider) & (User.oauth_id == provider_user_id))
    ))).scalar_one_or_none()

    is_new = False
    if user is None:
        # Create new user + auto-create workspace
        is_new = True
        ws_id = email.replace("@", "_").replace(".", "_").replace("-", "_")[:32].lower()
        # Avoid conflict
        if (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one_or_none():
            ws_id = f"{ws_id[:24]}_{_secrets.token_hex(3)}"

        # Generate 3-char code from email username initial letters
        # e.g. "doanhnhancaotuan" -> "DOA", "caotuanphat581" -> "CAO"
        _username = email.split("@")[0]
        _code = "".join(c for c in _username if c.isalpha())[:3].upper() or "WS"
        if not _code or len(_code) < 2:
            _code = ws_id[:3].upper()
        ws = Workspace(id=ws_id, code=_code, name=f"{name}'s Workspace",
                        tagline=f"Auto from {provider} OAuth", color="var(--crown)")
        db.add(ws)
        await db.flush()

        user = User(
            email=email, password_hash=None, name=name[:128],
            role="Owner", avatar=avatar[:255] if avatar else None,
            oauth_provider=provider, oauth_id=provider_user_id,
        )
        db.add(user)
        await db.flush()
        db.add(UserWorkspace(user_id=user.id, workspace_id=ws_id, role="Owner"))

        # Free 50K credit
        await db.execute(text(
            "INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up) "
            "VALUES(:w, 50000, 50000) ON CONFLICT DO NOTHING"
        ), {"w": ws_id})
    else:
        # Link OAuth if not already
        if not user.oauth_provider:
            user.oauth_provider = provider
            user.oauth_id = provider_user_id
        if not user.avatar and avatar:
            user.avatar = avatar[:255]
        if user.disabled:
            return _redirect_with_error("Tài khoản đã bị disabled")

    # Issue Zeni JWT
    access = make_access_token(str(user.id), extra={"role": user.role, "email": user.email})
    refresh, jti, exp = make_refresh_token(str(user.id))
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    user.last_login = datetime.now(timezone.utc)

    await audit_push(
        db, actor=email, workspace_id=None,
        action=f"auth.oauth.{provider}.{'signup' if is_new else 'login'}",
        target=email, severity="ok",
        metadata={"provider": provider, "provider_user_id": provider_user_id, "is_new": is_new},
    )
    await db.commit()

    # Redirect to app with tokens in URL fragment (#) so JS picks up but server logs don't capture
    base = settings.app_base_url.rstrip("/") if hasattr(settings, "app_base_url") else "https://zenicloud.io"
    fragment = urlencode({"access_token": access, "refresh_token": refresh,
                           "expires_in": settings.jwt_access_ttl,
                           "is_new": "1" if is_new else "0"})
    return RedirectResponse(url=f"{base}{return_to}#oauth={fragment}", status_code=302)


def _redirect_with_error(msg: str) -> RedirectResponse:
    base = settings.app_base_url.rstrip("/") if hasattr(settings, "app_base_url") else "https://zenicloud.io"
    return RedirectResponse(url=f"{base}/signup?oauth_error={msg[:120]}", status_code=302)


# ─── Helper: list available providers (frontend gọi để biết hiển thị buttons nào) ──
@router.get("/providers")
async def list_providers() -> dict:
    """Return providers configured + ready (có client_id env)."""
    out = []
    for name, cfg in PROVIDERS.items():
        configured = bool(os.environ.get(cfg["client_id_env"]))
        out.append({
            "name": name,
            "display_name": {"google": "Google", "github": "GitHub", "zenidigital": "Zeni Digital"}.get(name, name),
            "ready": configured,
            "authorize_path": f"/api/v1/auth/oauth/{name}/authorize",
            "scopes": cfg["scopes"],
        })
    return {"providers": out}
