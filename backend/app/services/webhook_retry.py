"""
Webhook retry queue with exponential backoff + Dead Letter Queue.

Flow:
  1. fire_event() → enqueue to webhook_attempts (status=pending, next_attempt_at=NOW)
  2. background dispatcher picks up due rows, makes HTTP POST
  3. If success (2xx) → status=succeeded
  4. If fail (non-2xx, timeout, network) → increment attempt_count, schedule next:
       attempt 1 → +1 min
       attempt 2 → +5 min
       attempt 3 → +30 min
       attempt 4 → +2 hours
       attempt 5 → DLQ (status='dlq', alert admin)
  5. Cron `/internal/cron/webhook-dispatch` chạy mỗi 1 phút pick up due retries
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.webhook_retry")

# Backoff schedule (minutes): 1, 5, 30, 120, then DLQ
BACKOFF_MINUTES = [1, 5, 30, 120, 360]
DISPATCH_TIMEOUT = 15.0
MAX_BATCH_PER_RUN = 50


async def enqueue_dispatch(
    db: AsyncSession, *,
    workspace_id: str, connector_id: str | None,
    source: str, action: str, target_url: str,
    payload: dict, headers: dict | None = None,
    actor: str | None = None, max_attempts: int = 5,
) -> int:
    """Enqueue webhook attempt — returns id."""
    row = (await db.execute(text("""
        INSERT INTO webhook_attempts(workspace_id, connector_id, source, action,
                                     target_url, payload, headers, max_attempts,
                                     actor, next_attempt_at)
        VALUES(:w, CAST(:c AS UUID), :s, :a, :u,
               CAST(:p AS JSONB), CAST(:h AS JSONB),
               :m, :ac, NOW())
        RETURNING id
    """), {
        "w": workspace_id,
        "c": connector_id,
        "s": source, "a": action, "u": target_url,
        "p": json.dumps(payload, ensure_ascii=False),
        "h": json.dumps(headers or {}, ensure_ascii=False),
        "m": max_attempts, "ac": actor,
    })).first()
    await db.commit()
    return int(row[0])


async def _try_dispatch_one(attempt_id: int, db: AsyncSession) -> dict:
    """Try to dispatch single webhook attempt. Update status accordingly."""
    row = (await db.execute(text("""
        SELECT id, workspace_id, target_url, payload, headers,
               attempt_count, max_attempts, source, action
        FROM webhook_attempts WHERE id = :id AND status = 'pending'
        FOR UPDATE SKIP LOCKED
    """), {"id": attempt_id})).first()
    if row is None:
        return {"id": attempt_id, "skipped": True}

    aid, ws, url, payload, headers, attempt_count, max_attempts, source, action = row
    next_attempt = attempt_count + 1

    # Mark first_attempted_at if first try
    if attempt_count == 0:
        await db.execute(
            text("UPDATE webhook_attempts SET first_attempted_at=NOW() WHERE id=:id"),
            {"id": aid}
        )

    body_envelope = {
        "source": source, "action": action,
        "payload": payload, "ts": int(time.time()),
        "platform": "zenicloud", "attempt": next_attempt,
    }

    h = {"Content-Type": "application/json", "User-Agent": "ZeniCloud-Webhook/1.0"}
    if isinstance(headers, dict):
        h.update(headers)
    elif headers:
        try: h.update(dict(headers))
        except Exception: pass

    try:
        async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
            r = await client.post(url, json=body_envelope, headers=h)
        ok = 200 <= r.status_code < 300
        if ok:
            await db.execute(text("""
                UPDATE webhook_attempts SET
                  status = 'succeeded', last_status_code = :sc,
                  last_response = :resp, attempt_count = :n,
                  succeeded_at = NOW(), updated_at = NOW()
                WHERE id = :id
            """), {"id": aid, "sc": r.status_code, "resp": r.text[:500], "n": next_attempt})
            await db.commit()
            return {"id": aid, "ok": True, "status": r.status_code, "attempt": next_attempt}
        else:
            return await _schedule_retry(db, aid, next_attempt, max_attempts,
                                          status_code=r.status_code,
                                          error=f"HTTP {r.status_code}",
                                          response=r.text[:500])
    except httpx.TimeoutException:
        return await _schedule_retry(db, aid, next_attempt, max_attempts,
                                      status_code=None, error="timeout")
    except Exception as e:
        return await _schedule_retry(db, aid, next_attempt, max_attempts,
                                      status_code=None, error=f"{type(e).__name__}: {e}")


async def _schedule_retry(
    db: AsyncSession, aid: int, attempt_count: int, max_attempts: int,
    *, status_code: int | None = None, error: str = "", response: str = ""
) -> dict:
    """Either schedule next retry or move to DLQ."""
    if attempt_count >= max_attempts:
        # → DLQ
        await db.execute(text("""
            UPDATE webhook_attempts SET
              status = 'dlq', last_status_code = :sc, last_error = :err,
              last_response = :resp, attempt_count = :n,
              dlq_at = NOW(), updated_at = NOW()
            WHERE id = :id
        """), {"id": aid, "sc": status_code, "err": error[:500],
                "resp": response[:500], "n": attempt_count})
        await db.commit()
        log.warning("[webhook_retry] attempt id=%d exhausted retries → DLQ (%s)", aid, error)
        return {"id": aid, "ok": False, "dlq": True, "error": error}

    # Schedule next attempt
    backoff_min = BACKOFF_MINUTES[min(attempt_count - 1, len(BACKOFF_MINUTES) - 1)]
    next_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)
    await db.execute(text("""
        UPDATE webhook_attempts SET
          status = 'pending', last_status_code = :sc, last_error = :err,
          last_response = :resp, attempt_count = :n,
          next_attempt_at = :next, updated_at = NOW()
        WHERE id = :id
    """), {"id": aid, "sc": status_code, "err": error[:500],
            "resp": response[:500], "n": attempt_count, "next": next_at})
    await db.commit()
    log.info("[webhook_retry] id=%d retry %d/%d scheduled at %s",
             aid, attempt_count, max_attempts, next_at)
    return {"id": aid, "ok": False, "retry_at": next_at.isoformat(), "attempt": attempt_count}


async def process_due_batch(db: AsyncSession, max_batch: int = MAX_BATCH_PER_RUN) -> dict:
    """Pick up all webhook_attempts due (next_attempt_at <= NOW), dispatch in parallel."""
    rows = (await db.execute(text("""
        SELECT id FROM webhook_attempts
        WHERE status = 'pending' AND next_attempt_at <= NOW()
        ORDER BY next_attempt_at ASC
        LIMIT :lim
        FOR UPDATE SKIP LOCKED
    """), {"lim": max_batch})).all()
    ids = [r[0] for r in rows]
    if not ids:
        return {"processed": 0, "results": []}
    # Dispatch in parallel batches of 10
    results = []
    for i in range(0, len(ids), 10):
        chunk = ids[i:i+10]
        batch_results = await asyncio.gather(
            *[_try_dispatch_one(aid, db) for aid in chunk],
            return_exceptions=True,
        )
        for r in batch_results:
            if isinstance(r, Exception):
                results.append({"error": str(r)})
            else:
                results.append(r)
    return {"processed": len(ids), "results": results}
