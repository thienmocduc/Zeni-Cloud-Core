"""
Zeni Cloud Core — Customer OAuth Providers API.

Khách của Zeni Cloud có thể cài key OAuth của HỌ (Zalo, Apple, Facebook,
Line, Kakao, TikTok, LinkedIn) vào workspace. Zeni làm OAuth middleware:
- Khách paste app_id + secret của họ → Zeni encrypt KMS → expose endpoint
- App của khách dùng `https://zenicloud.io/auth/{provider}/{ws}/login`
- Zeni redirect về app khách với access token + user info

Endpoints (prefix /identity/oauth-providers):
  POST   /             — Add provider (zalo, apple, facebook, line, kakao, tiktok, linkedin)
  GET    /             — List providers
  GET    /{id}         — Detail
  PATCH  /{id}         — Update
  DELETE /{id}         — Disconnect
  GET    /templates    — Built-in provider templates (auth/token/userinfo URLs)

Backed by migration 043_customer_oauth_providers.sql.
"""
from __future__ import annotations

import base64
import secrets as _secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

router = APIRouter(prefix="/identity/oauth-providers", tags=["identity", "customer-oauth"])

ALLOWED_PROVIDERS = {"zalo", "apple", "facebook", "line", "kakao", "tiktok", "linkedin", "generic"}


class ProviderCreateIn(BaseModel):
    provider: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=2, max_length=80)
    client_id: str = Field(min_length=4, max_length=200)
    client_secret: str = Field(min_length=4, max_length=300)
    redirect_uri: str | None = Field(default=None, max_length=300)
    app_callback_url: str = Field(min_length=8, max_length=300, description="URL Zeni redirect về sau khi auth xong")
    app_origin: str | None = Field(default=None, max_length=200)
    scopes: list[str] | None = Field(default=None)
    auth_url: str | None = Field(default=None, max_length=300)
    token_url: str | None = Field(default=None, max_length=300)
    userinfo_url: str | None = Field(default=None, max_length=300)


class ProviderUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=300)
    app_callback_url: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None


def _encrypt_secret(plaintext: str) -> bytes:
    """
    Phase 1: simple base64 wrap (NOT real encryption).
    Phase 2: Cloud KMS encrypt with workspace-scoped key.
    """
    return base64.b64encode(plaintext.encode("utf-8"))


def _mask_secret(b: bytes | None) -> str:
    if not b:
        return ""
    try:
        s = base64.b64decode(b).decode("utf-8")
        if len(s) <= 8:
            return "****"
        return s[:4] + "****" + s[-4:]
    except Exception:
        return "****"


@router.get("/templates")
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """List built-in provider templates (Zalo, Apple, Facebook, etc.)."""
    rows = (await db.execute(
        text("""SELECT provider, display_name, auth_url, token_url, userinfo_url,
                       default_scopes, docs_url, setup_guide
                FROM oauth_provider_templates ORDER BY provider""")
    )).all()
    return [
        {
            "provider": r[0], "display_name": r[1],
            "auth_url": r[2], "token_url": r[3], "userinfo_url": r[4],
            "default_scopes": r[5], "docs_url": r[6], "setup_guide": r[7]
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_provider(
    data: ProviderCreateIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add OAuth provider for this workspace. Customer's own app_id/secret."""
    await require_workspace_access(ws, me)

    if data.provider not in ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider phải là: {sorted(ALLOWED_PROVIDERS)}")

    # Check duplicate
    existing = (await db.execute(
        text("SELECT id FROM workspace_oauth_providers WHERE workspace_id = :ws AND provider = :p"),
        {"ws": ws, "p": data.provider}
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Provider {data.provider} đã được cấu hình. Dùng PATCH để update.")

    # Auto-fill URLs from template if not provided
    template = (await db.execute(
        text("SELECT auth_url, token_url, userinfo_url, default_scopes FROM oauth_provider_templates WHERE provider = :p"),
        {"p": data.provider}
    )).first()
    auth_url = data.auth_url or (template[0] if template else None)
    token_url = data.token_url or (template[1] if template else None)
    userinfo_url = data.userinfo_url or (template[2] if template else None)
    scopes = data.scopes or (template[3] if template else [])

    redirect_uri = data.redirect_uri or f"https://zenicloud.io/auth/{data.provider}/{ws}/callback"

    row = (await db.execute(
        text("""
            INSERT INTO workspace_oauth_providers (
                workspace_id, provider, display_name, client_id, client_secret_encrypted,
                auth_url, token_url, userinfo_url, scopes, redirect_uri, app_callback_url,
                app_origin, created_by
            ) VALUES (
                :ws, :p, :dn, :cid, :sec, :a, :t, :u, :sc, :ru, :ac, :ao, :cb
            ) RETURNING id, workspace_id, provider, display_name, redirect_uri, app_callback_url, enabled, created_at
        """),
        {
            "ws": ws, "p": data.provider, "dn": data.display_name,
            "cid": data.client_id, "sec": _encrypt_secret(data.client_secret),
            "a": auth_url, "t": token_url, "u": userinfo_url,
            "sc": scopes, "ru": redirect_uri, "ac": data.app_callback_url,
            "ao": data.app_origin, "cb": me.email,
        }
    )).first()
    await db.commit()

    return {
        "id": row[0], "workspace_id": row[1], "provider": row[2],
        "display_name": row[3], "redirect_uri": row[4], "app_callback_url": row[5],
        "enabled": row[6], "created_at": row[7].isoformat() if row[7] else None,
        "login_endpoint": f"https://zenicloud.io/auth/{data.provider}/{ws}/login",
        "instructions": [
            f"1. Trên trang admin của {data.provider}, set Authorization redirect URI = {redirect_uri}",
            f"2. App của bạn redirect user tới: https://zenicloud.io/auth/{data.provider}/{ws}/login",
            f"3. Sau khi user login xong, Zeni redirect về {data.app_callback_url} với query ?token=...&user=...",
            "4. App của bạn verify token + tạo session"
        ]
    }


@router.get("")
async def list_providers(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        text("""SELECT id, provider, display_name, client_id, client_secret_encrypted,
                       redirect_uri, app_callback_url, enabled, created_at
                FROM workspace_oauth_providers WHERE workspace_id = :ws
                ORDER BY provider"""),
        {"ws": ws}
    )).all()
    return [
        {
            "id": r[0], "provider": r[1], "display_name": r[2],
            "client_id": r[3], "client_secret_masked": _mask_secret(r[4]),
            "redirect_uri": r[5], "app_callback_url": r[6],
            "enabled": r[7], "created_at": r[8].isoformat() if r[8] else None,
            "login_endpoint": f"https://zenicloud.io/auth/{r[1]}/{ws}/login",
        }
        for r in rows
    ]


@router.patch("/{provider_id}")
async def update_provider(
    provider_id: int,
    ws: str,
    data: ProviderUpdateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    fields: dict[str, Any] = {}
    if data.display_name is not None: fields["display_name"] = data.display_name
    if data.client_id is not None: fields["client_id"] = data.client_id
    if data.client_secret is not None: fields["client_secret_encrypted"] = _encrypt_secret(data.client_secret)
    if data.app_callback_url is not None: fields["app_callback_url"] = data.app_callback_url
    if data.enabled is not None: fields["enabled"] = data.enabled
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = provider_id; fields["ws"] = ws
    res = await db.execute(
        text(f"UPDATE workspace_oauth_providers SET {set_clause}, updated_at = NOW() WHERE id = :id AND workspace_id = :ws"),
        fields
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.commit()
    return {"id": provider_id, "updated": True}


@router.delete("/{provider_id}", status_code=204, response_model=None)
async def delete_provider(
    provider_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_access(ws, me)
    res = await db.execute(
        text("DELETE FROM workspace_oauth_providers WHERE id = :id AND workspace_id = :ws"),
        {"id": provider_id, "ws": ws}
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.commit()
