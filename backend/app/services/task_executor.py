"""
Zeni Cloud Core — Scheduled task executor (Cloud Tasks-like).

Cron `/internal/cron/tasks-execute` runs this batch every 30s:
  - SELECT scheduled_tasks WHERE status='pending' AND scheduled_at <= NOW()
                                       FOR UPDATE SKIP LOCKED LIMIT 20
  - Execute HTTP request to target_url with headers/body
  - On success (2xx) → status='succeeded'
  - On failure / non-2xx / timeout → retry_count+1
       attempt 1 → +1 min     (exponential backoff)
       attempt 2 → +5 min
       attempt 3 → +30 min
       retry_count >= max_retries → status='dlq' + insert dlq_messages
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.task_executor")

EXEC_TIMEOUT = 25.0
MAX_BATCH_PER_RUN = 20
PARALLEL_CHUNK = 5
BACKOFF_MINUTES = [1, 5, 30, 120, 720]   # progressive


async def _move_task_to_dlq(
    db: AsyncSession, *,
    workspace_id: str, source_id: int, payload: dict[str, Any],
    attempts: int, reason: str,
) -> None:
    """Insert into dlq_messages. Best-effort."""
    try:
        await db.execute(text("""
            INSERT INTO dlq_messages
              (workspace_id, source_type, source_id, payload, failure_reason, attempts)
            VALUES
              (:ws, 'task', :sid, CAST(:p AS JSONB), :r, :a)
        """), {
            "ws": workspace_id,
            "sid": source_id,
            "p": json.dumps(payload, ensure_ascii=False),
            "r": (reason or "")[:1000],
            "a": attempts,
        })
    except Exception:
        log.exception("[task] failed to insert DLQ for task %d", source_id)


async def _try_execute_one(task_id: int, db: AsyncSession) -> dict:
    """Execute a single scheduled_tasks row."""
    row = (await db.execute(text("""
        SELECT id, workspace_id, task_name, target_url, method,
               headers, body, retry_count, max_retries
        FROM scheduled_tasks
        WHERE id = :id AND status = 'pending'
        FOR UPDATE SKIP LOCKED
    """), {"id": task_id})).first()

    if row is None:
        return {"id": task_id, "skipped": True}

    (tid, ws, name, url, method,
     headers, body, retry_count, max_retries) = row
    next_attempt = (retry_count or 0) + 1

    h = {"Content-Type": "application/json", "User-Agent": "ZeniCloud-Tasks/1.0"}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(k, str) and isinstance(v, str):
                h[k] = v

    body_json: bytes | None = None
    if body is not None:
        try:
            body_json = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except Exception:
            body_json = None

    try:
        async with httpx.AsyncClient(timeout=EXEC_TIMEOUT) as client:
            if (method or "POST").upper() == "GET":
                r = await client.get(url, headers=h)
            elif (method or "POST").upper() == "DELETE":
                r = await client.delete(url, headers=h)
            elif (method or "POST").upper() == "PUT":
                r = await client.put(url, content=body_json, headers=h)
            elif (method or "POST").upper() == "PATCH":
                r = await client.patch(url, content=body_json, headers=h)
            else:  # POST
                r = await client.post(url, content=body_json, headers=h)

        ok = 200 <= r.status_code < 300
        if ok:
            await db.execute(text("""
                UPDATE scheduled_tasks SET
                  status = 'succeeded',
                  retry_count = :n,
                  executed_at = NOW(),
                  response_code = :sc,
                  response_body = :rb,
                  last_error = NULL
                WHERE id = :id
            """), {"id": tid, "n": next_attempt,
                   "sc": r.status_code, "rb": r.text[:500]})
            await db.commit()
            return {"id": tid, "ok": True, "status_code": r.status_code,
                    "attempt": next_attempt}

        return await _schedule_retry_task(
            db, tid, ws=ws, name=name, url=url, body=body,
            attempt_count=next_attempt, max_retries=int(max_retries or 3),
            status_code=r.status_code, error=f"HTTP {r.status_code}",
            response=r.text[:500],
        )
    except httpx.TimeoutException:
        return await _schedule_retry_task(
            db, tid, ws=ws, name=name, url=url, body=body,
            attempt_count=next_attempt, max_retries=int(max_retries or 3),
            status_code=None, error="timeout", response="",
        )
    except Exception as e:
        return await _schedule_retry_task(
            db, tid, ws=ws, name=name, url=url, body=body,
            attempt_count=next_attempt, max_retries=int(max_retries or 3),
            status_code=None, error=f"{type(e).__name__}: {e}", response="",
        )


async def _schedule_retry_task(
    db: AsyncSession, task_id: int, *,
    ws: str, name: str, url: str, body: Any,
    attempt_count: int, max_retries: int,
    status_code: int | None, error: str, response: str,
) -> dict:
    """Either reschedule for retry or move task to DLQ."""
    if attempt_count > max_retries:
        await db.execute(text("""
            UPDATE scheduled_tasks SET
              status = 'dlq',
              retry_count = :n,
              executed_at = NOW(),
              response_code = :sc,
              response_body = :rb,
              last_error = :err
            WHERE id = :id
        """), {"id": task_id, "n": attempt_count,
               "sc": status_code, "rb": response[:500], "err": error[:500]})
        await _move_task_to_dlq(
            db, workspace_id=ws, source_id=task_id,
            payload={"task_name": name, "target_url": url, "body": body},
            attempts=attempt_count, reason=error,
        )
        await db.commit()
        log.warning("[task] task %d → DLQ after %d attempts (%s)",
                    task_id, attempt_count, error[:120])
        return {"id": task_id, "ok": False, "dlq": True, "error": error}

    backoff_min = BACKOFF_MINUTES[min(attempt_count - 1, len(BACKOFF_MINUTES) - 1)]
    next_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)
    await db.execute(text("""
        UPDATE scheduled_tasks SET
          status = 'pending',
          retry_count = :n,
          scheduled_at = :next,
          response_code = :sc,
          response_body = :rb,
          last_error = :err
        WHERE id = :id
    """), {"id": task_id, "n": attempt_count, "next": next_at,
           "sc": status_code, "rb": response[:500], "err": error[:500]})
    await db.commit()
    log.info("[task] task %d retry %d/%d scheduled at %s",
             task_id, attempt_count, max_retries, next_at)
    return {"id": task_id, "ok": False, "retry_at": next_at.isoformat(),
            "attempt": attempt_count}


async def execute_due_tasks(
    db: AsyncSession, max_batch: int = MAX_BATCH_PER_RUN,
) -> dict:
    """Pick up scheduled_tasks due now, execute in parallel chunks."""
    rows = (await db.execute(text("""
        SELECT id FROM scheduled_tasks
        WHERE status = 'pending' AND scheduled_at <= NOW()
        ORDER BY scheduled_at ASC
        LIMIT :lim
        FOR UPDATE SKIP LOCKED
    """), {"lim": max_batch})).all()

    ids = [int(r[0]) for r in rows]
    if not ids:
        return {"processed": 0, "results": []}

    results: list = []
    for i in range(0, len(ids), PARALLEL_CHUNK):
        chunk = ids[i:i + PARALLEL_CHUNK]
        batch = await asyncio.gather(
            *[_try_execute_one(tid, db) for tid in chunk],
            return_exceptions=True,
        )
        for r in batch:
            if isinstance(r, Exception):
                results.append({"error": str(r)})
            else:
                results.append(r)

    succeeded = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    dlq = sum(1 for r in results if isinstance(r, dict) and r.get("dlq"))
    log.info("[task] execute batch: %d processed, %d succeeded, %d DLQ",
             len(ids), succeeded, dlq)
    return {"processed": len(ids), "succeeded": succeeded, "dlq": dlq, "results": results}
