"""
Zeni Cloud Core — Pub/Sub dispatcher (background webhook delivery worker).

Flow:
  1. publish_message() (in api/messaging.py) inserts pubsub_messages
     + N pubsub_deliveries (1 per matching subscription, status='pending')
  2. dispatch_pending_deliveries() picks due rows with FOR UPDATE SKIP LOCKED
  3. For webhook delivery: HTTP POST with HMAC SHA256 signature header
  4. On success → status='delivered', delivered_at=NOW
  5. On failure → exponential backoff (1m → 5m → 30m → 2h → 12h)
                  attempt_count >= max_retry_count → DLQ + insert dlq_messages

Cron `/internal/cron/pubsub-dispatch` runs this batch every 1 minute.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.pubsub_dispatcher")

# Backoff schedule (minutes): 1, 5, 30, 120, 720 then DLQ
BACKOFF_MINUTES = [1, 5, 30, 120, 720]
DISPATCH_TIMEOUT = 15.0
MAX_BATCH_PER_RUN = 50
PARALLEL_CHUNK = 10
SIGNATURE_HEADER = "X-Zeni-Signature"
TIMESTAMP_HEADER = "X-Zeni-Timestamp"
EVENT_ID_HEADER = "X-Zeni-Event-Id"


def _hmac_sign(secret: str, body_bytes: bytes, ts: int) -> str:
    """HMAC SHA256 signature: 'sha256=<hex>' over '<ts>.<body>'."""
    key = (secret or "").encode("utf-8")
    msg = f"{ts}.".encode("utf-8") + body_bytes
    mac = hmac.new(key, msg=msg, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def _move_to_dlq(
    db: AsyncSession, *,
    workspace_id: str, source_id: int,
    payload: dict, attempts: int, reason: str,
) -> None:
    """Insert into dlq_messages. Best-effort, don't raise."""
    try:
        await db.execute(text("""
            INSERT INTO dlq_messages
              (workspace_id, source_type, source_id, payload, failure_reason, attempts)
            VALUES
              (:ws, 'pubsub', :sid, CAST(:p AS JSONB), :r, :a)
        """), {
            "ws": workspace_id,
            "sid": source_id,
            "p": json.dumps(payload, ensure_ascii=False),
            "r": (reason or "")[:1000],
            "a": attempts,
        })
    except Exception:
        log.exception("[pubsub] failed to insert DLQ for delivery %d", source_id)


async def _try_dispatch_one(delivery_id: int, db: AsyncSession) -> dict:
    """Try to dispatch single delivery. Update status accordingly."""
    # SELECT delivery + JOIN message + subscription with row lock
    row = (await db.execute(text("""
        SELECT
          d.id, d.message_id, d.subscription_id, d.workspace_id,
          d.attempt_count,
          s.delivery_type, s.webhook_url, s.webhook_secret,
          s.max_retry_count, s.ack_deadline_seconds, s.name AS sub_name,
          m.payload, m.attributes, m.topic_id,
          t.name AS topic_name
        FROM pubsub_deliveries d
        JOIN pubsub_subscriptions s ON s.id = d.subscription_id
        JOIN pubsub_messages m       ON m.message_id = d.message_id
        JOIN pubsub_topics t         ON t.id = m.topic_id
        WHERE d.id = :id AND d.status = 'pending'
        FOR UPDATE SKIP LOCKED
    """), {"id": delivery_id})).first()

    if row is None:
        return {"id": delivery_id, "skipped": True}

    (did, msg_id, sub_id, ws, attempt_count,
     delivery_type, webhook_url, webhook_secret,
     max_retry_count, _ack_dl, sub_name,
     payload, attributes, topic_id, topic_name) = row

    # Pull-mode subscriptions don't push — just keep them pending until consumer pulls
    if delivery_type == "pull":
        return {"id": did, "skipped": True, "reason": "pull-mode"}

    if not webhook_url:
        # Misconfigured subscription — fail it and move to DLQ immediately
        await db.execute(text("""
            UPDATE pubsub_deliveries SET
              status = 'dlq', last_error = :err, attempt_count = :n
            WHERE id = :id
        """), {"id": did, "err": "subscription has no webhook_url", "n": attempt_count + 1})
        await _move_to_dlq(
            db, workspace_id=ws, source_id=did,
            payload={"message_id": msg_id, "subscription_id": sub_id, "payload": payload},
            attempts=attempt_count + 1,
            reason="missing_webhook_url",
        )
        await db.commit()
        return {"id": did, "ok": False, "dlq": True, "error": "missing_webhook_url"}

    next_attempt = attempt_count + 1
    ts = int(time.time())
    envelope = {
        "message_id": msg_id,
        "topic_id": int(topic_id),
        "topic_name": topic_name,
        "subscription_id": int(sub_id),
        "subscription_name": sub_name,
        "attributes": attributes or {},
        "payload": payload,
        "published_ts": ts,
        "attempt": next_attempt,
        "platform": "zenicloud",
    }
    body_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ZeniCloud-PubSub/1.0",
        EVENT_ID_HEADER: str(uuid.uuid4()),
        TIMESTAMP_HEADER: str(ts),
    }
    if webhook_secret:
        headers[SIGNATURE_HEADER] = _hmac_sign(webhook_secret, body_bytes, ts)

    try:
        async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
            r = await client.post(webhook_url, content=body_bytes, headers=headers)
        ok = 200 <= r.status_code < 300

        if ok:
            await db.execute(text("""
                UPDATE pubsub_deliveries SET
                  status = 'delivered',
                  attempt_count = :n,
                  delivered_at = NOW(),
                  response_code = :sc,
                  response_body = :rb,
                  last_error = NULL
                WHERE id = :id
            """), {"id": did, "n": next_attempt,
                   "sc": r.status_code, "rb": r.text[:500]})
            await db.commit()
            return {"id": did, "ok": True, "status_code": r.status_code,
                    "attempt": next_attempt}

        return await _schedule_retry(
            db, did,
            ws=ws, message_id=msg_id, subscription_id=sub_id, payload=payload,
            attempt_count=next_attempt, max_retry_count=int(max_retry_count or 5),
            status_code=r.status_code, error=f"HTTP {r.status_code}",
            response=r.text[:500],
        )
    except httpx.TimeoutException:
        return await _schedule_retry(
            db, did,
            ws=ws, message_id=msg_id, subscription_id=sub_id, payload=payload,
            attempt_count=next_attempt, max_retry_count=int(max_retry_count or 5),
            status_code=None, error="timeout", response="",
        )
    except Exception as e:
        return await _schedule_retry(
            db, did,
            ws=ws, message_id=msg_id, subscription_id=sub_id, payload=payload,
            attempt_count=next_attempt, max_retry_count=int(max_retry_count or 5),
            status_code=None, error=f"{type(e).__name__}: {e}", response="",
        )


async def _schedule_retry(
    db: AsyncSession, delivery_id: int, *,
    ws: str, message_id: str, subscription_id: int, payload: dict,
    attempt_count: int, max_retry_count: int,
    status_code: int | None, error: str, response: str,
) -> dict:
    """Either schedule next retry or move to DLQ."""
    if attempt_count >= max_retry_count:
        await db.execute(text("""
            UPDATE pubsub_deliveries SET
              status = 'dlq',
              attempt_count = :n,
              response_code = :sc,
              response_body = :rb,
              last_error = :err
            WHERE id = :id
        """), {"id": delivery_id, "n": attempt_count,
               "sc": status_code, "rb": response[:500], "err": error[:500]})
        await _move_to_dlq(
            db,
            workspace_id=ws, source_id=delivery_id,
            payload={
                "message_id": message_id,
                "subscription_id": subscription_id,
                "payload": payload,
            },
            attempts=attempt_count,
            reason=error,
        )
        await db.commit()
        log.warning("[pubsub] delivery %d → DLQ after %d attempts (%s)",
                    delivery_id, attempt_count, error[:120])
        return {"id": delivery_id, "ok": False, "dlq": True, "error": error}

    backoff_min = BACKOFF_MINUTES[min(attempt_count - 1, len(BACKOFF_MINUTES) - 1)]
    next_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)
    await db.execute(text("""
        UPDATE pubsub_deliveries SET
          status = 'pending',
          attempt_count = :n,
          next_attempt_at = :next,
          response_code = :sc,
          response_body = :rb,
          last_error = :err
        WHERE id = :id
    """), {"id": delivery_id, "n": attempt_count,
           "next": next_at, "sc": status_code,
           "rb": response[:500], "err": error[:500]})
    await db.commit()
    log.info("[pubsub] delivery %d retry %d/%d scheduled at %s",
             delivery_id, attempt_count, max_retry_count, next_at)
    return {"id": delivery_id, "ok": False,
            "retry_at": next_at.isoformat(), "attempt": attempt_count}


async def dispatch_pending_deliveries(
    db: AsyncSession, max_batch: int = MAX_BATCH_PER_RUN,
) -> dict:
    """
    Pick up all pubsub_deliveries due (next_attempt_at <= NOW), dispatch in parallel.
    Skip pull-mode subscriptions. Return summary.
    """
    rows = (await db.execute(text("""
        SELECT d.id
        FROM pubsub_deliveries d
        JOIN pubsub_subscriptions s ON s.id = d.subscription_id
        WHERE d.status = 'pending'
          AND d.next_attempt_at <= NOW()
          AND s.enabled = TRUE
          AND s.delivery_type IN ('webhook', 'queue')
        ORDER BY d.next_attempt_at ASC
        LIMIT :lim
        FOR UPDATE SKIP LOCKED
    """), {"lim": max_batch})).all()

    ids = [int(r[0]) for r in rows]
    if not ids:
        return {"processed": 0, "results": []}

    results: list = []
    for i in range(0, len(ids), PARALLEL_CHUNK):
        chunk = ids[i:i + PARALLEL_CHUNK]
        batch_results = await asyncio.gather(
            *[_try_dispatch_one(did, db) for did in chunk],
            return_exceptions=True,
        )
        for r in batch_results:
            if isinstance(r, Exception):
                results.append({"error": str(r)})
            else:
                results.append(r)

    delivered = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    dlq = sum(1 for r in results if isinstance(r, dict) and r.get("dlq"))
    log.info("[pubsub] dispatch batch: %d processed, %d delivered, %d DLQ",
             len(ids), delivered, dlq)
    return {"processed": len(ids), "delivered": delivered, "dlq": dlq, "results": results}


async def purge_expired_messages(db: AsyncSession) -> int:
    """Cron-callable: drop pubsub_messages whose expires_at has passed."""
    res = await db.execute(text("""
        DELETE FROM pubsub_messages WHERE expires_at < NOW()
    """))
    await db.commit()
    n = int(res.rowcount or 0)
    if n > 0:
        log.info("[pubsub] purged %d expired messages", n)
    return n


async def pull_messages(
    db: AsyncSession, *,
    workspace_id: str, subscription_id: int, max_messages: int = 10,
) -> list[dict]:
    """
    Pull-mode: consumer-driven retrieval of pending deliveries.
    Marks them as 'delivered' atomically (FOR UPDATE SKIP LOCKED).
    """
    if max_messages < 1 or max_messages > 100:
        max_messages = 10

    rows = (await db.execute(text("""
        WITH due AS (
          SELECT d.id
          FROM pubsub_deliveries d
          JOIN pubsub_subscriptions s ON s.id = d.subscription_id
          WHERE d.subscription_id = :sid
            AND d.workspace_id = :ws
            AND d.status = 'pending'
            AND s.delivery_type = 'pull'
            AND s.enabled = TRUE
          ORDER BY d.id ASC
          LIMIT :lim
          FOR UPDATE SKIP LOCKED
        )
        UPDATE pubsub_deliveries d
        SET status = 'delivered',
            delivered_at = NOW(),
            attempt_count = d.attempt_count + 1
        FROM due
        WHERE d.id = due.id
        RETURNING d.id, d.message_id, d.subscription_id
    """), {"sid": subscription_id, "ws": workspace_id, "lim": max_messages})).all()
    await db.commit()
    if not rows:
        return []

    msg_ids = [r[1] for r in rows]
    msgs = (await db.execute(text("""
        SELECT message_id, payload, attributes, published_at
        FROM pubsub_messages
        WHERE message_id = ANY(:ids)
    """), {"ids": msg_ids})).all()
    by_id = {m[0]: m for m in msgs}

    out: list[dict] = []
    for r in rows:
        m = by_id.get(r[1])
        if m is None:
            continue
        out.append({
            "delivery_id": int(r[0]),
            "message_id": m[0],
            "subscription_id": int(r[2]),
            "payload": m[1],
            "attributes": m[2],
            "published_at": m[3].isoformat() if m[3] else None,
        })
    return out
