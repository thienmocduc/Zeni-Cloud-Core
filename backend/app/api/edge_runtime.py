"""
Zeni Cloud Core — Edge Runtime API.

Sandboxed microVM execution cho AI agent code (Claude Computer Use, Playwright,
Python data science, Node automation, Ubuntu shell).

Khách KHÔNG cần mua VM riêng — Zeni Edge Runtime sandbox tự cấp + tự destroy + tự bill.

Endpoints (prefix /edge):
  GET    /runtimes                         — List supported runtimes (Python/Node/Computer Use/Playwright/Shell)
  POST   /sandboxes                        — Create + execute sandbox (returns stdout/stderr/exit_code)
  GET    /sandboxes                        — List recent sandbox executions
  GET    /sandboxes/{id}                   — Get sandbox status + logs
  POST   /sandboxes/{id}/terminate         — Terminate running sandbox
  GET    /quotas                           — Current usage
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.edge_runtime")

router = APIRouter(prefix="/edge", tags=["edge-runtime"])


# ===== Schemas =====

class RuntimeOut(BaseModel):
    id: str
    display_name: str
    default_cpu_millis: int
    default_memory_mb: int
    cost_per_second_credits: float
    description: str


class SandboxCreate(BaseModel):
    runtime: str = Field(..., description="python-3.12 | node-20 | computer-use | playwright | shell-ubuntu")
    exec_command: str = Field(..., description="Shell command or script to run, eg: 'python script.py' or 'node app.js'")
    exec_args: list[str] = Field(default_factory=list)
    exec_env: dict[str, str] = Field(default_factory=dict)
    stdin_payload: Optional[str] = Field(None, description="STDIN data to feed the process")
    cpu_millis: Optional[int] = Field(None, ge=100, le=8000)
    memory_mb: Optional[int] = Field(None, ge=128, le=16384)
    timeout_sec: int = Field(600, ge=10, le=3600, description="Max execution time (default 10 min, max 1 hour)")
    network_policy: str = Field("allow-public", description="allow-public | allow-list | deny-all")
    network_allowlist: list[str] = Field(default_factory=list, description="If allow-list, list of allowed domains")


class SandboxOut(BaseModel):
    id: str
    workspace_id: str
    runtime_type: str
    status: str
    exec_command: str
    cpu_millis: int
    memory_mb: int
    stdout_log: Optional[str] = None
    stderr_log: Optional[str] = None
    exit_code: Optional[int] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cpu_seconds_used: float = 0
    poll_url: str


# ===== Endpoints =====

@router.get("/runtimes", response_model=list[RuntimeOut])
async def list_runtimes(db: AsyncSession = Depends(get_db)):
    """List all available sandbox runtimes."""
    rows = (await db.execute(text(
        "SELECT id, display_name, default_cpu_millis, default_memory_mb, "
        "cost_per_second_credits, description FROM edge_runtimes "
        "WHERE is_active = TRUE ORDER BY display_name"
    ))).mappings().all()
    return [
        RuntimeOut(
            id=r["id"],
            display_name=r["display_name"],
            default_cpu_millis=r["default_cpu_millis"],
            default_memory_mb=r["default_memory_mb"],
            cost_per_second_credits=float(r["cost_per_second_credits"]),
            description=r["description"] or "",
        ) for r in rows
    ]


@router.post("/sandboxes", response_model=SandboxOut, status_code=202)
async def create_sandbox(
    data: SandboxCreate,
    bg: BackgroundTasks,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Create + execute sandbox. Returns sandbox_id + poll_url."""
    await require_workspace_access(ws, me)
    # Validate runtime
    rt = (await db.execute(text(
        "SELECT id, base_image, default_cpu_millis, default_memory_mb "
        "FROM edge_runtimes WHERE id = :id AND is_active = TRUE"
    ), {"id": data.runtime})).mappings().first()
    if not rt:
        raise HTTPException(404, f"Runtime '{data.runtime}' not found. Try GET /edge/runtimes")

    cpu = data.cpu_millis or rt["default_cpu_millis"]
    mem = data.memory_mb or rt["default_memory_mb"]

    # Quota check
    quota = (await db.execute(text(
        "SELECT max_concurrent, max_seconds_per_month, used_seconds_this_month "
        "FROM edge_runtime_quotas WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not quota:
        await db.execute(text(
            "INSERT INTO edge_runtime_quotas (workspace_id) VALUES (:ws) ON CONFLICT DO NOTHING"
        ), {"ws": ws})
        max_concurrent, max_sec, used_sec = 3, 18000, 0
    else:
        max_concurrent = quota["max_concurrent"]
        max_sec = quota["max_seconds_per_month"]
        used_sec = quota["used_seconds_this_month"]

    if used_sec >= max_sec:
        raise HTTPException(429, f"Monthly sandbox quota exceeded ({used_sec}/{max_sec}s).")

    running_count = (await db.execute(text(
        "SELECT COUNT(*) FROM edge_sandboxes WHERE workspace_id = :ws AND status IN ('idle','running')"
    ), {"ws": ws})).scalar() or 0
    if running_count >= max_concurrent:
        raise HTTPException(429, f"Max concurrent sandboxes ({running_count}/{max_concurrent}). Wait or terminate one.")

    # Create sandbox row
    sb_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO edge_sandboxes (id, workspace_id, user_id, runtime_type, base_image, "
        "cpu_millis, memory_mb, timeout_sec, network_policy, network_allowlist, "
        "exec_command, exec_args, exec_env, stdin_payload, status) "
        "VALUES (:id, :ws, :uid, :rt, :img, :cpu, :mem, :to, :np, CAST(:nal AS jsonb), "
        ":cmd, CAST(:ar AS jsonb), CAST(:env AS jsonb), :stdin, 'idle')"
    ), {
        "id": str(sb_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "rt": data.runtime,
        "img": rt["base_image"],
        "cpu": cpu,
        "mem": mem,
        "to": data.timeout_sec,
        "np": data.network_policy,
        "nal": json.dumps(data.network_allowlist),
        "cmd": data.exec_command,
        "ar": json.dumps(data.exec_args),
        "env": json.dumps(data.exec_env),
        "stdin": data.stdin_payload,
    })
    await db.commit()

    # Schedule background execution (Phase 2 worker pending)
    bg.add_task(_stub_sandbox_runner, str(sb_id))

    return SandboxOut(
        id=str(sb_id),
        workspace_id=ws,
        runtime_type=data.runtime,
        status="idle",
        exec_command=data.exec_command,
        cpu_millis=cpu,
        memory_mb=mem,
        created_at=datetime.now(timezone.utc).isoformat(),
        poll_url=f"/api/v1/edge/sandboxes/{sb_id}?ws={ws}",
    )


@router.get("/sandboxes", response_model=list[SandboxOut])
async def list_sandboxes(
    ws: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, workspace_id, runtime_type, status, exec_command, cpu_millis, memory_mb, "
        "stdout_log, stderr_log, exit_code, created_at, started_at, finished_at, cpu_seconds_used "
        "FROM edge_sandboxes WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT :lim"
    ), {"ws": ws, "lim": limit})).mappings().all()
    return [_row_to_sandbox(r, ws) for r in rows]


@router.get("/sandboxes/{sandbox_id}", response_model=SandboxOut)
async def get_sandbox(
    sandbox_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, runtime_type, status, exec_command, cpu_millis, memory_mb, "
        "stdout_log, stderr_log, exit_code, created_at, started_at, finished_at, cpu_seconds_used "
        "FROM edge_sandboxes WHERE id = :id AND workspace_id = :ws"
    ), {"id": sandbox_id, "ws": ws})).mappings().first()
    if not r:
        raise HTTPException(404, "Sandbox not found")
    return _row_to_sandbox(r, ws)


@router.post("/sandboxes/{sandbox_id}/terminate", status_code=202)
async def terminate_sandbox(
    sandbox_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "UPDATE edge_sandboxes SET status='terminated', finished_at=NOW() "
        "WHERE id = :id AND workspace_id = :ws AND status IN ('idle','running') RETURNING id"
    ), {"id": sandbox_id, "ws": ws})).first()
    await db.commit()
    if not r:
        raise HTTPException(404, "Sandbox not found or already finished")
    return {"id": sandbox_id, "status": "terminated"}


@router.get("/quotas")
async def get_quota(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT max_concurrent, max_seconds_per_month, used_seconds_this_month, reset_at "
        "FROM edge_runtime_quotas WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not r:
        return {
            "workspace_id": ws,
            "max_concurrent": 3,
            "max_seconds_per_month": 18000,
            "used_seconds_this_month": 0,
            "tier": "free",
        }
    return {
        "workspace_id": ws,
        "max_concurrent": r["max_concurrent"],
        "max_seconds_per_month": r["max_seconds_per_month"],
        "used_seconds_this_month": r["used_seconds_this_month"],
        "reset_at": r["reset_at"].isoformat() if r["reset_at"] else None,
        "tier": "free" if r["max_seconds_per_month"] <= 18000 else "pro",
    }


def _row_to_sandbox(r, ws: str) -> SandboxOut:
    return SandboxOut(
        id=str(r["id"]),
        workspace_id=ws,
        runtime_type=r["runtime_type"],
        status=r["status"],
        exec_command=r["exec_command"] or "",
        cpu_millis=r["cpu_millis"],
        memory_mb=r["memory_mb"],
        stdout_log=r.get("stdout_log"),
        stderr_log=r.get("stderr_log"),
        exit_code=r.get("exit_code"),
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
        started_at=r["started_at"].isoformat() if r.get("started_at") else None,
        finished_at=r["finished_at"].isoformat() if r.get("finished_at") else None,
        cpu_seconds_used=float(r.get("cpu_seconds_used") or 0),
        poll_url=f"/api/v1/edge/sandboxes/{r['id']}?ws={ws}",
    )


async def _stub_sandbox_runner(sandbox_id: str) -> None:
    """STUB: Phase 2 worker pending. Will use Cloud Run Jobs to spin sandbox container."""
    log.info("[EDGE_RUNTIME_STUB] Sandbox %s queued — Phase 2 Cloud Run Jobs worker pending", sandbox_id)
