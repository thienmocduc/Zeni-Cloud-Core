"""
Zeni Cloud Core — Build Farm API.

Cloud build service cho native apps (Tauri / Rust / Electron / Go / Flutter / .NET).
Khách upload source → Build Farm chạy toolchain trong cloud → trả binary.

Khách KHÔNG cần cài đặt rustc, MSVC, Xcode, Android NDK locally.

Endpoints (prefix /build-farm):
  GET    /toolchains                       — List supported toolchains (Tauri/Rust/Electron/Go/Flutter/.NET)
  POST   /jobs                             — Submit build job (zip URL or github)
  GET    /jobs                             — List recent jobs for workspace
  GET    /jobs/{job_id}                    — Get job status + artifact URLs
  POST   /jobs/{job_id}/cancel             — Cancel running build
  GET    /quotas                           — Current quota usage
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.build_farm")

router = APIRouter(prefix="/build-farm", tags=["build-farm"])


# ===== Schemas =====

class BuildJobCreate(BaseModel):
    toolchain: str = Field(..., description="tauri-latest | rust-stable | electron-builder | go-modules | flutter-stable | dotnet-8")
    source_type: str = Field("zip", description="zip | github | gcs")
    source_ref: str = Field(..., description="GCS path (gs://...) or github://owner/repo@branch or upload_id from /upload/source")
    target_platforms: list[str] = Field(default_factory=lambda: ["linux-x64"], description="linux-x64, windows-x64, macos-x64, etc.")
    build_config: dict[str, Any] = Field(default_factory=dict, description="ENV vars, build args, signing certs")


class BuildJobOut(BaseModel):
    id: str
    workspace_id: str
    job_type: str
    status: str
    target_platforms: list[str]
    artifact_urls: list[dict]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    build_duration_sec: int = 0
    cost_credits: int = 0
    error_message: Optional[str] = None
    poll_url: str
    estimated_duration_sec: int = 360


class ToolchainOut(BaseModel):
    id: str
    display_name: str
    supported_targets: list[str]
    cost_per_minute_credits: int
    estimated_duration_sec: int
    description: str


# ===== Endpoints =====

@router.get("/toolchains", response_model=list[ToolchainOut])
async def list_toolchains(db: AsyncSession = Depends(get_db)):
    """List all supported build toolchains.

    Khách pick 1 toolchain → Build Farm pull image + run build trong cloud.
    """
    rows = (await db.execute(text(
        "SELECT id, display_name, supported_targets, cost_per_minute_credits, "
        "estimated_duration_sec, description FROM build_farm_toolchains "
        "WHERE is_active = TRUE ORDER BY display_name"
    ))).mappings().all()
    return [
        ToolchainOut(
            id=r["id"],
            display_name=r["display_name"],
            supported_targets=r["supported_targets"] if isinstance(r["supported_targets"], list) else json.loads(r["supported_targets"] or "[]"),
            cost_per_minute_credits=r["cost_per_minute_credits"],
            estimated_duration_sec=r["estimated_duration_sec"],
            description=r["description"] or "",
        )
        for r in rows
    ]


@router.post("/jobs", response_model=BuildJobOut, status_code=202)
async def create_build_job(
    data: BuildJobCreate,
    bg: BackgroundTasks,
    ws: str = Query(..., description="workspace_id"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Submit native build job. Returns job_id + poll_url.

    AI agent integration:
      curl -X POST https://zenicloud.io/api/v1/build-farm/jobs?ws=myws \\
        -H "Authorization: Bearer $ZENI_TOKEN" \\
        -d '{"toolchain":"tauri-latest","source_type":"zip","source_ref":"gs://...","target_platforms":["windows-x64","linux-x64"]}'
    """
    await require_workspace_access(ws, me)
    # Validate toolchain
    tc_row = (await db.execute(text(
        "SELECT id, supported_targets, estimated_duration_sec, cost_per_minute_credits, base_image "
        "FROM build_farm_toolchains WHERE id = :id AND is_active = TRUE"
    ), {"id": data.toolchain})).mappings().first()
    if not tc_row:
        raise HTTPException(404, f"Toolchain '{data.toolchain}' not found. Try GET /build-farm/toolchains")

    supported = tc_row["supported_targets"] if isinstance(tc_row["supported_targets"], list) else json.loads(tc_row["supported_targets"] or "[]")
    invalid = [p for p in data.target_platforms if p not in supported]
    if invalid:
        raise HTTPException(422, f"Toolchain '{data.toolchain}' does not support platforms: {invalid}. Supported: {supported}")

    # Check quota
    quota = (await db.execute(text(
        "SELECT max_concurrent, max_minutes_per_month, used_minutes_this_month "
        "FROM build_farm_quotas WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not quota:
        # First time → seed default
        await db.execute(text(
            "INSERT INTO build_farm_quotas (workspace_id) VALUES (:ws) ON CONFLICT DO NOTHING"
        ), {"ws": ws})
        max_concurrent = 2
        max_min = 500
        used = 0
    else:
        max_concurrent = quota["max_concurrent"]
        max_min = quota["max_minutes_per_month"]
        used = quota["used_minutes_this_month"]

    if used >= max_min:
        raise HTTPException(429, f"Monthly build quota exceeded ({used}/{max_min} min). Upgrade workspace plan or wait until reset.")

    # Check concurrent
    running_count = (await db.execute(text(
        "SELECT COUNT(*) FROM build_jobs WHERE workspace_id = :ws AND status IN ('queued','running')"
    ), {"ws": ws})).scalar() or 0
    if running_count >= max_concurrent:
        raise HTTPException(429, f"Too many concurrent builds ({running_count}/{max_concurrent}). Wait for one to finish.")

    # Create job
    job_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    await db.execute(text(
        "INSERT INTO build_jobs (id, workspace_id, user_id, job_type, source_type, source_ref, "
        "target_platforms, build_config, status, expires_at) "
        "VALUES (:id, :ws, :uid, :jt, :st, :sr, CAST(:tp AS jsonb), CAST(:bc AS jsonb), 'queued', :exp)"
    ), {
        "id": str(job_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "jt": data.toolchain,
        "st": data.source_type,
        "sr": data.source_ref,
        "tp": json.dumps(data.target_platforms),
        "bc": json.dumps(data.build_config),
        "exp": expires_at,
    })
    await db.commit()

    # Schedule real Phase 2 worker (Cloud Build submit + poll + artifact upload)
    try:
        from app.services.build_farm_worker import run_build_job
        bg.add_task(run_build_job, str(job_id))
    except Exception as e:
        log.warning("build_farm_worker not available, using stub: %s", e)
        bg.add_task(_stub_build_worker, str(job_id))

    return BuildJobOut(
        id=str(job_id),
        workspace_id=ws,
        job_type=data.toolchain,
        status="queued",
        target_platforms=data.target_platforms,
        artifact_urls=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        estimated_duration_sec=tc_row["estimated_duration_sec"],
        poll_url=f"/api/v1/build-farm/jobs/{job_id}?ws={ws}",
    )


@router.get("/jobs", response_model=list[BuildJobOut])
async def list_build_jobs(
    ws: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, workspace_id, job_type, status, target_platforms, artifact_urls, created_at, "
        "started_at, finished_at, build_duration_sec, cost_credits, error_message "
        "FROM build_jobs WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT :lim"
    ), {"ws": ws, "lim": limit})).mappings().all()
    return [_row_to_job_out(r, ws) for r in rows]


@router.get("/jobs/{job_id}", response_model=BuildJobOut)
async def get_build_job(
    job_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, job_type, status, target_platforms, artifact_urls, created_at, "
        "started_at, finished_at, build_duration_sec, cost_credits, error_message "
        "FROM build_jobs WHERE id = :id AND workspace_id = :ws"
    ), {"id": job_id, "ws": ws})).mappings().first()
    if not r:
        raise HTTPException(404, "Build job not found")
    return _row_to_job_out(r, ws)


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_build_job(
    job_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "UPDATE build_jobs SET status = 'cancelled', finished_at = NOW() "
        "WHERE id = :id AND workspace_id = :ws AND status IN ('queued','running') "
        "RETURNING id"
    ), {"id": job_id, "ws": ws})).first()
    await db.commit()
    if not r:
        raise HTTPException(404, "Build job not found or already finished")
    return {"id": job_id, "status": "cancelled"}


@router.get("/quotas")
async def get_quota(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT max_concurrent, max_minutes_per_month, used_minutes_this_month, reset_at "
        "FROM build_farm_quotas WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not r:
        return {
            "workspace_id": ws,
            "max_concurrent": 2,
            "max_minutes_per_month": 500,
            "used_minutes_this_month": 0,
            "reset_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "tier": "free",
        }
    return {
        "workspace_id": ws,
        "max_concurrent": r["max_concurrent"],
        "max_minutes_per_month": r["max_minutes_per_month"],
        "used_minutes_this_month": r["used_minutes_this_month"],
        "reset_at": r["reset_at"].isoformat() if r["reset_at"] else None,
        "tier": "free" if r["max_minutes_per_month"] <= 500 else "pro",
    }


def _row_to_job_out(r, ws: str) -> BuildJobOut:
    return BuildJobOut(
        id=str(r["id"]),
        workspace_id=ws,
        job_type=r["job_type"],
        status=r["status"],
        target_platforms=r["target_platforms"] if isinstance(r["target_platforms"], list) else json.loads(r["target_platforms"] or "[]"),
        artifact_urls=r["artifact_urls"] if isinstance(r["artifact_urls"], list) else json.loads(r["artifact_urls"] or "[]"),
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
        started_at=r["started_at"].isoformat() if r["started_at"] else None,
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        build_duration_sec=r["build_duration_sec"] or 0,
        cost_credits=r["cost_credits"] or 0,
        error_message=r["error_message"],
        poll_url=f"/api/v1/build-farm/jobs/{r['id']}?ws={ws}",
        estimated_duration_sec=360,
    )


async def _stub_build_worker(job_id: str) -> None:
    """STUB: Background build worker. Full implementation pending in services/build_farm_worker.py.

    TODO Phase 2:
      1. Pull source from GCS / GitHub
      2. Submit Cloud Build with toolchain image (tauri/rust/electron)
      3. Run cargo tauri build / npm run electron-builder
      4. Upload artifacts to GCS bucket
      5. Generate signed URLs (24h expiry)
      6. Update build_jobs.status = 'success' + artifact_urls
    """
    log.info("[BUILD_FARM_STUB] Job %s queued — Phase 2 worker pending", job_id)
