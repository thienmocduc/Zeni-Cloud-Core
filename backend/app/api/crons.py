"""
Zeni Cloud Core — L4 Cron Scheduler API.

Wraps Cloud Scheduler. Khách dùng cho tasks định kỳ:
  - ANIMA escrow release 7 ngày
  - Daily report email
  - Cleanup old data
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.scheduler import (
    CronError,
    create_cron,
    delete_cron,
    list_crons,
    pause_cron,
    resume_cron,
    run_cron_now,
)

log = logging.getLogger("zeni.api.crons")
router = APIRouter(prefix="/automation/crons", tags=["automation", "crons"])

# Hard-cap to prevent abuse
MAX_CRONS_PER_WS = 30
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,47}$")
_CRON_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")  # 5 fields cron expression
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _validate_cron_create(name: str, schedule: str, target_url: str, method: str) -> None:
    """Pre-flight 422 validation — friendly VN messages BEFORE Cloud Scheduler call."""
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"Cron name '{name}' không hợp lệ: dùng chữ thường + số + dấu '-', 3-48 ký tự, bắt đầu bằng chữ/số.",
        )
    if not _CRON_RE.match(schedule):
        raise HTTPException(
            status_code=422,
            detail=f"Cron expression '{schedule}' không hợp lệ. Format: 'phút giờ ngày tháng thứ' (5 trường). VD '0 7 * * *' = 7AM daily.",
        )
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        raise HTTPException(
            status_code=422,
            detail=f"target_url '{target_url[:80]}' phải bắt đầu bằng http:// hoặc https://.",
        )
    if method.upper() not in _HTTP_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"http_method '{method}' không support. Dùng: {sorted(_HTTP_METHODS)}.",
        )


class CronCreateIn(BaseModel):
    name: str = Field(min_length=3, max_length=48, pattern=r"^[a-z0-9][a-z0-9\-]{2,47}$")
    schedule: str = Field(min_length=9, max_length=64,
                          description="Cron expression: 'minute hour day month weekday'")
    target_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=512)
    method: str = Field(default="POST", pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=8192)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=64)
    description: str | None = Field(default=None, max_length=255)


class CronUpdateIn(BaseModel):
    schedule: str | None = None
    target_url: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    body: str | None = None


@router.get("")
async def list_workspace_crons(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    try:
        jobs = list_crons(ws)
    except CronError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"workspace": ws, "count": len(jobs), "crons": jobs}


@router.post("", status_code=201)
async def create_workspace_cron(
    ws: str,
    data: CronCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không tạo cron được")

    # Pre-flight validation (422 fast feedback) — name, schedule, url, method
    _validate_cron_create(data.name, data.schedule, data.target_url, data.method)

    # Cap per-workspace
    try:
        existing = list_crons(ws)
        if len(existing) >= MAX_CRONS_PER_WS:
            raise HTTPException(status_code=429,
                                detail=f"Vượt giới hạn {MAX_CRONS_PER_WS} crons/workspace")
        # Name conflict check
        if any(j.get("name") == data.name for j in existing):
            raise HTTPException(
                status_code=409,
                detail=f"Cron name '{data.name}' đã tồn tại trong workspace '{ws}'. Đặt tên khác hoặc xóa cron cũ.",
            )
    except CronError:
        pass  # ignore list error during create

    try:
        job = create_cron(
            workspace=ws, name=data.name, schedule=data.schedule,
            target_url=data.target_url, method=data.method,
            headers=data.headers, body=data.body, timezone=data.timezone,
            description=data.description,
        )
    except CronError as e:
        raise HTTPException(status_code=502, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="cron.create",
        target=data.name, severity="ok",
        metadata={"schedule": data.schedule, "url": data.target_url, "method": data.method},
    )
    await billing_push(db, workspace_id=ws, layer="L4", action="cron.create", cost_usd=0.00001)
    await db.commit()
    return job


@router.delete("/{name}", status_code=204, response_class=Response)
async def delete_workspace_cron(
    ws: str,
    name: str,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete cron — runs Cloud Scheduler delete in background to avoid LB 30s timeout."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    await audit_push(db, actor=me.email, workspace_id=ws, action="cron.delete",
                     target=name, severity="warn")
    await db.commit()

    def _bg_delete():
        try:
            delete_cron(workspace=ws, name=name)
            log.info("[cron.delete bg] %s/%s OK", ws, name)
        except CronError as e:
            log.exception("[cron.delete bg] %s/%s failed: %s", ws, name, e)

    bg.add_task(_bg_delete)
    return Response(status_code=204)


@router.post("/{name}/pause")
async def pause_workspace_cron(
    ws: str, name: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    try:
        job = pause_cron(workspace=ws, name=name)
    except CronError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await audit_push(db, actor=me.email, workspace_id=ws, action="cron.pause", target=name, severity="info")
    await db.commit()
    return job


@router.post("/{name}/resume")
async def resume_workspace_cron(
    ws: str, name: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    try:
        job = resume_cron(workspace=ws, name=name)
    except CronError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await audit_push(db, actor=me.email, workspace_id=ws, action="cron.resume", target=name, severity="info")
    await db.commit()
    return job


@router.post("/{name}/run-now")
async def run_workspace_cron_now(
    ws: str, name: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không chạy cron được")
    try:
        job = run_cron_now(workspace=ws, name=name)
    except CronError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await audit_push(db, actor=me.email, workspace_id=ws, action="cron.run_now",
                     target=name, severity="ok")
    await billing_push(db, workspace_id=ws, layer="L4", action="cron.execute", cost_usd=0.00003)
    await db.commit()
    return job
