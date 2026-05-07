"""
Zeni Cloud Core — Multi-Region Deployment + Auto-Scaling API (Sprint A5).

Routes (prefix `/multi-region`):
  ── Regions ────────────────────────────────────────────────────────────────
    GET    /regions                         (public)         list regions
    GET    /regions/{code}/availability                       region health/capacity

  ── Multi-region deployments ───────────────────────────────────────────────
    POST   /projects/{id}/regions?ws=                         deploy to region
    GET    /projects/{id}/regions?ws=                         list deployments
    PATCH  /projects/{id}/regions/{rid}?ws=                   adjust traffic %
    DELETE /projects/{id}/regions/{rid}?ws=                   remove deployment

  ── Traffic policies ───────────────────────────────────────────────────────
    POST   /projects/{id}/traffic?ws=                         create policy
    GET    /projects/{id}/traffic?ws=                         list policies
    PATCH  /projects/{id}/traffic/{tid}?ws=                   update policy

  ── Canary deployment ──────────────────────────────────────────────────────
    POST   /projects/{id}/canary?ws=                          start canary
    GET    /projects/{id}/canary?ws=                          status
    POST   /projects/{id}/canary/promote?ws=                  100% to canary
    POST   /projects/{id}/canary/rollback?ws=                 back to stable

  ── Auto-scaling ───────────────────────────────────────────────────────────
    POST   /projects/{id}/scaling?ws=                         create policy
    GET    /projects/{id}/scaling?ws=                         list policies
    PATCH  /projects/{id}/scaling/{sid}?ws=                   update policy
    DELETE /projects/{id}/scaling/{sid}?ws=                   delete policy
    GET    /projects/{id}/scaling/events?ws=                  recent scale events

  ── Health checks ──────────────────────────────────────────────────────────
    GET    /projects/{id}/health?ws=                          all-region health
    POST   /projects/{id}/health/probe?ws=                    manual probe trigger

All non-public endpoints require the standard `get_current_user` dep and
`require_workspace_access(ws)`. Mutation endpoints write `audit_push` rows
keyed `multi_region.*`.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.services.audit import audit_push
from app.services.cloud_run import CloudRunError, delete_service
from app.services.multi_region_engine import (
    apply_traffic_policy,
    deploy_to_region,
    evaluate_scaling,
    health_check_all_regions,
    run_canary_ramp,
)

log = logging.getLogger("zeni.api.multi_region")

router = APIRouter(prefix="/multi-region", tags=["multi-region"])


# ─── Pydantic v2 schemas ───────────────────────────────────────────────────
class RegionOut(BaseModel):
    id: int
    code: str
    name: str
    country: str
    gcp_region: str
    latency_ms_from_vn: int
    available_for_tier: list[str]
    enabled: bool


class RegionAvailabilityOut(BaseModel):
    code: str
    enabled: bool
    healthy: bool
    active_deployments: int
    average_latency_ms: float | None = None


class DeployRegionIn(BaseModel):
    region_code: str = Field(min_length=2, max_length=48)
    traffic_percent: int = Field(default=100, ge=0, le=100)


class DeployRegionOut(BaseModel):
    deployment_id: UUID
    region_code: str
    cloud_run_service_url: str | None
    revision: str | None
    status: str
    traffic_percent: int


class DeploymentOut(BaseModel):
    id: UUID
    project_id: UUID
    region_code: str
    cloud_run_service_url: str | None
    cloud_run_service_name: str | None
    revision: str | None
    status: str
    traffic_percent: int
    deployed_at: str | None
    deployed_by: str | None


class PatchTrafficIn(BaseModel):
    traffic_percent: int = Field(ge=0, le=100)


class TrafficPolicyIn(BaseModel):
    policy_type: str = Field(pattern="^(geo|percent|canary|blue_green)$")
    routing_rules: dict[str, Any] = Field(default_factory=dict)


class TrafficPolicyOut(BaseModel):
    id: UUID
    project_id: UUID
    policy_type: str
    routing_rules: dict[str, Any]
    active: bool
    created_at: str
    created_by: str | None


class CanaryStartIn(BaseModel):
    new_image: str | None = None                    # informational only — image lives on `projects` row
    stable_region: str = Field(min_length=2)
    canary_region: str = Field(min_length=2)
    percent_start: int = Field(default=10, ge=1, le=99)
    ramp_schedule: list[dict[str, Any]] = Field(default_factory=list)
    # ramp_schedule: [{"at":"+15m","pct":25}, {"at":"+1h","pct":50}, {"at":"+2h","pct":100}]


class ScalingPolicyIn(BaseModel):
    region_code: str | None = None
    policy_type: str = Field(pattern="^(cpu|memory|rps|queue_depth|schedule)$")
    threshold_value: float | None = None
    scale_up_step: int = Field(default=1, ge=1, le=20)
    scale_down_step: int = Field(default=1, ge=1, le=20)
    min_instances: int = Field(default=0, ge=0, le=100)
    max_instances: int = Field(default=10, ge=1, le=200)
    cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    cron_schedule: str | None = None
    enabled: bool = True


class ScalingPolicyPatch(BaseModel):
    threshold_value: float | None = None
    scale_up_step: int | None = None
    scale_down_step: int | None = None
    min_instances: int | None = None
    max_instances: int | None = None
    cooldown_seconds: int | None = None
    cron_schedule: str | None = None
    enabled: bool | None = None


class ScalingPolicyOut(BaseModel):
    id: UUID
    project_id: UUID
    region_code: str | None
    policy_type: str
    threshold_value: float | None
    scale_up_step: int
    scale_down_step: int
    min_instances: int
    max_instances: int
    cooldown_seconds: int
    cron_schedule: str | None
    enabled: bool
    created_at: str


class ScalingEventOut(BaseModel):
    id: int
    region_code: str | None
    policy_id: UUID | None
    event_type: str
    trigger_metric: str | None
    trigger_value: float | None
    instances_before: int
    instances_after: int
    reason: str | None
    occurred_at: str


class HealthRegionOut(BaseModel):
    region_code: str
    healthy: bool
    status_code: int | None
    latency_ms: int | None
    checked_at: str


# ─── Helpers ───────────────────────────────────────────────────────────────
async def _ensure_project(db: AsyncSession, project_id: UUID, ws: str) -> dict[str, Any]:
    row = (await db.execute(text(
        "SELECT id, name, workspace_id, image, size FROM projects "
        "WHERE id = :pid AND workspace_id = :ws"
    ), {"pid": str(project_id), "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="project không tồn tại trong workspace")
    return dict(row)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


# ─── Regions ───────────────────────────────────────────────────────────────
@router.get("/regions", response_model=list[RegionOut])
async def list_regions(db: AsyncSession = Depends(get_db)) -> list[RegionOut]:
    """Public — list all enabled regions with latency/tier metadata."""
    rows = (await db.execute(text(
        "SELECT id, code, name, country, gcp_region, latency_ms_from_vn, "
        "       available_for_tier, enabled "
        "FROM regions WHERE enabled = TRUE ORDER BY latency_ms_from_vn ASC"
    ))).mappings().all()
    return [
        RegionOut(
            id=r["id"], code=r["code"], name=r["name"], country=r["country"],
            gcp_region=r["gcp_region"], latency_ms_from_vn=r["latency_ms_from_vn"],
            available_for_tier=list(r["available_for_tier"] or []),
            enabled=bool(r["enabled"]),
        )
        for r in rows
    ]


@router.get("/regions/{code}/availability", response_model=RegionAvailabilityOut)
async def region_availability(code: str, db: AsyncSession = Depends(get_db)) -> RegionAvailabilityOut:
    region = (await db.execute(text(
        "SELECT id, code, enabled FROM regions WHERE code = :c"
    ), {"c": code})).mappings().first()
    if not region:
        raise HTTPException(status_code=404, detail="region not found")

    counts = (await db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM project_deployments
            WHERE region_id = :rid AND status = 'running')                              AS active,
          (SELECT AVG(latency_ms)::float FROM health_check_results
            WHERE region_id = :rid AND checked_at > NOW() - INTERVAL '1 hour')          AS avg_latency,
          (SELECT BOOL_OR(healthy) FROM health_check_results
            WHERE region_id = :rid AND checked_at > NOW() - INTERVAL '5 minutes')       AS healthy
    """), {"rid": region["id"]})).mappings().first()

    return RegionAvailabilityOut(
        code=region["code"],
        enabled=bool(region["enabled"]),
        healthy=bool(counts["healthy"]) if counts and counts["healthy"] is not None else True,
        active_deployments=int(counts["active"] or 0),
        average_latency_ms=float(counts["avg_latency"]) if counts and counts["avg_latency"] is not None else None,
    )


# ─── Multi-region deployments ──────────────────────────────────────────────
@router.post("/projects/{project_id}/regions", response_model=DeployRegionOut, status_code=202)
async def deploy_project_to_region(
    project_id: UUID,
    data: DeployRegionIn,
    bg: BackgroundTasks,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeployRegionOut:
    """Async deploy to additional region. Returns 202; deploy runs in background."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể deploy")
    project = await _ensure_project(db, project_id, ws)

    # Insert pending row immediately (so subsequent GET sees `deploying`)
    region_row = (await db.execute(text(
        "SELECT id, code FROM regions WHERE code = :c AND enabled = TRUE"
    ), {"c": data.region_code})).mappings().first()
    if not region_row:
        raise HTTPException(status_code=400, detail=f"region {data.region_code} không khả dụng")

    upserted = (await db.execute(text("""
        INSERT INTO project_deployments
            (project_id, region_id, status, traffic_percent, deployed_by)
        VALUES (:pid, :rid, 'deploying', :pct, :by)
        ON CONFLICT (project_id, region_id) DO UPDATE
        SET status = 'deploying',
            traffic_percent = EXCLUDED.traffic_percent,
            deployed_by = EXCLUDED.deployed_by,
            updated_at = NOW()
        RETURNING id
    """), {
        "pid": str(project_id),
        "rid": region_row["id"],
        "pct": data.traffic_percent,
        "by": me.email,
    })).mappings().first()
    deployment_id = UUID(str(upserted["id"]))

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.deploy.requested",
        target=f"{project['name']}@{data.region_code}",
        severity="info",
        metadata={"region": data.region_code, "traffic_percent": data.traffic_percent},
    )
    await db.commit()

    bg.add_task(
        _bg_deploy_region,
        project_id=project_id,
        ws=ws,
        region_code=data.region_code,
        traffic_percent=data.traffic_percent,
        actor_email=me.email,
        project_name=project["name"],
    )

    return DeployRegionOut(
        deployment_id=deployment_id,
        region_code=data.region_code,
        cloud_run_service_url=None,
        revision=None,
        status="deploying",
        traffic_percent=data.traffic_percent,
    )


async def _bg_deploy_region(
    *, project_id: UUID, ws: str, region_code: str,
    traffic_percent: int, actor_email: str, project_name: str,
) -> None:
    async with SessionLocal() as db:
        try:
            result = await deploy_to_region(
                db, project_id=project_id, workspace_id=ws,
                region_code=region_code, traffic_percent=traffic_percent,
                actor_email=actor_email,
            )
            await audit_push(
                db, actor=actor_email, workspace_id=ws,
                action="multi_region.deploy",
                target=f"{project_name}@{region_code}",
                severity="ok",
                metadata={"region": region_code, "url": result.cloud_run_url or "",
                          "revision": result.revision or ""},
            )
            await db.commit()
        except (CloudRunError, ValueError) as e:
            log.exception("[bg_deploy_region] failed: %s", e)
            await audit_push(
                db, actor=actor_email, workspace_id=ws,
                action="multi_region.deploy.failed",
                target=f"{project_name}@{region_code}",
                severity="err",
                metadata={"error": str(e), "region": region_code},
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("[bg_deploy_region] unexpected error: %s", e)


@router.get("/projects/{project_id}/regions", response_model=list[DeploymentOut])
async def list_project_deployments(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeploymentOut]:
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    rows = (await db.execute(text("""
        SELECT d.id, d.project_id, r.code AS region_code,
               d.cloud_run_service_url, d.cloud_run_service_name, d.revision,
               d.status, d.traffic_percent, d.deployed_at, d.deployed_by
          FROM project_deployments d
          JOIN regions r ON r.id = d.region_id
         WHERE d.project_id = :pid
         ORDER BY d.created_at DESC
    """), {"pid": str(project_id)})).mappings().all()
    return [
        DeploymentOut(
            id=UUID(str(r["id"])),
            project_id=UUID(str(r["project_id"])),
            region_code=r["region_code"],
            cloud_run_service_url=r["cloud_run_service_url"],
            cloud_run_service_name=r["cloud_run_service_name"],
            revision=r["revision"],
            status=r["status"],
            traffic_percent=int(r["traffic_percent"] or 0),
            deployed_at=_iso(r["deployed_at"]),
            deployed_by=r["deployed_by"],
        )
        for r in rows
    ]


@router.patch("/projects/{project_id}/regions/{rid}", response_model=DeploymentOut)
async def update_deployment_traffic(
    project_id: UUID,
    rid: UUID,
    data: PatchTrafficIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeploymentOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể chỉnh traffic")
    await _ensure_project(db, project_id, ws)

    row = (await db.execute(text("""
        UPDATE project_deployments d SET traffic_percent = :pct, updated_at = NOW()
          FROM regions r
         WHERE d.region_id = r.id
           AND d.id = :did AND d.project_id = :pid
        RETURNING d.id, d.project_id, r.code AS region_code,
                  d.cloud_run_service_url, d.cloud_run_service_name, d.revision,
                  d.status, d.traffic_percent, d.deployed_at, d.deployed_by
    """), {"did": str(rid), "pid": str(project_id), "pct": data.traffic_percent})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="deployment not found")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.traffic.update", target=f"{project_id}@{row['region_code']}",
        severity="info", metadata={"traffic_percent": data.traffic_percent},
    )
    await db.commit()
    return DeploymentOut(
        id=UUID(str(row["id"])), project_id=UUID(str(row["project_id"])),
        region_code=row["region_code"],
        cloud_run_service_url=row["cloud_run_service_url"],
        cloud_run_service_name=row["cloud_run_service_name"],
        revision=row["revision"], status=row["status"],
        traffic_percent=int(row["traffic_percent"] or 0),
        deployed_at=_iso(row["deployed_at"]), deployed_by=row["deployed_by"],
    )


@router.delete("/projects/{project_id}/regions/{rid}", status_code=204, response_class=Response)
async def remove_region_deployment(
    project_id: UUID,
    rid: UUID,
    bg: BackgroundTasks,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    project = await _ensure_project(db, project_id, ws)

    row = (await db.execute(text("""
        SELECT d.id, r.code AS region_code, r.gcp_region, d.cloud_run_service_name
          FROM project_deployments d
          JOIN regions r ON r.id = d.region_id
         WHERE d.id = :did AND d.project_id = :pid
    """), {"did": str(rid), "pid": str(project_id)})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="deployment not found")

    await db.execute(text(
        "DELETE FROM project_deployments WHERE id = :did"
    ), {"did": str(rid)})
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.deploy.remove",
        target=f"{project['name']}@{row['region_code']}", severity="warn",
        metadata={"region": row["region_code"]},
    )
    await db.commit()

    bg.add_task(
        _bg_delete_cloudrun, ws=ws, project_name=project["name"],
        region=row["gcp_region"],
    )
    return Response(status_code=204)


async def _bg_delete_cloudrun(*, ws: str, project_name: str, region: str) -> None:
    try:
        await delete_service(workspace=ws, project_name=project_name, region=region)
    except CloudRunError as e:
        log.warning("[bg_delete_cloudrun] %s/%s in %s: %s", ws, project_name, region, e)


# ─── Traffic policies ──────────────────────────────────────────────────────
@router.post("/projects/{project_id}/traffic", response_model=TrafficPolicyOut)
async def create_traffic_policy(
    project_id: UUID,
    data: TrafficPolicyIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrafficPolicyOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể tạo traffic policy")
    await _ensure_project(db, project_id, ws)

    try:
        policy_id = await apply_traffic_policy(
            db, project_id=project_id, policy_type=data.policy_type,
            routing_rules=data.routing_rules, created_by=me.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    row = (await db.execute(text(
        "SELECT id, project_id, policy_type, routing_rules, active, created_at, created_by "
        "FROM traffic_policies WHERE id = :id"
    ), {"id": str(policy_id)})).mappings().first()

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.traffic_policy.create",
        target=str(project_id), severity="info",
        metadata={"policy_type": data.policy_type, "rules": data.routing_rules},
    )
    await db.commit()

    return TrafficPolicyOut(
        id=UUID(str(row["id"])), project_id=UUID(str(row["project_id"])),
        policy_type=row["policy_type"], routing_rules=dict(row["routing_rules"] or {}),
        active=bool(row["active"]), created_at=_iso(row["created_at"]) or "",
        created_by=row["created_by"],
    )


@router.get("/projects/{project_id}/traffic", response_model=list[TrafficPolicyOut])
async def list_traffic_policies(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TrafficPolicyOut]:
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    rows = (await db.execute(text(
        "SELECT id, project_id, policy_type, routing_rules, active, created_at, created_by "
        "FROM traffic_policies WHERE project_id = :pid ORDER BY created_at DESC"
    ), {"pid": str(project_id)})).mappings().all()
    return [
        TrafficPolicyOut(
            id=UUID(str(r["id"])), project_id=UUID(str(r["project_id"])),
            policy_type=r["policy_type"], routing_rules=dict(r["routing_rules"] or {}),
            active=bool(r["active"]), created_at=_iso(r["created_at"]) or "",
            created_by=r["created_by"],
        )
        for r in rows
    ]


@router.patch("/projects/{project_id}/traffic/{tid}", response_model=TrafficPolicyOut)
async def update_traffic_policy(
    project_id: UUID,
    tid: UUID,
    data: TrafficPolicyIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrafficPolicyOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể sửa traffic policy")

    row = (await db.execute(text(
        "SELECT id FROM traffic_policies WHERE id = :tid AND project_id = :pid"
    ), {"tid": str(tid), "pid": str(project_id)})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="policy not found")

    import json
    await db.execute(text("""
        UPDATE traffic_policies
           SET policy_type = :pt, routing_rules = CAST(:rules AS JSONB), updated_at = NOW()
         WHERE id = :tid
    """), {"tid": str(tid), "pt": data.policy_type, "rules": json.dumps(data.routing_rules, default=str)})
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.traffic_policy.update",
        target=str(project_id), severity="info",
        metadata={"policy_type": data.policy_type},
    )
    await db.commit()

    out = (await db.execute(text(
        "SELECT id, project_id, policy_type, routing_rules, active, created_at, created_by "
        "FROM traffic_policies WHERE id = :tid"
    ), {"tid": str(tid)})).mappings().first()
    return TrafficPolicyOut(
        id=UUID(str(out["id"])), project_id=UUID(str(out["project_id"])),
        policy_type=out["policy_type"], routing_rules=dict(out["routing_rules"] or {}),
        active=bool(out["active"]), created_at=_iso(out["created_at"]) or "",
        created_by=out["created_by"],
    )


# ─── Canary ────────────────────────────────────────────────────────────────
@router.post("/projects/{project_id}/canary", response_model=TrafficPolicyOut)
async def start_canary(
    project_id: UUID,
    data: CanaryStartIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrafficPolicyOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể tạo canary")
    await _ensure_project(db, project_id, ws)

    rules = {
        "stable_region": data.stable_region,
        "canary_region": data.canary_region,
        "canary_percent": data.percent_start,
        "ramp": data.ramp_schedule,
    }
    if data.new_image:
        rules["new_image"] = data.new_image
    try:
        policy_id = await apply_traffic_policy(
            db, project_id=project_id, policy_type="canary",
            routing_rules=rules, created_by=me.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.canary.start", target=str(project_id),
        severity="info",
        metadata={"stable": data.stable_region, "canary": data.canary_region,
                  "percent_start": data.percent_start},
    )
    await db.commit()

    row = (await db.execute(text(
        "SELECT id, project_id, policy_type, routing_rules, active, created_at, created_by "
        "FROM traffic_policies WHERE id = :id"
    ), {"id": str(policy_id)})).mappings().first()
    return TrafficPolicyOut(
        id=UUID(str(row["id"])), project_id=UUID(str(row["project_id"])),
        policy_type=row["policy_type"], routing_rules=dict(row["routing_rules"] or {}),
        active=bool(row["active"]), created_at=_iso(row["created_at"]) or "",
        created_by=row["created_by"],
    )


@router.get("/projects/{project_id}/canary")
async def canary_status(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    return await run_canary_ramp(db, project_id=project_id)


@router.post("/projects/{project_id}/canary/promote")
async def canary_promote(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể promote")
    await _ensure_project(db, project_id, ws)

    pol = (await db.execute(text("""
        SELECT id, routing_rules FROM traffic_policies
         WHERE project_id = :pid AND active = TRUE AND policy_type = 'canary'
         ORDER BY created_at DESC LIMIT 1
    """), {"pid": str(project_id)})).mappings().first()
    if not pol:
        raise HTTPException(status_code=404, detail="No active canary")

    rules = dict(pol["routing_rules"] or {})
    rules["canary_percent"] = 100
    canary_region = rules.get("canary_region")
    stable_region = rules.get("stable_region")

    import json
    await db.execute(text("""
        UPDATE traffic_policies SET routing_rules = CAST(:rules AS JSONB),
               updated_at = NOW(), active = FALSE
         WHERE id = :id
    """), {"id": str(pol["id"]), "rules": json.dumps(rules, default=str)})
    if canary_region:
        await db.execute(text("""
            UPDATE project_deployments d SET traffic_percent = 100, updated_at = NOW()
              FROM regions r WHERE d.region_id = r.id
               AND r.code = :c AND d.project_id = :pid
        """), {"c": canary_region, "pid": str(project_id)})
    if stable_region:
        await db.execute(text("""
            UPDATE project_deployments d SET traffic_percent = 0, updated_at = NOW()
              FROM regions r WHERE d.region_id = r.id
               AND r.code = :c AND d.project_id = :pid
        """), {"c": stable_region, "pid": str(project_id)})

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.canary.promote", target=str(project_id),
        severity="ok",
        metadata={"canary_region": canary_region, "stable_region": stable_region},
    )
    await db.commit()
    return {"ok": True, "canary_region": canary_region, "promoted": True}


@router.post("/projects/{project_id}/canary/rollback")
async def canary_rollback(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể rollback")
    await _ensure_project(db, project_id, ws)

    pol = (await db.execute(text("""
        SELECT id, routing_rules FROM traffic_policies
         WHERE project_id = :pid AND active = TRUE AND policy_type = 'canary'
         ORDER BY created_at DESC LIMIT 1
    """), {"pid": str(project_id)})).mappings().first()
    if not pol:
        raise HTTPException(status_code=404, detail="No active canary")

    rules = dict(pol["routing_rules"] or {})
    rules["canary_percent"] = 0
    canary_region = rules.get("canary_region")
    stable_region = rules.get("stable_region")

    import json
    await db.execute(text("""
        UPDATE traffic_policies SET routing_rules = CAST(:rules AS JSONB),
               updated_at = NOW(), active = FALSE
         WHERE id = :id
    """), {"id": str(pol["id"]), "rules": json.dumps(rules, default=str)})
    if stable_region:
        await db.execute(text("""
            UPDATE project_deployments d SET traffic_percent = 100, updated_at = NOW()
              FROM regions r WHERE d.region_id = r.id
               AND r.code = :c AND d.project_id = :pid
        """), {"c": stable_region, "pid": str(project_id)})
    if canary_region:
        await db.execute(text("""
            UPDATE project_deployments d SET traffic_percent = 0, updated_at = NOW()
              FROM regions r WHERE d.region_id = r.id
               AND r.code = :c AND d.project_id = :pid
        """), {"c": canary_region, "pid": str(project_id)})

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.canary.rollback", target=str(project_id),
        severity="warn",
        metadata={"canary_region": canary_region, "stable_region": stable_region},
    )
    await db.commit()
    return {"ok": True, "stable_region": stable_region, "rolled_back": True}


# ─── Auto-scaling policies ─────────────────────────────────────────────────
@router.post("/projects/{project_id}/scaling", response_model=ScalingPolicyOut)
async def create_scaling_policy(
    project_id: UUID,
    data: ScalingPolicyIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScalingPolicyOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể tạo scaling policy")
    await _ensure_project(db, project_id, ws)

    if data.min_instances > data.max_instances:
        raise HTTPException(status_code=400, detail="min_instances > max_instances")

    region_id: int | None = None
    if data.region_code:
        rr = (await db.execute(text(
            "SELECT id FROM regions WHERE code = :c"
        ), {"c": data.region_code})).mappings().first()
        if not rr:
            raise HTTPException(status_code=400, detail=f"region {data.region_code} not found")
        region_id = rr["id"]

    inserted = (await db.execute(text("""
        INSERT INTO scaling_policies
            (project_id, region_id, policy_type, threshold_value,
             scale_up_step, scale_down_step, min_instances, max_instances,
             cooldown_seconds, cron_schedule, enabled, created_by)
        VALUES (:pid, :rid, :pt, :th, :su, :sd, :mn, :mx, :cd, :cron, :en, :by)
        RETURNING id
    """), {
        "pid": str(project_id), "rid": region_id, "pt": data.policy_type,
        "th": data.threshold_value, "su": data.scale_up_step, "sd": data.scale_down_step,
        "mn": data.min_instances, "mx": data.max_instances,
        "cd": data.cooldown_seconds, "cron": data.cron_schedule,
        "en": data.enabled, "by": me.email,
    })).mappings().first()
    sid = UUID(str(inserted["id"]))

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.scaling.create",
        target=str(project_id), severity="info",
        metadata={"policy_type": data.policy_type, "threshold": data.threshold_value,
                  "min": data.min_instances, "max": data.max_instances},
    )
    await db.commit()

    return await _scaling_policy_out(db, sid)


@router.get("/projects/{project_id}/scaling", response_model=list[ScalingPolicyOut])
async def list_scaling_policies(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ScalingPolicyOut]:
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    rows = (await db.execute(text("""
        SELECT s.id, s.project_id, r.code AS region_code, s.policy_type,
               s.threshold_value, s.scale_up_step, s.scale_down_step,
               s.min_instances, s.max_instances, s.cooldown_seconds,
               s.cron_schedule, s.enabled, s.created_at
          FROM scaling_policies s
          LEFT JOIN regions r ON r.id = s.region_id
         WHERE s.project_id = :pid
         ORDER BY s.created_at DESC
    """), {"pid": str(project_id)})).mappings().all()
    return [
        ScalingPolicyOut(
            id=UUID(str(r["id"])), project_id=UUID(str(r["project_id"])),
            region_code=r["region_code"], policy_type=r["policy_type"],
            threshold_value=float(r["threshold_value"]) if r["threshold_value"] is not None else None,
            scale_up_step=int(r["scale_up_step"]), scale_down_step=int(r["scale_down_step"]),
            min_instances=int(r["min_instances"]), max_instances=int(r["max_instances"]),
            cooldown_seconds=int(r["cooldown_seconds"]),
            cron_schedule=r["cron_schedule"], enabled=bool(r["enabled"]),
            created_at=_iso(r["created_at"]) or "",
        )
        for r in rows
    ]


@router.patch("/projects/{project_id}/scaling/{sid}", response_model=ScalingPolicyOut)
async def update_scaling_policy(
    project_id: UUID,
    sid: UUID,
    data: ScalingPolicyPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScalingPolicyOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể sửa scaling policy")

    row = (await db.execute(text(
        "SELECT id FROM scaling_policies WHERE id = :sid AND project_id = :pid"
    ), {"sid": str(sid), "pid": str(project_id)})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="scaling policy not found")

    fields: dict[str, Any] = data.model_dump(exclude_unset=True)
    if not fields:
        return await _scaling_policy_out(db, sid)

    set_parts = []
    params: dict[str, Any] = {"sid": str(sid)}
    for k, v in fields.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = v
    set_parts.append("updated_at = NOW()")
    sql = f"UPDATE scaling_policies SET {', '.join(set_parts)} WHERE id = :sid"
    await db.execute(text(sql), params)

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.scaling.update",
        target=str(project_id), severity="info",
        metadata={"sid": str(sid), **{k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool))}},
    )
    await db.commit()
    return await _scaling_policy_out(db, sid)


@router.delete("/projects/{project_id}/scaling/{sid}", status_code=204, response_class=Response)
async def delete_scaling_policy(
    project_id: UUID,
    sid: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    res = await db.execute(text(
        "DELETE FROM scaling_policies WHERE id = :sid AND project_id = :pid"
    ), {"sid": str(sid), "pid": str(project_id)})
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="scaling policy not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.scaling.delete",
        target=str(project_id), severity="warn", metadata={"sid": str(sid)},
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/scaling/events", response_model=list[ScalingEventOut])
async def list_scaling_events(
    project_id: UUID,
    ws: str = Query(...),
    limit: int = Query(default=50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ScalingEventOut]:
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    rows = (await db.execute(text("""
        SELECT e.id, r.code AS region_code, e.policy_id, e.event_type,
               e.trigger_metric, e.trigger_value,
               e.instances_before, e.instances_after, e.reason, e.occurred_at
          FROM scaling_events e
          LEFT JOIN regions r ON r.id = e.region_id
         WHERE e.project_id = :pid
         ORDER BY e.occurred_at DESC
         LIMIT :lim
    """), {"pid": str(project_id), "lim": limit})).mappings().all()
    return [
        ScalingEventOut(
            id=int(r["id"]),
            region_code=r["region_code"],
            policy_id=UUID(str(r["policy_id"])) if r["policy_id"] else None,
            event_type=r["event_type"],
            trigger_metric=r["trigger_metric"],
            trigger_value=float(r["trigger_value"]) if r["trigger_value"] is not None else None,
            instances_before=int(r["instances_before"]),
            instances_after=int(r["instances_after"]),
            reason=r["reason"],
            occurred_at=_iso(r["occurred_at"]) or "",
        )
        for r in rows
    ]


async def _scaling_policy_out(db: AsyncSession, sid: UUID) -> ScalingPolicyOut:
    r = (await db.execute(text("""
        SELECT s.id, s.project_id, r.code AS region_code, s.policy_type,
               s.threshold_value, s.scale_up_step, s.scale_down_step,
               s.min_instances, s.max_instances, s.cooldown_seconds,
               s.cron_schedule, s.enabled, s.created_at
          FROM scaling_policies s
          LEFT JOIN regions r ON r.id = s.region_id
         WHERE s.id = :sid
    """), {"sid": str(sid)})).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="scaling policy not found")
    return ScalingPolicyOut(
        id=UUID(str(r["id"])), project_id=UUID(str(r["project_id"])),
        region_code=r["region_code"], policy_type=r["policy_type"],
        threshold_value=float(r["threshold_value"]) if r["threshold_value"] is not None else None,
        scale_up_step=int(r["scale_up_step"]), scale_down_step=int(r["scale_down_step"]),
        min_instances=int(r["min_instances"]), max_instances=int(r["max_instances"]),
        cooldown_seconds=int(r["cooldown_seconds"]),
        cron_schedule=r["cron_schedule"], enabled=bool(r["enabled"]),
        created_at=_iso(r["created_at"]) or "",
    )


# ─── Health checks ─────────────────────────────────────────────────────────
@router.get("/projects/{project_id}/health", response_model=list[HealthRegionOut])
async def get_project_health(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HealthRegionOut]:
    """Latest health check per region (one row per region, freshest first)."""
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    rows = (await db.execute(text("""
        SELECT DISTINCT ON (h.region_id)
               r.code AS region_code, h.healthy, h.status_code,
               h.latency_ms, h.checked_at
          FROM health_check_results h
          JOIN regions r ON r.id = h.region_id
         WHERE h.project_id = :pid
         ORDER BY h.region_id, h.checked_at DESC
    """), {"pid": str(project_id)})).mappings().all()
    return [
        HealthRegionOut(
            region_code=r["region_code"], healthy=bool(r["healthy"]),
            status_code=r["status_code"], latency_ms=r["latency_ms"],
            checked_at=_iso(r["checked_at"]) or "",
        )
        for r in rows
    ]


@router.post("/projects/{project_id}/health/probe")
async def probe_project_health(
    project_id: UUID,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manually trigger a fan-out probe to every running region of the project."""
    await require_workspace_access(ws, me)
    await _ensure_project(db, project_id, ws)
    results = await health_check_all_regions(db, project_id=project_id)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="multi_region.health.probe", target=str(project_id),
        severity="info",
        metadata={"checked": len(results),
                  "unhealthy": sum(1 for r in results if not r.get("healthy"))},
    )
    await db.commit()
    return {"ok": True, "results": results}


# ─── Optional: scaling evaluation trigger (for cron/observability hooks) ───
@router.post("/projects/{project_id}/scaling/evaluate")
async def trigger_scaling_evaluation(
    project_id: UUID,
    ws: str = Query(...),
    metrics: dict[str, float] | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manually evaluate scaling rules against a metrics snapshot (admin/cron)."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể trigger evaluation")
    await _ensure_project(db, project_id, ws)
    decisions = await evaluate_scaling(db, project_id=project_id, metrics_snapshot=metrics)
    return {
        "ok": True,
        "decisions": [
            {
                "policy_id": str(d.policy_id),
                "event_type": d.event_type,
                "instances_before": d.instances_before,
                "instances_after": d.instances_after,
                "trigger_metric": d.trigger_metric,
                "trigger_value": d.trigger_value,
                "reason": d.reason,
            }
            for d in decisions
        ],
    }
