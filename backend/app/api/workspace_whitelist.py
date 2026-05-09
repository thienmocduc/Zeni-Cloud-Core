"""
Workspace Image Whitelist API — per-workspace opt-in Docker registry.

Pattern Vercel/Netlify: workspace owner self-service add registry prefix
(vd: ghcr.io/myorg/) để được phép deploy image từ registry đó. Tránh
phải add global whitelist cho mọi khách (security + scale).
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.api.ws_whitelist")
router = APIRouter(prefix="/workspaces", tags=["workspace-whitelist"])


class WhitelistItem(BaseModel):
    id: str
    prefix: str
    description: Optional[str] = None
    pull_secret_id: Optional[str] = None
    enabled: bool = True
    added_at: str

    model_config = {"from_attributes": True}


class WhitelistAdd(BaseModel):
    prefix: str = Field(..., min_length=4, max_length=255,
                         description="Registry prefix, vd: 'ghcr.io/myorg/'. KẾT THÚC bằng '/' để tránh prefix collision.")
    description: Optional[str] = Field(None, max_length=255)
    pull_secret_id: Optional[str] = Field(None, max_length=120,
                                           description="Optional: Identity Vault secret_id chứa Docker pull credentials cho private registry.")

    @field_validator("prefix")
    @classmethod
    def _v_prefix(cls, v: str) -> str:
        v = v.strip().lower()
        # Phải có dấu '/' hoặc kết thúc bằng '/' để tránh prefix collision
        if "/" not in v:
            raise ValueError("prefix phải chứa '/' (vd: 'docker.io/myname/' hoặc 'us-central1-docker.pkg.dev/myproject/myrepo/')")
        if not v.endswith("/"):
            v += "/"
        # Disallow wildcard
        if v.startswith("*") or v.startswith("/"):
            raise ValueError("prefix không được bắt đầu bằng '*' hoặc '/'")
        # Reject registries Cloud Run KHÔNG pull được — tránh khách deploy fail
        # Cloud Run chỉ accept: *.gcr.io, *.docker.pkg.dev, docker.io
        unsupported_registries = {
            "ghcr.io/": "GitHub Container Registry (Cloud Run không pull trực tiếp được). Dùng docker.io/yourname/ hoặc us-central1-docker.pkg.dev/zeni-cloud-core/... thay thế.",
            "quay.io/": "Quay.io không được Cloud Run support. Dùng Docker Hub hoặc Zeni Artifact Registry.",
            "registry.gitlab.com/": "GitLab Registry không được Cloud Run support. Dùng Docker Hub hoặc Zeni Artifact Registry.",
            "registry.fly.io/": "Fly Registry không được Cloud Run support.",
        }
        for bad, hint in unsupported_registries.items():
            if v.startswith(bad):
                raise ValueError(f"Registry '{bad}' không support: {hint}")
        # Reject domain-only prefixes (quá rộng, security risk)
        bad_domain_only = ("docker.io/",)
        if v in bad_domain_only:
            raise ValueError(
                f"prefix '{v}' quá rộng (cả registry). Cần specific path: 'docker.io/yourname/' để allow images của bạn."
            )
        return v


@router.get("/{workspace_id}/image-whitelist", response_model=list[WhitelistItem])
async def list_whitelist(
    workspace_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WhitelistItem]:
    """List image registry whitelist của workspace."""
    await require_workspace_access(workspace_id, me)
    rows = (await db.execute(text(
        "SELECT id::text AS id, prefix, description, pull_secret_id, enabled, "
        "added_at::text AS added_at "
        "FROM workspace_image_whitelist WHERE workspace_id = :ws "
        "ORDER BY added_at DESC"
    ), {"ws": workspace_id})).mappings().all()
    return [WhitelistItem(**dict(r)) for r in rows]


@router.post("/{workspace_id}/image-whitelist", response_model=WhitelistItem, status_code=201)
async def add_whitelist(
    workspace_id: str,
    body: WhitelistAdd,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WhitelistItem:
    """Owner/Admin add registry prefix vào whitelist của workspace."""
    await require_workspace_access(workspace_id, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Chỉ Owner/Admin có quyền add image registry whitelist")

    # Check duplicate
    existing = (await db.execute(text(
        "SELECT id FROM workspace_image_whitelist WHERE workspace_id = :ws AND prefix = :p"
    ), {"ws": workspace_id, "p": body.prefix})).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Prefix '{body.prefix}' đã có trong whitelist")

    row = (await db.execute(text(
        "INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, pull_secret_id, added_by) "
        "VALUES (:ws, :p, :desc, :sid, :by) "
        "RETURNING id::text AS id, prefix, description, pull_secret_id, enabled, added_at::text AS added_at"
    ), {
        "ws": workspace_id, "p": body.prefix, "desc": body.description,
        "sid": body.pull_secret_id, "by": str(me.id),
    })).mappings().first()
    await db.commit()

    log.info("[whitelist] ws=%s added prefix=%s by=%s", workspace_id, body.prefix, me.email)
    return WhitelistItem(**dict(row))


@router.delete("/{workspace_id}/image-whitelist/{item_id}", status_code=204)
async def remove_whitelist(
    workspace_id: str,
    item_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner/Admin remove registry prefix khỏi whitelist."""
    await require_workspace_access(workspace_id, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Chỉ Owner/Admin có quyền remove whitelist")

    result = await db.execute(text(
        "DELETE FROM workspace_image_whitelist WHERE id = :id AND workspace_id = :ws"
    ), {"id": item_id, "ws": workspace_id})
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Whitelist item không tồn tại")
