"""
Workspace AI Providers API — BYO LLM key per tenant.

Pattern: ClawWits / WitsAGI tier Enterprise/Power → khách dùng API key của họ
thay vì shared Zeni key. Cho phép:
1. Cost isolation — khách trả tiền trực tiếp cho Anthropic/DeepSeek
2. Privacy — Zeni KHÔNG thấy LLM logs/usage của khách
3. Quota — khách kiểm soát budget

Endpoints (Owner/Admin only):
  POST   /workspaces/{ws}/ai-providers           — set workspace key
  GET    /workspaces/{ws}/ai-providers           — list configured (masked)
  DELETE /workspaces/{ws}/ai-providers/{prov}    — remove

Storage: Google Secret Manager `ws-{ws}-{provider}-key`.
Backend llm_gateway check workspace key FIRST → fallback global key.

v151 — chairman CRITICAL item #1.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.ai_providers")
router = APIRouter(prefix="/workspaces", tags=["workspace-ai-providers"])

VALID_PROVIDERS = {"anthropic", "deepseek", "gemini", "openai"}

# Regex pattern minimum validation (full format check at provider call time)
KEY_FORMATS = {
    "anthropic": re.compile(r"^sk-ant-api\d{2,}-[A-Za-z0-9_\-]{40,}$"),
    "deepseek":  re.compile(r"^sk-[a-f0-9]{32,}$"),
    "gemini":    re.compile(r"^AIzaSy[A-Za-z0-9_\-]{33,}$"),
    "openai":    re.compile(r"^sk-(proj-)?[A-Za-z0-9_\-]{40,}$"),
}


class AiProviderSetIn(BaseModel):
    provider: str = Field(..., description="anthropic|deepseek|gemini|openai")
    api_key: str = Field(..., min_length=20, max_length=300)
    note: Optional[str] = Field(None, max_length=200)


class AiProviderOut(BaseModel):
    provider: str
    set_at: str
    masked_key: str                    # vd: "sk-ant-api03-xxx...***"
    secret_name: str
    note: Optional[str] = None


def _secret_name(ws: str, provider: str) -> str:
    """Standard secret naming convention: ws-{workspace}-{provider}-key."""
    safe_ws = re.sub(r"[^a-z0-9-]", "-", ws.lower())[:32]
    return f"ws-{safe_ws}-{provider}-key"


def _mask_key(key: str) -> str:
    """Show first 8 + last 4 chars, rest as ***."""
    if len(key) <= 16:
        return key[:4] + "***"
    return key[:8] + "***" + key[-4:]


@router.post("/{workspace_id}/ai-providers", response_model=AiProviderOut, status_code=201)
async def set_workspace_ai_provider(
    workspace_id: str,
    body: AiProviderSetIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AiProviderOut:
    """Set workspace-specific AI provider API key.

    Khách (Owner/Admin) tự BYO key — Zeni store encrypted trong Secret Manager.
    Backend khi gọi LLM cho workspace này → dùng key này trước, fallback global.
    """
    await require_workspace_access(workspace_id, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để set AI provider key")

    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider phải là 1 trong {sorted(VALID_PROVIDERS)}")

    # Validate format
    pattern = KEY_FORMATS.get(body.provider)
    if pattern and not pattern.match(body.api_key):
        raise HTTPException(status_code=400,
            detail=f"API key format không khớp với {body.provider}. Kiểm tra lại — không phải lúc paste có thêm dấu cách hoặc dòng mới?")

    secret_name = _secret_name(workspace_id, body.provider)
    try:
        from google.cloud import secretmanager
        from google.api_core import exceptions as gcp_exc
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/zeni-cloud-core"
        # Check if secret exists, else create
        try:
            client.get_secret(name=f"{parent}/secrets/{secret_name}")
            secret_exists = True
        except gcp_exc.NotFound:
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_name,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            secret_exists = False
        # Add new version
        client.add_secret_version(
            request={
                "parent": f"{parent}/secrets/{secret_name}",
                "payload": {"data": body.api_key.encode("utf-8")},
            }
        )
    except Exception as e:
        log.exception("Secret Manager error: %s", e)
        raise HTTPException(status_code=500, detail=f"Cannot write to Secret Manager: {str(e)[:200]}")

    # Track metadata in DB
    await db.execute(text("""
        INSERT INTO workspace_ai_providers (workspace_id, provider, secret_name, set_by, note)
        VALUES (:ws, :prov, :sn, :by, :note)
        ON CONFLICT (workspace_id, provider) DO UPDATE SET
            secret_name = EXCLUDED.secret_name,
            set_by = EXCLUDED.set_by,
            note = EXCLUDED.note,
            updated_at = NOW()
    """), {
        "ws": workspace_id, "prov": body.provider, "sn": secret_name,
        "by": str(me.id), "note": body.note,
    })
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="ai_provider.set",
        target=body.provider, severity="info",
        metadata={"secret_name": secret_name, "masked": _mask_key(body.api_key)},
    )
    await db.commit()

    return AiProviderOut(
        provider=body.provider,
        set_at="now",
        masked_key=_mask_key(body.api_key),
        secret_name=secret_name,
        note=body.note,
    )


@router.get("/{workspace_id}/ai-providers", response_model=list[AiProviderOut])
async def list_workspace_ai_providers(
    workspace_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AiProviderOut]:
    """List configured providers (KHÔNG return value — masked only)."""
    await require_workspace_access(workspace_id, me)
    rows = (await db.execute(text("""
        SELECT provider, secret_name, set_by, updated_at::text, note
        FROM workspace_ai_providers WHERE workspace_id = :ws ORDER BY provider
    """), {"ws": workspace_id})).all()
    return [
        AiProviderOut(
            provider=r[0],
            set_at=(r[3] or "")[:19],
            masked_key="***-***-***-set",
            secret_name=r[1],
            note=r[4],
        )
        for r in rows
    ]


@router.delete("/{workspace_id}/ai-providers/{provider}", status_code=204)
async def remove_workspace_ai_provider(
    workspace_id: str,
    provider: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove workspace AI provider key (backend → fallback global key)."""
    await require_workspace_access(workspace_id, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để remove")

    secret_name = _secret_name(workspace_id, provider)
    try:
        from google.cloud import secretmanager
        from google.api_core import exceptions as gcp_exc
        client = secretmanager.SecretManagerServiceClient()
        try:
            client.delete_secret(name=f"projects/zeni-cloud-core/secrets/{secret_name}")
        except gcp_exc.NotFound:
            pass
    except Exception as e:
        log.warning("Secret delete failed (soft): %s", e)
    await db.execute(text(
        "DELETE FROM workspace_ai_providers WHERE workspace_id = :ws AND provider = :prov"
    ), {"ws": workspace_id, "prov": provider})
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id, action="ai_provider.remove",
        target=provider, severity="warn",
    )
    await db.commit()


async def get_workspace_provider_key(workspace_id: str, provider: str, db: AsyncSession) -> Optional[str]:
    """
    Helper: lookup workspace-specific key (latest version) từ Secret Manager.
    Fallback to None if not configured — caller can use global key.

    Used by llm_gateway.py to check workspace key BEFORE global.
    """
    secret_name = _secret_name(workspace_id, provider)
    try:
        from google.cloud import secretmanager
        from google.api_core import exceptions as gcp_exc
        client = secretmanager.SecretManagerServiceClient()
        try:
            response = client.access_secret_version(
                name=f"projects/zeni-cloud-core/secrets/{secret_name}/versions/latest"
            )
            return response.payload.data.decode("utf-8")
        except gcp_exc.NotFound:
            return None
    except Exception as e:
        log.warning("Cannot fetch workspace key %s: %s", secret_name, e)
        return None
