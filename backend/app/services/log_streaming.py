"""
Realtime Log Streaming (Phase 1 P1.3 — chairman approved 2026-05-11)

Mục đích: thay vì khách phải polling /projects/{id}/logs liên tục, dùng
Server-Sent Events (SSE) để stream log realtime — tương đương Vercel Functions
logs hoặc Railway Deploy logs.

Pattern lấy cảm hứng:
  - Vercel: GET /api/v1/projects/{id}/logs?stream=1 (SSE)
  - Railway: WebSocket connection cho realtime deploy logs
  - Cloud Run native: gcloud beta run services logs tail (gRPC streaming)

Implementation:
  - SSE protocol (Server-Sent Events) — chuẩn HTTP/1.1, work qua browser + curl
  - Cloud Logging API tail filter mỗi 2s, dedupe theo insertId
  - Heartbeat (`:ping`) mỗi 15s để keep connection alive qua proxy

KHÔNG đụng /projects/{id}/logs cũ — thêm endpoint MỚI /projects/{id}/logs/stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

from googleapiclient import discovery
from googleapiclient.errors import HttpError

from app.core.config import settings

log = logging.getLogger("zeni.log_streaming")


HEARTBEAT_INTERVAL_S = 15.0
POLL_INTERVAL_S = 2.0
MAX_STREAM_DURATION_S = 600  # 10 phút max per connection (client reconnects)


async def stream_cloud_run_logs(
    *,
    cloud_run_service: str,
    region: str = "asia-southeast1",
    severity_filter: str = "DEFAULT",  # DEFAULT, INFO, WARNING, ERROR
    max_duration_s: int = MAX_STREAM_DURATION_S,
) -> AsyncIterator[str]:
    """
    Yield SSE-formatted log entries from Cloud Logging for a Cloud Run service.

    Format mỗi entry: "data: {json}\\n\\n"
    Heartbeat: ":ping\\n\\n" mỗi 15s để keep alive.

    Args:
      cloud_run_service: tên Cloud Run service (e.g., "zeni-witsagi-flatform-witsagi-prod")
      region: GCP region của service
      severity_filter: filter mức log từ DEFAULT (all) → ERROR (chỉ errors)
      max_duration_s: tối đa stream bao lâu trước khi client phải reconnect

    Yields:
      SSE-formatted strings, ready to write to HTTP response.
    """
    start_time = time.time()
    last_heartbeat = start_time
    seen_insert_ids: set[str] = set()

    # Initialize Cloud Logging client
    try:
        logging_client = discovery.build("logging", "v2", cache_discovery=False)
    except Exception as e:
        log.error("[stream] Cloud Logging client init failed: %s", e)
        yield _sse_event("error", {"code": "STREAM_INIT_FAILED", "msg": str(e)})
        return

    # Build initial filter — tail last 30s, then incremental
    cutoff_iso = _now_iso(offset_s=-30)
    severity_clause = "" if severity_filter == "DEFAULT" else f' severity>="{severity_filter}"'
    base_filter = (
        f'resource.type="cloud_run_revision" AND '
        f'resource.labels.service_name="{cloud_run_service}" AND '
        f'resource.labels.location="{region}"'
        f'{severity_clause}'
    )

    # Open with handshake event so client knows stream is live
    yield _sse_event("ready", {
        "service": cloud_run_service,
        "region": region,
        "severity_filter": severity_filter,
        "started_at": cutoff_iso,
    })

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_duration_s:
            yield _sse_event("complete", {"reason": "max_duration_reached", "duration_s": elapsed})
            return

        # Periodic heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_S:
            yield ": ping\n\n"  # SSE comment line — keeps connection alive
            last_heartbeat = time.time()

        # Fetch logs since cutoff
        try:
            entries = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _fetch_logs_sync(logging_client, base_filter, cutoff_iso),
            )
        except Exception as e:
            log.warning("[stream] log fetch failed: %s", e)
            yield _sse_event("error", {"code": "FETCH_FAILED", "msg": str(e)[:200]})
            await asyncio.sleep(POLL_INTERVAL_S * 2)  # backoff
            continue

        new_entries = []
        for entry in entries:
            insert_id = entry.get("insertId")
            if insert_id and insert_id not in seen_insert_ids:
                seen_insert_ids.add(insert_id)
                new_entries.append(entry)

        # Emit new entries
        for entry in new_entries:
            yield _sse_event("log", _format_log_entry(entry))

        # Update cutoff to most recent timestamp
        if entries:
            latest_ts = entries[-1].get("timestamp")
            if latest_ts:
                cutoff_iso = latest_ts

        # Cleanup seen IDs to prevent memory bloat (keep last 1000)
        if len(seen_insert_ids) > 1000:
            seen_insert_ids = set(list(seen_insert_ids)[-500:])

        await asyncio.sleep(POLL_INTERVAL_S)


def _fetch_logs_sync(client: "discovery.Resource", filter_str: str,
                      since_iso: str) -> list[dict]:
    """Synchronous Cloud Logging query (run in executor)."""
    full_filter = f'{filter_str} AND timestamp>"{since_iso}"'
    try:
        body = {
            "resourceNames": [f"projects/{settings.gcp_project_id}"],
            "filter": full_filter,
            "orderBy": "timestamp asc",
            "pageSize": 100,
        }
        resp = client.entries().list(body=body).execute()
        return resp.get("entries", [])
    except HttpError as e:
        log.warning("[stream] HttpError: %s", e)
        return []
    except Exception as e:
        log.warning("[stream] unexpected: %s", e)
        return []


def _format_log_entry(entry: dict) -> dict:
    """Compact log entry for SSE payload."""
    out = {
        "timestamp": entry.get("timestamp"),
        "severity": entry.get("severity", "DEFAULT"),
        "insertId": entry.get("insertId"),
    }
    # Get message — could be in textPayload, jsonPayload.message, or protoPayload
    if entry.get("textPayload"):
        out["message"] = entry["textPayload"]
    elif entry.get("jsonPayload"):
        jp = entry["jsonPayload"]
        out["message"] = jp.get("message") or jp.get("msg") or json.dumps(jp)[:500]
        out["json"] = {k: v for k, v in jp.items() if k not in ("message", "msg")}
    elif entry.get("protoPayload"):
        out["message"] = "(proto event)"
        out["proto"] = entry.get("protoPayload", {}).get("methodName", "")
    else:
        out["message"] = "(empty)"
    return out


def _sse_event(event_type: str, data: dict) -> str:
    """Format as Server-Sent Event line."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _now_iso(offset_s: int = 0) -> str:
    """Get current UTC time in ISO-8601 format with optional offset."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()
