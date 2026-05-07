"""
Zeni Studio API — visual no-code app builder backend.

Surface area (mounted under /api/v1/studio in main.py):

Projects
  POST   /studio/projects?ws=                    create project
  GET    /studio/projects?ws=                    list workspace projects
  GET    /studio/projects/{id}?ws=               detail
  PATCH  /studio/projects/{id}?ws=               update name/description
  DELETE /studio/projects/{id}?ws=               delete
  POST   /studio/projects/{id}/duplicate?ws=     clone

Canvas (component tree)
  GET    /studio/projects/{id}/canvas?ws=        full tree
  POST   /studio/projects/{id}/canvas/components?ws=    add component
  PATCH  /studio/projects/{id}/canvas/components/{cid}?ws=  update props/style
  DELETE /studio/projects/{id}/canvas/components/{cid}?ws=  delete subtree
  POST   /studio/projects/{id}/canvas/move?ws=          reparent / reorder

Pages
  POST/GET/PATCH/DELETE /studio/projects/{id}/pages?ws=

Data sources + actions
  POST/GET/PATCH/DELETE /studio/projects/{id}/data-sources?ws=
  POST/GET/PATCH/DELETE /studio/projects/{id}/actions?ws=

Render + deploy
  POST   /studio/projects/{id}/render?ws=&framework=    -> zip
  POST   /studio/projects/{id}/preview?ws=              -> preview URL
  POST   /studio/projects/{id}/publish?ws=&domain=      -> production URL
  GET    /studio/projects/{id}/versions?ws=
  POST   /studio/projects/{id}/rollback?ws=&version=

AI assist
  POST   /studio/projects/{id}/ai-generate?ws=          NL prompt -> tree
  POST   /studio/projects/{id}/ai-suggest?ws=           component -> suggestions
  POST   /studio/projects/{id}/ai-style?ws=             description -> theme tokens

Templates
  GET    /studio/templates?category=                    public marketplace
  POST   /studio/templates/{id}/install?ws=             clone into workspace

Don't touch main.py — caller wires `include_router(studio_router, prefix="/api/v1")`.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.studio_ai import (
    generate_theme,
    generate_tree_from_prompt,
    suggest_improvements,
)
from app.services.studio_renderer import (
    render_next_project,
    render_react_project,
)

log = logging.getLogger("zeni.api.studio")
router = APIRouter(prefix="/studio", tags=["studio"])


# ════════════════════════════════════════════════════════════════════════════
# Constants + helpers
# ════════════════════════════════════════════════════════════════════════════
ALLOWED_STUDIO_SCOPES = ("studio", "build", "full")

VALID_PROJECT_TYPES = ("web", "mobile", "agent")
VALID_FRAMEWORKS = ("next", "react", "vue", "svelte")
VALID_DATA_SOURCE_TYPES = ("api", "sql", "static", "zeni-router")
VALID_ACTION_TYPES = ("js", "api", "workflow")
VALID_ASSET_TYPES = ("image", "font", "icon")
VALID_TEMPLATE_CATEGORIES = ("landing", "ecommerce", "blog", "dashboard", "form")


def _check_scope(me: CurrentUser) -> None:
    """PAT must carry studio|build|full. JWT users always pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and not (scopes & set(ALLOWED_STUDIO_SCOPES)):
        raise HTTPException(
            status_code=403,
            detail="PAT cần scope 'studio' / 'build' / 'full' để dùng /studio",
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()
    return s[:120] or f"proj-{secrets.token_hex(4)}"


async def _ensure_project(db: AsyncSession, project_id: int, ws: str) -> dict[str, Any]:
    row = (
        await db.execute(
            text("SELECT * FROM studio_projects WHERE id = :id AND workspace_id = :ws"),
            {"id": project_id, "ws": ws},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy project")
    return _row_to_dict(row)


async def _ensure_component(
    db: AsyncSession, project_id: int, component_id: int
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT * FROM studio_components WHERE id = :id AND project_id = :pid"
            ),
            {"id": component_id, "pid": project_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy component")
    return _row_to_dict(row)


async def _bump_project_version(db: AsyncSession, project_id: int) -> None:
    await db.execute(
        text(
            "UPDATE studio_projects SET updated_at = NOW(), version = version + 1 "
            "WHERE id = :id"
        ),
        {"id": project_id},
    )


async def _next_unique_slug(db: AsyncSession, ws: str, base: str) -> str:
    candidate = base
    i = 1
    while True:
        exists = (
            await db.execute(
                text(
                    "SELECT 1 FROM studio_projects WHERE workspace_id = :ws AND slug = :s"
                ),
                {"ws": ws, "s": candidate},
            )
        ).first()
        if not exists:
            return candidate
        i += 1
        candidate = f"{base}-{i}"
        if i > 200:
            return f"{base}-{secrets.token_hex(3)}"


async def _hydrate_project(
    db: AsyncSession, project_id: int
) -> dict[str, Any]:
    """Pull a project + all related rows for the renderer or AI."""
    project = (
        await db.execute(
            text("SELECT * FROM studio_projects WHERE id = :id"),
            {"id": project_id},
        )
    ).mappings().first()
    if not project:
        raise HTTPException(404, "Project không tồn tại")
    project_d = _row_to_dict(project)

    pages = [
        _row_to_dict(r)
        for r in (
            await db.execute(
                text(
                    "SELECT id, path, title, layout_id, root_component_id, meta, is_default "
                    "FROM studio_pages WHERE project_id = :pid ORDER BY id ASC"
                ),
                {"pid": project_id},
            )
        ).mappings().all()
    ]

    data_sources = [
        _row_to_dict(r)
        for r in (
            await db.execute(
                text(
                    "SELECT id, name, type, config, schema, cache_ttl_s "
                    "FROM studio_data_sources WHERE project_id = :pid ORDER BY id ASC"
                ),
                {"pid": project_id},
            )
        ).mappings().all()
    ]

    actions = [
        _row_to_dict(r)
        for r in (
            await db.execute(
                text(
                    "SELECT id, name, type, code, params "
                    "FROM studio_actions WHERE project_id = :pid ORDER BY id ASC"
                ),
                {"pid": project_id},
            )
        ).mappings().all()
    ]

    return {
        "project": project_d,
        "pages": pages,
        "data_sources": data_sources,
        "actions": actions,
    }


async def _build_canvas_tree(
    db: AsyncSession, project_id: int, root_id: int | None = None
) -> dict[str, Any] | None:
    """Recursive walk over studio_components → nested dict."""
    rows = (
        await db.execute(
            text(
                "SELECT id, parent_id, type, name, props, style, events, children_order "
                "FROM studio_components WHERE project_id = :pid"
            ),
            {"pid": project_id},
        )
    ).mappings().all()
    if not rows:
        return None
    by_id: dict[int, dict[str, Any]] = {}
    children_index: dict[int | None, list[int]] = {}
    for r in rows:
        d = _row_to_dict(r)
        d["children"] = []
        by_id[d["id"]] = d
        children_index.setdefault(d["parent_id"], []).append(d["id"])

    # Walk
    def hang(cid: int) -> dict[str, Any]:
        node = by_id[cid]
        order = node.get("children_order") or []
        # Prefer explicit order, fall back to insertion order
        ordered = [c for c in order if c in by_id]
        for child_id in children_index.get(cid, []):
            if child_id not in ordered:
                ordered.append(child_id)
        node["children"] = [hang(c) for c in ordered if c in by_id]
        return node

    if root_id and root_id in by_id:
        return hang(root_id)

    # Find a synthetic root: prefer node with parent_id NULL
    roots = children_index.get(None, [])
    if not roots:
        # No clear root, return first
        roots = [next(iter(by_id))]
    if len(roots) == 1:
        return hang(roots[0])
    return {
        "type": "container",
        "name": "synthetic-root",
        "props": {},
        "style": {"className": ""},
        "events": {},
        "children": [hang(r) for r in roots],
    }


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class ProjectIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    type: str = Field(default="web")
    framework: str = Field(default="next")
    canvas_tree: dict[str, Any] | None = None
    theme: dict[str, Any] | None = None

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in VALID_PROJECT_TYPES:
            raise ValueError(f"type phải thuộc {VALID_PROJECT_TYPES}")
        return v

    @field_validator("framework")
    @classmethod
    def _v_fw(cls, v: str) -> str:
        if v not in VALID_FRAMEWORKS:
            raise ValueError(f"framework phải thuộc {VALID_FRAMEWORKS}")
        return v


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    framework: str | None = None
    theme: dict[str, Any] | None = None
    canvas_tree: dict[str, Any] | None = None

    @field_validator("framework")
    @classmethod
    def _v_fw(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_FRAMEWORKS:
            raise ValueError(f"framework phải thuộc {VALID_FRAMEWORKS}")
        return v


class ComponentIn(BaseModel):
    parent_id: int | None = None
    type: str = Field(..., min_length=1, max_length=60)
    name: str | None = Field(default=None, max_length=160)
    props: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)
    events: dict[str, Any] = Field(default_factory=dict)


class ComponentPatch(BaseModel):
    type: str | None = None
    name: str | None = Field(default=None, max_length=160)
    props: dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    events: dict[str, Any] | None = None
    locked: bool | None = None


class ComponentMove(BaseModel):
    component_id: int = Field(..., gt=0)
    new_parent_id: int | None = None
    new_index: int = Field(default=0, ge=0, le=10_000)


class PageIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=200)
    layout_id: int | None = None
    root_component_id: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class PagePatch(BaseModel):
    path: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    layout_id: int | None = None
    root_component_id: int | None = None
    meta: dict[str, Any] | None = None
    is_default: bool | None = None


class DataSourceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    schema_def: dict[str, Any] = Field(default_factory=dict, alias="schema")
    cache_ttl_s: int = Field(default=60, ge=0, le=86_400)

    model_config = {"populate_by_name": True, "protected_namespaces": ()}

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in VALID_DATA_SOURCE_TYPES:
            raise ValueError(f"type phải thuộc {VALID_DATA_SOURCE_TYPES}")
        return v


class DataSourcePatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    config: dict[str, Any] | None = None
    schema_def: dict[str, Any] | None = Field(default=None, alias="schema")
    cache_ttl_s: int | None = Field(default=None, ge=0, le=86_400)

    model_config = {"populate_by_name": True, "protected_namespaces": ()}


class ActionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    type: str = Field(default="js")
    code: str = Field(default="", max_length=20_000)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in VALID_ACTION_TYPES:
            raise ValueError(f"type phải thuộc {VALID_ACTION_TYPES}")
        return v


class ActionPatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    code: str | None = Field(default=None, max_length=20_000)
    params: dict[str, Any] | None = None


class AiGenerateIn(BaseModel):
    prompt: str = Field(..., min_length=4, max_length=4000)
    framework: str = Field(default="next")
    apply: bool = Field(default=False, description="Nếu true: ghi tree vào canvas_tree luôn")

    @field_validator("framework")
    @classmethod
    def _v_fw(cls, v: str) -> str:
        if v not in VALID_FRAMEWORKS:
            raise ValueError(f"framework phải thuộc {VALID_FRAMEWORKS}")
        return v


class AiSuggestIn(BaseModel):
    component_id: int | None = None
    component: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class AiStyleIn(BaseModel):
    description: str = Field(..., min_length=4, max_length=2000)
    apply: bool = Field(default=False)


class TemplateInstallIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)


# ════════════════════════════════════════════════════════════════════════════
# Projects
# ════════════════════════════════════════════════════════════════════════════
@router.post("/projects")
async def create_project(
    body: ProjectIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)

    base_slug = _slugify(body.slug or body.name)
    slug = await _next_unique_slug(db, ws, base_slug)

    row = (
        await db.execute(
            text(
                """
                INSERT INTO studio_projects
                  (workspace_id, name, slug, description, type, framework,
                   canvas_tree, theme, created_by)
                VALUES
                  (:ws, :name, :slug, :desc, :typ, :fw,
                   CAST(:tree AS JSONB), CAST(:theme AS JSONB), :by)
                RETURNING id, workspace_id, name, slug, description, type, framework,
                          canvas_tree, theme, version, created_at, updated_at
                """
            ),
            {
                "ws": ws,
                "name": body.name,
                "slug": slug,
                "desc": body.description,
                "typ": body.type,
                "fw": body.framework,
                "tree": json.dumps(body.canvas_tree or {}, ensure_ascii=False),
                "theme": json.dumps(body.theme or {}, ensure_ascii=False),
                "by": me.email,
            },
        )
    ).mappings().first()
    pid = int(row["id"])

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.project.create", target=str(pid),
        severity="ok", metadata={"name": body.name, "slug": slug, "type": body.type},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/projects")
async def list_projects(
    ws: str = Query(...),
    type_filter: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)

    sql = (
        "SELECT id, name, slug, description, type, framework, version, "
        "published_at, preview_url, publish_url, created_at, updated_at "
        "FROM studio_projects WHERE workspace_id = :ws"
    )
    params: dict[str, Any] = {"ws": ws}
    if type_filter:
        if type_filter not in VALID_PROJECT_TYPES:
            raise HTTPException(400, f"type phải thuộc {VALID_PROJECT_TYPES}")
        sql += " AND type = :typ"
        params["typ"] = type_filter
    sql += " ORDER BY updated_at DESC LIMIT :lim OFFSET :off"
    params["lim"] = limit
    params["off"] = offset

    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    return await _ensure_project(db, project_id, ws)


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: int,
    body: ProjectPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    sets: list[str] = []
    params: dict[str, Any] = {"id": project_id, "ws": ws}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name
    if body.description is not None:
        sets.append("description = :desc")
        params["desc"] = body.description
    if body.framework is not None:
        sets.append("framework = :fw")
        params["fw"] = body.framework
    if body.theme is not None:
        sets.append("theme = CAST(:theme AS JSONB)")
        params["theme"] = json.dumps(body.theme, ensure_ascii=False)
    if body.canvas_tree is not None:
        sets.append("canvas_tree = CAST(:tree AS JSONB)")
        params["tree"] = json.dumps(body.canvas_tree, ensure_ascii=False)
    if not sets:
        raise HTTPException(400, "Không có trường nào được cập nhật")
    sets.append("updated_at = NOW()")
    sets.append("version = version + 1")

    sql = (
        f"UPDATE studio_projects SET {', '.join(sets)} "
        "WHERE id = :id AND workspace_id = :ws "
        "RETURNING id, name, slug, description, type, framework, version, updated_at"
    )
    row = (await db.execute(text(sql), params)).mappings().first()

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.project.update", target=str(project_id), severity="ok",
    )
    await db.commit()
    return _row_to_dict(row)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    await db.execute(
        text("DELETE FROM studio_projects WHERE id = :id AND workspace_id = :ws"),
        {"id": project_id, "ws": ws},
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.project.delete", target=str(project_id), severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted": project_id}


@router.post("/projects/{project_id}/duplicate")
async def duplicate_project(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    src = await _ensure_project(db, project_id, ws)

    new_name = f"{src['name']} (copy)"
    new_slug = await _next_unique_slug(db, ws, _slugify(new_name))

    new_row = (
        await db.execute(
            text(
                """
                INSERT INTO studio_projects
                  (workspace_id, name, slug, description, type, framework,
                   canvas_tree, theme, created_by)
                VALUES
                  (:ws, :name, :slug, :desc, :typ, :fw,
                   CAST(:tree AS JSONB), CAST(:theme AS JSONB), :by)
                RETURNING id, name, slug, type, framework, created_at
                """
            ),
            {
                "ws": ws,
                "name": new_name,
                "slug": new_slug,
                "desc": src.get("description"),
                "typ": src.get("type", "web"),
                "fw": src.get("framework", "next"),
                "tree": json.dumps(src.get("canvas_tree") or {}, ensure_ascii=False),
                "theme": json.dumps(src.get("theme") or {}, ensure_ascii=False),
                "by": me.email,
            },
        )
    ).mappings().first()
    new_id = int(new_row["id"])

    # Copy pages, data sources, actions (components are tracked separately under
    # the canvas tree — duplicating their JSON is enough for v1).
    await db.execute(
        text(
            """
            INSERT INTO studio_pages (project_id, path, title, meta, is_default)
            SELECT :nid, path, title, meta, is_default
            FROM studio_pages WHERE project_id = :sid
            """
        ),
        {"nid": new_id, "sid": project_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO studio_data_sources (project_id, name, type, config, schema, cache_ttl_s)
            SELECT :nid, name, type, config, schema, cache_ttl_s
            FROM studio_data_sources WHERE project_id = :sid
            """
        ),
        {"nid": new_id, "sid": project_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO studio_actions (project_id, name, type, code, params)
            SELECT :nid, name, type, code, params
            FROM studio_actions WHERE project_id = :sid
            """
        ),
        {"nid": new_id, "sid": project_id},
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.project.duplicate", target=str(new_id),
        severity="ok", metadata={"source_id": project_id},
    )
    await db.commit()
    return _row_to_dict(new_row)


# ════════════════════════════════════════════════════════════════════════════
# Canvas (component tree)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/projects/{project_id}/canvas")
async def get_canvas(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    project = await _ensure_project(db, project_id, ws)
    tree = await _build_canvas_tree(db, project_id) or project.get("canvas_tree") or {}
    return {"project_id": project_id, "tree": tree, "version": project.get("version")}


@router.post("/projects/{project_id}/canvas/components")
async def add_component(
    project_id: int,
    body: ComponentIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    if body.parent_id is not None:
        await _ensure_component(db, project_id, body.parent_id)

    row = (
        await db.execute(
            text(
                """
                INSERT INTO studio_components
                  (project_id, parent_id, type, name, props, style, events)
                VALUES
                  (:pid, :par, :typ, :nm, CAST(:props AS JSONB),
                   CAST(:style AS JSONB), CAST(:events AS JSONB))
                RETURNING id, parent_id, type, name, props, style, events, created_at
                """
            ),
            {
                "pid": project_id,
                "par": body.parent_id,
                "typ": body.type,
                "nm": body.name,
                "props": json.dumps(body.props, ensure_ascii=False),
                "style": json.dumps(body.style, ensure_ascii=False),
                "events": json.dumps(body.events, ensure_ascii=False),
            },
        )
    ).mappings().first()
    cid = int(row["id"])

    # Append to parent's children_order if parent exists
    if body.parent_id is not None:
        await db.execute(
            text(
                "UPDATE studio_components SET children_order = "
                "COALESCE(children_order, ARRAY[]::BIGINT[]) || :cid::BIGINT, "
                "updated_at = NOW() WHERE id = :par"
            ),
            {"cid": cid, "par": body.parent_id},
        )

    await _bump_project_version(db, project_id)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.canvas.add", target=str(cid),
        severity="ok",
        metadata={"project_id": project_id, "type": body.type},
    )
    await db.commit()
    return _row_to_dict(row)


@router.patch("/projects/{project_id}/canvas/components/{component_id}")
async def update_component(
    project_id: int,
    component_id: int,
    body: ComponentPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    await _ensure_component(db, project_id, component_id)

    sets: list[str] = []
    params: dict[str, Any] = {"id": component_id, "pid": project_id}
    if body.type is not None:
        sets.append("type = :typ")
        params["typ"] = body.type
    if body.name is not None:
        sets.append("name = :nm")
        params["nm"] = body.name
    if body.props is not None:
        sets.append("props = CAST(:props AS JSONB)")
        params["props"] = json.dumps(body.props, ensure_ascii=False)
    if body.style is not None:
        sets.append("style = CAST(:style AS JSONB)")
        params["style"] = json.dumps(body.style, ensure_ascii=False)
    if body.events is not None:
        sets.append("events = CAST(:events AS JSONB)")
        params["events"] = json.dumps(body.events, ensure_ascii=False)
    if body.locked is not None:
        sets.append("locked = :lk")
        params["lk"] = body.locked
    if not sets:
        raise HTTPException(400, "Không có trường nào được cập nhật")
    sets.append("updated_at = NOW()")

    sql = (
        f"UPDATE studio_components SET {', '.join(sets)} "
        "WHERE id = :id AND project_id = :pid "
        "RETURNING id, parent_id, type, name, props, style, events, locked, updated_at"
    )
    row = (await db.execute(text(sql), params)).mappings().first()

    await _bump_project_version(db, project_id)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.canvas.update", target=str(component_id),
        severity="ok", metadata={"project_id": project_id},
    )
    await db.commit()
    return _row_to_dict(row)


@router.delete("/projects/{project_id}/canvas/components/{component_id}")
async def delete_component(
    project_id: int,
    component_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    comp = await _ensure_component(db, project_id, component_id)

    await db.execute(
        text("DELETE FROM studio_components WHERE id = :id AND project_id = :pid"),
        {"id": component_id, "pid": project_id},
    )
    # Drop from parent's children_order
    if comp.get("parent_id"):
        await db.execute(
            text(
                "UPDATE studio_components SET children_order = "
                "ARRAY(SELECT x FROM unnest(COALESCE(children_order, ARRAY[]::BIGINT[])) x WHERE x <> :cid::BIGINT) "
                "WHERE id = :par"
            ),
            {"cid": component_id, "par": comp["parent_id"]},
        )

    await _bump_project_version(db, project_id)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.canvas.delete", target=str(component_id),
        severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted": component_id}


@router.post("/projects/{project_id}/canvas/move")
async def move_component(
    project_id: int,
    body: ComponentMove,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    comp = await _ensure_component(db, project_id, body.component_id)

    # Detect cycles: the new_parent cannot be a descendant of component_id
    if body.new_parent_id is not None:
        await _ensure_component(db, project_id, body.new_parent_id)
        # Walk descendants
        descendants: set[int] = set()
        stack = [body.component_id]
        while stack:
            cur = stack.pop()
            descendants.add(cur)
            child_rows = (
                await db.execute(
                    text(
                        "SELECT id FROM studio_components "
                        "WHERE project_id = :pid AND parent_id = :par"
                    ),
                    {"pid": project_id, "par": cur},
                )
            ).all()
            stack.extend(int(r[0]) for r in child_rows)
        if body.new_parent_id in descendants:
            raise HTTPException(400, "Cannot move component into its own descendant")

    old_parent = comp.get("parent_id")

    # Remove from old parent
    if old_parent is not None:
        await db.execute(
            text(
                "UPDATE studio_components SET children_order = "
                "ARRAY(SELECT x FROM unnest(COALESCE(children_order, ARRAY[]::BIGINT[])) x WHERE x <> :cid::BIGINT) "
                "WHERE id = :par"
            ),
            {"cid": body.component_id, "par": old_parent},
        )

    # Update parent
    await db.execute(
        text(
            "UPDATE studio_components SET parent_id = :par, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"par": body.new_parent_id, "id": body.component_id},
    )

    # Insert into new parent's children_order at index
    if body.new_parent_id is not None:
        siblings_row = (
            await db.execute(
                text(
                    "SELECT children_order FROM studio_components WHERE id = :par"
                ),
                {"par": body.new_parent_id},
            )
        ).first()
        siblings = list((siblings_row[0] if siblings_row else None) or [])
        siblings = [s for s in siblings if s != body.component_id]
        idx = max(0, min(body.new_index, len(siblings)))
        siblings.insert(idx, body.component_id)
        await db.execute(
            text(
                "UPDATE studio_components SET children_order = :order, updated_at = NOW() "
                "WHERE id = :par"
            ),
            {"order": siblings, "par": body.new_parent_id},
        )

    await _bump_project_version(db, project_id)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.canvas.move", target=str(body.component_id),
        severity="ok",
        metadata={
            "old_parent": old_parent,
            "new_parent": body.new_parent_id,
            "new_index": body.new_index,
        },
    )
    await db.commit()
    return {"ok": True, "moved": body.component_id, "new_parent": body.new_parent_id}


# ════════════════════════════════════════════════════════════════════════════
# Pages
# ════════════════════════════════════════════════════════════════════════════
@router.post("/projects/{project_id}/pages")
async def create_page(
    project_id: int,
    body: PageIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    try:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO studio_pages
                      (project_id, path, title, layout_id, root_component_id, meta, is_default)
                    VALUES
                      (:pid, :path, :title, :lid, :rid, CAST(:meta AS JSONB), :def)
                    RETURNING id, path, title, layout_id, root_component_id, meta, is_default, created_at
                    """
                ),
                {
                    "pid": project_id,
                    "path": body.path,
                    "title": body.title,
                    "lid": body.layout_id,
                    "rid": body.root_component_id,
                    "meta": json.dumps(body.meta, ensure_ascii=False),
                    "def": body.is_default,
                },
            )
        ).mappings().first()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"Tạo page thất bại (path đã tồn tại?): {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.page.create", target=str(row["id"]),
        severity="ok", metadata={"project_id": project_id, "path": body.path},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/projects/{project_id}/pages")
async def list_pages(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    rows = (
        await db.execute(
            text(
                "SELECT id, path, title, layout_id, root_component_id, meta, "
                "is_default, created_at FROM studio_pages "
                "WHERE project_id = :pid ORDER BY id ASC"
            ),
            {"pid": project_id},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.patch("/projects/{project_id}/pages/{page_id}")
async def update_page(
    project_id: int,
    page_id: int,
    body: PagePatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    sets: list[str] = []
    params: dict[str, Any] = {"id": page_id, "pid": project_id}
    if body.path is not None:
        sets.append("path = :path")
        params["path"] = body.path
    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.layout_id is not None:
        sets.append("layout_id = :lid")
        params["lid"] = body.layout_id
    if body.root_component_id is not None:
        sets.append("root_component_id = :rid")
        params["rid"] = body.root_component_id
    if body.meta is not None:
        sets.append("meta = CAST(:meta AS JSONB)")
        params["meta"] = json.dumps(body.meta, ensure_ascii=False)
    if body.is_default is not None:
        sets.append("is_default = :def")
        params["def"] = body.is_default
    if not sets:
        raise HTTPException(400, "Không có trường nào được cập nhật")

    sql = (
        f"UPDATE studio_pages SET {', '.join(sets)} "
        "WHERE id = :id AND project_id = :pid "
        "RETURNING id, path, title, meta, is_default"
    )
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Page không tồn tại")
    await db.commit()
    return _row_to_dict(row)


@router.delete("/projects/{project_id}/pages/{page_id}")
async def delete_page(
    project_id: int,
    page_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    res = await db.execute(
        text("DELETE FROM studio_pages WHERE id = :id AND project_id = :pid"),
        {"id": page_id, "pid": project_id},
    )
    if res.rowcount == 0:
        raise HTTPException(404, "Page không tồn tại")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.page.delete", target=str(page_id), severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted": page_id}


# ════════════════════════════════════════════════════════════════════════════
# Data sources
# ════════════════════════════════════════════════════════════════════════════
@router.post("/projects/{project_id}/data-sources")
async def create_data_source(
    project_id: int,
    body: DataSourceIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    try:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO studio_data_sources
                      (project_id, name, type, config, schema, cache_ttl_s)
                    VALUES
                      (:pid, :nm, :typ, CAST(:cfg AS JSONB), CAST(:sch AS JSONB), :ttl)
                    RETURNING id, name, type, config, schema, cache_ttl_s, created_at
                    """
                ),
                {
                    "pid": project_id,
                    "nm": body.name,
                    "typ": body.type,
                    "cfg": json.dumps(body.config, ensure_ascii=False),
                    "sch": json.dumps(body.schema_def, ensure_ascii=False),
                    "ttl": body.cache_ttl_s,
                },
            )
        ).mappings().first()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"Tạo data source thất bại: {e}")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.data.create", target=str(row["id"]),
        severity="ok", metadata={"project_id": project_id, "type": body.type},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/projects/{project_id}/data-sources")
async def list_data_sources(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    rows = (
        await db.execute(
            text(
                "SELECT id, name, type, config, schema, cache_ttl_s, created_at "
                "FROM studio_data_sources WHERE project_id = :pid ORDER BY id ASC"
            ),
            {"pid": project_id},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.patch("/projects/{project_id}/data-sources/{ds_id}")
async def update_data_source(
    project_id: int,
    ds_id: int,
    body: DataSourcePatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    sets: list[str] = []
    params: dict[str, Any] = {"id": ds_id, "pid": project_id}
    if body.name is not None:
        sets.append("name = :nm")
        params["nm"] = body.name
    if body.config is not None:
        sets.append("config = CAST(:cfg AS JSONB)")
        params["cfg"] = json.dumps(body.config, ensure_ascii=False)
    if body.schema_def is not None:
        sets.append("schema = CAST(:sch AS JSONB)")
        params["sch"] = json.dumps(body.schema_def, ensure_ascii=False)
    if body.cache_ttl_s is not None:
        sets.append("cache_ttl_s = :ttl")
        params["ttl"] = body.cache_ttl_s
    if not sets:
        raise HTTPException(400, "Không có trường nào được cập nhật")
    sql = (
        f"UPDATE studio_data_sources SET {', '.join(sets)} "
        "WHERE id = :id AND project_id = :pid "
        "RETURNING id, name, type, config, schema, cache_ttl_s"
    )
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Data source không tồn tại")
    await db.commit()
    return _row_to_dict(row)


@router.delete("/projects/{project_id}/data-sources/{ds_id}")
async def delete_data_source(
    project_id: int,
    ds_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    res = await db.execute(
        text(
            "DELETE FROM studio_data_sources WHERE id = :id AND project_id = :pid"
        ),
        {"id": ds_id, "pid": project_id},
    )
    if res.rowcount == 0:
        raise HTTPException(404, "Data source không tồn tại")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.data.delete", target=str(ds_id), severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted": ds_id}


# ════════════════════════════════════════════════════════════════════════════
# Actions
# ════════════════════════════════════════════════════════════════════════════
@router.post("/projects/{project_id}/actions")
async def create_action(
    project_id: int,
    body: ActionIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    try:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO studio_actions (project_id, name, type, code, params)
                    VALUES (:pid, :nm, :typ, :code, CAST(:params AS JSONB))
                    RETURNING id, name, type, code, params, created_at
                    """
                ),
                {
                    "pid": project_id,
                    "nm": body.name,
                    "typ": body.type,
                    "code": body.code,
                    "params": json.dumps(body.params, ensure_ascii=False),
                },
            )
        ).mappings().first()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"Tạo action thất bại: {e}")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.action.create", target=str(row["id"]),
        severity="ok", metadata={"project_id": project_id, "type": body.type},
    )
    await db.commit()
    return _row_to_dict(row)


@router.get("/projects/{project_id}/actions")
async def list_actions(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    rows = (
        await db.execute(
            text(
                "SELECT id, name, type, code, params, created_at "
                "FROM studio_actions WHERE project_id = :pid ORDER BY id ASC"
            ),
            {"pid": project_id},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.patch("/projects/{project_id}/actions/{action_id}")
async def update_action(
    project_id: int,
    action_id: int,
    body: ActionPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    sets: list[str] = []
    params: dict[str, Any] = {"id": action_id, "pid": project_id}
    if body.name is not None:
        sets.append("name = :nm")
        params["nm"] = body.name
    if body.code is not None:
        sets.append("code = :code")
        params["code"] = body.code
    if body.params is not None:
        sets.append("params = CAST(:p AS JSONB)")
        params["p"] = json.dumps(body.params, ensure_ascii=False)
    if not sets:
        raise HTTPException(400, "Không có trường nào được cập nhật")
    sql = (
        f"UPDATE studio_actions SET {', '.join(sets)} "
        "WHERE id = :id AND project_id = :pid "
        "RETURNING id, name, type, code, params"
    )
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Action không tồn tại")
    await db.commit()
    return _row_to_dict(row)


@router.delete("/projects/{project_id}/actions/{action_id}")
async def delete_action(
    project_id: int,
    action_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    res = await db.execute(
        text("DELETE FROM studio_actions WHERE id = :id AND project_id = :pid"),
        {"id": action_id, "pid": project_id},
    )
    if res.rowcount == 0:
        raise HTTPException(404, "Action không tồn tại")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.action.delete", target=str(action_id), severity="warn",
    )
    await db.commit()
    return {"ok": True, "deleted": action_id}


# ════════════════════════════════════════════════════════════════════════════
# Render + deploy
# ════════════════════════════════════════════════════════════════════════════
async def _hydrate_pages_with_trees(
    db: AsyncSession, project_id: int, pages_meta: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach a `tree` (component subtree) to each page row."""
    full_tree = await _build_canvas_tree(db, project_id) or {}
    out: list[dict[str, Any]] = []
    for p in pages_meta:
        root_id = p.get("root_component_id")
        if root_id:
            sub = await _build_canvas_tree(db, project_id, root_id=int(root_id))
            tree = sub or {}
        else:
            tree = full_tree
        out.append({**p, "tree": tree})
    return out


@router.post("/projects/{project_id}/render")
async def render_project(
    project_id: int,
    ws: str = Query(...),
    framework: str = Query(default="next"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    _check_scope(me)
    if framework not in VALID_FRAMEWORKS:
        raise HTTPException(400, f"framework phải thuộc {VALID_FRAMEWORKS}")
    await _ensure_project(db, project_id, ws)

    bundle = await _hydrate_project(db, project_id)
    project = bundle["project"]
    pages = await _hydrate_pages_with_trees(db, project_id, bundle["pages"])

    if framework in ("next", "vue", "svelte"):
        # Vue/Svelte fall back to Next renderer for v1 (better than nothing).
        zip_bytes = render_next_project(
            project,
            pages=pages,
            data_sources=bundle["data_sources"],
            actions=bundle["actions"],
            theme=project.get("theme") or {},
        )
    else:
        zip_bytes = render_react_project(
            project,
            pages=pages,
            data_sources=bundle["data_sources"],
            actions=bundle["actions"],
            theme=project.get("theme") or {},
        )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.render", target=str(project_id),
        severity="ok",
        metadata={"framework": framework, "bytes": len(zip_bytes)},
    )
    await db.commit()

    filename = f"{_slugify(project.get('name', 'zeni-app'))}-{framework}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/projects/{project_id}/preview")
async def preview_project(
    project_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    project = await _ensure_project(db, project_id, ws)

    # v1: synthesise a deterministic preview URL the frontend can host.
    # Real impl wires Cloud Run via app.services.cloud_run.deploy_preview().
    preview_url = (
        f"https://preview-{ws}-{project.get('slug')}-{project_id}.zenicloud.app"
    )

    snapshot_payload = await _hydrate_project(db, project_id)
    full_tree = await _build_canvas_tree(db, project_id) or project.get("canvas_tree") or {}
    snapshot = {
        "project": snapshot_payload["project"],
        "pages": snapshot_payload["pages"],
        "data_sources": snapshot_payload["data_sources"],
        "actions": snapshot_payload["actions"],
        "tree": full_tree,
    }

    next_version = int(project.get("version") or 1)
    await db.execute(
        text(
            """
            INSERT INTO studio_versions (project_id, version_number, snapshot, deployed,
                                         deployed_url, created_by, note)
            VALUES (:pid, :ver, CAST(:snap AS JSONB), FALSE, :url, :by, :note)
            ON CONFLICT (project_id, version_number) DO NOTHING
            """
        ),
        {
            "pid": project_id,
            "ver": next_version,
            "snap": json.dumps(snapshot, ensure_ascii=False, default=str),
            "url": preview_url,
            "by": me.email,
            "note": "preview",
        },
    )
    await db.execute(
        text(
            "UPDATE studio_projects SET preview_url = :url, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"url": preview_url, "id": project_id},
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.preview", target=str(project_id),
        severity="ok", metadata={"url": preview_url},
    )
    await billing_push(
        db, workspace_id=ws, layer="L4",
        action="studio.preview", cost_usd=0.001,
    )
    await db.commit()
    return {"ok": True, "preview_url": preview_url, "version": next_version}


@router.post("/projects/{project_id}/publish")
async def publish_project(
    project_id: int,
    ws: str = Query(...),
    domain: str | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    project = await _ensure_project(db, project_id, ws)

    publish_url = domain or f"https://{ws}-{project.get('slug')}.zenicloud.app"
    if not (publish_url.startswith("http://") or publish_url.startswith("https://")):
        publish_url = "https://" + publish_url

    snapshot_payload = await _hydrate_project(db, project_id)
    full_tree = await _build_canvas_tree(db, project_id) or project.get("canvas_tree") or {}
    snapshot = {
        "project": snapshot_payload["project"],
        "pages": snapshot_payload["pages"],
        "data_sources": snapshot_payload["data_sources"],
        "actions": snapshot_payload["actions"],
        "tree": full_tree,
    }

    next_version = int(project.get("version") or 1) + 1
    row = (
        await db.execute(
            text(
                """
                INSERT INTO studio_versions (project_id, version_number, snapshot, deployed,
                                             deployed_url, created_by, note)
                VALUES (:pid, :ver, CAST(:snap AS JSONB), TRUE, :url, :by, :note)
                RETURNING id, version_number, deployed_url, created_at
                """
            ),
            {
                "pid": project_id,
                "ver": next_version,
                "snap": json.dumps(snapshot, ensure_ascii=False, default=str),
                "url": publish_url,
                "by": me.email,
                "note": "publish",
            },
        )
    ).mappings().first()

    await db.execute(
        text(
            "UPDATE studio_projects SET publish_url = :url, version = :ver, "
            "published_at = NOW(), updated_at = NOW() WHERE id = :id"
        ),
        {"url": publish_url, "ver": next_version, "id": project_id},
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.publish", target=str(project_id),
        severity="ok", metadata={"url": publish_url, "version": next_version},
    )
    await billing_push(
        db, workspace_id=ws, layer="L4",
        action="studio.publish", cost_usd=0.05,
    )
    await db.commit()
    return {
        "ok": True,
        "publish_url": publish_url,
        "version": next_version,
        "version_id": row["id"],
    }


@router.get("/projects/{project_id}/versions")
async def list_versions(
    project_id: int,
    ws: str = Query(...),
    limit: int = Query(default=50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)
    rows = (
        await db.execute(
            text(
                "SELECT id, version_number, deployed, deployed_url, note, created_by, created_at "
                "FROM studio_versions WHERE project_id = :pid "
                "ORDER BY version_number DESC LIMIT :lim"
            ),
            {"pid": project_id, "lim": limit},
        )
    ).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.post("/projects/{project_id}/rollback")
async def rollback_project(
    project_id: int,
    ws: str = Query(...),
    version: int = Query(..., ge=1),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    row = (
        await db.execute(
            text(
                "SELECT snapshot FROM studio_versions "
                "WHERE project_id = :pid AND version_number = :ver"
            ),
            {"pid": project_id, "ver": version},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, f"Version {version} không tồn tại")

    snap = row["snapshot"] or {}
    if isinstance(snap, str):
        snap = json.loads(snap)

    proj_payload = snap.get("project") or {}
    canvas_tree = snap.get("tree") or proj_payload.get("canvas_tree") or {}
    theme = proj_payload.get("theme") or {}

    await db.execute(
        text(
            "UPDATE studio_projects SET canvas_tree = CAST(:tree AS JSONB), "
            "theme = CAST(:theme AS JSONB), updated_at = NOW(), version = version + 1 "
            "WHERE id = :id"
        ),
        {
            "tree": json.dumps(canvas_tree, ensure_ascii=False),
            "theme": json.dumps(theme, ensure_ascii=False),
            "id": project_id,
        },
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.rollback", target=str(project_id),
        severity="warn", metadata={"version": version},
    )
    await db.commit()
    return {"ok": True, "rolled_back_to": version}


# ════════════════════════════════════════════════════════════════════════════
# AI assist
# ════════════════════════════════════════════════════════════════════════════
@router.post("/projects/{project_id}/ai-generate")
async def ai_generate(
    project_id: int,
    body: AiGenerateIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    try:
        result = await generate_tree_from_prompt(
            body.prompt,
            framework=body.framework,
            workspace_id=ws,
            db=db,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("ai-generate failed")
        raise HTTPException(502, f"AI generation failed: {e}")

    if body.apply:
        await db.execute(
            text(
                "UPDATE studio_projects SET canvas_tree = CAST(:tree AS JSONB), "
                "framework = :fw, updated_at = NOW(), version = version + 1 "
                "WHERE id = :id"
            ),
            {
                "tree": json.dumps(result.get("tree") or {}, ensure_ascii=False),
                "fw": body.framework,
                "id": project_id,
            },
        )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.ai.generate", target=str(project_id),
        severity="ok",
        metadata={
            "model": result.get("model"),
            "cost_usd": result.get("cost_usd"),
            "applied": body.apply,
        },
    )
    await billing_push(
        db, workspace_id=ws, layer="L5",
        action="studio.ai.generate",
        cost_usd=float(result.get("cost_usd") or 0.0),
    )
    await db.commit()
    return {
        "tree": result.get("tree"),
        "data_sources": result.get("data_sources", []),
        "actions": result.get("actions", []),
        "model": result.get("model"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "cost_usd": result.get("cost_usd"),
        "applied": body.apply,
    }


@router.post("/projects/{project_id}/ai-suggest")
async def ai_suggest(
    project_id: int,
    body: AiSuggestIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    if body.component_id is not None:
        comp = await _ensure_component(db, project_id, body.component_id)
        component_payload = {
            "type": comp.get("type"),
            "name": comp.get("name"),
            "props": comp.get("props"),
            "style": comp.get("style"),
            "events": comp.get("events"),
        }
    elif body.component is not None:
        component_payload = body.component
    else:
        raise HTTPException(400, "Cần cung cấp component_id hoặc component")

    suggestions = await suggest_improvements(
        component_payload, body.context,
        workspace_id=ws, db=db,
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.ai.suggest", target=str(project_id),
        severity="ok", metadata={"count": len(suggestions)},
    )
    await db.commit()
    return {"suggestions": suggestions, "count": len(suggestions)}


@router.post("/projects/{project_id}/ai-style")
async def ai_style(
    project_id: int,
    body: AiStyleIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _ensure_project(db, project_id, ws)

    result = await generate_theme(body.description, workspace_id=ws, db=db)

    if body.apply:
        await db.execute(
            text(
                "UPDATE studio_projects SET theme = CAST(:t AS JSONB), "
                "updated_at = NOW(), version = version + 1 WHERE id = :id"
            ),
            {
                "t": json.dumps(result.get("tokens") or {}, ensure_ascii=False),
                "id": project_id,
            },
        )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.ai.style", target=str(project_id),
        severity="ok",
        metadata={"model": result.get("model"), "applied": body.apply},
    )
    await billing_push(
        db, workspace_id=ws, layer="L5",
        action="studio.ai.style",
        cost_usd=float(result.get("cost_usd") or 0.0),
    )
    await db.commit()
    return {
        "tokens": result.get("tokens"),
        "model": result.get("model"),
        "cost_usd": result.get("cost_usd"),
        "applied": body.apply,
    }


# ════════════════════════════════════════════════════════════════════════════
# Templates marketplace
# ════════════════════════════════════════════════════════════════════════════
@router.get("/templates")
async def list_templates(
    category: str | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Public templates list — no workspace gate (PAT scope still enforced)."""
    _check_scope(me)
    sql = (
        "SELECT id, name, category, description, preview_url, install_count, created_at "
        "FROM studio_templates WHERE is_public = TRUE"
    )
    params: dict[str, Any] = {}
    if category:
        if category not in VALID_TEMPLATE_CATEGORIES:
            raise HTTPException(400, f"category phải thuộc {VALID_TEMPLATE_CATEGORIES}")
        sql += " AND category = :cat"
        params["cat"] = category
    sql += " ORDER BY install_count DESC, id ASC"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.post("/templates/{template_id}/install")
async def install_template(
    template_id: int,
    ws: str = Query(...),
    body: TemplateInstallIn | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_scope(me)

    tpl = (
        await db.execute(
            text(
                "SELECT id, name, category, description, tree FROM studio_templates "
                "WHERE id = :id AND is_public = TRUE"
            ),
            {"id": template_id},
        )
    ).mappings().first()
    if not tpl:
        raise HTTPException(404, "Template không tồn tại")
    tpl_d = _row_to_dict(tpl)
    tree_payload = tpl_d.get("tree") or {}
    if isinstance(tree_payload, str):
        tree_payload = json.loads(tree_payload)

    name = (body.name if body else None) or f"{tpl_d['name']}"
    slug = await _next_unique_slug(db, ws, _slugify(name))
    framework = (tree_payload.get("framework") or "next") if isinstance(tree_payload, dict) else "next"
    if framework not in VALID_FRAMEWORKS:
        framework = "next"
    theme = tree_payload.get("theme") if isinstance(tree_payload, dict) else {}
    canvas_root = {}
    pages_payload: list[dict[str, Any]] = []
    if isinstance(tree_payload, dict):
        if "pages" in tree_payload and isinstance(tree_payload["pages"], list):
            pages_payload = tree_payload["pages"]
            # Use first page tree as canvas root for projects that look at canvas_tree directly
            first = pages_payload[0] if pages_payload else {}
            if isinstance(first, dict) and isinstance(first.get("tree"), dict):
                canvas_root = first["tree"]
        elif "tree" in tree_payload:
            canvas_root = tree_payload["tree"] if isinstance(tree_payload["tree"], dict) else {}
        else:
            canvas_root = tree_payload

    new_row = (
        await db.execute(
            text(
                """
                INSERT INTO studio_projects
                  (workspace_id, name, slug, description, type, framework,
                   canvas_tree, theme, created_by)
                VALUES
                  (:ws, :name, :slug, :desc, 'web', :fw,
                   CAST(:tree AS JSONB), CAST(:theme AS JSONB), :by)
                RETURNING id, name, slug, framework, created_at
                """
            ),
            {
                "ws": ws,
                "name": name,
                "slug": slug,
                "desc": tpl_d.get("description"),
                "fw": framework,
                "tree": json.dumps(canvas_root, ensure_ascii=False),
                "theme": json.dumps(theme or {}, ensure_ascii=False),
                "by": me.email,
            },
        )
    ).mappings().first()
    new_id = int(new_row["id"])

    # Materialise pages (path/title) — keep tree only on canvas for v1
    for p in pages_payload:
        if not isinstance(p, dict):
            continue
        path = p.get("path") or "/"
        title = p.get("title") or "Page"
        try:
            await db.execute(
                text(
                    "INSERT INTO studio_pages (project_id, path, title, meta, is_default) "
                    "VALUES (:pid, :path, :title, CAST(:meta AS JSONB), :def) "
                    "ON CONFLICT (project_id, path) DO NOTHING"
                ),
                {
                    "pid": new_id,
                    "path": path,
                    "title": title,
                    "meta": json.dumps(p.get("meta") or {}, ensure_ascii=False),
                    "def": path == "/",
                },
            )
        except Exception:  # noqa: BLE001
            pass

    # Materialise data sources + actions
    for ds in (tree_payload.get("data_sources") or []) if isinstance(tree_payload, dict) else []:
        if not isinstance(ds, dict):
            continue
        ds_type = ds.get("type", "static")
        if ds_type not in VALID_DATA_SOURCE_TYPES:
            continue
        try:
            await db.execute(
                text(
                    "INSERT INTO studio_data_sources (project_id, name, type, config, schema) "
                    "VALUES (:pid, :nm, :typ, CAST(:cfg AS JSONB), CAST(:sch AS JSONB)) "
                    "ON CONFLICT (project_id, name) DO NOTHING"
                ),
                {
                    "pid": new_id,
                    "nm": ds.get("name") or f"ds-{secrets.token_hex(3)}",
                    "typ": ds_type,
                    "cfg": json.dumps(ds.get("config") or {}, ensure_ascii=False),
                    "sch": json.dumps(ds.get("schema") or {}, ensure_ascii=False),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    for ac in (tree_payload.get("actions") or []) if isinstance(tree_payload, dict) else []:
        if not isinstance(ac, dict):
            continue
        ac_type = ac.get("type", "js")
        if ac_type not in VALID_ACTION_TYPES:
            continue
        try:
            await db.execute(
                text(
                    "INSERT INTO studio_actions (project_id, name, type, code, params) "
                    "VALUES (:pid, :nm, :typ, :code, CAST(:p AS JSONB)) "
                    "ON CONFLICT (project_id, name) DO NOTHING"
                ),
                {
                    "pid": new_id,
                    "nm": ac.get("name") or f"action-{secrets.token_hex(3)}",
                    "typ": ac_type,
                    "code": ac.get("code") or "",
                    "p": json.dumps(ac.get("params") or {}, ensure_ascii=False),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    await db.execute(
        text("UPDATE studio_templates SET install_count = install_count + 1 WHERE id = :id"),
        {"id": template_id},
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="studio.template.install", target=str(new_id),
        severity="ok", metadata={"template_id": template_id, "template_name": tpl_d.get("name")},
    )
    await db.commit()
    return {"ok": True, "project": _row_to_dict(new_row), "template_id": template_id}
