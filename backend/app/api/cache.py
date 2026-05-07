"""
Zeni Cloud Core — L4 Cache API (Postgres KV với TTL).

Endpoints (CHỈ có 4):
  GET    /cache?ws=&prefix=     → list (KHÔNG trả value)   [đặt TRƯỚC để khỏi bị {key:path} ăn]
  PUT    /cache/{key}?ws=       → set (body: {value, ttl_seconds?})
  GET    /cache/{key}?ws=       → get (404 nếu missing/expired)
  DELETE /cache/{key}?ws=       → delete

Quy tắc:
  - Mọi endpoint require_user + require_workspace_access(ws)
  - PAT scope check: cần "data" hoặc "full"
  - KHÔNG audit cache ops (giảm noise, đúng spec A2)
  - Try/except Exception → 502 cho upstream error
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import cache as cache_svc

log = logging.getLogger("zeni.api.cache")
router = APIRouter(prefix="/cache", tags=["cache"])


# ─── Schemas ─────────────────────────────────────────
class CacheSetIn(BaseModel):
    value: Any = Field(..., description="JSON-serializable value")
    ttl_seconds: int | None = Field(default=None, ge=1, le=86400 * 30,
                                     description="TTL: 1..2,592,000s (30 ngày). None = không hết hạn")


# ─── Helpers ─────────────────────────────────────────
def _check_scope(me: CurrentUser) -> None:
    """PAT phải có scope 'data' hoặc 'full'. JWT user thì pass."""
    if me.auth_scope is None:
        return  # JWT user
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "data" not in scopes:
        raise HTTPException(status_code=403, detail="PAT cần scope 'data' hoặc 'full' để dùng /cache")


# ─── List (đặt TRƯỚC route {key:path}) ───────────────
@router.get("")
async def list_cache(
    ws: str,
    prefix: str = "",
    limit: int = 100,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List keys (KHÔNG trả value để tránh leak). Filter theo prefix."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        items = await cache_svc.cache_list(db, ws, prefix=prefix or "", limit=limit)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("cache_list failed for ws=%s prefix=%r", ws, (prefix or "")[:80])
        raise HTTPException(status_code=502, detail=f"không list được cache: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "prefix": prefix or "",
        "count": len(items),
        "keys": items,
    }


# ─── Per-key endpoints ───────────────────────────────
@router.put("/{key:path}")
async def set_cache(
    key: str,
    ws: str,
    data: CacheSetIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set/replace cache entry."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được ghi cache")

    try:
        await cache_svc.cache_set(db, ws, key, data.value, data.ttl_seconds)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("cache_set failed for ws=%s key=%s", ws, key[:80])
        raise HTTPException(status_code=502, detail=f"không ghi được cache: {type(e).__name__}")

    return {"ok": True, "key": key, "ttl_seconds": data.ttl_seconds}


@router.get("/{key:path}")
async def get_cache(
    key: str,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lấy giá trị. 404 nếu missing/expired."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        value = await cache_svc.cache_get(db, ws, key)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("cache_get failed for ws=%s key=%s", ws, key[:80])
        raise HTTPException(status_code=502, detail=f"không đọc được cache: {type(e).__name__}")

    if value is None:
        raise HTTPException(status_code=404, detail="cache key không tồn tại hoặc đã hết hạn")

    return {"key": key, "value": value}


@router.delete("/{key:path}")
async def delete_cache(
    key: str,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Xoá entry. Trả deleted=True nếu thực sự xoá được, False nếu không tồn tại."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được xoá cache")

    try:
        deleted = await cache_svc.cache_delete(db, ws, key)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("cache_delete failed for ws=%s key=%s", ws, key[:80])
        raise HTTPException(status_code=502, detail=f"không xoá được cache: {type(e).__name__}")

    return {"ok": True, "key": key, "deleted": deleted}
