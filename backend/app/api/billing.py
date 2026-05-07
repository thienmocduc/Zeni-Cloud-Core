from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import BillingEvent, Workspace

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/summary")
async def summary(
    ws: str | None = None,
    days: int = 30,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(
        BillingEvent.workspace_id,
        BillingEvent.layer,
        func.sum(BillingEvent.cost_usd).label("total"),
    ).where(BillingEvent.ts >= since).group_by(BillingEvent.workspace_id, BillingEvent.layer)

    if ws:
        await require_workspace_access(ws, me)
        q = q.where(BillingEvent.workspace_id == ws)
    elif me.role != "Owner":
        if me.workspaces:
            q = q.where(BillingEvent.workspace_id.in_(me.workspaces))
        else:
            return {"total_usd": 0, "by_workspace": {}, "by_layer": {}, "period_days": days}

    rows = (await db.execute(q)).all()

    by_workspace: dict[str, float] = {}
    by_layer: dict[str, float] = {}
    total = 0.0
    for ws_id, layer, tot in rows:
        val = float(tot)
        total += val
        by_workspace[ws_id] = by_workspace.get(ws_id, 0) + val
        by_layer[layer] = by_layer.get(layer, 0) + val

    return {
        "total_usd": round(total, 6),
        "period_days": days,
        "by_workspace": {k: round(v, 6) for k, v in by_workspace.items()},
        "by_layer": {k: round(v, 6) for k, v in by_layer.items()},
    }


@router.get("/by-entity")
async def by_entity(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    q = (
        select(
            BillingEvent.workspace_id,
            BillingEvent.layer,
            func.sum(BillingEvent.cost_usd).label("total"),
        )
        .where(BillingEvent.ts >= since)
        .group_by(BillingEvent.workspace_id, BillingEvent.layer)
    )
    if me.role != "Owner":
        if me.workspaces:
            q = q.where(BillingEvent.workspace_id.in_(me.workspaces))
        else:
            return []

    rows = (await db.execute(q)).all()
    ws_rows = (await db.execute(select(Workspace))).scalars().all()
    ws_names = {w.id: w.name for w in ws_rows}

    buckets: dict[str, dict] = {}
    for ws_id, layer, tot in rows:
        b = buckets.setdefault(ws_id, {
            "workspace_id": ws_id, "name": ws_names.get(ws_id, ws_id),
            "compute": 0, "data": 0, "ai": 0, "automation": 0, "identity": 0, "web3": 0, "total": 0,
        })
        key = {"L1": "compute", "L2": "data", "L3": "ai", "L4": "automation", "L5": "identity", "L6": "web3"}.get(layer, "other")
        b[key] = round(float(tot), 4)
        b["total"] = round(b["total"] + float(tot), 4)
    return list(buckets.values())
