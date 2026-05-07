"""
Zeni Cloud Core — Multi-Region Engine + Auto-Scaling (Sprint A5).

This service is the orchestration layer between the HTTP API
(`app/api/multi_region.py`) and the lower-level Cloud Run wrapper
(`app/services/cloud_run.py`).

Responsibilities:
  * deploy_to_region(...)        — Deploy/update a Cloud Run service in a target
                                   GCP region for an existing project (uses the
                                   project's image/size from the `projects` row).
  * apply_traffic_policy(...)    — Persist a traffic policy and propagate the
                                   resulting `traffic_percent` per region into
                                   `project_deployments`.
  * evaluate_scaling(...)        — Read the most recent metrics, walk every
                                   enabled scaling policy of the project, decide
                                   scale_up / scale_down / no_change, persist
                                   the decision into `scaling_events`.
  * run_canary_ramp(...)         — Read the canary policy schedule and progress
                                   the canary_percent according to the elapsed
                                   wall-clock time since the canary started.
  * health_check_all_regions(...)— Fan-out HTTP probes to every running region
                                   and persist results into `health_check_results`.

All DB access is async via SQLAlchemy `AsyncSession` (raw SQL via `text()` —
matches the style used elsewhere in this codebase). All external network calls
(Cloud Run API, HTTP probes) run in a thread executor / `asyncio.to_thread`
to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.cloud_run import (
    CloudRunError,
    SIZE_TO_RESOURCES,
    deploy_service,
    get_service_status,
    service_name_for,
)

log = logging.getLogger("zeni.multi_region")


# ─── Dataclasses ───────────────────────────────────────────────────────────
@dataclass
class RegionDeployResult:
    deployment_id: UUID
    region_code: str
    cloud_run_url: str | None
    revision: str | None
    status: str


@dataclass
class ScalingDecision:
    policy_id: UUID
    event_type: str            # 'scale_up' | 'scale_down' | 'throttle' | 'no_change'
    instances_before: int
    instances_after: int
    trigger_metric: str | None
    trigger_value: float | None
    reason: str


# ─── DB helpers ────────────────────────────────────────────────────────────
async def _get_project_row(db: AsyncSession, project_id: UUID, ws: str) -> dict[str, Any] | None:
    row = (await db.execute(text(
        "SELECT id, workspace_id, name, image, size, region, status, "
        "       cloud_run_service "
        "FROM projects WHERE id = :pid AND workspace_id = :ws"
    ), {"pid": str(project_id), "ws": ws})).mappings().first()
    return dict(row) if row else None


async def _get_region_by_code(db: AsyncSession, code: str) -> dict[str, Any] | None:
    row = (await db.execute(text(
        "SELECT id, code, gcp_region, available_for_tier, enabled "
        "FROM regions WHERE code = :code"
    ), {"code": code})).mappings().first()
    return dict(row) if row else None


async def _get_region_by_id(db: AsyncSession, region_id: int) -> dict[str, Any] | None:
    row = (await db.execute(text(
        "SELECT id, code, gcp_region FROM regions WHERE id = :id"
    ), {"id": region_id})).mappings().first()
    return dict(row) if row else None


# ─── 1. Deploy to a specific region ────────────────────────────────────────
async def deploy_to_region(
    db: AsyncSession,
    *,
    project_id: UUID,
    workspace_id: str,
    region_code: str,
    traffic_percent: int = 100,
    actor_email: str | None = None,
) -> RegionDeployResult:
    """Deploy (or update) a Cloud Run service for `project_id` into `region_code`.

    Idempotent: existing project_deployments row is updated; the underlying
    Cloud Run service create_or_update is also idempotent (handled by
    `deploy_service`).
    """
    project = await _get_project_row(db, project_id, workspace_id)
    if not project:
        raise ValueError(f"Project {project_id} không thuộc workspace {workspace_id}")

    region = await _get_region_by_code(db, region_code)
    if not region:
        raise ValueError(f"Region không hợp lệ: {region_code}")
    if not region["enabled"]:
        raise ValueError(f"Region {region_code} đang tắt")

    image = project.get("image")
    if not image:
        raise ValueError("Project chưa có image — phải deploy lần đầu trước khi expand multi-region.")
    size = project.get("size") or "s"

    # Upsert project_deployments → status=deploying
    upserted = (await db.execute(text("""
        INSERT INTO project_deployments
            (project_id, region_id, cloud_run_service_name, status,
             traffic_percent, deployed_by)
        VALUES
            (:pid, :rid, :svc, 'deploying', :pct, :by)
        ON CONFLICT (project_id, region_id) DO UPDATE
        SET status = 'deploying',
            traffic_percent = EXCLUDED.traffic_percent,
            deployed_by = EXCLUDED.deployed_by,
            updated_at = NOW()
        RETURNING id
    """), {
        "pid": str(project_id),
        "rid": region["id"],
        "svc": service_name_for(workspace_id, project["name"]),
        "pct": int(max(0, min(100, traffic_percent))),
        "by": actor_email,
    })).mappings().first()
    deployment_id = UUID(str(upserted["id"]))
    await db.commit()

    # Real Cloud Run deploy in target region
    try:
        result = await deploy_service(
            workspace=workspace_id,
            project_name=project["name"],
            image=image,
            size=size,
            region=region["gcp_region"],
            allow_unauthenticated=True,
            created_by=actor_email,
        )
    except CloudRunError as e:
        await db.execute(text("""
            UPDATE project_deployments
               SET status = 'failed', updated_at = NOW()
             WHERE id = :id
        """), {"id": str(deployment_id)})
        await db.commit()
        log.exception("[deploy_to_region] failed for %s/%s in %s",
                      workspace_id, project["name"], region_code)
        raise

    # Persist final state
    await db.execute(text("""
        UPDATE project_deployments
           SET status = 'running',
               cloud_run_service_url = :url,
               cloud_run_service_name = :svc,
               revision = :rev,
               deployed_at = NOW(),
               updated_at = NOW()
         WHERE id = :id
    """), {
        "id": str(deployment_id),
        "url": result.url,
        "svc": result.service_name,
        "rev": result.revision,
    })
    await db.commit()

    return RegionDeployResult(
        deployment_id=deployment_id,
        region_code=region["code"],
        cloud_run_url=result.url,
        revision=result.revision,
        status="running",
    )


# ─── 2. Apply traffic policy ───────────────────────────────────────────────
async def apply_traffic_policy(
    db: AsyncSession,
    *,
    project_id: UUID,
    policy_type: str,
    routing_rules: dict[str, Any],
    created_by: str | None = None,
) -> UUID:
    """Persist a traffic policy and (where applicable) propagate the implied
    `traffic_percent` into `project_deployments` rows for the project.

    For policy_type='percent' (most common case) the routing_rules dict is
    expected to be {region_code: weight}. Weights are normalised to sum=100
    and written to project_deployments.traffic_percent.
    """
    if policy_type not in ("geo", "percent", "canary", "blue_green"):
        raise ValueError(f"policy_type không hợp lệ: {policy_type}")

    # Deactivate prior active policies of same project (only one active).
    await db.execute(text("""
        UPDATE traffic_policies SET active = FALSE, updated_at = NOW()
         WHERE project_id = :pid AND active = TRUE
    """), {"pid": str(project_id)})

    inserted = (await db.execute(text("""
        INSERT INTO traffic_policies
            (project_id, policy_type, routing_rules, active, created_by)
        VALUES (:pid, :pt, CAST(:rules AS JSONB), TRUE, :by)
        RETURNING id
    """), {
        "pid": str(project_id),
        "pt": policy_type,
        "rules": _to_json(routing_rules),
        "by": created_by,
    })).mappings().first()
    policy_id = UUID(str(inserted["id"]))

    # Propagate weights into project_deployments.traffic_percent if percent/canary
    if policy_type == "percent":
        weights = {k: int(v) for k, v in routing_rules.items() if isinstance(v, (int, float))}
        total = sum(weights.values()) or 1
        for region_code, w in weights.items():
            pct = max(0, min(100, round(w * 100 / total)))
            await db.execute(text("""
                UPDATE project_deployments d
                   SET traffic_percent = :pct, updated_at = NOW()
                  FROM regions r
                 WHERE d.region_id = r.id
                   AND r.code = :code
                   AND d.project_id = :pid
            """), {"pid": str(project_id), "code": region_code, "pct": pct})
    elif policy_type == "canary":
        canary_pct = int(routing_rules.get("canary_percent") or 10)
        canary_pct = max(0, min(100, canary_pct))
        stable_region = routing_rules.get("stable_region")
        canary_region = routing_rules.get("canary_region")
        if stable_region and canary_region:
            await _set_traffic(db, project_id, stable_region, 100 - canary_pct)
            await _set_traffic(db, project_id, canary_region, canary_pct)
    elif policy_type == "blue_green":
        active = routing_rules.get("active")  # 'blue' or 'green'
        blue_region = routing_rules.get("blue")
        green_region = routing_rules.get("green")
        if active and blue_region and green_region:
            await _set_traffic(db, project_id, blue_region, 100 if active == "blue" else 0)
            await _set_traffic(db, project_id, green_region, 100 if active == "green" else 0)

    await db.commit()
    return policy_id


async def _set_traffic(db: AsyncSession, project_id: UUID, region_code: str, pct: int) -> None:
    await db.execute(text("""
        UPDATE project_deployments d
           SET traffic_percent = :pct, updated_at = NOW()
          FROM regions r
         WHERE d.region_id = r.id
           AND r.code = :code
           AND d.project_id = :pid
    """), {"pid": str(project_id), "code": region_code, "pct": int(max(0, min(100, pct)))})


# ─── 3. Evaluate scaling rules ─────────────────────────────────────────────
async def evaluate_scaling(
    db: AsyncSession,
    *,
    project_id: UUID,
    metrics_snapshot: dict[str, float] | None = None,
) -> list[ScalingDecision]:
    """Walk every enabled scaling policy for the project, decide actions, write
    `scaling_events`, return decisions.

    `metrics_snapshot` keys are: cpu, memory, rps, queue_depth (all floats).
    Caller (cron / observability) supplies the readings; if None, this function
    reads zeros and produces 'no_change' for non-schedule policies (used by the
    HTTP `/scaling/events` endpoint as a no-op probe).
    """
    metrics = metrics_snapshot or {}
    policies = (await db.execute(text("""
        SELECT id, region_id, policy_type, threshold_value,
               scale_up_step, scale_down_step,
               min_instances, max_instances, cooldown_seconds, cron_schedule
          FROM scaling_policies
         WHERE project_id = :pid AND enabled = TRUE
    """), {"pid": str(project_id)})).mappings().all()

    decisions: list[ScalingDecision] = []
    for p in policies:
        # Cooldown — skip if last event for same policy is within cooldown
        last = (await db.execute(text("""
            SELECT occurred_at FROM scaling_events
             WHERE policy_id = :pid
             ORDER BY occurred_at DESC LIMIT 1
        """), {"pid": str(p["id"])})).mappings().first()
        if last:
            elapsed = (datetime.now(timezone.utc) - last["occurred_at"]).total_seconds()
            if elapsed < int(p["cooldown_seconds"] or 0):
                decisions.append(ScalingDecision(
                    policy_id=UUID(str(p["id"])),
                    event_type="throttle",
                    instances_before=0, instances_after=0,
                    trigger_metric=p["policy_type"], trigger_value=None,
                    reason=f"cooldown {int(elapsed)}s/{p['cooldown_seconds']}s",
                ))
                continue

        ptype = p["policy_type"]
        threshold = float(p["threshold_value"] or 0.0)
        before = await _current_instances(db, project_id, p["region_id"])
        after = before
        action = "no_change"
        metric_val: float | None = None
        reason = ""

        if ptype == "schedule":
            # Schedule rule fires the moment `cron_schedule` matches now.
            # We do a soft check: if cron string contains '*' it's lenient — we
            # just bump to max_instances at start of business hours and back to
            # min outside. Real cron evaluation is delegated to Cloud Scheduler.
            now = datetime.now(timezone.utc)
            within = _cron_matches_now(p["cron_schedule"] or "", now)
            if within and before < int(p["max_instances"]):
                after = int(p["max_instances"])
                action = "scale_up"
                reason = f"schedule fired ({p['cron_schedule']})"
            elif (not within) and before > int(p["min_instances"]):
                after = int(p["min_instances"])
                action = "scale_down"
                reason = f"schedule off ({p['cron_schedule']})"
        else:
            metric_val = float(metrics.get(ptype, 0.0))
            if threshold > 0 and metric_val >= threshold:
                after = min(int(p["max_instances"]), before + int(p["scale_up_step"]))
                if after > before:
                    action = "scale_up"
                    reason = f"{ptype}={metric_val:.2f} >= {threshold:.2f}"
            elif threshold > 0 and metric_val < threshold * 0.5 and before > int(p["min_instances"]):
                after = max(int(p["min_instances"]), before - int(p["scale_down_step"]))
                if after < before:
                    action = "scale_down"
                    reason = f"{ptype}={metric_val:.2f} < {threshold * 0.5:.2f}"

        await db.execute(text("""
            INSERT INTO scaling_events
                (project_id, region_id, policy_id, event_type,
                 trigger_metric, trigger_value, instances_before, instances_after, reason)
            VALUES
                (:pid, :rid, :polid, :etype,
                 :tm, :tv, :ib, :ia, :reason)
        """), {
            "pid": str(project_id),
            "rid": p["region_id"],
            "polid": str(p["id"]),
            "etype": action,
            "tm": ptype,
            "tv": metric_val,
            "ib": before,
            "ia": after,
            "reason": reason,
        })
        decisions.append(ScalingDecision(
            policy_id=UUID(str(p["id"])),
            event_type=action,
            instances_before=before,
            instances_after=after,
            trigger_metric=ptype,
            trigger_value=metric_val,
            reason=reason or "no trigger",
        ))

    if decisions:
        await db.commit()
    return decisions


async def _current_instances(db: AsyncSession, project_id: UUID, region_id: int | None) -> int:
    """Best-effort current instance count from projects.instances column.

    Cloud Run Admin API does expose realtime instance counts but we cache the
    last applied value in the project row (set by deploy_service). Sufficient
    for evaluation purposes.
    """
    row = (await db.execute(text("""
        SELECT instances FROM projects WHERE id = :pid
    """), {"pid": str(project_id)})).mappings().first()
    return int(row["instances"]) if row and row["instances"] is not None else 0


def _cron_matches_now(spec: str, now: datetime) -> bool:
    """Very lightweight cron matcher: supports `m h dom mon dow` with `*` and
    plain integers. Comma lists and ranges (`1-5`) are also supported. This is
    intentionally tiny — production cron should be routed through Cloud
    Scheduler. The matcher only powers in-process schedule policy evaluation
    (`evaluate_scaling`)."""
    if not spec:
        return False
    parts = spec.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, mon, dow = parts
    fields = [
        (minute, now.minute, 0, 59),
        (hour,   now.hour,   0, 23),
        (dom,    now.day,    1, 31),
        (mon,    now.month,  1, 12),
        (dow,    now.weekday() + 1, 1, 7),  # cron 1=Mon..7=Sun-ish
    ]
    for token, value, lo, hi in fields:
        if not _cron_field_matches(token, value, lo, hi):
            return False
    return True


def _cron_field_matches(token: str, value: int, lo: int, hi: int) -> bool:
    if token == "*":
        return True
    for chunk in token.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                if int(a) <= value <= int(b):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(chunk) == value:
                    return True
            except ValueError:
                continue
    return False


# ─── 4. Canary ramp ────────────────────────────────────────────────────────
async def run_canary_ramp(
    db: AsyncSession,
    *,
    project_id: UUID,
) -> dict[str, Any]:
    """Read the active canary policy and progress canary_percent according to
    the wall-clock ramp schedule (`{"ramp":[{"at":"+1h","pct":25},...]}`).

    Returns a snapshot dict with keys:
      stable_region, canary_region, current_canary_percent, next_step_at.
    """
    pol = (await db.execute(text("""
        SELECT id, routing_rules, created_at FROM traffic_policies
         WHERE project_id = :pid AND active = TRUE AND policy_type = 'canary'
         ORDER BY created_at DESC LIMIT 1
    """), {"pid": str(project_id)})).mappings().first()
    if not pol:
        return {"active": False}

    rules = dict(pol["routing_rules"] or {})
    started = pol["created_at"]
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    current = int(rules.get("canary_percent") or 0)
    next_step_at: str | None = None
    for step in (rules.get("ramp") or []):
        delay = _parse_relative(step.get("at") or "0s")
        if elapsed >= delay:
            current = int(max(current, int(step.get("pct") or current)))
        else:
            next_step_at = (started + timedelta(seconds=delay)).isoformat()
            break

    rules["canary_percent"] = current

    # Persist updated percent + propagate to project_deployments
    await db.execute(text("""
        UPDATE traffic_policies
           SET routing_rules = CAST(:rules AS JSONB), updated_at = NOW()
         WHERE id = :id
    """), {"id": str(pol["id"]), "rules": _to_json(rules)})

    stable_region = rules.get("stable_region")
    canary_region = rules.get("canary_region")
    if stable_region and canary_region:
        await _set_traffic(db, project_id, stable_region, 100 - current)
        await _set_traffic(db, project_id, canary_region, current)

    await db.commit()
    return {
        "active": True,
        "stable_region": stable_region,
        "canary_region": canary_region,
        "current_canary_percent": current,
        "elapsed_seconds": int(elapsed),
        "next_step_at": next_step_at,
    }


def _parse_relative(s: str) -> float:
    """Parse '+1h' / '30m' / '15s' / '0' into seconds. Bare ints = seconds."""
    s = (s or "").strip().lstrip("+")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    unit = s[-1].lower()
    try:
        n = float(s[:-1])
    except ValueError:
        return 0.0
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)


# ─── 5. Health checks (fan-out HTTP probes) ────────────────────────────────
async def health_check_all_regions(
    db: AsyncSession,
    *,
    project_id: UUID,
) -> list[dict[str, Any]]:
    """For each running deployment of `project_id`, fire an HTTP HEAD probe
    against the Cloud Run URL. Persist results into `health_check_results`.
    Returns a list of {region_code, healthy, latency_ms, status_code}.
    """
    deployments = (await db.execute(text("""
        SELECT d.id, d.region_id, d.cloud_run_service_url, r.code AS region_code
          FROM project_deployments d
          JOIN regions r ON r.id = d.region_id
         WHERE d.project_id = :pid AND d.status = 'running'
    """), {"pid": str(project_id)})).mappings().all()

    if not deployments:
        return []

    async def _probe(dep: dict[str, Any]) -> dict[str, Any]:
        url = dep.get("cloud_run_service_url") or ""
        if not url:
            return {
                "region_code": dep["region_code"], "healthy": False,
                "status_code": None, "latency_ms": None,
                "error": "no URL", "deployment_id": dep["id"], "region_id": dep["region_id"],
            }
        # Lazy import — keep module import lightweight if httpx isn't installed
        try:
            import httpx  # type: ignore
        except ImportError:  # pragma: no cover
            return {
                "region_code": dep["region_code"], "healthy": False,
                "status_code": None, "latency_ms": None,
                "error": "httpx not available",
                "deployment_id": dep["id"], "region_id": dep["region_id"],
            }
        start = time.perf_counter()
        status_code: int | None = None
        err: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as c:
                r = await c.get(url)
                status_code = r.status_code
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        latency_ms = int((time.perf_counter() - start) * 1000)
        healthy = (status_code is not None) and (200 <= status_code < 500)
        return {
            "region_code": dep["region_code"], "healthy": healthy,
            "status_code": status_code, "latency_ms": latency_ms,
            "error": err, "deployment_id": dep["id"], "region_id": dep["region_id"],
        }

    results = await asyncio.gather(*(_probe(d) for d in deployments))

    for r in results:
        await db.execute(text("""
            INSERT INTO health_check_results
                (project_id, region_id, deployment_id,
                 status_code, latency_ms, healthy, error_message)
            VALUES
                (:pid, :rid, :did, :sc, :lat, :ok, :err)
        """), {
            "pid": str(project_id),
            "rid": r["region_id"],
            "did": str(r["deployment_id"]),
            "sc": r["status_code"],
            "lat": r["latency_ms"],
            "ok": r["healthy"],
            "err": r.get("error"),
        })
    await db.commit()
    return [
        {
            "region_code": r["region_code"], "healthy": r["healthy"],
            "status_code": r["status_code"], "latency_ms": r["latency_ms"],
        }
        for r in results
    ]


# ─── Misc helpers ──────────────────────────────────────────────────────────
def _to_json(d: Any) -> str:
    import json
    return json.dumps(d, default=str)


__all__ = [
    "RegionDeployResult",
    "ScalingDecision",
    "deploy_to_region",
    "apply_traffic_policy",
    "evaluate_scaling",
    "run_canary_ramp",
    "health_check_all_regions",
]
