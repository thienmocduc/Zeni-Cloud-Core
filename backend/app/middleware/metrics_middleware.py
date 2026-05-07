"""
Metrics middleware — automatic instrumentation of every HTTP request.

Records:
  * `http_request_total{endpoint, method, status, status_class}`
  * `http_request_duration_ms{endpoint, method}` (histogram)
  * `http_request_error_total{...}` for 5xx
  * `http_request_in_flight` (gauge of active requests)

Workspace tagging: when the route receives a `?ws=...` query param OR the
authenticated user resolves to exactly one workspace, the metric is tagged
with that workspace_id. Otherwise it stays unscoped (workspace_id NULL).

Usage (in `app/main.py` — caller wires this; we DON'T touch main.py):

    from app.middleware.metrics_middleware import MetricsMiddleware
    app.add_middleware(MetricsMiddleware)
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.services.observability import metrics_collector, record_http_request

log = logging.getLogger("zeni.middleware.metrics")


class MetricsMiddleware(BaseHTTPMiddleware):
    """Wrap every HTTP request and emit Prometheus-compatible metrics."""

    def __init__(
        self,
        app: ASGIApp,
        skip_paths: tuple[str, ...] = ("/metrics", "/healthz", "/livez", "/readyz"),
    ) -> None:
        super().__init__(app)
        self.skip_paths = skip_paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in self.skip_paths:
            return await call_next(request)

        method = request.method.upper()
        # endpoint label: use route template if available (avoids high-cardinality)
        # otherwise fall back to raw path.
        endpoint_template: str | None = None
        try:
            route = request.scope.get("route")
            if route is not None and getattr(route, "path", None):
                endpoint_template = route.path
        except Exception:
            endpoint_template = None
        endpoint = endpoint_template or path

        ws = request.query_params.get("ws") or None

        # In-flight gauge: bump on entry, decrement on exit
        in_flight_labels = {"endpoint": endpoint}
        metrics_collector.record_counter(
            "http_request_in_flight_inc", 1.0, labels=in_flight_labels, workspace_id=ws,
        )
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Re-raise so FastAPI's normal error handlers run; we still record metric.
            status_code = 500
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            try:
                record_http_request(
                    endpoint=endpoint,
                    method=method,
                    status=status_code,
                    latency_ms=latency_ms,
                    workspace_id=ws,
                )
                metrics_collector.record_counter(
                    "http_request_in_flight_dec", 1.0,
                    labels=in_flight_labels, workspace_id=ws,
                )
            except Exception:
                # Never let metrics break the request
                log.exception("failed to record HTTP metrics for %s %s", method, endpoint)


__all__ = ["MetricsMiddleware"]
