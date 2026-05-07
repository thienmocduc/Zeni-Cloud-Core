"""
Zeni Cloud Core — Customer OAuth Flow handlers.

Endpoints (NO prefix — top-level /auth/{provider}/{ws}/...):

  GET  /auth/{provider}/{ws}/login     — Bắt đầu OAuth flow (redirect tới provider)
  GET  /auth/{provider}/{ws}/callback  — Provider redirect về đây sau auth, exchange code

Flow:
  1. App của khách redirect user → /auth/zalo/myapp/login
  2. Zeni lookup workspace_oauth_providers → build authorize URL → redirect tới Zalo
  3. Zalo user login + grant → redirect về /auth/zalo/myapp/callback?code=...&state=...
  4. Zeni verify state, exchange code → access_token + user info
  5. Zeni redirect về app_callback_url với ?token=...&email=...&name=...

Phase 1: support generic OAuth 2.0 code flow (Zalo, Facebook, LinkedIn, Line, Kakao).
Phase 2 (later): Apple Sign In (form_post mode), TikTok (special encoding).
"""
from __future__ import annotations

import base64
import secrets as _secrets
from typing import Any
from urllib.parse import urlencode, quote_plus

import httpx
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db

# NOTE: This router has NO /api/v1 prefix — it's mounted at /auth directly.
router = APIRouter(prefix="/auth", tags=["customer-oauth-flow"])


def _decrypt_secret(b: bytes | None) -> str:
    if not b:
        return ""
    try:
        return base64.b64decode(b).decode("utf-8")
    except Exception:
        return ""


@router.get("/{provider}/{ws}/login")
async def oauth_login(
    provider: str,
    ws: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start OAuth flow — redirect to provider's authorize URL."""
    row = (await db.execute(
        text("""SELECT id, client_id, auth_url, scopes, redirect_uri, app_callback_url, enabled
                FROM workspace_oauth_providers
                WHERE workspace_id = :ws AND provider = :p"""),
        {"ws": ws, "p": provider}
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider} chưa cấu hình cho workspace {ws}")
    if not row[6]:
        raise HTTPException(status_code=403, detail="Provider đã bị disable")

    provider_id, client_id, auth_url, scopes, redirect_uri, app_callback_url = row[0], row[1], row[2], row[3], row[4], row[5]
    if not auth_url:
        raise HTTPException(status_code=500, detail="Provider config thiếu auth_url")

    # Generate state token (CSRF protection)
    state_token = _secrets.token_urlsafe(32)

    # Persist state for verification on callback
    await db.execute(
        text("""INSERT INTO oauth_login_attempts (workspace_id, provider, state_token, ip_address, user_agent, status)
                VALUES (:ws, :p, :st, :ip, :ua, 'pending')"""),
        {"ws": ws, "p": provider, "st": state_token,
         "ip": request.client.host if request.client else None,
         "ua": request.headers.get("user-agent", "")[:500]}
    )
    await db.commit()

    # Build authorize URL
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes) if scopes else "",
        "state": state_token,
    }
    # Provider-specific tweaks
    if provider == "facebook":
        # Facebook uses comma-separated scopes
        params["scope"] = ",".join(scopes) if scopes else "email,public_profile"
    elif provider == "kakao":
        # Kakao uses space-separated, default scope OK
        pass

    sep = "&" if "?" in auth_url else "?"
    return RedirectResponse(url=f"{auth_url}{sep}{urlencode(params, quote_via=quote_plus)}", status_code=302)


@router.get("/{provider}/{ws}/callback")
async def oauth_callback(
    provider: str,
    ws: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle callback from OAuth provider."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Verify state token
    attempt = (await db.execute(
        text("""SELECT id FROM oauth_login_attempts
                WHERE state_token = :st AND workspace_id = :ws AND provider = :p
                  AND status = 'pending' AND created_at > NOW() - INTERVAL '10 minutes'"""),
        {"st": state, "ws": ws, "p": provider}
    )).first()
    if attempt is None:
        raise HTTPException(status_code=400, detail="State token invalid or expired")

    # Load provider config
    row = (await db.execute(
        text("""SELECT client_id, client_secret_encrypted, token_url, userinfo_url, redirect_uri, app_callback_url
                FROM workspace_oauth_providers WHERE workspace_id = :ws AND provider = :p"""),
        {"ws": ws, "p": provider}
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Provider config not found")

    client_id, secret_enc, token_url, userinfo_url, redirect_uri, app_callback_url = row[0], row[1], row[2], row[3], row[4], row[5]
    client_secret = _decrypt_secret(secret_enc)

    # Exchange code for token
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"}
            )
        except Exception as e:
            await db.execute(
                text("UPDATE oauth_login_attempts SET status='failed', error_message=:e, completed_at=NOW() WHERE state_token=:st"),
                {"e": f"token_exchange: {type(e).__name__}", "st": state}
            )
            await db.commit()
            raise HTTPException(status_code=502, detail=f"Token exchange failed: {type(e).__name__}")

        if token_resp.status_code != 200:
            await db.execute(
                text("UPDATE oauth_login_attempts SET status='failed', error_message=:e, completed_at=NOW() WHERE state_token=:st"),
                {"e": f"token_status_{token_resp.status_code}", "st": state}
            )
            await db.commit()
            raise HTTPException(status_code=502, detail=f"Token exchange returned {token_resp.status_code}")

        try:
            token_data = token_resp.json()
        except Exception:
            token_data = {}
        access_token = token_data.get("access_token", "")

        # Fetch userinfo (if URL provided)
        external_user_id = ""
        external_email = ""
        external_name = ""
        if userinfo_url and access_token:
            try:
                ui_resp = await client.get(
                    userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
                )
                if ui_resp.status_code == 200:
                    ui_data = ui_resp.json()
                    external_user_id = str(ui_data.get("id") or ui_data.get("sub") or "")
                    external_email = ui_data.get("email", "")
                    external_name = ui_data.get("name") or ui_data.get("nickname") or ""
            except Exception:
                pass

    # Update attempt as success
    await db.execute(
        text("""UPDATE oauth_login_attempts
                SET status='success', external_user_id=:uid, external_email=:em, completed_at=NOW()
                WHERE state_token=:st"""),
        {"uid": external_user_id, "em": external_email, "st": state}
    )
    await db.commit()

    # Redirect back to customer's app with user info as query params
    callback_params = {
        "provider": provider,
        "user_id": external_user_id,
        "email": external_email,
        "name": external_name,
        "access_token": access_token,
        "ws": ws,
    }
    sep = "&" if "?" in app_callback_url else "?"
    return RedirectResponse(url=f"{app_callback_url}{sep}{urlencode(callback_params)}", status_code=302)
