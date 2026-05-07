"""
Zeni Cloud Core — L4 Queue API (Postgres SKIP LOCKED).

Endpoints (CHỈ có 4):
  POST /queue/{name}/push?ws=    body {payload, delay_seconds?, max_attempts?}
  POST /queue/{name}/pull?ws=    body {lease_seconds?}
  POST /queue/{name}/ack?ws=     body {job_id, lease_token, success, error?}
  GET  /queue/{name}/stats?ws=

Quy tắc:
  - Mọi endpoint require_user + require_workspace_access(ws)
  - PAT scope check: cần "automation" hoặc "full"
  - Audit MỌI state-changing op: queue.push, queue.ack
  - queue.pull KHÔNG audit (high-frequency, tạo noise)
  - Try/except Exception → 502
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import queue as queue_svc
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.queue")
router = APIRouter(prefix="/queue", tags=["queue"])


# ─── Schemas ─────────────────────────────────────────
class QueuePushIn(BaseModel):
    payload: Any = Field(..., description="JSON-serializable payload")
    delay_seconds: int = Field(default=0, ge=0, le=86400 * 7,
                               description="Đợi N giây trước khi job available")
    max_attempts: int = Field(default=3, ge=1, le=20)


class QueuePullIn(BaseModel):
    lease_seconds: int = Field(default=60, ge=1, le=3600,
                               description="Worker giữ lease bao lâu trước khi auto-reclaim")


class QueueAckIn(BaseModel):
    job_id: int = Field(..., gt=0)
    lease_token: str = Field(..., min_length=1, max_length=64)
    success: bool
    error: str | None = Field(default=None, max_length=2000)


# ─── Helpers ─────────────────────────────────────────
def _check_scope(me: CurrentUser) -> None:
    """PAT phải có scope 'automation' hoặc 'full'."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "automation" not in scopes:
        raise HTTPException(status_code=403, detail="PAT cần scope 'automation' hoặc 'full' để dùng /queue")


# ─── Endpoints ───────────────────────────────────────
@router.post("/{name}/push")
async def push_job(
    name: str,
    ws: str,
    data: QueuePushIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Enqueue 1 job vào queue {name}."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được push job")

    try:
        result = await queue_svc.queue_push(
            db, ws, name, data.payload,
            delay_seconds=data.delay_seconds,
            max_attempts=data.max_attempts,
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("queue_push failed for ws=%s queue=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"không push được job: {type(e).__name__}")

    # Audit (state-changing)
    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="queue.push", target=f"{name}#{result['job_id']}",
            severity="ok",
            metadata={
                "queue": name, "delay_seconds": data.delay_seconds,
                "max_attempts": data.max_attempts,
                "payload_keys": list(data.payload.keys()) if isinstance(data.payload, dict) else "non_dict",
            },
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for queue.push (best-effort)")
        await db.rollback()

    return {"ok": True, **result}


@router.post("/{name}/pull")
async def pull_job(
    name: str,
    ws: str,
    data: QueuePullIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict | None:
    """Pull next pending job + lease nó. Trả null nếu queue rỗng."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được pull job")

    try:
        result = await queue_svc.queue_pull(db, ws, name, lease_seconds=data.lease_seconds)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("queue_pull failed for ws=%s queue=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"không pull được job: {type(e).__name__}")

    return result  # None hoặc {job_id, payload, ...}


@router.post("/{name}/ack")
async def ack_job(
    name: str,
    ws: str,
    data: QueueAckIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ack 1 job. success=True → completed; False → backoff hoặc dead_letter."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được ack job")

    try:
        result = await queue_svc.queue_ack(
            db, ws, name,
            job_id=data.job_id,
            lease_token=data.lease_token,
            success=data.success,
            error=data.error,
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("queue_ack failed for ws=%s queue=%s job=%s", ws, name, data.job_id)
        raise HTTPException(status_code=502, detail=f"không ack được job: {type(e).__name__}")

    # Audit (state-changing) — severity dựa trên kết quả
    try:
        sev = "ok" if data.success else ("err" if result.get("status") == "dead_letter" else "warn")
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="queue.ack", target=f"{name}#{data.job_id}",
            severity=sev,
            metadata={
                "queue": name, "success": data.success,
                "result_status": result.get("status"),
                "error_excerpt": (data.error or "")[:200] if data.error else None,
            },
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for queue.ack (best-effort)")
        await db.rollback()

    return result


@router.get("/{name}/stats")
async def stats_job(
    name: str,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Đếm job theo status: pending|leased|completed|failed|dead_letter."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        stats = await queue_svc.queue_stats(db, ws, name)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("queue_stats failed for ws=%s queue=%s", ws, name)
        raise HTTPException(status_code=502, detail=f"không lấy được stats: {type(e).__name__}")

    return {"workspace_id": ws, "queue": name, **stats}
