"""
Zeni Coder Swarm API — endpoint orchestration cho 6-vai Council pattern.

Endpoints:
  POST /cto/coder/runs               — start new coder run
  GET  /cto/coder/runs               — list runs (Owner-only)
  GET  /cto/coder/runs/{id}          — full run detail (council + steps + votes)
  POST /cto/coder/runs/{id}/start    — chairman approve + execute
  POST /cto/coder/runs/{id}/abort    — abort run
  POST /cto/coder/runs/{id}/steps/{idx}/approve  — approve 1 step
  POST /cto/coder/runs/{id}/steps/{idx}/reject   — reject 1 step
  GET  /cto/coder/runs/{id}/votes    — council votes detail
  GET  /cto/coder/skills             — list composite skills
  GET  /cto/coder/personas           — list 6-vai personas
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db, SessionLocal
from app.services.coder.council import deliberate
from app.services.coder.executor import execute_run, materialize_steps
from app.services.coder.orchestrator import (
    COMPLEXITY_TIER_MAP,
    PERSONA_CONFIG,
    detect_complexity,
    get_persona_model,
    get_routing_preview,
)

log = logging.getLogger("zeni.cto_coder")
router = APIRouter(prefix="/cto/coder", tags=["cto-coder"])


def _require_cto(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(403, "Coder Swarm chỉ dành cho Owner role (chairman/CTO)")


# ═════════════════════════════════════════════════
# SCHEMAS
# ═════════════════════════════════════════════════
class RunCreateIn(BaseModel):
    session_id: Optional[str] = None
    requirement: str = Field(..., min_length=10, max_length=4000)
    target_workspace: Optional[str] = None
    target_project_id: Optional[str] = None
    context: dict = Field(default_factory=dict)


class RunOut(BaseModel):
    id: str
    session_id: Optional[str] = None
    requirement: str
    target_workspace: Optional[str] = None
    target_project_id: Optional[str] = None
    status: str
    council_consensus: Optional[str] = None
    architect_design: Optional[dict] = None
    planner_steps: Optional[list] = None
    council_votes: Optional[list] = None
    current_step_idx: Optional[int] = 0
    total_steps: Optional[int] = 0
    total_cost_usd: Optional[float] = 0.0
    total_input_tokens: Optional[int] = 0
    total_output_tokens: Optional[int] = 0
    error_summary: Optional[str] = None
    final_result: Optional[dict] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None


class StepOut(BaseModel):
    id: str
    step_idx: int
    tool_name: str
    tool_args: dict
    depends_on: list = Field(default_factory=list)
    description: Optional[str] = None
    status: str
    retry_count: int = 0
    max_retries: int = 3
    requires_approval: bool = True
    approved_at: Optional[str] = None
    rejected_reason: Optional[str] = None
    executed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    result: Optional[Any] = None
    error_detail: Optional[str] = None
    reviewer_verdict: Optional[str] = None
    qa_verdict: Optional[str] = None


# ═════════════════════════════════════════════════
# Run lifecycle
# ═════════════════════════════════════════════════
@router.post("/runs", response_model=RunOut, status_code=201)
async def create_run(
    body: RunCreateIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    """
    Tạo coder run mới. Background: 6-vai Council deliberation.
    Trả về immediately với status='planning'.
    """
    _require_cto(me)
    rid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO cto_coder_runs (id, session_id, requested_by, target_workspace, "
        "target_project_id, requirement, context, status) "
        "VALUES (:id, :sid, :uid, :tws, :tpid, :req, CAST(:ctx AS jsonb), 'planning')"
    ), {
        "id": str(rid), "sid": body.session_id, "uid": str(me.id),
        "tws": body.target_workspace, "tpid": body.target_project_id,
        "req": body.requirement, "ctx": json.dumps(body.context),
    })
    await db.commit()

    # Background: run council deliberation
    bg.add_task(_run_council, str(rid))

    row = (await db.execute(text(
        "SELECT id::text, session_id::text, requirement, target_workspace, target_project_id, "
        "status, council_consensus, architect_design, planner_steps, council_votes, "
        "current_step_idx, total_steps, total_cost_usd, total_input_tokens, total_output_tokens, "
        "error_summary, final_result, created_at::text, started_at::text, completed_at::text, "
        "duration_ms FROM cto_coder_runs WHERE id = :id"
    ), {"id": str(rid)})).mappings().first()
    return _row_to_runout(row)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    session_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    target_workspace: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RunOut]:
    _require_cto(me)
    sql = (
        "SELECT id::text, session_id::text, requirement, target_workspace, target_project_id, "
        "status, council_consensus, architect_design, planner_steps, council_votes, "
        "current_step_idx, total_steps, total_cost_usd, total_input_tokens, total_output_tokens, "
        "error_summary, final_result, created_at::text, started_at::text, completed_at::text, "
        "duration_ms FROM cto_coder_runs"
    )
    where = []
    params: dict[str, Any] = {"lim": limit}
    if session_id:
        where.append("session_id = :sid")
        params["sid"] = session_id
    if status:
        where.append("status = :st")
        params["st"] = status
    if target_workspace:
        where.append("target_workspace = :tws")
        params["tws"] = target_workspace
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT :lim"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_runout(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    _require_cto(me)
    row = (await db.execute(text(
        "SELECT id::text, session_id::text, requirement, target_workspace, target_project_id, "
        "status, council_consensus, architect_design, planner_steps, council_votes, "
        "current_step_idx, total_steps, total_cost_usd, total_input_tokens, total_output_tokens, "
        "error_summary, final_result, created_at::text, started_at::text, completed_at::text, "
        "duration_ms FROM cto_coder_runs WHERE id = :id"
    ), {"id": run_id})).mappings().first()
    if not row:
        raise HTTPException(404, "Run not found")
    return _row_to_runout(row)


@router.get("/runs/{run_id}/steps", response_model=list[StepOut])
async def list_run_steps(
    run_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[StepOut]:
    _require_cto(me)
    rows = (await db.execute(text(
        "SELECT id::text, step_idx, tool_name, tool_args, depends_on, description, status, "
        "retry_count, max_retries, requires_approval, approved_at::text, rejected_reason, "
        "executed_at::text, duration_ms, result, error_detail, reviewer_verdict, qa_verdict "
        "FROM cto_run_steps WHERE run_id = :rid ORDER BY step_idx"
    ), {"rid": run_id})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_args"] = d["tool_args"] if isinstance(d["tool_args"], dict) else (json.loads(d["tool_args"] or "{}") if d["tool_args"] else {})
        d["depends_on"] = d["depends_on"] if isinstance(d["depends_on"], list) else (json.loads(d["depends_on"] or "[]") if d["depends_on"] else [])
        d["result"] = d["result"] if not isinstance(d["result"], str) else (json.loads(d["result"]) if d["result"] else None)
        out.append(StepOut(**d))
    return out


@router.get("/runs/{run_id}/votes")
async def list_council_votes(
    run_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    rows = (await db.execute(text(
        "SELECT id::text, agent_role, agent_model, vote, reasoning, output_json, "
        "input_tokens, output_tokens, cost_usd, latency_ms, router_decision, created_at::text "
        "FROM cto_council_votes WHERE run_id = :rid ORDER BY created_at"
    ), {"rid": run_id})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["output_json"] = d["output_json"] if not isinstance(d["output_json"], str) else (json.loads(d["output_json"]) if d["output_json"] else None)
        d["router_decision"] = d["router_decision"] if not isinstance(d["router_decision"], str) else (json.loads(d["router_decision"]) if d["router_decision"] else None)
        out.append(d)
    return out


@router.post("/runs/{run_id}/start", response_model=RunOut)
async def start_run(
    run_id: str,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    """Chairman approve plan → switch to executing."""
    _require_cto(me)
    row = (await db.execute(text(
        "SELECT status, council_consensus FROM cto_coder_runs WHERE id = :id"
    ), {"id": run_id})).mappings().first()
    if not row:
        raise HTTPException(404, "Run not found")
    if row["status"] not in ("approved", "awaiting_approval", "planning"):
        raise HTTPException(409, f"Cannot start — run status is '{row['status']}'")
    if row["council_consensus"] not in ("approved",):
        raise HTTPException(409, f"Council consensus = '{row['council_consensus']}', không thể start")

    await db.execute(text(
        "UPDATE cto_coder_runs SET status='executing' WHERE id = :id"
    ), {"id": run_id})
    await db.commit()
    bg.add_task(_run_executor, run_id)
    return await get_run(run_id, me, db)


@router.post("/runs/{run_id}/abort", status_code=200)
async def abort_run(
    run_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    await db.execute(text(
        "UPDATE cto_coder_runs SET status='aborted', completed_at=NOW(), "
        "error_summary='Aborted by chairman' WHERE id = :id "
        "AND status NOT IN ('completed', 'failed', 'aborted')"
    ), {"id": run_id})
    await db.commit()
    return {"status": "aborted"}


# ═════════════════════════════════════════════════
# Step approval (chairman gate cho medium+)
# ═════════════════════════════════════════════════
@router.post("/runs/{run_id}/steps/{step_idx}/approve")
async def approve_step(
    run_id: str,
    step_idx: int,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    await db.execute(text(
        "UPDATE cto_run_steps SET approved_by=:by, approved_at=NOW() "
        "WHERE run_id=:rid AND step_idx=:idx AND status='pending'"
    ), {"by": str(me.id), "rid": run_id, "idx": step_idx})
    await db.commit()
    # Resume executor
    bg.add_task(_run_executor, run_id)
    return {"status": "approved", "step_idx": step_idx}


@router.post("/runs/{run_id}/steps/{step_idx}/reject")
async def reject_step(
    run_id: str,
    step_idx: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    await db.execute(text(
        "UPDATE cto_run_steps SET status='skipped', rejected_reason='Rejected by chairman' "
        "WHERE run_id=:rid AND step_idx=:idx"
    ), {"rid": run_id, "idx": step_idx})
    await db.commit()
    return {"status": "rejected"}


# ═════════════════════════════════════════════════
# Skills + Personas catalog
# ═════════════════════════════════════════════════
@router.get("/skills")
async def list_skills(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    rows = (await db.execute(text(
        "SELECT skill_name, display_name, description, tool_sequence, required_args, "
        "risk_level, use_count, success_rate, enabled "
        "FROM cto_skill_registry WHERE enabled = TRUE ORDER BY use_count DESC, skill_name"
    ))).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_sequence"] = d["tool_sequence"] if isinstance(d["tool_sequence"], list) else (json.loads(d["tool_sequence"]) if d["tool_sequence"] else [])
        d["required_args"] = d["required_args"] if isinstance(d["required_args"], list) else (json.loads(d["required_args"]) if d["required_args"] else [])
        out.append(d)
    return out


@router.get("/personas")
async def list_personas(
    complexity: str = Query("medium", description="simple|medium|complex|critical"),
    me: CurrentUser = Depends(get_current_user),
):
    """List 6-vai personas — model chọn theo complexity (adaptive routing).

    Examples:
    - GET /cto/coder/personas?complexity=simple   → all FAST tier
    - GET /cto/coder/personas?complexity=critical → all FRONTIER tier (Opus 4.7)
    """
    _require_cto(me)
    return [
        {
            "name": name,
            "default_tier": cfg["default_tier"].value,
            "deep_tier": cfg["deep_tier"].value,
            "task_type": cfg["task_type"],
            "selected_model_for_complexity": get_persona_model(name, complexity),
            "capabilities": [c.value for c in cfg["capabilities"]],
            "max_tokens": cfg["max_tokens"],
            "temperature": cfg["temperature"],
        }
        for name, cfg in PERSONA_CONFIG.items()
    ]


@router.get("/routing-preview")
async def routing_preview(
    requirement: Optional[str] = Query(None),
    target_workspace: Optional[str] = Query(None),
    explicit_complexity: Optional[str] = Query(None, description="Override auto-detect"),
    me: CurrentUser = Depends(get_current_user),
):
    """
    Preview Zeni Router decisions cho 1 requirement.

    Detect complexity → return model chosen cho mỗi 6-vai persona.
    Useful cho chairman trước khi run: thấy trước Opus 4.7 sẽ kick in nếu critical.
    """
    _require_cto(me)
    complexity = explicit_complexity if explicit_complexity in COMPLEXITY_TIER_MAP else (
        detect_complexity(requirement or "", target_workspace) if requirement else "medium"
    )
    preview = get_routing_preview(complexity)
    # Estimate cost cho 1 council run (5 vai active)
    est_input_tokens = 2000
    est_output_tokens = 1000
    estimated_cost = sum(
        (est_input_tokens * v["input_price_per_mtok"] + est_output_tokens * v["output_price_per_mtok"]) / 1_000_000
        for v in preview.values()
    )
    return {
        "requirement_preview": (requirement or "")[:200],
        "complexity_detected": complexity,
        "personas": preview,
        "estimated_cost_per_run_usd": round(estimated_cost, 5),
        "tier_distribution": _count_tiers(preview),
    }


def _count_tiers(preview: dict) -> dict[str, int]:
    counts = {"fast": 0, "balanced": 0, "frontier": 0}
    for v in preview.values():
        t = v.get("tier", "fast")
        counts[t] = counts.get(t, 0) + 1
    return counts


# ═════════════════════════════════════════════════
# Background tasks
# ═════════════════════════════════════════════════
async def _run_council(run_id: str) -> None:
    """Execute 6-vai council deliberation."""
    try:
        async with SessionLocal() as db:
            # Load tool catalog
            tools = (await db.execute(text(
                "SELECT tool_name, risk_level, description FROM cto_tool_policy "
                "WHERE enabled = TRUE"
            ))).mappings().all()
            tool_catalog = [dict(t) for t in tools]

            run = (await db.execute(text(
                "SELECT requirement, target_workspace, target_project_id "
                "FROM cto_coder_runs WHERE id = :id"
            ), {"id": run_id})).mappings().first()
            if not run:
                return

            outcome = await deliberate(
                db, run_id, run["requirement"], tool_catalog,
                target_workspace=run.get("target_workspace"),
                target_project=run.get("target_project_id"),
            )

            # Persist consensus + design + steps
            new_status = "approved" if outcome["consensus"] == "approved" else (
                "aborted" if outcome["consensus"] == "veto" else "planning"
            )
            await db.execute(text(
                "UPDATE cto_coder_runs SET council_consensus = :cons, "
                "architect_design = CAST(:ad AS jsonb), planner_steps = CAST(:ps AS jsonb), "
                "council_votes = CAST(:cv AS jsonb), "
                "total_cost_usd = total_cost_usd + :cost, "
                "total_input_tokens = total_input_tokens + :it, "
                "total_output_tokens = total_output_tokens + :ot, "
                "status = :st, error_summary = COALESCE(:err, error_summary) "
                "WHERE id = :id"
            ), {
                "cons": outcome["consensus"],
                "ad": json.dumps(outcome["architect_design"], default=str),
                "ps": json.dumps(outcome["planner_steps"], default=str),
                "cv": json.dumps(outcome["votes"], default=str),
                "cost": outcome["total_cost_usd"],
                "it": outcome["total_input_tokens"],
                "ot": outcome["total_output_tokens"],
                "st": new_status,
                "err": outcome.get("abort_reason"),
                "id": run_id,
            })
            await db.commit()

            # If approved, materialize steps
            if new_status == "approved" and outcome["planner_steps"]:
                await materialize_steps(db, run_id, outcome["planner_steps"])
    except Exception as e:
        log.exception("[cto_coder] council run %s crashed: %s", run_id, e)
        try:
            async with SessionLocal() as db:
                await db.execute(text(
                    "UPDATE cto_coder_runs SET status='failed', error_summary=:e WHERE id=:id"
                ), {"e": f"Council crash: {str(e)[:500]}", "id": run_id})
                await db.commit()
        except Exception:
            pass


async def _run_executor(run_id: str) -> None:
    try:
        async with SessionLocal() as db:
            await execute_run(db, run_id)
    except Exception as e:
        log.exception("[cto_coder] executor run %s crashed: %s", run_id, e)


# ═════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════
def _row_to_runout(r: dict) -> RunOut:
    d = dict(r)
    for k in ("architect_design", "final_result"):
        v = d.get(k)
        d[k] = v if not isinstance(v, str) else (json.loads(v) if v else None)
    for k in ("planner_steps", "council_votes"):
        v = d.get(k)
        d[k] = v if not isinstance(v, str) else (json.loads(v) if v else [])
    # Coerce None → 0 for numeric fields (DB column DEFAULT but Pydantic may strict)
    for k in ("current_step_idx", "total_steps", "total_input_tokens", "total_output_tokens"):
        if d.get(k) is None:
            d[k] = 0
    if d.get("total_cost_usd") is None:
        d["total_cost_usd"] = 0.0
    return RunOut(**d)
