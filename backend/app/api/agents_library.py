"""
AI Agents Library Marketplace API.

50+ pre-built AI agents users can 1-click activate. Each agent =
pre-configured: system prompt + tools + cost ceiling + UI metadata.

Endpoints (prefix `/agents-library`):
  Public catalog:
    GET    /catalog                        — browse 50+ agents (filterable)
    GET    /catalog/{id}                   — agent detail
    GET    /catalog/{id}/reviews           — public reviews

  Workspace install + run:
    POST   /catalog/{id}/install?ws=...    — install agent → workspace_agents
    GET    /workspace?ws=...               — list installed agents
    PATCH  /workspace/{id}?ws=...          — update instance config
    DELETE /workspace/{id}?ws=...          — uninstall
    POST   /workspace/{id}/run?ws=...      — execute agent
    GET    /workspace/{id}/runs?ws=...     — run history (filterable)
    GET    /workspace/{id}/runs/{run_id}   — run detail

  Reviews:
    POST   /reviews?ws=...                 — submit review for catalog agent
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import agent_executor
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.agents_library")
router = APIRouter(prefix="/agents-library", tags=["agents-library"])


# ===========================================================================
# Pydantic v2 schemas
# ===========================================================================
class AgentCatalogOut(BaseModel):
    id: str
    name: str
    name_vi: str | None = None
    description: str | None = None
    description_vi: str | None = None
    category: str | None = None
    icon: str | None = None
    default_model: str | None = None
    tools_enabled: list[str] = Field(default_factory=list)
    pricing_tier: str = "starter"
    cost_per_run_usd: float = 0.005
    avg_latency_ms: int = 2000
    rating: float = 4.5
    install_count: int = 0
    is_featured: bool = False
    is_active: bool = True


class AgentCatalogDetail(AgentCatalogOut):
    system_prompt: str
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    sample_inputs: dict[str, Any] | None = None


class InstallIn(BaseModel):
    instance_name: str = Field(min_length=1, max_length=120)
    custom_prompt: str | None = Field(default=None, max_length=20000)
    custom_model: str | None = Field(default=None, max_length=60)
    custom_config: dict[str, Any] | None = None


class WorkspaceAgentOut(BaseModel):
    id: int
    workspace_id: str
    catalog_id: str
    instance_name: str
    custom_system_prompt: str | None = None
    custom_model: str | None = None
    custom_config: dict[str, Any] | None = None
    is_active: bool = True
    total_runs: int = 0
    total_cost_usd: float = 0.0
    last_run_at: datetime | None = None
    installed_at: datetime
    catalog_name: str | None = None
    catalog_icon: str | None = None
    catalog_category: str | None = None


class UpdateInstanceIn(BaseModel):
    instance_name: str | None = Field(default=None, min_length=1, max_length=120)
    custom_prompt: str | None = Field(default=None, max_length=20000)
    custom_model: str | None = Field(default=None, max_length=60)
    custom_config: dict[str, Any] | None = None
    is_active: bool | None = None


class RunIn(BaseModel):
    input_data: dict[str, Any]
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=300, ge=10, le=86_400)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=32_000)


class RunOut(BaseModel):
    run_id: int
    status: str
    output_text: str
    output_data: dict[str, Any]
    cost_usd: float
    duration_ms: int
    routing_decision: dict[str, Any]
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    error_message: str | None = None
    catalog_id: str
    instance_name: str


class RunHistoryItem(BaseModel):
    id: int
    workspace_agent_id: int
    user_email: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    cost_usd: float = 0.0
    error_message: str | None = None


class RunDetail(RunHistoryItem):
    input_data: dict[str, Any]
    output_data: dict[str, Any] | None = None
    routing_decision: dict[str, Any] | None = None


class ReviewIn(BaseModel):
    catalog_id: str = Field(min_length=1, max_length=60)
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=4000)


class ReviewOut(BaseModel):
    id: int
    catalog_id: str
    workspace_id: str
    user_email: str | None = None
    rating: int
    comment: str | None = None
    created_at: datetime


# ===========================================================================
# Helpers
# ===========================================================================
async def _resolve_workspace_id(db: AsyncSession, ws: str) -> str:
    row = (await db.execute(text("""
        SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1
    """), {"ws": ws})).mappings().first()
    if not row:
        raise HTTPException(404, "workspace not found")
    return row["id"]


async def _ensure_catalog_exists(db: AsyncSession, catalog_id: str) -> dict:
    row = (await db.execute(text("""
        SELECT id, is_active, pricing_tier FROM agent_catalog WHERE id = :id
    """), {"id": catalog_id})).mappings().first()
    if not row:
        raise HTTPException(404, f"agent catalog '{catalog_id}' not found")
    if not row["is_active"]:
        raise HTTPException(410, f"agent '{catalog_id}' is no longer active")
    return dict(row)


# ===========================================================================
# Public catalog endpoints
# ===========================================================================
@router.get("/catalog", response_model=list[AgentCatalogOut])
async def list_catalog(
    category: str | None = Query(default=None, description="Filter: support|legal|dev|marketing|data|ops|wellness"),
    pricing_tier: str | None = Query(default=None, description="Filter: free|starter|pro|business"),
    featured_only: bool = Query(default=False),
    search: str | None = Query(default=None, description="Search in name/description"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[AgentCatalogOut]:
    """Public catalog browse. No auth — anyone can preview agents."""
    sql = """
        SELECT id, name, name_vi, description, description_vi, category, icon,
               default_model, tools_enabled, pricing_tier, cost_per_run_usd,
               avg_latency_ms, rating, install_count, is_featured, is_active
          FROM agent_catalog
         WHERE is_active = TRUE
    """
    params: dict[str, Any] = {}
    if category:
        sql += " AND category = :category"
        params["category"] = category
    if pricing_tier:
        sql += " AND pricing_tier = :tier"
        params["tier"] = pricing_tier
    if featured_only:
        sql += " AND is_featured = TRUE"
    if search:
        sql += " AND (name ILIKE :q OR name_vi ILIKE :q OR description ILIKE :q OR description_vi ILIKE :q)"
        params["q"] = f"%{search}%"
    sql += " ORDER BY is_featured DESC, install_count DESC, name ASC LIMIT :limit OFFSET :offset"
    params.update({"limit": limit, "offset": offset})

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        AgentCatalogOut(
            id=r["id"], name=r["name"], name_vi=r["name_vi"],
            description=r["description"], description_vi=r["description_vi"],
            category=r["category"], icon=r["icon"],
            default_model=r["default_model"],
            tools_enabled=list(r["tools_enabled"] or []),
            pricing_tier=r["pricing_tier"] or "starter",
            cost_per_run_usd=float(r["cost_per_run_usd"] or 0.005),
            avg_latency_ms=int(r["avg_latency_ms"] or 2000),
            rating=float(r["rating"] or 4.5),
            install_count=int(r["install_count"] or 0),
            is_featured=bool(r["is_featured"]),
            is_active=bool(r["is_active"]),
        )
        for r in rows
    ]


@router.get("/catalog/{catalog_id}", response_model=AgentCatalogDetail)
async def get_catalog_detail(
    catalog_id: str,
    db: AsyncSession = Depends(get_db),
) -> AgentCatalogDetail:
    """Public agent detail — full system_prompt + schemas + sample inputs."""
    row = (await db.execute(text("""
        SELECT id, name, name_vi, description, description_vi, category, icon,
               system_prompt, default_model, tools_enabled,
               input_schema, output_schema, sample_inputs,
               pricing_tier, cost_per_run_usd, avg_latency_ms,
               rating, install_count, is_featured, is_active
          FROM agent_catalog
         WHERE id = :id
    """), {"id": catalog_id})).mappings().first()

    if not row:
        raise HTTPException(404, f"agent '{catalog_id}' not found")

    return AgentCatalogDetail(
        id=row["id"], name=row["name"], name_vi=row["name_vi"],
        description=row["description"], description_vi=row["description_vi"],
        category=row["category"], icon=row["icon"],
        system_prompt=row["system_prompt"],
        default_model=row["default_model"],
        tools_enabled=list(row["tools_enabled"] or []),
        input_schema=row["input_schema"],
        output_schema=row["output_schema"],
        sample_inputs=row["sample_inputs"],
        pricing_tier=row["pricing_tier"] or "starter",
        cost_per_run_usd=float(row["cost_per_run_usd"] or 0.005),
        avg_latency_ms=int(row["avg_latency_ms"] or 2000),
        rating=float(row["rating"] or 4.5),
        install_count=int(row["install_count"] or 0),
        is_featured=bool(row["is_featured"]),
        is_active=bool(row["is_active"]),
    )


@router.get("/catalog/{catalog_id}/reviews", response_model=list[ReviewOut])
async def get_catalog_reviews(
    catalog_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[ReviewOut]:
    """Public reviews for a catalog agent (paged)."""
    rows = (await db.execute(text("""
        SELECT id, catalog_id, workspace_id, user_email, rating, comment, created_at
          FROM agent_reviews
         WHERE catalog_id = :id
         ORDER BY created_at DESC
         LIMIT :limit OFFSET :offset
    """), {"id": catalog_id, "limit": limit, "offset": offset})).mappings().all()
    return [
        ReviewOut(
            id=r["id"], catalog_id=r["catalog_id"], workspace_id=r["workspace_id"],
            user_email=r["user_email"], rating=int(r["rating"]),
            comment=r["comment"], created_at=r["created_at"],
        )
        for r in rows
    ]


# ===========================================================================
# Install / list / update / uninstall
# ===========================================================================
@router.post("/catalog/{catalog_id}/install", response_model=WorkspaceAgentOut, status_code=201)
async def install_agent(
    catalog_id: str,
    payload: InstallIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceAgentOut:
    """Install a catalog agent into a workspace. Creates workspace_agents row."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)
    catalog = await _ensure_catalog_exists(db, catalog_id)

    # Insert workspace_agents row (UNIQUE on workspace_id + instance_name)
    try:
        row = (await db.execute(text("""
            INSERT INTO workspace_agents
                (workspace_id, catalog_id, instance_name,
                 custom_system_prompt, custom_model, custom_config)
            VALUES
                (:ws, :cid, :name, :prompt, :model, CAST(:cfg AS JSONB))
            RETURNING id, workspace_id, catalog_id, instance_name,
                      custom_system_prompt, custom_model, custom_config,
                      is_active, total_runs, total_cost_usd, last_run_at, installed_at
        """), {
            "ws": workspace_id,
            "cid": catalog_id,
            "name": payload.instance_name,
            "prompt": payload.custom_prompt,
            "model": payload.custom_model,
            "cfg": _dump_json(payload.custom_config),
        })).mappings().first()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(409, f"instance_name '{payload.instance_name}' already exists in workspace")
        raise HTTPException(500, f"install failed: {e}")

    # Increment catalog install_count + audit log
    await db.execute(text("""
        UPDATE agent_catalog SET install_count = install_count + 1 WHERE id = :id
    """), {"id": catalog_id})
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="agent.install", target=catalog_id, severity="ok",
        metadata={"instance_name": payload.instance_name, "instance_id": row["id"]},
    )
    await db.commit()

    return _ws_agent_row_to_out(row, catalog_extras=None)


@router.get("/workspace", response_model=list[WorkspaceAgentOut])
async def list_workspace_agents(
    ws: str,
    is_active: bool | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceAgentOut]:
    """List agents installed in this workspace (with catalog metadata join)."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    sql = """
        SELECT wa.id, wa.workspace_id, wa.catalog_id, wa.instance_name,
               wa.custom_system_prompt, wa.custom_model, wa.custom_config,
               wa.is_active, wa.total_runs, wa.total_cost_usd, wa.last_run_at, wa.installed_at,
               ac.name AS catalog_name, ac.icon AS catalog_icon, ac.category AS catalog_category
          FROM workspace_agents wa
          JOIN agent_catalog ac ON ac.id = wa.catalog_id
         WHERE wa.workspace_id = :ws
    """
    params: dict[str, Any] = {"ws": workspace_id}
    if is_active is not None:
        sql += " AND wa.is_active = :ia"
        params["ia"] = is_active
    sql += " ORDER BY wa.installed_at DESC"

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        _ws_agent_row_to_out(r, catalog_extras={
            "catalog_name": r.get("catalog_name"),
            "catalog_icon": r.get("catalog_icon"),
            "catalog_category": r.get("catalog_category"),
        })
        for r in rows
    ]


@router.patch("/workspace/{instance_id}", response_model=WorkspaceAgentOut)
async def update_workspace_agent(
    instance_id: int,
    payload: UpdateInstanceIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceAgentOut:
    """Update a workspace agent instance (name/prompt/model/config/active)."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    # Build dynamic UPDATE
    sets: list[str] = []
    params: dict[str, Any] = {"id": instance_id, "ws": workspace_id}
    if payload.instance_name is not None:
        sets.append("instance_name = :name"); params["name"] = payload.instance_name
    if payload.custom_prompt is not None:
        sets.append("custom_system_prompt = :prompt"); params["prompt"] = payload.custom_prompt
    if payload.custom_model is not None:
        sets.append("custom_model = :model"); params["model"] = payload.custom_model
    if payload.custom_config is not None:
        sets.append("custom_config = CAST(:cfg AS JSONB)")
        params["cfg"] = _dump_json(payload.custom_config)
    if payload.is_active is not None:
        sets.append("is_active = :ia"); params["ia"] = payload.is_active

    if not sets:
        raise HTTPException(400, "no fields to update")

    sql = f"""
        UPDATE workspace_agents SET {", ".join(sets)}
         WHERE id = :id AND workspace_id = :ws
        RETURNING id, workspace_id, catalog_id, instance_name,
                  custom_system_prompt, custom_model, custom_config,
                  is_active, total_runs, total_cost_usd, last_run_at, installed_at
    """
    try:
        row = (await db.execute(text(sql), params)).mappings().first()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(500, f"update failed: {e}")
    if not row:
        raise HTTPException(404, f"workspace agent {instance_id} not found")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="agent.update", target=str(instance_id), severity="ok",
        metadata={k: v for k, v in payload.model_dump().items() if v is not None and k != "custom_prompt"},
    )
    await db.commit()
    return _ws_agent_row_to_out(row, catalog_extras=None)


@router.delete("/workspace/{instance_id}")
async def uninstall_agent(
    instance_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Uninstall agent (CASCADE drops agent_runs)."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    row = (await db.execute(text("""
        DELETE FROM workspace_agents
         WHERE id = :id AND workspace_id = :ws
        RETURNING catalog_id, instance_name
    """), {"id": instance_id, "ws": workspace_id})).mappings().first()

    if not row:
        raise HTTPException(404, f"workspace agent {instance_id} not found")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="agent.uninstall", target=row["catalog_id"], severity="ok",
        metadata={"instance_id": instance_id, "instance_name": row["instance_name"]},
    )
    await db.commit()
    return {"deleted": True, "instance_id": instance_id}


# ===========================================================================
# Run agent
# ===========================================================================
@router.post("/workspace/{instance_id}/run", response_model=RunOut)
async def run_workspace_agent(
    instance_id: int,
    payload: RunIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    """Execute the workspace agent: route → cache → run → record."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    try:
        result = await agent_executor.execute_agent(
            db,
            workspace_agent_id=instance_id,
            workspace_id=workspace_id,
            input_data=payload.input_data,
            user_email=me.email,
            cache_enabled=payload.cache_enabled,
            cache_ttl_seconds=payload.cache_ttl_seconds,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
        )
    except PermissionError as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("agent run failed: instance=%s ws=%s", instance_id, ws)
        raise HTTPException(502, f"agent run failed: {e}")

    return RunOut(
        run_id=result.run_id, status=result.status,
        output_text=result.output_text, output_data=result.output_data,
        cost_usd=result.cost_usd, duration_ms=result.duration_ms,
        routing_decision=result.routing_decision, cache_hit=result.cache_hit,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        error_message=result.error_message,
        catalog_id=result.catalog_id, instance_name=result.instance_name,
    )


@router.get("/workspace/{instance_id}/runs", response_model=list[RunHistoryItem])
async def list_runs(
    instance_id: int,
    ws: str,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    status: str | None = Query(default=None, description="pending|running|success|failed"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RunHistoryItem]:
    """Paginated run history filtered by date + status."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    sql = """
        SELECT id, workspace_agent_id, user_email, status,
               started_at, completed_at, duration_ms, cost_usd, error_message
          FROM agent_runs
         WHERE workspace_agent_id = :id AND workspace_id = :ws
    """
    params: dict[str, Any] = {"id": instance_id, "ws": workspace_id}
    if from_ is not None:
        sql += " AND started_at >= :from_"; params["from_"] = from_
    if to is not None:
        sql += " AND started_at <= :to"; params["to"] = to
    if status:
        sql += " AND status = :st"; params["st"] = status
    sql += " ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
    params.update({"limit": limit, "offset": offset})

    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        RunHistoryItem(
            id=r["id"], workspace_agent_id=r["workspace_agent_id"],
            user_email=r["user_email"], status=r["status"],
            started_at=r["started_at"], completed_at=r["completed_at"],
            duration_ms=r["duration_ms"],
            cost_usd=float(r["cost_usd"] or 0),
            error_message=r["error_message"],
        )
        for r in rows
    ]


@router.get("/workspace/{instance_id}/runs/{run_id}", response_model=RunDetail)
async def get_run_detail(
    instance_id: int,
    run_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunDetail:
    """Full run detail: input + output + routing decision."""
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    row = (await db.execute(text("""
        SELECT id, workspace_agent_id, user_email, status,
               started_at, completed_at, duration_ms, cost_usd, error_message,
               input_data, output_data, routing_decision
          FROM agent_runs
         WHERE id = :rid
           AND workspace_agent_id = :id
           AND workspace_id = :ws
    """), {"rid": run_id, "id": instance_id, "ws": workspace_id})).mappings().first()

    if not row:
        raise HTTPException(404, f"run {run_id} not found")

    return RunDetail(
        id=row["id"], workspace_agent_id=row["workspace_agent_id"],
        user_email=row["user_email"], status=row["status"],
        started_at=row["started_at"], completed_at=row["completed_at"],
        duration_ms=row["duration_ms"],
        cost_usd=float(row["cost_usd"] or 0),
        error_message=row["error_message"],
        input_data=row["input_data"] or {},
        output_data=row["output_data"],
        routing_decision=row["routing_decision"],
    )


# ===========================================================================
# Reviews
# ===========================================================================
@router.post("/reviews", response_model=ReviewOut, status_code=201)
async def submit_review(
    payload: ReviewIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReviewOut:
    """Submit a 5-star review for a catalog agent.

    Upsert on (catalog_id, workspace_id, user_email) — re-submitting overwrites
    the previous review. After insert, recompute the catalog's average rating.
    """
    workspace_id = await _resolve_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)
    await _ensure_catalog_exists(db, payload.catalog_id)

    # Upsert review
    row = (await db.execute(text("""
        INSERT INTO agent_reviews (catalog_id, workspace_id, user_email, rating, comment)
        VALUES (:cid, :ws, :email, :rating, :comment)
        ON CONFLICT (catalog_id, workspace_id, user_email) DO UPDATE SET
            rating = EXCLUDED.rating,
            comment = EXCLUDED.comment,
            created_at = NOW()
        RETURNING id, catalog_id, workspace_id, user_email, rating, comment, created_at
    """), {
        "cid": payload.catalog_id,
        "ws": workspace_id,
        "email": me.email,
        "rating": payload.rating,
        "comment": payload.comment,
    })).mappings().first()

    # Recompute average rating for the catalog (cap to 5.0, floor at 1.0)
    await db.execute(text("""
        UPDATE agent_catalog
           SET rating = COALESCE((
               SELECT ROUND(AVG(rating)::numeric, 1)
                 FROM agent_reviews
                WHERE catalog_id = :id
           ), 4.5)
         WHERE id = :id
    """), {"id": payload.catalog_id})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="agent.review", target=payload.catalog_id, severity="ok",
        metadata={"rating": payload.rating},
    )
    await db.commit()

    return ReviewOut(
        id=row["id"], catalog_id=row["catalog_id"], workspace_id=row["workspace_id"],
        user_email=row["user_email"], rating=int(row["rating"]),
        comment=row["comment"], created_at=row["created_at"],
    )


# ===========================================================================
# Internal helpers
# ===========================================================================
def _dump_json(value: Any) -> str | None:
    """Serialize Python dict/list to JSON for a JSONB column. None stays None."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _ws_agent_row_to_out(row: Any, catalog_extras: dict | None) -> WorkspaceAgentOut:
    extras = catalog_extras or {}
    return WorkspaceAgentOut(
        id=row["id"],
        workspace_id=row["workspace_id"],
        catalog_id=row["catalog_id"],
        instance_name=row["instance_name"],
        custom_system_prompt=row["custom_system_prompt"],
        custom_model=row["custom_model"],
        custom_config=row["custom_config"],
        is_active=bool(row["is_active"]),
        total_runs=int(row["total_runs"] or 0),
        total_cost_usd=float(row["total_cost_usd"] or 0),
        last_run_at=row["last_run_at"],
        installed_at=row["installed_at"],
        catalog_name=extras.get("catalog_name"),
        catalog_icon=extras.get("catalog_icon"),
        catalog_category=extras.get("catalog_category"),
    )
