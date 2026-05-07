"""
Zeni Workspace — Notion-like docs + tasks API (replaces Notion).

Router prefix `/workspace`.

Endpoints (all require `get_current_user` + `require_workspace_access(ws)`):

  Pages
    POST   /workspace/pages?ws=
    GET    /workspace/pages?ws=&parent_id=
    GET    /workspace/pages/{id}?ws=
    PATCH  /workspace/pages/{id}?ws=
    DELETE /workspace/pages/{id}?ws=                  (soft archive)
    POST   /workspace/pages/{id}/duplicate?ws=
    POST   /workspace/pages/{id}/move?ws=

  Blocks
    POST   /workspace/pages/{id}/blocks?ws=
    PATCH  /workspace/pages/{id}/blocks/{bid}?ws=
    DELETE /workspace/pages/{id}/blocks/{bid}?ws=
    POST   /workspace/pages/{id}/blocks/bulk?ws=

  Tasks
    POST   /workspace/tasks?ws=
    GET    /workspace/tasks?ws=&status=&assignee=&due_before=
    PATCH  /workspace/tasks/{id}?ws=
    DELETE /workspace/tasks/{id}?ws=
    POST   /workspace/tasks/{id}/complete?ws=
    GET    /workspace/tasks/calendar?ws=&month=YYYY-MM

  Databases
    POST   /workspace/databases?ws=
    GET    /workspace/databases?ws=
    POST   /workspace/databases/{id}/rows?ws=
    GET    /workspace/databases/{id}/rows?ws=&filter=&sort=
    PATCH  /workspace/databases/{id}/rows/{rid}?ws=

  Collaboration
    POST   /workspace/pages/{id}/collaborators?ws=
    GET    /workspace/pages/{id}/collaborators?ws=
    DELETE /workspace/pages/{id}/collaborators/{email}?ws=
    POST   /workspace/pages/{id}/comments?ws=
    GET    /workspace/pages/{id}/comments?ws=
    POST   /workspace/comments/{id}/resolve?ws=

  Version history
    GET    /workspace/pages/{id}/history?ws=
    POST   /workspace/pages/{id}/restore?ws=&version_id=

  Search
    GET    /workspace/search?ws=&q=
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.workspace_engine import (
    BLOCK_TYPES,
    DEFAULT_BLOCK_TYPE,
    export_markdown,
    import_markdown,
    render_page_html,
    restore_snapshot,
    search as engine_search,
    take_snapshot,
)

log = logging.getLogger("zeni.api.workspace")
router = APIRouter(prefix="/workspace", tags=["zeni-workspace"])


# ─── Constants ───────────────────────────────────────────────
ALLOWED_TASK_STATUS = {
    "backlog", "todo", "inprogress", "review", "done", "cancelled",
}
ALLOWED_TASK_PRIORITY = {"low", "medium", "high", "urgent"}
ALLOWED_PERMISSIONS = {"view", "comment", "edit", "admin"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BULK_BLOCKS = 1000
MAX_PAGE_TITLE = 500
MAX_TASK_TITLE = 500
MAX_TAGS = 32
DEFAULT_TASK_LIMIT = 200
MAX_TASK_LIMIT = 1000


# ─── Helpers ────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_scope(me: CurrentUser) -> None:
    """PAT must have scope 'workspace' or 'full'. JWT users pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "workspace" not in scopes:
        raise HTTPException(
            status_code=403,
            detail="PAT cần scope 'workspace' hoặc 'full' để dùng /workspace",
        )


def _require_writer(me: CurrentUser) -> None:
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không có quyền ghi")


def _validate_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not e or not EMAIL_RE.match(e) or len(e) > 255:
        raise HTTPException(status_code=400, detail=f"email không hợp lệ: {email!r}")
    return e


def _safe_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


async def _ensure_page_in_ws(
    db: AsyncSession, page_id: int, ws: str
) -> tuple[int, str]:
    """Return (page_id, workspace_id). 404 if page not in workspace."""
    r = (await db.execute(text("""
        SELECT id, workspace_id FROM ws_pages
         WHERE id = :id AND workspace_id = :ws
    """), {"id": page_id, "ws": ws})).first()
    if not r:
        raise HTTPException(status_code=404, detail="page không tồn tại")
    return int(r[0]), r[1]


async def _ensure_task_in_ws(db: AsyncSession, task_id: int, ws: str) -> int:
    r = (await db.execute(text("""
        SELECT id FROM ws_tasks WHERE id = :id AND workspace_id = :ws
    """), {"id": task_id, "ws": ws})).first()
    if not r:
        raise HTTPException(status_code=404, detail="task không tồn tại")
    return int(r[0])


async def _ensure_db_in_ws(db: AsyncSession, db_id: int, ws: str) -> int:
    r = (await db.execute(text("""
        SELECT id FROM ws_databases WHERE id = :id AND workspace_id = :ws
    """), {"id": db_id, "ws": ws})).first()
    if not r:
        raise HTTPException(status_code=404, detail="database không tồn tại")
    return int(r[0])


def _row_to_page(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "parent_id": int(r[2]) if r[2] is not None else None,
        "title": r[3],
        "icon": r[4],
        "cover_url": r[5],
        "slug": r[6],
        "is_archived": bool(r[7]),
        "position": float(r[8]) if r[8] is not None else 0.0,
        "created_by": r[9],
        "updated_by": r[10],
        "created_at": r[11].isoformat() if r[11] else None,
        "updated_at": r[12].isoformat() if r[12] else None,
    }


PAGE_FIELDS = (
    "id, workspace_id, parent_id, title, icon, cover_url, slug, is_archived, "
    "position, created_by, updated_by, created_at, updated_at"
)


def _row_to_block(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "page_id": int(r[1]),
        "parent_block_id": int(r[2]) if r[2] is not None else None,
        "type": r[3],
        "content": r[4],
        "properties": _safe_jsonb(r[5]) or {},
        "position": float(r[6]) if r[6] is not None else 0.0,
        "created_at": r[7].isoformat() if r[7] else None,
        "updated_at": r[8].isoformat() if r[8] else None,
    }


BLOCK_FIELDS = (
    "id, page_id, parent_block_id, type, content, properties, position, "
    "created_at, updated_at"
)


def _row_to_task(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "page_id": int(r[2]) if r[2] is not None else None,
        "parent_task_id": int(r[3]) if r[3] is not None else None,
        "title": r[4],
        "description": r[5],
        "status": r[6],
        "priority": r[7],
        "assignee_email": r[8],
        "due_date": r[9].isoformat() if r[9] else None,
        "completed_at": r[10].isoformat() if r[10] else None,
        "position": float(r[11]) if r[11] is not None else 0.0,
        "tags": list(r[12]) if r[12] is not None else [],
        "created_by": r[13],
        "created_at": r[14].isoformat() if r[14] else None,
        "updated_at": r[15].isoformat() if r[15] else None,
    }


TASK_FIELDS = (
    "id, workspace_id, page_id, parent_task_id, title, description, status, "
    "priority, assignee_email, due_date, completed_at, position, tags, "
    "created_by, created_at, updated_at"
)


# ═════════════════════════════════════════════════════════════
# 1. PAGES
# ═════════════════════════════════════════════════════════════
class PageIn(BaseModel):
    title: str = Field(default="Untitled", max_length=MAX_PAGE_TITLE)
    parent_id: int | None = None
    icon: str | None = Field(default=None, max_length=40)
    cover_url: str | None = Field(default=None, max_length=2000)
    slug: str | None = Field(default=None, max_length=120)
    position: float | None = None


class PagePatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_PAGE_TITLE)
    icon: str | None = Field(default=None, max_length=40)
    cover_url: str | None = Field(default=None, max_length=2000)
    slug: str | None = Field(default=None, max_length=120)
    parent_id: int | None = None
    position: float | None = None
    is_archived: bool | None = None


class PageMoveIn(BaseModel):
    new_parent_id: int | None = None
    new_position: float | None = None


@router.post("/pages", status_code=201)
async def create_page(
    data: PageIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    # Validate parent_id belongs to same workspace
    if data.parent_id is not None:
        await _ensure_page_in_ws(db, data.parent_id, ws)

    # Default position = max(siblings) + 1
    pos = data.position
    if pos is None:
        sib = (await db.execute(text("""
            SELECT COALESCE(MAX(position), 0) + 1 FROM ws_pages
             WHERE workspace_id = :ws
               AND parent_id IS NOT DISTINCT FROM :pid
        """), {"ws": ws, "pid": data.parent_id})).first()
        pos = float(sib[0]) if sib and sib[0] is not None else 1.0

    try:
        row = (await db.execute(text(f"""
            INSERT INTO ws_pages
              (workspace_id, parent_id, title, icon, cover_url, slug,
               position, created_by, updated_by)
            VALUES (:ws, :pid, :title, :icon, :cover, :slug, :pos, :cb, :cb)
            RETURNING {PAGE_FIELDS}
        """), {
            "ws": ws,
            "pid": data.parent_id,
            "title": data.title,
            "icon": data.icon,
            "cover": data.cover_url,
            "slug": data.slug,
            "pos": float(pos),
            "cb": me.email,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="slug đã tồn tại")
        log.exception("create_page failed ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"không tạo được page: {type(e).__name__}")

    out = _row_to_page(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.page.create",
                         target=f"page#{out['id']}",
                         severity="ok",
                         metadata={"title": data.title})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/pages")
async def list_pages(
    ws: str = Query(...),
    parent_id: int | None = Query(default=None),
    include_archived: bool = Query(default=False),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    sql = f"""
        SELECT {PAGE_FIELDS}
          FROM ws_pages
         WHERE workspace_id = :ws
           AND parent_id IS NOT DISTINCT FROM :pid
    """
    params: dict[str, Any] = {"ws": ws, "pid": parent_id}
    if not include_archived:
        sql += " AND is_archived = FALSE"
    sql += " ORDER BY position ASC, id ASC"

    rows = (await db.execute(text(sql), params)).all()
    return {
        "workspace_id": ws,
        "parent_id": parent_id,
        "count": len(rows),
        "pages": [_row_to_page(r) for r in rows],
    }


@router.get("/pages/{page_id}")
async def get_page(
    page_id: int,
    ws: str = Query(...),
    include_blocks: bool = Query(default=True),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    page = (await db.execute(text(f"""
        SELECT {PAGE_FIELDS} FROM ws_pages
         WHERE id = :id AND workspace_id = :ws
    """), {"id": page_id, "ws": ws})).first()
    if not page:
        raise HTTPException(status_code=404, detail="page không tồn tại")

    out = _row_to_page(page)
    if include_blocks:
        blk = (await db.execute(text(f"""
            SELECT {BLOCK_FIELDS} FROM ws_blocks
             WHERE page_id = :pid
             ORDER BY position ASC, id ASC
        """), {"pid": page_id})).all()
        out["blocks"] = [_row_to_block(b) for b in blk]
    # Children count for tree UI
    cnt = (await db.execute(text("""
        SELECT COUNT(*) FROM ws_pages
         WHERE parent_id = :pid AND is_archived = FALSE
    """), {"pid": page_id})).first()
    out["children_count"] = int(cnt[0]) if cnt else 0
    return out


@router.patch("/pages/{page_id}")
async def patch_page(
    page_id: int,
    data: PagePatchIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    if data.parent_id is not None:
        if data.parent_id == page_id:
            raise HTTPException(status_code=400, detail="parent không thể là chính nó")
        await _ensure_page_in_ws(db, data.parent_id, ws)

    sets: list[str] = []
    params: dict[str, Any] = {"id": page_id, "ws": ws, "eb": me.email}
    for f in ("title", "icon", "cover_url", "slug",
              "parent_id", "position", "is_archived"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if not sets:
        raise HTTPException(status_code=400, detail="không có field nào cần update")
    sets.append("updated_by = :eb")

    # Snapshot existing state before destructive change (best-effort).
    try:
        await take_snapshot(db, page_id=page_id, edited_by=me.email,
                            note="auto pre-edit")
    except Exception:
        log.exception("pre-edit snapshot failed page=%s", page_id)

    sql = f"""
        UPDATE ws_pages SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {PAGE_FIELDS}
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="slug đã tồn tại")
        log.exception("patch_page failed page=%s", page_id)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")

    if not row:
        raise HTTPException(status_code=404, detail="page không tồn tại")
    return _row_to_page(row)


@router.delete("/pages/{page_id}")
async def archive_page(
    page_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    res = await db.execute(text("""
        UPDATE ws_pages SET is_archived = TRUE, updated_by = :eb
         WHERE id = :id AND workspace_id = :ws
    """), {"id": page_id, "ws": ws, "eb": me.email})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="page không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.page.archive",
                         target=f"page#{page_id}", severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"archived": True, "id": page_id}


@router.post("/pages/{page_id}/duplicate", status_code=201)
async def duplicate_page(
    page_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    src = (await db.execute(text(f"""
        SELECT {PAGE_FIELDS} FROM ws_pages WHERE id = :id
    """), {"id": page_id})).first()
    if not src:
        raise HTTPException(status_code=404, detail="page không tồn tại")
    src_d = _row_to_page(src)

    try:
        new_row = (await db.execute(text(f"""
            INSERT INTO ws_pages
              (workspace_id, parent_id, title, icon, cover_url, position,
               created_by, updated_by)
            VALUES (:ws, :pid, :title, :icon, :cover, :pos, :cb, :cb)
            RETURNING {PAGE_FIELDS}
        """), {
            "ws": ws,
            "pid": src_d["parent_id"],
            "title": (src_d["title"] or "Untitled") + " (copy)",
            "icon": src_d["icon"],
            "cover": src_d["cover_url"],
            "pos": float(src_d["position"]) + 0.5,
            "cb": me.email,
        })).first()

        new_id = int(new_row[0])
        # Copy blocks
        await db.execute(text("""
            INSERT INTO ws_blocks
              (page_id, parent_block_id, type, content, properties, position)
            SELECT :nid, NULL, type, content, properties, position
              FROM ws_blocks WHERE page_id = :pid
        """), {"nid": new_id, "pid": page_id})
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("duplicate_page failed page=%s", page_id)
        raise HTTPException(status_code=502, detail=f"duplicate failed: {type(e).__name__}")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.page.duplicate",
                         target=f"page#{page_id}",
                         severity="ok", metadata={"new_id": new_id})
        await db.commit()
    except Exception:
        await db.rollback()
    return _row_to_page(new_row)


@router.post("/pages/{page_id}/move")
async def move_page(
    page_id: int,
    data: PageMoveIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    if data.new_parent_id is not None:
        if data.new_parent_id == page_id:
            raise HTTPException(status_code=400, detail="parent không thể là chính nó")
        await _ensure_page_in_ws(db, data.new_parent_id, ws)

    sets, params = [], {"id": page_id, "ws": ws, "eb": me.email}
    if data.new_parent_id is not None or "new_parent_id" in data.model_fields_set:
        sets.append("parent_id = :pid")
        params["pid"] = data.new_parent_id
    if data.new_position is not None:
        sets.append("position = :pos")
        params["pos"] = float(data.new_position)
    if not sets:
        raise HTTPException(status_code=400, detail="không có thay đổi")
    sets.append("updated_by = :eb")

    row = (await db.execute(text(f"""
        UPDATE ws_pages SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {PAGE_FIELDS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="page không tồn tại")
    return _row_to_page(row)


# ═════════════════════════════════════════════════════════════
# 2. BLOCKS
# ═════════════════════════════════════════════════════════════
class BlockIn(BaseModel):
    type: str = Field(default=DEFAULT_BLOCK_TYPE, max_length=20)
    content: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    position: float | None = None
    parent_block_id: int | None = None

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in BLOCK_TYPES:
            raise ValueError(f"unknown block type: {v}")
        return v


class BlockPatchIn(BaseModel):
    type: str | None = Field(default=None, max_length=20)
    content: str | None = None
    properties: dict[str, Any] | None = None
    position: float | None = None
    parent_block_id: int | None = None

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in BLOCK_TYPES:
            raise ValueError(f"unknown block type: {v}")
        return v


class BulkBlocksIn(BaseModel):
    blocks: list[BlockIn] = Field(default_factory=list)


@router.post("/pages/{page_id}/blocks", status_code=201)
async def create_block(
    page_id: int,
    data: BlockIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    pos = data.position
    if pos is None:
        sib = (await db.execute(text("""
            SELECT COALESCE(MAX(position), 0) + 1 FROM ws_blocks
             WHERE page_id = :pid
               AND parent_block_id IS NOT DISTINCT FROM :pb
        """), {"pid": page_id, "pb": data.parent_block_id})).first()
        pos = float(sib[0]) if sib and sib[0] is not None else 1.0

    row = (await db.execute(text(f"""
        INSERT INTO ws_blocks
          (page_id, parent_block_id, type, content, properties, position)
        VALUES (:pid, :pb, :type, :content, CAST(:props AS jsonb), :pos)
        RETURNING {BLOCK_FIELDS}
    """), {
        "pid": page_id,
        "pb": data.parent_block_id,
        "type": data.type,
        "content": data.content,
        "props": json.dumps(data.properties or {}),
        "pos": float(pos),
    })).first()
    await db.commit()
    return _row_to_block(row)


@router.patch("/pages/{page_id}/blocks/{block_id}")
async def patch_block(
    page_id: int,
    block_id: int,
    data: BlockPatchIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    sets, params = [], {"id": block_id, "pid": page_id}
    fset = data.model_fields_set
    if "type" in fset and data.type is not None:
        sets.append("type = :type")
        params["type"] = data.type
    if "content" in fset:
        sets.append("content = :content")
        params["content"] = data.content
    if "properties" in fset and data.properties is not None:
        sets.append("properties = CAST(:props AS jsonb)")
        params["props"] = json.dumps(data.properties)
    if "position" in fset and data.position is not None:
        sets.append("position = :pos")
        params["pos"] = float(data.position)
    if "parent_block_id" in fset:
        sets.append("parent_block_id = :pb")
        params["pb"] = data.parent_block_id
    if not sets:
        raise HTTPException(status_code=400, detail="không có field nào cần update")

    row = (await db.execute(text(f"""
        UPDATE ws_blocks SET {', '.join(sets)}
         WHERE id = :id AND page_id = :pid
         RETURNING {BLOCK_FIELDS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="block không tồn tại")
    return _row_to_block(row)


@router.delete("/pages/{page_id}/blocks/{block_id}")
async def delete_block(
    page_id: int,
    block_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    res = await db.execute(text("""
        DELETE FROM ws_blocks WHERE id = :id AND page_id = :pid
    """), {"id": block_id, "pid": page_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="block không tồn tại")
    return {"deleted": True, "id": block_id}


@router.post("/pages/{page_id}/blocks/bulk", status_code=201)
async def bulk_create_blocks(
    page_id: int,
    data: BulkBlocksIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    if not data.blocks:
        return {"created": 0, "ids": []}
    if len(data.blocks) > MAX_BULK_BLOCKS:
        raise HTTPException(status_code=400,
                            detail=f"tối đa {MAX_BULK_BLOCKS} blocks mỗi lần")

    # Determine starting position from existing siblings
    sib = (await db.execute(text("""
        SELECT COALESCE(MAX(position), 0) FROM ws_blocks WHERE page_id = :pid
    """), {"pid": page_id})).first()
    base_pos = float(sib[0]) if sib and sib[0] is not None else 0.0

    new_ids: list[int] = []
    try:
        for idx, b in enumerate(data.blocks):
            pos_val = b.position if b.position is not None else base_pos + idx + 1
            row = (await db.execute(text("""
                INSERT INTO ws_blocks
                  (page_id, parent_block_id, type, content, properties, position)
                VALUES (:pid, :pb, :type, :content, CAST(:props AS jsonb), :pos)
                RETURNING id
            """), {
                "pid": page_id,
                "pb": b.parent_block_id,
                "type": b.type,
                "content": b.content,
                "props": json.dumps(b.properties or {}),
                "pos": float(pos_val),
            })).first()
            new_ids.append(int(row[0]))
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("bulk_create_blocks failed page=%s", page_id)
        raise HTTPException(status_code=502, detail=f"bulk failed: {type(e).__name__}")

    return {"created": len(new_ids), "ids": new_ids}


# ═════════════════════════════════════════════════════════════
# 3. TASKS
# ═════════════════════════════════════════════════════════════
class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=MAX_TASK_TITLE)
    description: str | None = None
    page_id: int | None = None
    parent_task_id: int | None = None
    status: str = Field(default="todo")
    priority: str = Field(default="medium")
    assignee_email: str | None = None
    due_date: datetime | None = None
    position: float | None = None
    tags: list[str] | None = None

    @field_validator("status")
    @classmethod
    def _v_status(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in ALLOWED_TASK_STATUS:
            raise ValueError(f"status phải thuộc {sorted(ALLOWED_TASK_STATUS)}")
        return v

    @field_validator("priority")
    @classmethod
    def _v_pri(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in ALLOWED_TASK_PRIORITY:
            raise ValueError(f"priority phải thuộc {sorted(ALLOWED_TASK_PRIORITY)}")
        return v

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [str(t).strip()[:40] for t in v if str(t).strip()]
        if len(cleaned) > MAX_TAGS:
            raise ValueError(f"tối đa {MAX_TAGS} tags")
        return cleaned


class TaskPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TASK_TITLE)
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee_email: str | None = None
    due_date: datetime | None = None
    position: float | None = None
    tags: list[str] | None = None
    page_id: int | None = None
    parent_task_id: int | None = None

    @field_validator("status")
    @classmethod
    def _v_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in ALLOWED_TASK_STATUS:
            raise ValueError(f"status phải thuộc {sorted(ALLOWED_TASK_STATUS)}")
        return v

    @field_validator("priority")
    @classmethod
    def _v_pri(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in ALLOWED_TASK_PRIORITY:
            raise ValueError(f"priority phải thuộc {sorted(ALLOWED_TASK_PRIORITY)}")
        return v


@router.post("/tasks", status_code=201)
async def create_task(
    data: TaskIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    if data.page_id is not None:
        await _ensure_page_in_ws(db, data.page_id, ws)
    if data.parent_task_id is not None:
        await _ensure_task_in_ws(db, data.parent_task_id, ws)
    if data.assignee_email:
        data.assignee_email = _validate_email(data.assignee_email)

    pos = data.position
    if pos is None:
        sib = (await db.execute(text("""
            SELECT COALESCE(MAX(position), 0) + 1 FROM ws_tasks
             WHERE workspace_id = :ws AND status = :status
        """), {"ws": ws, "status": data.status})).first()
        pos = float(sib[0]) if sib and sib[0] is not None else 1.0

    row = (await db.execute(text(f"""
        INSERT INTO ws_tasks
          (workspace_id, page_id, parent_task_id, title, description,
           status, priority, assignee_email, due_date, position, tags,
           created_by)
        VALUES (:ws, :pid, :ptid, :title, :desc, :status, :pri,
                :ae, :due, :pos, :tags, :cb)
        RETURNING {TASK_FIELDS}
    """), {
        "ws": ws,
        "pid": data.page_id,
        "ptid": data.parent_task_id,
        "title": data.title,
        "desc": data.description,
        "status": data.status,
        "pri": data.priority,
        "ae": data.assignee_email,
        "due": data.due_date,
        "pos": float(pos),
        "tags": data.tags or [],
        "cb": me.email,
    })).first()
    await db.commit()

    out = _row_to_task(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.task.create",
                         target=f"task#{out['id']}",
                         severity="ok",
                         metadata={"title": data.title, "status": data.status})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/tasks")
async def list_tasks(
    ws: str = Query(...),
    status: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
    due_before: datetime | None = Query(default=None),
    page_id: int | None = Query(default=None),
    limit: int = Query(default=DEFAULT_TASK_LIMIT, ge=1, le=MAX_TASK_LIMIT),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    sql = f"SELECT {TASK_FIELDS} FROM ws_tasks WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws}

    if status:
        s = status.lower()
        if s not in ALLOWED_TASK_STATUS:
            raise HTTPException(status_code=400, detail="status không hợp lệ")
        sql += " AND status = :status"
        params["status"] = s
    if assignee:
        sql += " AND assignee_email = :ae"
        params["ae"] = _validate_email(assignee)
    if due_before:
        sql += " AND due_date <= :db"
        params["db"] = due_before
    if page_id is not None:
        sql += " AND page_id = :pid"
        params["pid"] = page_id

    sql += " ORDER BY position ASC, created_at DESC LIMIT :lim"
    params["lim"] = int(limit)

    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "tasks": [_row_to_task(r) for r in rows]}


@router.patch("/tasks/{task_id}")
async def patch_task(
    task_id: int,
    data: TaskPatchIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_task_in_ws(db, task_id, ws)

    if data.page_id is not None:
        await _ensure_page_in_ws(db, data.page_id, ws)
    if data.parent_task_id is not None:
        if data.parent_task_id == task_id:
            raise HTTPException(status_code=400, detail="parent task không thể là chính nó")
        await _ensure_task_in_ws(db, data.parent_task_id, ws)
    if data.assignee_email:
        data.assignee_email = _validate_email(data.assignee_email)

    sets, params = [], {"id": task_id, "ws": ws}
    fset = data.model_fields_set
    field_map = {
        "title": ("title = :title", "title"),
        "description": ("description = :desc", "desc"),
        "status": ("status = :status", "status"),
        "priority": ("priority = :pri", "pri"),
        "assignee_email": ("assignee_email = :ae", "ae"),
        "due_date": ("due_date = :due", "due"),
        "position": ("position = :pos", "pos"),
        "tags": ("tags = :tags", "tags"),
        "page_id": ("page_id = :pid", "pid"),
        "parent_task_id": ("parent_task_id = :ptid", "ptid"),
    }
    for fname, (clause, pname) in field_map.items():
        if fname in fset:
            v = getattr(data, fname)
            sets.append(clause)
            params[pname] = v if fname != "position" else (float(v) if v is not None else None)

    if not sets:
        raise HTTPException(status_code=400, detail="không có field nào cần update")

    # Auto-complete bookkeeping
    if data.status == "done":
        sets.append("completed_at = NOW()")
    elif data.status and data.status != "done":
        sets.append("completed_at = NULL")

    row = (await db.execute(text(f"""
        UPDATE ws_tasks SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {TASK_FIELDS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="task không tồn tại")
    return _row_to_task(row)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_task_in_ws(db, task_id, ws)

    res = await db.execute(text("""
        DELETE FROM ws_tasks WHERE id = :id AND workspace_id = :ws
    """), {"id": task_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="task không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.task.delete",
                         target=f"task#{task_id}",
                         severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": task_id}


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_task_in_ws(db, task_id, ws)

    row = (await db.execute(text(f"""
        UPDATE ws_tasks
           SET status = 'done', completed_at = NOW()
         WHERE id = :id AND workspace_id = :ws
         RETURNING {TASK_FIELDS}
    """), {"id": task_id, "ws": ws})).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="task không tồn tại")
    return _row_to_task(row)


@router.get("/tasks/calendar")
async def task_calendar(
    ws: str = Query(...),
    month: str | None = Query(default=None, description="YYYY-MM"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    today = date.today()
    if month:
        try:
            y, m = month.split("-")
            year, mon = int(y), int(m)
            if not (1 <= mon <= 12) or not (2000 <= year <= 2100):
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400, detail="month phải có dạng YYYY-MM")
    else:
        year, mon = today.year, today.month

    # First day of month + first day of next month
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)

    rows = (await db.execute(text(f"""
        SELECT {TASK_FIELDS}
          FROM ws_tasks
         WHERE workspace_id = :ws
           AND due_date >= :start
           AND due_date <  :end
         ORDER BY due_date ASC, priority DESC
    """), {"ws": ws, "start": start, "end": end})).all()

    tasks = [_row_to_task(r) for r in rows]
    by_day: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        if not t["due_date"]:
            continue
        day_key = t["due_date"][:10]
        by_day.setdefault(day_key, []).append(t)

    return {
        "workspace_id": ws,
        "month": f"{year:04d}-{mon:02d}",
        "count": len(tasks),
        "by_day": by_day,
        "tasks": tasks,
    }


# ═════════════════════════════════════════════════════════════
# 4. DATABASES (Notion-style inline databases)
# ═════════════════════════════════════════════════════════════
class DatabaseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    page_id: int | None = None
    properties: dict[str, Any] = Field(default_factory=lambda: {"fields": []})


class DatabaseRowIn(BaseModel):
    properties: dict[str, Any] = Field(default_factory=dict)
    position: float | None = None


class DatabaseRowPatchIn(BaseModel):
    properties: dict[str, Any] | None = None
    position: float | None = None


def _row_to_database(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "page_id": int(r[2]) if r[2] is not None else None,
        "name": r[3],
        "properties": _safe_jsonb(r[4]) or {"fields": []},
        "created_by": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
        "updated_at": r[7].isoformat() if r[7] else None,
    }


def _row_to_database_row(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "database_id": int(r[1]),
        "properties": _safe_jsonb(r[2]) or {},
        "position": float(r[3]) if r[3] is not None else 0.0,
        "created_at": r[4].isoformat() if r[4] else None,
        "updated_at": r[5].isoformat() if r[5] else None,
    }


@router.post("/databases", status_code=201)
async def create_database(
    data: DatabaseIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if data.page_id is not None:
        await _ensure_page_in_ws(db, data.page_id, ws)

    row = (await db.execute(text("""
        INSERT INTO ws_databases
          (workspace_id, page_id, name, properties, created_by)
        VALUES (:ws, :pid, :name, CAST(:props AS jsonb), :cb)
        RETURNING id, workspace_id, page_id, name, properties,
                  created_by, created_at, updated_at
    """), {
        "ws": ws,
        "pid": data.page_id,
        "name": data.name,
        "props": json.dumps(data.properties or {"fields": []}),
        "cb": me.email,
    })).first()
    await db.commit()
    return _row_to_database(row)


@router.get("/databases")
async def list_databases(
    ws: str = Query(...),
    page_id: int | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    sql = """
        SELECT id, workspace_id, page_id, name, properties,
               created_by, created_at, updated_at
          FROM ws_databases
         WHERE workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": ws}
    if page_id is not None:
        sql += " AND page_id = :pid"
        params["pid"] = page_id
    sql += " ORDER BY created_at DESC"
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "databases": [_row_to_database(r) for r in rows]}


@router.post("/databases/{db_id}/rows", status_code=201)
async def create_database_row(
    db_id: int,
    data: DatabaseRowIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_db_in_ws(db, db_id, ws)

    pos = data.position
    if pos is None:
        sib = (await db.execute(text("""
            SELECT COALESCE(MAX(position), 0) + 1 FROM ws_database_rows
             WHERE database_id = :did
        """), {"did": db_id})).first()
        pos = float(sib[0]) if sib and sib[0] is not None else 1.0

    row = (await db.execute(text("""
        INSERT INTO ws_database_rows (database_id, properties, position)
        VALUES (:did, CAST(:props AS jsonb), :pos)
        RETURNING id, database_id, properties, position, created_at, updated_at
    """), {
        "did": db_id,
        "props": json.dumps(data.properties or {}),
        "pos": float(pos),
    })).first()
    await db.commit()
    return _row_to_database_row(row)


@router.get("/databases/{db_id}/rows")
async def list_database_rows(
    db_id: int,
    ws: str = Query(...),
    filter: str | None = Query(default=None,
                                description="JSON-encoded {key:value} equality filter"),
    sort: str | None = Query(default=None, description="<key>:<asc|desc>"),
    limit: int = Query(default=200, ge=1, le=2000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_db_in_ws(db, db_id, ws)

    sql = """
        SELECT id, database_id, properties, position, created_at, updated_at
          FROM ws_database_rows
         WHERE database_id = :did
    """
    params: dict[str, Any] = {"did": db_id}

    if filter:
        try:
            f_obj = json.loads(filter)
            if not isinstance(f_obj, dict):
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400,
                                detail="filter phải là JSON object {key:value}")
        # JSONB containment — properties @> :flt
        sql += " AND properties @> CAST(:flt AS jsonb)"
        params["flt"] = json.dumps(f_obj)

    # Sort: only allow position or created_at (safe), or any key with explicit json path
    sort_clause = " ORDER BY position ASC, id ASC"
    if sort:
        try:
            key, direction = sort.split(":", 1)
            direction = direction.lower()
            if direction not in {"asc", "desc"}:
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400, detail="sort phải có dạng key:asc|desc")
        if key in {"position", "created_at", "updated_at", "id"}:
            sort_clause = f" ORDER BY {key} {direction.upper()}"
        else:
            # JSONB property sort — bind key safely as parameter
            if not re.match(r"^[A-Za-z0-9_\-\.]{1,80}$", key):
                raise HTTPException(status_code=400, detail="sort key không hợp lệ")
            sort_clause = (f" ORDER BY properties ->> '{key}' "
                           f"{direction.upper()} NULLS LAST")

    sql += sort_clause + " LIMIT :lim"
    params["lim"] = int(limit)
    rows = (await db.execute(text(sql), params)).all()
    return {"database_id": db_id, "count": len(rows),
            "rows": [_row_to_database_row(r) for r in rows]}


@router.patch("/databases/{db_id}/rows/{row_id}")
async def patch_database_row(
    db_id: int,
    row_id: int,
    data: DatabaseRowPatchIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_db_in_ws(db, db_id, ws)

    sets, params = [], {"id": row_id, "did": db_id}
    if data.properties is not None:
        sets.append("properties = CAST(:props AS jsonb)")
        params["props"] = json.dumps(data.properties)
    if data.position is not None:
        sets.append("position = :pos")
        params["pos"] = float(data.position)
    if not sets:
        raise HTTPException(status_code=400, detail="không có field nào cần update")
    sets.append("updated_at = NOW()")

    row = (await db.execute(text(f"""
        UPDATE ws_database_rows SET {', '.join(sets)}
         WHERE id = :id AND database_id = :did
         RETURNING id, database_id, properties, position, created_at, updated_at
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="row không tồn tại")
    return _row_to_database_row(row)


# ═════════════════════════════════════════════════════════════
# 5. COLLABORATION — collaborators + comments
# ═════════════════════════════════════════════════════════════
class CollabIn(BaseModel):
    email: str
    permission: str = Field(default="view")

    @field_validator("permission")
    @classmethod
    def _v_perm(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in ALLOWED_PERMISSIONS:
            raise ValueError(f"permission phải thuộc {sorted(ALLOWED_PERMISSIONS)}")
        return v


class CommentIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=10_000)
    block_id: int | None = None
    parent_comment_id: int | None = None


@router.post("/pages/{page_id}/collaborators", status_code=201)
async def add_collaborator(
    page_id: int,
    data: CollabIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)
    email = _validate_email(data.email)

    try:
        await db.execute(text("""
            INSERT INTO ws_collaborators
              (page_id, user_email, permission, invited_by)
            VALUES (:pid, :ue, :perm, :ib)
            ON CONFLICT (page_id, user_email)
              DO UPDATE SET permission = EXCLUDED.permission,
                            invited_by = EXCLUDED.invited_by,
                            invited_at = NOW()
        """), {"pid": page_id, "ue": email, "perm": data.permission,
               "ib": me.email})
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("add_collaborator failed page=%s", page_id)
        raise HTTPException(status_code=502,
                            detail=f"không thêm được collaborator: {type(e).__name__}")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.collab.add",
                         target=f"page#{page_id}/{email}",
                         severity="info",
                         metadata={"permission": data.permission})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"page_id": page_id, "email": email, "permission": data.permission}


@router.get("/pages/{page_id}/collaborators")
async def list_collaborators(
    page_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_page_in_ws(db, page_id, ws)

    rows = (await db.execute(text("""
        SELECT page_id, user_email, permission, invited_by, invited_at
          FROM ws_collaborators
         WHERE page_id = :pid
         ORDER BY invited_at DESC
    """), {"pid": page_id})).all()
    return {
        "page_id": page_id,
        "count": len(rows),
        "collaborators": [
            {
                "page_id": int(r[0]),
                "email": r[1],
                "permission": r[2],
                "invited_by": r[3],
                "invited_at": r[4].isoformat() if r[4] else None,
            } for r in rows
        ],
    }


@router.delete("/pages/{page_id}/collaborators/{email}")
async def remove_collaborator(
    page_id: int,
    email: str,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)
    e = _validate_email(email)

    res = await db.execute(text("""
        DELETE FROM ws_collaborators WHERE page_id = :pid AND user_email = :ue
    """), {"pid": page_id, "ue": e})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="collaborator không tồn tại")
    return {"removed": True, "email": e}


@router.post("/pages/{page_id}/comments", status_code=201)
async def add_comment(
    page_id: int,
    data: CommentIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_page_in_ws(db, page_id, ws)

    # If block_id given, validate it lives on this page
    if data.block_id is not None:
        chk = (await db.execute(text("""
            SELECT id FROM ws_blocks WHERE id = :id AND page_id = :pid
        """), {"id": data.block_id, "pid": page_id})).first()
        if not chk:
            raise HTTPException(status_code=400, detail="block không thuộc page này")

    if data.parent_comment_id is not None:
        chk2 = (await db.execute(text("""
            SELECT id FROM ws_comments WHERE id = :id AND page_id = :pid
        """), {"id": data.parent_comment_id, "pid": page_id})).first()
        if not chk2:
            raise HTTPException(status_code=400, detail="parent_comment không thuộc page này")

    row = (await db.execute(text("""
        INSERT INTO ws_comments
          (page_id, block_id, parent_comment_id, author_email, content)
        VALUES (:pid, :bid, :ptid, :ae, :content)
        RETURNING id, page_id, block_id, parent_comment_id,
                  author_email, content, resolved, created_at
    """), {
        "pid": page_id,
        "bid": data.block_id,
        "ptid": data.parent_comment_id,
        "ae": me.email,
        "content": data.content,
    })).first()
    await db.commit()
    return {
        "id": int(row[0]),
        "page_id": int(row[1]),
        "block_id": int(row[2]) if row[2] is not None else None,
        "parent_comment_id": int(row[3]) if row[3] is not None else None,
        "author_email": row[4],
        "content": row[5],
        "resolved": bool(row[6]),
        "created_at": row[7].isoformat() if row[7] else None,
    }


@router.get("/pages/{page_id}/comments")
async def list_comments(
    page_id: int,
    ws: str = Query(...),
    include_resolved: bool = Query(default=False),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_page_in_ws(db, page_id, ws)

    sql = """
        SELECT id, page_id, block_id, parent_comment_id, author_email,
               content, resolved, resolved_at, resolved_by, created_at
          FROM ws_comments
         WHERE page_id = :pid
    """
    if not include_resolved:
        sql += " AND resolved = FALSE"
    sql += " ORDER BY created_at ASC"
    rows = (await db.execute(text(sql), {"pid": page_id})).all()
    return {
        "page_id": page_id,
        "count": len(rows),
        "comments": [
            {
                "id": int(r[0]),
                "page_id": int(r[1]),
                "block_id": int(r[2]) if r[2] is not None else None,
                "parent_comment_id": int(r[3]) if r[3] is not None else None,
                "author_email": r[4],
                "content": r[5],
                "resolved": bool(r[6]),
                "resolved_at": r[7].isoformat() if r[7] else None,
                "resolved_by": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
            } for r in rows
        ],
    }


@router.post("/comments/{comment_id}/resolve")
async def resolve_comment(
    comment_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    # Ensure the comment's page belongs to this workspace
    chk = (await db.execute(text("""
        SELECT c.id, p.workspace_id
          FROM ws_comments c
          JOIN ws_pages p ON p.id = c.page_id
         WHERE c.id = :id
    """), {"id": comment_id})).first()
    if not chk or chk[1] != ws:
        raise HTTPException(status_code=404, detail="comment không tồn tại")

    res = await db.execute(text("""
        UPDATE ws_comments
           SET resolved = TRUE, resolved_at = NOW(), resolved_by = :rb
         WHERE id = :id
    """), {"id": comment_id, "rb": me.email})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="comment không tồn tại")
    return {"resolved": True, "id": comment_id}


# ═════════════════════════════════════════════════════════════
# 6. VERSION HISTORY
# ═════════════════════════════════════════════════════════════
@router.get("/pages/{page_id}/history")
async def list_history(
    page_id: int,
    ws: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_page_in_ws(db, page_id, ws)

    rows = (await db.execute(text("""
        SELECT id, page_id, edited_by, note, created_at
          FROM ws_page_history
         WHERE page_id = :pid
         ORDER BY created_at DESC
         LIMIT :lim
    """), {"pid": page_id, "lim": int(limit)})).all()
    return {
        "page_id": page_id,
        "count": len(rows),
        "versions": [
            {
                "id": int(r[0]),
                "page_id": int(r[1]),
                "edited_by": r[2],
                "note": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            } for r in rows
        ],
    }


@router.post("/pages/{page_id}/restore")
async def restore_page(
    page_id: int,
    ws: str = Query(...),
    version_id: int = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _ensure_page_in_ws(db, page_id, ws)

    try:
        ok = await restore_snapshot(
            db, page_id=page_id, version_id=version_id, edited_by=me.email
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("restore_page failed page=%s version=%s", page_id, version_id)
        raise HTTPException(status_code=502,
                            detail=f"restore failed: {type(e).__name__}")
    if not ok:
        raise HTTPException(status_code=404, detail="version không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.page.restore",
                         target=f"page#{page_id}",
                         severity="warn",
                         metadata={"version_id": version_id})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"restored": True, "page_id": page_id, "version_id": version_id}


# ═════════════════════════════════════════════════════════════
# 7. SEARCH + EXPORT/IMPORT
# ═════════════════════════════════════════════════════════════
@router.get("/search")
async def search_workspace(
    ws: str = Query(...),
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=100),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    return await engine_search(db, workspace_id=ws, query=q, limit=limit)


class ImportMdIn(BaseModel):
    parent_id: int | None = None
    title: str | None = Field(default=None, max_length=MAX_PAGE_TITLE)
    md: str = Field(..., min_length=1, max_length=2_000_000)


@router.post("/pages/import-markdown", status_code=201)
async def import_markdown_endpoint(
    data: ImportMdIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if data.parent_id is not None:
        await _ensure_page_in_ws(db, data.parent_id, ws)

    try:
        new_id = await import_markdown(
            db,
            workspace_id=ws,
            parent_id=data.parent_id,
            md_content=data.md,
            author_email=me.email,
            title=data.title,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("import_markdown failed ws=%s", ws)
        raise HTTPException(status_code=502,
                            detail=f"import failed: {type(e).__name__}")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="workspace.page.import_md",
                         target=f"page#{new_id}",
                         severity="ok")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"page_id": new_id}


@router.get("/pages/{page_id}/export")
async def export_page_endpoint(
    page_id: int,
    ws: str = Query(...),
    fmt: str = Query(default="markdown", description="markdown | html"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_page_in_ws(db, page_id, ws)

    f = (fmt or "markdown").lower()
    if f == "html":
        body = await render_page_html(db, page_id)
        return {"page_id": page_id, "format": "html", "content": body}
    if f != "markdown":
        raise HTTPException(status_code=400, detail="fmt phải là 'markdown' hoặc 'html'")
    body = await export_markdown(db, page_id)
    return {"page_id": page_id, "format": "markdown", "content": body}
