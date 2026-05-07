"""
Zeni Cloud Core — Trial Management API.

14-day free trial enforcement:
- Workspace mới: trial_ends_at = NOW + 14 ngày
- Còn ngày: API call free, dashboard show banner "Còn N ngày"
- Hết ngày: API call → 402 Payment Required, dashboard lock + suggest upgrade
- Subscribe → trial_status = 'converted'

Endpoints (prefix /trial, tag trial):
  GET  /status?ws=X       — Trạng thái trial của workspace
  POST /extend?ws=X       — Extend trial (admin/sales action)
  GET  /platform/expiring — List workspace sắp hết trial (super-admin)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

router = APIRouter(prefix="/trial", tags=["trial"])


class ExtendIn(BaseModel):
    days: int = Field(default=7, ge=1, le=365)


@router.get("/status")
async def trial_status(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get trial status for a workspace."""
    await require_workspace_access(ws, me)

    # Try to read trial_ends_at column (may not exist if migration 044 not applied)
    try:
        row = (await db.execute(
            text("""SELECT trial_ends_at, trial_status, created_at
                    FROM workspaces WHERE id = :ws"""),
            {"ws": ws}
        )).first()
    except Exception:
        # Column doesn't exist — return permissive default
        return {
            "workspace_id": ws,
            "status": "active",
            "days_remaining": 14,
            "trial_ends_at": None,
            "expired": False,
            "note": "Trial tracking pending migration",
        }

    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    trial_end, status, created_at = row[0], row[1] or "active", row[2]

    # If no trial_ends_at, derive from created_at + 14 days
    if trial_end is None and created_at is not None:
        trial_end = created_at + timedelta(days=14)
    elif trial_end is None:
        trial_end = datetime.now(timezone.utc) + timedelta(days=14)

    now = datetime.now(timezone.utc)
    delta = trial_end - now
    days_remaining = max(0, delta.days)
    hours_remaining = max(0, int(delta.total_seconds() / 3600))
    expired = now > trial_end

    return {
        "workspace_id": ws,
        "status": "expired" if expired else status,
        "trial_ends_at": trial_end.isoformat() if trial_end else None,
        "days_remaining": days_remaining,
        "hours_remaining": hours_remaining,
        "expired": expired,
        "warning_level": (
            "critical" if days_remaining <= 1 else
            "warning" if days_remaining <= 3 else
            "info"
        ),
        "upgrade_url": "/app#settings/billing",
    }


@router.post("/extend")
async def extend_trial(
    ws: str,
    data: ExtendIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Extend trial by N days. Owner-only."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Chỉ super-admin mới extend được trial.")
    try:
        await db.execute(
            text("""UPDATE workspaces
                    SET trial_ends_at = COALESCE(trial_ends_at, NOW()) + (:d * INTERVAL '1 day'),
                        trial_status = 'extended'
                    WHERE id = :ws"""),
            {"d": data.days, "ws": ws}
        )
        await db.commit()
        return {"workspace_id": ws, "extended_days": data.days, "ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extend failed: {type(e).__name__} (migration 044 may be pending)")


@router.get("/platform/expiring")
async def list_expiring(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List workspaces with trial expiring in next 7 days. Super-admin only."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Super-admin only")
    try:
        rows = (await db.execute(
            text("""SELECT id, name, trial_ends_at, trial_status,
                           EXTRACT(DAY FROM (trial_ends_at - NOW())) as days_left
                    FROM workspaces
                    WHERE trial_status = 'active'
                      AND trial_ends_at IS NOT NULL
                      AND trial_ends_at > NOW()
                      AND trial_ends_at < NOW() + INTERVAL '7 days'
                    ORDER BY trial_ends_at ASC""")
        )).all()
        return [
            {"id": r[0], "name": r[1], "trial_ends_at": r[2].isoformat() if r[2] else None,
             "trial_status": r[3], "days_left": int(r[4]) if r[4] else 0}
            for r in rows
        ]
    except Exception:
        return []
