"""
Observability stack — Prometheus-compatible metrics + OpenTelemetry-style traces.

Design:
  * `MetricsCollector` buffers metric samples in-memory keyed by
    (metric_name, label-set, bucket_minute) so a flush every 60s emits one
    aggregated row per (workspace, name, label-set, minute) combination —
    keeping `app_metrics` table cardinality low.
  * `TraceContext` is an async context manager that records a span on exit
    (success or failure) directly to `app_traces`. Spans are also buffered
    and flushed in batches.
  * `/metrics` endpoint exposes the in-memory state in Prometheus exposition
    text format (no external client lib).
  * Cron tasks (`flush_metrics_loop`, `flush_traces_loop`) drain buffers
    once per minute. Single-shot helpers (`flush_metrics`, `flush_traces`)
    are exposed for tests + manual triggers.
  * Helper `record_http_request(...)` is the standard instrumentation point
    used by the metrics middleware.

This module DOES NOT depend on prometheus_client / opentelemetry-sdk on
purpose — the goal is zero new third-party deps.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal

log = logging.getLogger("zeni.observability")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SERVICE_NAME = "zenicloud"
FLUSH_INTERVAL_SECONDS = 60
DEFAULT_HISTOGRAM_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
MAX_BUFFER = 50_000  # safety bound — drop samples if collector falls behind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _truncate_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _label_key(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Stable canonical key for a label set (used for in-memory grouping)."""
    if not labels:
        return tuple()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _format_labels(labels: dict[str, Any] | None) -> str:
    """Render labels for Prometheus exposition format: {k="v",k2="v2"}."""
    if not labels:
        return ""
    parts = []
    for k, v in sorted(labels.items()):
        # escape per Prometheus text format spec
        sv = str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        parts.append(f'{k}="{sv}"')
    return "{" + ",".join(parts) + "}"


def _new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars (fits VARCHAR(40))


def _new_span_id() -> str:
    return secrets.token_hex(8)   # 16 hex chars (fits VARCHAR(20))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class _MetricSample:
    workspace_id: str | None
    metric_name: str
    metric_type: str  # 'counter','gauge','histogram'
    value: float
    labels: dict[str, Any]
    bucket_minute: datetime


@dataclass
class _SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    workspace_id: str | None
    operation_name: str
    started_at: datetime
    duration_ms: int
    status: str
    attributes: dict[str, Any]
    error_message: str | None = None


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------
class MetricsCollector:
    """In-memory metrics buffer + Prometheus exposition + DB flush.

    Counters and gauges are stored as a single value per
    (name, label-set, ws, bucket-minute). Histograms accumulate per-bucket
    counters AND a `_sum` and `_count`.
    """

    def __init__(self) -> None:
        # counters / gauges:
        # key: (name, label_key, ws, bucket_minute) -> {labels, value, type}
        self._scalars: dict[tuple, dict[str, Any]] = {}
        # histograms (separate to keep semantics clear):
        # key: (name, label_key, ws, bucket_minute)
        #   -> {labels, buckets: dict[float|str,int], sum, count}
        self._histograms: dict[tuple, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._dropped = 0

    # ---- public recording API --------------------------------------------
    def record_counter(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, Any] | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._add_scalar(name, "counter", float(value), labels, workspace_id, accumulate=True)

    def record_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, Any] | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._add_scalar(name, "gauge", float(value), labels, workspace_id, accumulate=False)

    def record_histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, Any] | None = None,
        workspace_id: str | None = None,
        buckets: Iterable[float] | None = None,
    ) -> None:
        bucket_minute = _truncate_minute(datetime.now(timezone.utc))
        lk = _label_key(labels)
        key = (name, lk, workspace_id, bucket_minute)
        if len(self._histograms) > MAX_BUFFER:
            self._dropped += 1
            return
        h = self._histograms.get(key)
        if h is None:
            h = {
                "name": name,
                "labels": dict(labels) if labels else {},
                "workspace_id": workspace_id,
                "bucket_minute": bucket_minute,
                "buckets": defaultdict(int),
                "sum": 0.0,
                "count": 0,
                "bucket_bounds": tuple(buckets) if buckets else DEFAULT_HISTOGRAM_BUCKETS,
            }
            self._histograms[key] = h
        # cumulative bucket counts (Prometheus convention: le="<=bound")
        for b in h["bucket_bounds"]:
            if value <= b:
                h["buckets"][b] += 1
        h["buckets"]["+Inf"] += 1
        h["sum"] += float(value)
        h["count"] += 1

    # ---- internal --------------------------------------------------------
    def _add_scalar(
        self,
        name: str,
        mtype: str,
        value: float,
        labels: dict[str, Any] | None,
        workspace_id: str | None,
        *,
        accumulate: bool,
    ) -> None:
        bucket_minute = _truncate_minute(datetime.now(timezone.utc))
        lk = _label_key(labels)
        key = (name, lk, workspace_id, bucket_minute)
        if len(self._scalars) > MAX_BUFFER:
            self._dropped += 1
            return
        cur = self._scalars.get(key)
        if cur is None:
            self._scalars[key] = {
                "name": name,
                "type": mtype,
                "labels": dict(labels) if labels else {},
                "workspace_id": workspace_id,
                "bucket_minute": bucket_minute,
                "value": value,
            }
        else:
            cur["value"] = cur["value"] + value if accumulate else value

    # ---- exposition format -----------------------------------------------
    def render_prometheus(self) -> str:
        """Render current in-memory state as Prometheus text format."""
        # group scalars by metric name to emit a single # HELP/# TYPE header
        out: list[str] = []
        seen_names: set[str] = set()

        # scalars
        by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in self._scalars.values():
            by_name[entry["name"]].append(entry)
        for name, items in sorted(by_name.items()):
            mtype = items[0]["type"]
            if name not in seen_names:
                out.append(f"# HELP {name} Zeni Cloud {mtype}")
                out.append(f"# TYPE {name} {mtype}")
                seen_names.add(name)
            for it in items:
                lbls = dict(it["labels"])
                if it["workspace_id"]:
                    lbls.setdefault("workspace", it["workspace_id"])
                out.append(f"{name}{_format_labels(lbls)} {it['value']}")

        # histograms
        h_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in self._histograms.values():
            h_by_name[entry["name"]].append(entry)
        for name, items in sorted(h_by_name.items()):
            if name not in seen_names:
                out.append(f"# HELP {name} Zeni Cloud histogram")
                out.append(f"# TYPE {name} histogram")
                seen_names.add(name)
            for it in items:
                base_lbls = dict(it["labels"])
                if it["workspace_id"]:
                    base_lbls.setdefault("workspace", it["workspace_id"])
                # bucket lines
                for b in it["bucket_bounds"]:
                    lbls = {**base_lbls, "le": str(b)}
                    out.append(f"{name}_bucket{_format_labels(lbls)} {it['buckets'][b]}")
                lbls_inf = {**base_lbls, "le": "+Inf"}
                out.append(f"{name}_bucket{_format_labels(lbls_inf)} {it['buckets']['+Inf']}")
                out.append(f"{name}_sum{_format_labels(base_lbls)} {it['sum']}")
                out.append(f"{name}_count{_format_labels(base_lbls)} {it['count']}")
        if self._dropped:
            out.append(f"zeni_metrics_dropped_total {self._dropped}")
        return "\n".join(out) + "\n"

    # ---- DB flush --------------------------------------------------------
    async def flush(self, db: AsyncSession) -> int:
        """Persist current buffer to `app_metrics`. Returns rows written."""
        async with self._lock:
            scalars = list(self._scalars.values())
            histograms = list(self._histograms.values())
            self._scalars.clear()
            self._histograms.clear()

        rows_written = 0
        if scalars:
            payload = [
                {
                    "ws": s["workspace_id"], "name": s["name"], "type": s["type"],
                    "value": s["value"], "labels": _json_dump(s["labels"]),
                    "bucket": s["bucket_minute"],
                }
                for s in scalars
            ]
            await db.execute(text("""
                INSERT INTO app_metrics
                    (workspace_id, metric_name, metric_type, metric_value, labels, bucket_minute)
                VALUES (:ws, :name, :type, :value, CAST(:labels AS JSONB), :bucket)
            """), payload)
            rows_written += len(payload)

        if histograms:
            hist_payload = []
            for h in histograms:
                # store one row per bucket bound + _sum + _count
                for b in h["bucket_bounds"]:
                    lbl = {**h["labels"], "le": str(b)}
                    hist_payload.append({
                        "ws": h["workspace_id"], "name": f"{h['name']}_bucket",
                        "type": "histogram", "value": h["buckets"][b],
                        "labels": _json_dump(lbl), "bucket": h["bucket_minute"],
                    })
                inf_lbl = {**h["labels"], "le": "+Inf"}
                hist_payload.append({
                    "ws": h["workspace_id"], "name": f"{h['name']}_bucket",
                    "type": "histogram", "value": h["buckets"]["+Inf"],
                    "labels": _json_dump(inf_lbl), "bucket": h["bucket_minute"],
                })
                hist_payload.append({
                    "ws": h["workspace_id"], "name": f"{h['name']}_sum",
                    "type": "histogram", "value": h["sum"],
                    "labels": _json_dump(h["labels"]), "bucket": h["bucket_minute"],
                })
                hist_payload.append({
                    "ws": h["workspace_id"], "name": f"{h['name']}_count",
                    "type": "histogram", "value": h["count"],
                    "labels": _json_dump(h["labels"]), "bucket": h["bucket_minute"],
                })
            if hist_payload:
                await db.execute(text("""
                    INSERT INTO app_metrics
                        (workspace_id, metric_name, metric_type, metric_value, labels, bucket_minute)
                    VALUES (:ws, :name, :type, :value, CAST(:labels AS JSONB), :bucket)
                """), hist_payload)
                rows_written += len(hist_payload)

        await db.commit()
        return rows_written


def _json_dump(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str, separators=(",", ":"))


# Singleton — wired by middleware + API handlers
metrics_collector = MetricsCollector()


# ---------------------------------------------------------------------------
# Trace buffer + TraceContext
# ---------------------------------------------------------------------------
class TraceBuffer:
    def __init__(self) -> None:
        self._spans: list[_SpanRecord] = []
        self._lock = asyncio.Lock()

    def add(self, span: _SpanRecord) -> None:
        if len(self._spans) > MAX_BUFFER:
            return
        self._spans.append(span)

    async def flush(self, db: AsyncSession) -> int:
        async with self._lock:
            spans = self._spans
            self._spans = []
        if not spans:
            return 0
        payload = [
            {
                "trace_id": s.trace_id, "span_id": s.span_id,
                "parent_span_id": s.parent_span_id, "ws": s.workspace_id,
                "op": s.operation_name, "service": SERVICE_NAME,
                "started": s.started_at, "duration": s.duration_ms,
                "status": s.status, "attrs": _json_dump(s.attributes),
                "err": s.error_message,
            }
            for s in spans
        ]
        await db.execute(text("""
            INSERT INTO app_traces
                (trace_id, span_id, parent_span_id, workspace_id, operation_name,
                 service_name, started_at, duration_ms, status, attributes, error_message)
            VALUES
                (:trace_id, :span_id, :parent_span_id, :ws, :op,
                 :service, :started, :duration, :status, CAST(:attrs AS JSONB), :err)
            ON CONFLICT (trace_id, span_id) DO NOTHING
        """), payload)
        await db.commit()
        return len(payload)


trace_buffer = TraceBuffer()


class TraceContext:
    """Async context manager for distributed tracing.

    Usage::

        async with TraceContext("router.complete", workspace_id=ws,
                                attributes={"model": "gpt-4o"}) as span:
            # ... work ...
            span.set_attribute("output_tokens", 512)
    """

    def __init__(
        self,
        operation_name: str,
        workspace_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.operation_name = operation_name
        self.workspace_id = workspace_id
        self.attributes: dict[str, Any] = dict(attributes) if attributes else {}
        self.parent_span_id = parent_span_id
        self.trace_id = trace_id or _new_trace_id()
        self.span_id = _new_span_id()
        self.status = "ok"
        self.error_message: str | None = None
        self._start_perf: float = 0.0
        self._start_at: datetime | None = None

    async def __aenter__(self) -> "TraceContext":
        self._start_perf = time.perf_counter()
        self._start_at = datetime.now(timezone.utc)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        duration_ms = int((time.perf_counter() - self._start_perf) * 1000)
        if exc is not None:
            self.status = "error"
            self.error_message = f"{exc_type.__name__}: {exc}"[:500]
        span = _SpanRecord(
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            workspace_id=self.workspace_id,
            operation_name=self.operation_name,
            started_at=self._start_at or datetime.now(timezone.utc),
            duration_ms=duration_ms,
            status=self.status,
            attributes=self.attributes,
            error_message=self.error_message,
        )
        trace_buffer.add(span)
        # also surface latency as a histogram metric for free
        try:
            metrics_collector.record_histogram(
                f"trace_{self.operation_name}_duration_ms",
                duration_ms,
                labels={"status": self.status},
                workspace_id=self.workspace_id,
            )
        except Exception:
            pass
        return False  # don't swallow exceptions

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def child(self, operation_name: str, attributes: dict[str, Any] | None = None) -> "TraceContext":
        return TraceContext(
            operation_name=operation_name,
            workspace_id=self.workspace_id,
            attributes=attributes,
            parent_span_id=self.span_id,
            trace_id=self.trace_id,
        )


# ---------------------------------------------------------------------------
# HTTP request convenience helper
# ---------------------------------------------------------------------------
def record_http_request(
    endpoint: str,
    method: str,
    status: int,
    latency_ms: float,
    workspace_id: str | None = None,
) -> None:
    """Standard instrumentation for HTTP requests — used by the middleware."""
    labels = {
        "endpoint": endpoint,
        "method": method.upper(),
        "status": str(status),
        "status_class": f"{status // 100}xx",
    }
    metrics_collector.record_counter(
        "http_request_total", 1.0, labels=labels, workspace_id=workspace_id,
    )
    metrics_collector.record_histogram(
        "http_request_duration_ms", float(latency_ms),
        labels={"endpoint": endpoint, "method": method.upper()},
        workspace_id=workspace_id,
    )
    if status >= 500:
        metrics_collector.record_counter(
            "http_request_error_total", 1.0, labels=labels, workspace_id=workspace_id,
        )


# ---------------------------------------------------------------------------
# Public flush helpers + cron loops
# ---------------------------------------------------------------------------
async def flush_metrics() -> int:
    async with SessionLocal() as db:
        try:
            n = await metrics_collector.flush(db)
            if n:
                log.debug("flushed %d metric rows", n)
            return n
        except Exception as e:
            await db.rollback()
            log.exception("metrics flush failed: %s", e)
            return 0


async def flush_traces() -> int:
    async with SessionLocal() as db:
        try:
            n = await trace_buffer.flush(db)
            if n:
                log.debug("flushed %d trace rows", n)
            return n
        except Exception as e:
            await db.rollback()
            log.exception("trace flush failed: %s", e)
            return 0


async def flush_metrics_loop() -> None:
    """Background loop — flush every FLUSH_INTERVAL_SECONDS."""
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await flush_metrics()
        except asyncio.CancelledError:
            await flush_metrics()
            raise
        except Exception:
            log.exception("metrics flush loop iteration failed")


async def flush_traces_loop() -> None:
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await flush_traces()
        except asyncio.CancelledError:
            await flush_traces()
            raise
        except Exception:
            log.exception("trace flush loop iteration failed")


# ---------------------------------------------------------------------------
# Alert evaluator (run periodically, e.g. by internal cron)
# ---------------------------------------------------------------------------
_ALERT_CONDITIONS = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
}


async def evaluate_alerts(db: AsyncSession) -> int:
    """Evaluate every enabled alert rule against the last `window_minutes`
    of `app_metrics`. Triggers an `alert_events` row when condition matches.

    Returns number of new alert events written.
    """
    rules = (await db.execute(text("""
        SELECT id, workspace_id, name, metric_name, condition, threshold,
               window_minutes, severity
        FROM alert_rules
        WHERE enabled = TRUE
    """))).mappings().all()
    triggered = 0
    for r in rules:
        cond = _ALERT_CONDITIONS.get(r["condition"])
        if not cond:
            continue
        agg = (await db.execute(text("""
            SELECT COALESCE(SUM(metric_value), 0) AS v
            FROM app_metrics
            WHERE workspace_id = :ws AND metric_name = :n
              AND bucket_minute >= NOW() - (:win || ' minutes')::INTERVAL
        """), {"ws": r["workspace_id"], "n": r["metric_name"], "win": str(r["window_minutes"])})).mappings().first()
        v = float(agg["v"] or 0)
        if cond(v, float(r["threshold"])):
            await db.execute(text("""
                INSERT INTO alert_events (rule_id, workspace_id, metric_value, detail)
                VALUES (:rid, :ws, :v, :d)
            """), {
                "rid": r["id"], "ws": r["workspace_id"], "v": v,
                "d": f"rule '{r['name']}' triggered: {r['metric_name']} {r['condition']} {r['threshold']} (actual {v})",
            })
            triggered += 1
    if triggered:
        await db.commit()
    return triggered


# ---------------------------------------------------------------------------
# Startup hook (optional — caller wires this)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def observability_lifespan() -> AsyncIterator[None]:
    """Suggested lifespan helper — start the flush loops while the app runs."""
    metrics_task = asyncio.create_task(flush_metrics_loop(), name="zeni-metrics-flush")
    traces_task = asyncio.create_task(flush_traces_loop(), name="zeni-traces-flush")
    try:
        yield
    finally:
        for t in (metrics_task, traces_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


__all__ = [
    "metrics_collector",
    "trace_buffer",
    "TraceContext",
    "MetricsCollector",
    "record_http_request",
    "flush_metrics",
    "flush_traces",
    "flush_metrics_loop",
    "flush_traces_loop",
    "evaluate_alerts",
    "observability_lifespan",
    "SERVICE_NAME",
]
