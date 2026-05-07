"""
Zeni Cloud Core — Queue service (Postgres SKIP LOCKED).

Pattern: 1 bảng public.queue_jobs cho TẤT CẢ workspaces & queues.
Pull dùng `FOR UPDATE SKIP LOCKED` → nhiều worker concurrent không tranh.

Public API (async):
  queue_push(db, ws, queue_name, payload, delay_seconds=0, max_attempts=3)
    -> {"job_id": int, "available_at": iso}
  queue_pull(db, ws, queue_name, lease_seconds=60)
    -> {job_id, payload, attempts, lease_token, leased_until} | None
  queue_ack(db, ws, queue_name, job_id, lease_token, success, error=None)
    -> {ok, status}
  queue_stats(db, ws, queue_name)
    -> {pending, leased, completed, failed, dead_letter}
  queue_reclaim_expired_leases(db)
    -> int   (cron-callable)

Validate:
  - queue_name: ^[a-z][a-z0-9_-]{0,30}$
  - payload: JSON-serializable, max 1MB
  - delay_seconds: 0..86400*7
  - max_attempts: 1..20
  - lease_seconds: 1..3600
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.services.queue")

QUEUE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
MAX_DELAY_SECONDS = 86400 * 7        # 7 ngày
MAX_LEASE_SECONDS = 3600             # 1 giờ
MAX_PAYLOAD_BYTES = 1_000_000        # 1MB
MAX_MAX_ATTEMPTS = 20

# Backoff khi job fail nhưng còn attempt: 30s, 2m, 10m, 30m, 2h, 6h, 24h ...
_BACKOFF_SECONDS = [30, 120, 600, 1800, 7200, 21600, 86400]


def _validate_queue_name(name: str) -> None:
    if not name or not isinstance(name, str) or not QUEUE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="queue_name không hợp lệ. Định dạng: bắt đầu chữ thường, [a-z0-9_-], max 31 ký tự",
        )


def _validate_payload(payload: Any) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"payload không JSON-serializable: {e}")
    if len(s.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"payload vượt {MAX_PAYLOAD_BYTES} bytes")
    return s


def _backoff_for_attempt(attempts: int) -> int:
    """attempts đã tính sau khi increment. attempts=1 → backoff[0]."""
    idx = max(0, min(attempts - 1, len(_BACKOFF_SECONDS) - 1))
    return _BACKOFF_SECONDS[idx]


async def queue_push(
    db: AsyncSession,
    workspace_id: str,
    queue_name: str,
    payload: Any,
    delay_seconds: int = 0,
    max_attempts: int = 3,
) -> dict:
    """Enqueue job. Return {"job_id": int, "available_at": ISO str}."""
    _validate_queue_name(queue_name)
    payload_json = _validate_payload(payload)

    if not isinstance(delay_seconds, int) or delay_seconds < 0 or delay_seconds > MAX_DELAY_SECONDS:
        raise HTTPException(status_code=400, detail=f"delay_seconds phải 0..{MAX_DELAY_SECONDS}")
    if not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > MAX_MAX_ATTEMPTS:
        raise HTTPException(status_code=400, detail=f"max_attempts phải 1..{MAX_MAX_ATTEMPTS}")

    row = (await db.execute(text("""
        INSERT INTO public.queue_jobs
          (workspace_id, queue_name, payload, status, max_attempts, available_at)
        VALUES
          (:ws, :qn, CAST(:p AS JSONB), 'pending', :ma,
           NOW() + (:d || ' seconds')::interval)
        RETURNING id, available_at
    """), {
        "ws": workspace_id,
        "qn": queue_name,
        "p": payload_json,
        "ma": max_attempts,
        "d": str(delay_seconds),
    })).first()
    await db.commit()

    if row is None:
        raise HTTPException(status_code=502, detail="không insert được job")

    return {
        "job_id": int(row[0]),
        "available_at": row[1].isoformat() if row[1] else None,
    }


async def queue_pull(
    db: AsyncSession,
    workspace_id: str,
    queue_name: str,
    lease_seconds: int = 60,
) -> dict | None:
    """Pull 1 job pending (oldest available_at) + lease nó.

    Dùng FOR UPDATE SKIP LOCKED → nhiều worker đồng thời không double-pull.
    Trả None nếu queue rỗng.
    """
    _validate_queue_name(queue_name)
    if not isinstance(lease_seconds, int) or lease_seconds < 1 or lease_seconds > MAX_LEASE_SECONDS:
        raise HTTPException(status_code=400, detail=f"lease_seconds phải 1..{MAX_LEASE_SECONDS}")

    lease_token = str(uuid.uuid4())

    # Single round-trip: SELECT + UPDATE bằng CTE để giữ tính atomic
    # và đảm bảo SKIP LOCKED được honor.
    row = (await db.execute(text("""
        WITH next_job AS (
          SELECT id
          FROM public.queue_jobs
          WHERE workspace_id = :ws
            AND queue_name = :qn
            AND status = 'pending'
            AND available_at <= NOW()
          ORDER BY available_at ASC, id ASC
          LIMIT 1
          FOR UPDATE SKIP LOCKED
        )
        UPDATE public.queue_jobs q
        SET status       = 'leased',
            leased_until = NOW() + (:ls || ' seconds')::interval,
            lease_token  = CAST(:tok AS UUID),
            attempts     = q.attempts + 1
        FROM next_job
        WHERE q.id = next_job.id
        RETURNING q.id, q.payload, q.attempts, q.lease_token, q.leased_until, q.max_attempts
    """), {
        "ws": workspace_id,
        "qn": queue_name,
        "ls": str(lease_seconds),
        "tok": lease_token,
    })).first()
    await db.commit()

    if row is None:
        return None

    return {
        "job_id": int(row[0]),
        "payload": row[1],
        "attempts": int(row[2]),
        "max_attempts": int(row[5]),
        "lease_token": str(row[3]),
        "leased_until": row[4].isoformat() if row[4] else None,
    }


async def queue_ack(
    db: AsyncSession,
    workspace_id: str,
    queue_name: str,
    job_id: int,
    lease_token: str,
    success: bool,
    error: str | None = None,
) -> dict:
    """Ack 1 job đã pull.

    Bắt buộc verify lease_token KHỚP — chống race condition khi lease expired
    + worker khác đã pull lại job.

    success=True  → completed
    success=False → attempts >= max_attempts ? dead_letter : pending (backoff)
    """
    _validate_queue_name(queue_name)
    if not isinstance(job_id, int) or job_id <= 0:
        raise HTTPException(status_code=400, detail="job_id không hợp lệ")
    if not lease_token or not isinstance(lease_token, str):
        raise HTTPException(status_code=400, detail="lease_token bắt buộc")
    try:
        uuid.UUID(lease_token)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="lease_token định dạng UUID không hợp lệ")

    # Load job, verify lease + workspace + queue + status
    row = (await db.execute(text("""
        SELECT id, status, attempts, max_attempts, lease_token
        FROM public.queue_jobs
        WHERE id = :id AND workspace_id = :ws AND queue_name = :qn
        FOR UPDATE
    """), {"id": job_id, "ws": workspace_id, "qn": queue_name})).first()

    if row is None:
        raise HTTPException(status_code=404, detail="job không tồn tại trong workspace/queue")

    db_status = row[1]
    db_attempts = int(row[2])
    db_max = int(row[3])
    db_token = str(row[4]) if row[4] else None

    if db_status != "leased":
        raise HTTPException(status_code=409, detail=f"job đã ở trạng thái '{db_status}', không ack được")

    if db_token != lease_token:
        # Có thể lease đã reclaimed + worker khác đã pull → token mới khác.
        raise HTTPException(status_code=403, detail="lease_token không khớp (lease đã hết hạn hoặc bị thay thế)")

    if success:
        await db.execute(text("""
            UPDATE public.queue_jobs
            SET status = 'completed',
                completed_at = NOW(),
                lease_token = NULL,
                leased_until = NULL,
                last_error = NULL
            WHERE id = :id
        """), {"id": job_id})
        await db.commit()
        return {"ok": True, "status": "completed"}

    # Failure path
    if db_attempts >= db_max:
        await db.execute(text("""
            UPDATE public.queue_jobs
            SET status = 'dead_letter',
                last_error = :err,
                lease_token = NULL,
                leased_until = NULL,
                completed_at = NOW()
            WHERE id = :id
        """), {"id": job_id, "err": (error or "")[:2000]})
        await db.commit()
        log.warning("[queue] job %d → dead_letter (attempts %d/%d): %s",
                    job_id, db_attempts, db_max, (error or "")[:200])
        return {"ok": True, "status": "dead_letter"}

    # Còn attempt → trả về pending với backoff
    backoff = _backoff_for_attempt(db_attempts)
    await db.execute(text("""
        UPDATE public.queue_jobs
        SET status = 'pending',
            available_at = NOW() + (:b || ' seconds')::interval,
            lease_token = NULL,
            leased_until = NULL,
            last_error = :err
        WHERE id = :id
    """), {"id": job_id, "b": str(backoff), "err": (error or "")[:2000]})
    await db.commit()
    return {"ok": True, "status": "pending", "retry_in_seconds": backoff}


async def queue_stats(
    db: AsyncSession,
    workspace_id: str,
    queue_name: str,
) -> dict:
    """Đếm theo status. Trả tất cả 5 buckets (kể cả 0)."""
    _validate_queue_name(queue_name)
    rows = (await db.execute(text("""
        SELECT status, COUNT(*)::BIGINT
        FROM public.queue_jobs
        WHERE workspace_id = :ws AND queue_name = :qn
        GROUP BY status
    """), {"ws": workspace_id, "qn": queue_name})).all()

    out = {"pending": 0, "leased": 0, "completed": 0, "failed": 0, "dead_letter": 0}
    for r in rows:
        st = r[0]
        if st in out:
            out[st] = int(r[1])
    return out


async def queue_reclaim_expired_leases(db: AsyncSession) -> int:
    """Cron-callable: lease quá hạn → đẩy về pending lại.

    KHÔNG increment attempts ở đây — attempts đã tăng khi pull. Worker chết
    → coi như fail attempt đó, pending sẽ đợi backoff dựa theo attempts hiện tại.
    """
    res = await db.execute(text("""
        UPDATE public.queue_jobs
        SET status = 'pending',
            lease_token = NULL,
            leased_until = NULL,
            available_at = GREATEST(
              available_at,
              NOW() + (
                CASE
                  WHEN attempts >= 7 THEN INTERVAL '86400 seconds'
                  WHEN attempts >= 5 THEN INTERVAL '7200 seconds'
                  WHEN attempts >= 3 THEN INTERVAL '600 seconds'
                  ELSE INTERVAL '30 seconds'
                END
              )
            ),
            last_error = COALESCE(last_error, 'lease_expired')
        WHERE status = 'leased' AND leased_until < NOW()
    """))
    await db.commit()
    n = int(res.rowcount or 0)
    if n > 0:
        log.info("[queue] reclaimed %d expired leases", n)
    return n
