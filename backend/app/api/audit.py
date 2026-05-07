from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit(
    ws: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    severity: str | None = Query(default=None, pattern=r"^(info|ok|warn|err)$"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if ws:
        await require_workspace_access(ws, me)
        q = q.where(AuditLog.workspace_id == ws)
    elif me.role != "Owner":
        # Non-owner: restrict to accessible workspaces only
        if me.workspaces:
            q = q.where(AuditLog.workspace_id.in_(me.workspaces))
        else:
            return []
    if severity:
        q = q.where(AuditLog.severity == severity)

    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "actor": r.actor,
            "workspace_id": r.workspace_id,
            "action": r.action,
            "target": r.target,
            "severity": r.severity,
            "metadata": r.metadata_,
        }
        for r in rows
    ]
