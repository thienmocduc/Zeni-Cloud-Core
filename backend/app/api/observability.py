"""
Observability API — Prometheus scrape + metrics/traces query + alert config.

Endpoints
---------
Public (Prometheus scrape target):
  GET  /metrics
        Prometheus text-format exposition of in-memory metrics.

Authenticated:
  GET    /api/v1/observability/metrics?ws=&name=&from=&to=
  GET    /api/v1/observability/traces?ws=&trace_id=
  GET    /api/v1/observability/dashboard?ws=
  POST   /api/v1/observability/alerts
  GET    /api/v1/observability/alerts?ws=
  PATCH  /api/v1/observability/alerts/{id}
  DELETE /api/v1/observability/alerts/{id}
  GET    /api/v1/observability/alert-events?ws=

All authenticated endpoints honour `require_workspace_access`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.observability import (
    evaluate_alerts as _evaluate_alerts,
    flush_metrics,
    flush_traces,
    metrics_collector,
)

log = logging.getLogger("zeni.api.observability")

# Two routers: one without prefix for /metrics (public scrape target)
# and one nested under /api/v1/observability for auth'd endpoints.
prom_router = APIRouter(tags=["observability"])
router = APIRouter(prefix="/observability", tags=["observability"])


# ---------------------------------------------------------------------------
# Pydantic v2 schemas
# ---------------------------------------------------------------------------
class AlertRuleIn(BaseModel):
    workspace_id: str = Field(..., max_length=32)
    name: str = Field(..., max_length=120)
    metric_name: str = Field(..., max_length=80)
    condition: Literal["gt", "lt", "gte", "lte", "eq"]
    threshold: float
    window_minutes: int = Field(default=5, ge=1, le=1440)
    severity: Literal["info", "warning", "critical"] = "warning"
    enabled: bool = True
    notify_channels: list[str] = Field(default_factory=lambda: ["email"])


class AlertRulePatch(BaseModel):
    enabled: bool | None = None
    threshold: float | None = None
    window_minutes: int | None = Field(default=None, ge=1, le=1440)
    severity: Literal["info", "warning", "critical"] | None = None
    notify_channels: list[str] | None = None


class AlertRuleOut(BaseModel):
    id: int
    workspace_id: str
    name: str
    metric_name: str
    condition: str
    threshold: float
    window_minutes: int
    severity: str
    enabled: bool
    notify_channels: list[str]
    created_at: datetime


# ---------------------------------------------------------------------------
# /metrics — Prometheus scrape (public)
# ---------------------------------------------------------------------------
@prom_router.get("/metrics", include_in_schema=False)
async def prometheus_scrape() -> Response:
    """Prometheus text-format exposition of in-memory metrics.

    NOTE: kept public so a Prometheus server can scrape directly. If you
    deploy on the public internet, terminate at a reverse proxy with
    IP allowlist or basic-auth.
    """
    body = metrics_collector.render_prometheus()
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------------------------------------------------------------------
# Metric query
# ---------------------------------------------------------------------------
@router.get("/metrics")
async def query_metrics(
    ws: str,
    name: str | None = Query(default=None, description="Optional metric_name filter"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10_000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Query persisted metrics rows for charting / debugging."""
    await require_workspace_access(ws, me)
    if to is None:
        to = datetime.now(timezone.utc)
    if from_ is None:
        from_ = to - timedelta(hours=1)

    sql = """
        SELECT bucket_minute, metric_name, metric_type, metric_value, labels
        FROM app_metrics
        WHERE workspace_id = :ws
          AND bucket_minute >= :f AND bucket_minute <= :t
    """
    params: dict[str, Any] = {"ws": ws, "f": from_, "t": to, "lim": limit}
    if name:
        sql += " AND metric_name = :n"
        params["n"] = name
    sql += " ORDER BY bucket_minute DESC LIMIT :lim"

    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "workspace_id": ws,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "count": len(rows),
        "metrics": [
            {
                "bucket_minute": r["bucket_minute"].isoformat() if r["bucket_minute"] else None,
                "name": r["metric_name"],
                "type": r["metric_type"],
                "value": float(r["metric_value"] or 0),
                "labels": r["labels"] or {},
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Trace query
# ---------------------------------------------------------------------------
@router.get("/traces")
async def query_traces(
    ws: str,
    trace_id: str | None = Query(default=None, max_length=40),
    operation: str | None = Query(default=None, max_length=120),
    status: Literal["ok", "error", "timeout"] | None = None,
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=2000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Distributed trace browser. With `trace_id` returns the full span tree."""
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    if trace_id:
        rows = (await db.execute(text("""
            SELECT trace_id, span_id, parent_span_id, operation_name, service_name,
                   started_at, duration_ms, status, attributes, error_message
            FROM app_traces
            WHERE workspace_id = :ws AND trace_id = :tid
            ORDER BY started_at ASC
        """), {"ws": ws, "tid": trace_id})).mappings().all()
        return {
            "workspace_id": ws,
            "trace_id": trace_id,
            "spans": [_span_dict(r) for r in rows],
        }

    sql = """
        SELECT trace_id, span_id, parent_span_id, operation_name, service_name,
               started_at, duration_ms, status, attributes, error_message
        FROM app_traces
        WHERE workspace_id = :ws AND started_at >= :since
    """
    params: dict[str, Any] = {"ws": ws, "since": since, "lim": limit}
    if operation:
        sql += " AND operation_name = :op"
        params["op"] = operation
    if status:
        sql += " AND status = :s"
        params["s"] = status
    sql += " ORDER BY started_at DESC LIMIT :lim"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "workspace_id": ws, "hours": hours,
        "count": len(rows),
        "traces": [_span_dict(r) for r in rows],
    }


def _span_dict(r: Any) -> dict[str, Any]:
    return {
        "trace_id": r["trace_id"],
        "span_id": r["span_id"],
        "parent_span_id": r["parent_span_id"],
        "operation": r["operation_name"],
        "service": r["service_name"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "duration_ms": r["duration_ms"],
        "status": r["status"],
        "attributes": r["attributes"] or {},
        "error": r["error_message"],
    }


# ---------------------------------------------------------------------------
# Pre-built dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard")
async def dashboard(
    ws: str,
    hours: int = Query(default=24, ge=1, le=24 * 7),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One-shot dashboard payload — feeds the Grafana / app dashboard UI."""
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # 1. Total HTTP requests + error rate (last `hours`)
    http_total = (await db.execute(text("""
        SELECT COALESCE(SUM(metric_value), 0) AS v
        FROM app_metrics
        WHERE workspace_id = :ws AND metric_name = 'http_request_total'
          AND bucket_minute >= :since
    """), {"ws": ws, "since": since})).mappings().first()
    http_errors = (await db.execute(text("""
        SELECT COALESCE(SUM(metric_value), 0) AS v
        FROM app_metrics
        WHERE workspace_id = :ws AND metric_name = 'http_request_error_total'
          AND bucket_minute >= :since
    """), {"ws": ws, "since": since})).mappings().first()

    # 2. Latency percentiles (approx via histogram buckets — pull p50/p95/p99
    #    by reading bucket counts and reconstructing CDF). For simplicity
    #    surface raw _sum / _count averages here; deeper percentiles can
    #    be done client-side from the buckets returned by /metrics.
    lat = (await db.execute(text("""
        SELECT
          SUM(CASE WHEN metric_name = 'http_request_duration_ms_sum'   THEN metric_value END) AS lsum,
          SUM(CASE WHEN metric_name = 'http_request_duration_ms_count' THEN metric_value END) AS lcount
        FROM app_metrics
        WHERE workspace_id = :ws AND bucket_minute >= :since
    """), {"ws": ws, "since": since})).mappings().first()
    avg_lat_ms = float(lat["lsum"] or 0) / max(1.0, float(lat["lcount"] or 1))

    # 3. Top endpoints
    top_eps = (await db.execute(text("""
        SELECT (labels->>'endpoint') AS endpoint,
               SUM(metric_value) AS reqs
        FROM app_metrics
        WHERE workspace_id = :ws AND metric_name = 'http_request_total'
          AND bucket_minute >= :since
        GROUP BY (labels->>'endpoint')
        ORDER BY reqs DESC
        LIMIT 10
    """), {"ws": ws, "since": since})).mappings().all()

    # 4. AI cost per hour (joins existing router_usage_log from migration 021)
    ai_cost = (await db.execute(text("""
        SELECT date_trunc('hour', created_at) AS bucket,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM router_usage_log
        WHERE workspace_id = :ws AND created_at >= :since
        GROUP BY bucket ORDER BY bucket ASC
    """), {"ws": ws, "since": since})).mappings().all()

    # 5. Cache hit ratio (router_usage_log)
    cache_row = (await db.execute(text("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END) AS hits
        FROM router_usage_log
        WHERE workspace_id = :ws AND created_at >= :since
    """), {"ws": ws, "since": since})).mappings().first()
    total = int(cache_row["total"] or 0)
    hits = int(cache_row["hits"] or 0)
    hit_ratio = (hits / total) if total else 0.0

    # 6. Recent error spans
    err_spans = (await db.execute(text("""
        SELECT trace_id, operation_name, started_at, duration_ms, error_message
        FROM app_traces
        WHERE workspace_id = :ws AND status = 'error' AND started_at >= :since
        ORDER BY started_at DESC LIMIT 20
    """), {"ws": ws, "since": since})).mappings().all()

    # 7. Active alert events (unresolved)
    open_alerts = (await db.execute(text("""
        SELECT a.id, a.metric_value, a.triggered_at, r.name, r.severity
        FROM alert_events a JOIN alert_rules r ON r.id = a.rule_id
        WHERE a.workspace_id = :ws AND a.resolved_at IS NULL
        ORDER BY a.triggered_at DESC LIMIT 20
    """), {"ws": ws})).mappings().all()

    total_reqs = float(http_total["v"] or 0)
    total_errs = float(http_errors["v"] or 0)
    error_rate = (total_errs / total_reqs) if total_reqs else 0.0

    return {
        "workspace_id": ws,
        "window_hours": hours,
        "http": {
            "total_requests": total_reqs,
            "total_errors": total_errs,
            "error_rate": round(error_rate, 4),
            "avg_latency_ms": round(avg_lat_ms, 2),
            "rps": round(total_reqs / max(1, hours * 3600), 4),
        },
        "top_endpoints": [
            {"endpoint": r["endpoint"] or "(none)", "requests": float(r["reqs"] or 0)}
            for r in top_eps
        ],
        "ai_cost_per_hour": [
            {"bucket": r["bucket"].isoformat() if r["bucket"] else None,
             "cost_usd": float(r["cost"] or 0)}
            for r in ai_cost
        ],
        "cache": {
            "total_calls": total, "hits": hits,
            "hit_ratio": round(hit_ratio, 4),
        },
        "recent_errors": [
            {"trace_id": r["trace_id"], "operation": r["operation_name"],
             "started_at": r["started_at"].isoformat() if r["started_at"] else None,
             "duration_ms": r["duration_ms"], "error": r["error_message"]}
            for r in err_spans
        ],
        "open_alerts": [
            {"id": r["id"], "rule": r["name"], "severity": r["severity"],
             "metric_value": float(r["metric_value"] or 0),
             "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None}
            for r in open_alerts
        ],
    }


# ---------------------------------------------------------------------------
# Alert rules CRUD
# ---------------------------------------------------------------------------
@router.post("/alerts", response_model=AlertRuleOut, status_code=201)
async def create_alert(
    body: AlertRuleIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleOut:
    await require_workspace_access(body.workspace_id, me)
    row = (await db.execute(text("""
        INSERT INTO alert_rules
            (workspace_id, name, metric_name, condition, threshold,
             window_minutes, severity, enabled, notify_channels)
        VALUES (:ws, :name, :mn, :cond, :thr, :win, :sev, :en, :ch)
        RETURNING id, workspace_id, name, metric_name, condition, threshold,
                  window_minutes, severity, enabled, notify_channels, created_at
    """), {
        "ws": body.workspace_id, "name": body.name, "mn": body.metric_name,
        "cond": body.condition, "thr": body.threshold,
        "win": body.window_minutes, "sev": body.severity,
        "en": body.enabled, "ch": body.notify_channels,
    })).mappings().first()
    await db.commit()
    return AlertRuleOut(
        id=row["id"], workspace_id=row["workspace_id"], name=row["name"],
        metric_name=row["metric_name"], condition=row["condition"],
        threshold=float(row["threshold"]), window_minutes=row["window_minutes"],
        severity=row["severity"], enabled=row["enabled"],
        notify_channels=list(row["notify_channels"] or []),
        created_at=row["created_at"],
    )


@router.get("/alerts")
async def list_alerts(
    ws: str,
    enabled_only: bool = Query(default=False),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    sql = """
        SELECT id, workspace_id, name, metric_name, condition, threshold,
               window_minutes, severity, enabled, notify_channels, created_at
        FROM alert_rules WHERE workspace_id = :ws
    """
    if enabled_only:
        sql += " AND enabled = TRUE"
    sql += " ORDER BY created_at DESC"
    rows = (await db.execute(text(sql), {"ws": ws})).mappings().all()
    return {
        "workspace_id": ws,
        "count": len(rows),
        "rules": [
            {
                "id": r["id"], "workspace_id": r["workspace_id"], "name": r["name"],
                "metric_name": r["metric_name"], "condition": r["condition"],
                "threshold": float(r["threshold"]),
                "window_minutes": r["window_minutes"], "severity": r["severity"],
                "enabled": r["enabled"],
                "notify_channels": list(r["notify_channels"] or []),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.patch("/alerts/{rule_id}")
async def patch_alert(
    rule_id: int,
    body: AlertRulePatch,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cur = (await db.execute(text("""
        SELECT workspace_id FROM alert_rules WHERE id = :id
    """), {"id": rule_id})).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="alert rule not found")
    await require_workspace_access(cur["workspace_id"], me)

    fields, params = [], {"id": rule_id}
    if body.enabled is not None:
        fields.append("enabled = :en"); params["en"] = body.enabled
    if body.threshold is not None:
        fields.append("threshold = :thr"); params["thr"] = body.threshold
    if body.window_minutes is not None:
        fields.append("window_minutes = :win"); params["win"] = body.window_minutes
    if body.severity is not None:
        fields.append("severity = :sev"); params["sev"] = body.severity
    if body.notify_channels is not None:
        fields.append("notify_channels = :ch"); params["ch"] = body.notify_channels
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    await db.execute(text(f"UPDATE alert_rules SET {', '.join(fields)} WHERE id = :id"), params)
    await db.commit()
    return {"id": rule_id, "updated": True}


@router.delete("/alerts/{rule_id}", status_code=204)
async def delete_alert(
    rule_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    cur = (await db.execute(text("""
        SELECT workspace_id FROM alert_rules WHERE id = :id
    """), {"id": rule_id})).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="alert rule not found")
    await require_workspace_access(cur["workspace_id"], me)
    await db.execute(text("DELETE FROM alert_rules WHERE id = :id"), {"id": rule_id})
    await db.commit()
    return Response(status_code=204)


@router.get("/alert-events")
async def list_alert_events(
    ws: str,
    severity: Literal["info", "warning", "critical"] | None = None,
    unresolved_only: bool = False,
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=100, ge=1, le=1000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    sql = """
        SELECT a.id, a.rule_id, a.metric_value, a.triggered_at, a.resolved_at,
               a.notified, a.detail, r.name AS rule_name, r.severity, r.metric_name
        FROM alert_events a JOIN alert_rules r ON r.id = a.rule_id
        WHERE a.workspace_id = :ws AND a.triggered_at >= :since
    """
    params: dict[str, Any] = {"ws": ws, "since": since, "lim": limit}
    if severity:
        sql += " AND r.severity = :sev"; params["sev"] = severity
    if unresolved_only:
        sql += " AND a.resolved_at IS NULL"
    sql += " ORDER BY a.triggered_at DESC LIMIT :lim"

    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "workspace_id": ws,
        "hours": hours,
        "count": len(rows),
        "events": [
            {
                "id": r["id"], "rule_id": r["rule_id"],
                "rule_name": r["rule_name"], "severity": r["severity"],
                "metric_name": r["metric_name"],
                "metric_value": float(r["metric_value"] or 0),
                "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None,
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "notified": r["notified"], "detail": r["detail"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Manual triggers (admin-only convenience)
# ---------------------------------------------------------------------------
@router.post("/_internal/flush", include_in_schema=False)
async def manual_flush(
    me: CurrentUser = Depends(get_current_user),
) -> dict:
    """Owner-only manual flush — handy in dev / debugging."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Owner only")
    m = await flush_metrics()
    t = await flush_traces()
    return {"metrics_rows": m, "trace_rows": t}


@router.post("/_internal/evaluate-alerts", include_in_schema=False)
async def manual_evaluate_alerts(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Owner only")
    n = await _evaluate_alerts(db)
    return {"triggered": n}
