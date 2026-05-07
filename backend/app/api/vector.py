"""
Zeni Cloud Core — L2 Vector Search API (pgvector backed).

Endpoints (workspace-scoped):
  POST   /vector/collections?ws=        — create collection (name, dim, metric)
  GET    /vector/collections?ws=        — list collections
  POST   /vector/{name}/upsert?ws=      — bulk upsert points
  POST   /vector/{name}/search?ws=      — top-k similarity search
  DELETE /vector/{name}?ws=             — drop collection

Pricing tham khảo (Sprint A2):
  $0.10 / 1K vectors stored / month
  $0.05 / 1K search ops
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import vector_search
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.vector")
router = APIRouter(prefix="/vector", tags=["vector", "data"])


# ─── Schemas ────────────────────────────────────────────────
class CreateCollectionIn(BaseModel):
    name:   str = Field(min_length=1, max_length=31, pattern=r"^[a-z][a-z0-9_]{0,30}$")
    dim:    int = Field(ge=1, le=4096)
    metric: str = Field(default="cosine", pattern=r"^(cosine|l2|ip)$")


class PointIn(BaseModel):
    id:       str          = Field(min_length=1, max_length=256)
    vector:   list[float]  = Field(min_length=1, max_length=4096)
    metadata: dict | None  = Field(default=None)


class UpsertIn(BaseModel):
    points: list[PointIn] = Field(min_length=1, max_length=1000)


class SearchIn(BaseModel):
    vector: list[float] = Field(min_length=1, max_length=4096)
    k:      int         = Field(default=10, ge=1, le=100)
    filter: dict | None = Field(default=None)


# ─── Helpers ────────────────────────────────────────────────
def _check_scope(me: CurrentUser) -> None:
    """Nếu auth qua PAT, phải có scope 'vector', 'data', hoặc 'full'."""
    if me.auth_scope and not any(
        s in me.auth_scope for s in ("vector", "data", "full")
    ):
        raise HTTPException(
            status_code=403, detail="Token thiếu scope 'vector' hoặc 'data'"
        )


def _map_business_error(e: vector_search.VectorError) -> HTTPException:
    msg = str(e)
    # Heuristic: message "không tồn tại" → 404, còn lại → 400
    if "không tồn tại" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=400, detail=msg)


# ─── Endpoints ──────────────────────────────────────────────
@router.post("/collections", status_code=201)
async def create_collection(
    ws: str,
    data: CreateCollectionIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Tạo collection mới (idempotent: trùng tên → 409 'đã tồn tại')."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    try:
        result = await vector_search.create_collection(
            db, workspace_id=ws, name=data.name, dim=data.dim, metric=data.metric,
        )
    except vector_search.VectorError as e:
        # Conflict riêng → 409
        if "đã tồn tại" in str(e).lower():
            raise HTTPException(status_code=409, detail=str(e))
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_collection upstream error ws=%s name=%s", ws, data.name)
        raise HTTPException(status_code=502, detail=f"Vector store lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="vector.collection.create",
        target=data.name[:80], severity="ok",
        metadata={"dim": data.dim, "metric": data.metric},
    )
    await db.commit()
    return result


@router.get("/collections")
async def list_collections(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Liệt kê collections trong workspace."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    try:
        cols = await vector_search.list_collections(db, workspace_id=ws)
    except vector_search.VectorError as e:
        raise _map_business_error(e)
    except Exception as e:
        log.exception("list_collections upstream error ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"Vector store lỗi: {e}")
    return {"workspace_id": ws, "collections": cols, "count": len(cols)}


@router.post("/{name}/upsert")
async def upsert_points(
    ws: str,
    name: str,
    data: UpsertIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bulk upsert vector points vào collection."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    points_payload = [
        {
            "id":       p.id,
            "vector":   p.vector,
            "metadata": p.metadata or {},
        }
        for p in data.points
    ]

    try:
        result = await vector_search.upsert_points(
            db, workspace_id=ws, collection=name, points=points_payload,
        )
    except vector_search.VectorError as e:
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("upsert_points upstream error ws=%s name=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"Vector upsert lỗi: {e}")

    n = result.get("upserted", 0)
    cost = 0.0001 * n  # ~ $0.10 / 1K vectors
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="vector.upsert",
        target=name[:80], severity="ok",
        metadata={"count": n, "row_count": result.get("row_count")},
    )
    await billing_push(
        db, workspace_id=ws, layer="L2", action="vector.upsert", cost_usd=cost,
    )
    await db.commit()
    result["cost_usd"] = cost
    return result


@router.post("/{name}/search")
async def search_collection(
    ws: str,
    name: str,
    data: SearchIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Top-k similarity search trong collection."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        matches = await vector_search.search(
            db, workspace_id=ws, collection=name,
            vector=data.vector, k=data.k, filter=data.filter,
        )
    except vector_search.VectorError as e:
        raise _map_business_error(e)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("search upstream error ws=%s name=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"Vector search lỗi: {e}")

    cost = 0.00005  # ~ $0.05 / 1K search ops
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="vector.search",
        target=name[:80], severity="ok",
        metadata={"k": data.k, "matched": len(matches),
                  "filter_keys": list(data.filter.keys()) if data.filter else []},
    )
    await billing_push(
        db, workspace_id=ws, layer="L2", action="vector.search", cost_usd=cost,
    )
    await db.commit()
    return {"matches": matches, "count": len(matches), "cost_usd": cost}


@router.delete("/{name}")
async def delete_collection(
    ws: str,
    name: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Drop collection (xoá data + registry row)."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        await vector_search.delete_collection(db, workspace_id=ws, name=name)
    except vector_search.VectorError as e:
        raise _map_business_error(e)
    except Exception as e:
        log.exception("delete_collection upstream error ws=%s name=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"Vector delete lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="vector.collection.delete",
        target=name[:80], severity="warn", metadata={},
    )
    await db.commit()
    return {"ok": True, "deleted": name}
