"""
Cost dashboard API — usage analytics per workspace, time series, top spenders.
Cho khách thấy chi tiết: tiêu bao nhiêu, ở đâu, khi nào.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.api.cost_dashboard")
router = APIRouter(prefix="/billing/dashboard", tags=["billing", "dashboard"])


@router.get("/summary")
async def cost_summary(
    ws: str,
    days: int = Query(default=30, ge=1, le=365),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Summary: total spent, breakdown per layer, top action types."""
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Total spent (charge transactions only)
    total_row = (await db.execute(text("""
        SELECT COALESCE(SUM(-amount_vnd), 0), COUNT(*)
        FROM wallet_transactions
        WHERE workspace_id = :w AND kind = 'charge' AND created_at >= :since
    """), {"w": ws, "since": since})).first()
    total_spent = float(total_row[0] or 0)
    total_charges = int(total_row[1] or 0)

    # Per-layer breakdown (from billing_events table)
    layer_rows = (await db.execute(text("""
        SELECT layer, action, COUNT(*) as n, SUM(cost_usd) as cost
        FROM billing_events
        WHERE workspace_id = :w AND ts >= :since
        GROUP BY layer, action
        ORDER BY cost DESC
        LIMIT 30
    """), {"w": ws, "since": since})).all()

    by_layer: dict = {}
    for r in layer_rows:
        layer = r[0]
        if layer not in by_layer:
            by_layer[layer] = {"actions": [], "total_cost_usd": 0, "total_count": 0}
        by_layer[layer]["actions"].append({"action": r[1], "count": r[2], "cost_usd": float(r[3] or 0)})
        by_layer[layer]["total_cost_usd"] += float(r[3] or 0)
        by_layer[layer]["total_count"] += r[2]

    # Wallet info
    wallet_row = (await db.execute(text("""
        SELECT balance_vnd, total_topped_up, total_spent
        FROM wallet_balances WHERE workspace_id = :w
    """), {"w": ws})).first()
    wallet = {
        "balance_vnd": float(wallet_row[0] or 0) if wallet_row else 0,
        "total_topped_up_vnd": float(wallet_row[1] or 0) if wallet_row else 0,
        "total_spent_lifetime_vnd": float(wallet_row[2] or 0) if wallet_row else 0,
    }

    # Active subscription
    sub_row = (await db.execute(text("""
        SELECT tier, quota_agent_runs, quota_image_renders, quota_text_tokens_out,
               used_agent_runs, used_image_renders, used_text_tokens_out, period_end
        FROM subscriptions WHERE workspace_id = :w AND status = 'active'
        ORDER BY created_at DESC LIMIT 1
    """), {"w": ws})).first()
    sub = None
    if sub_row:
        sub = {
            "tier": sub_row[0],
            "quota": {"runs": sub_row[1], "renders": sub_row[2], "tokens": sub_row[3]},
            "used":  {"runs": sub_row[4], "renders": sub_row[5], "tokens": sub_row[6]},
            "usage_pct": {
                "runs":    round((sub_row[4] / max(1, sub_row[1])) * 100, 1),
                "renders": round((sub_row[5] / max(1, sub_row[2])) * 100, 1),
                "tokens":  round((sub_row[6] / max(1, sub_row[3])) * 100, 1),
            },
            "period_end": sub_row[7].isoformat() if sub_row[7] else None,
        }

    return {
        "workspace_id": ws,
        "period_days": days,
        "since": since.isoformat(),
        "total_spent_vnd": total_spent,
        "total_charges_count": total_charges,
        "by_layer": by_layer,
        "wallet": wallet,
        "subscription": sub,
    }


@router.get("/timeseries")
async def cost_timeseries(
    ws: str,
    days: int = Query(default=30, ge=1, le=365),
    granularity: Literal["hour", "day"] = "day",
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Time-series chi phí (per day or hour) — cho chart UI."""
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    trunc = "day" if granularity == "day" else "hour"
    rows = (await db.execute(text(f"""
        SELECT date_trunc('{trunc}', created_at) AS bucket,
               SUM(-amount_vnd) AS spent_vnd,
               COUNT(*) AS tx_count
        FROM wallet_transactions
        WHERE workspace_id = :w AND kind = 'charge' AND created_at >= :since
        GROUP BY bucket
        ORDER BY bucket ASC
    """), {"w": ws, "since": since})).all()

    series = [
        {"timestamp": r[0].isoformat() if r[0] else None,
         "spent_vnd": float(r[1] or 0), "transactions": r[2]}
        for r in rows
    ]
    return {
        "workspace_id": ws, "granularity": granularity,
        "period_days": days, "data_points": len(series), "series": series,
    }


@router.get("/top-actions")
async def top_actions(
    ws: str,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Top actions tốn nhiều tiền nhất — phát hiện hot path."""
    await require_workspace_access(ws, me)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(text("""
        SELECT description, COUNT(*) as n, SUM(-amount_vnd) as total_vnd, AVG(-amount_vnd) as avg_vnd
        FROM wallet_transactions
        WHERE workspace_id = :w AND kind = 'charge' AND created_at >= :since
        GROUP BY description
        ORDER BY total_vnd DESC
        LIMIT :lim
    """), {"w": ws, "since": since, "lim": limit})).all()
    return {
        "workspace_id": ws, "period_days": days,
        "top_actions": [
            {"description": r[0] or "(unknown)", "count": r[1],
             "total_vnd": float(r[2] or 0), "avg_vnd": float(r[3] or 0)}
            for r in rows
        ],
    }


@router.get("/admin/all-workspaces")
async def admin_all_workspaces_summary(
    days: int = Query(default=30, ge=1, le=365),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: tổng quan chi phí toàn bộ workspaces."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role")
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (await db.execute(text("""
        SELECT w.id, w.name,
               COALESCE(wb.balance_vnd, 0) AS balance,
               COALESCE(wb.total_spent, 0) AS lifetime_spent,
               COALESCE(SUM(CASE WHEN wt.created_at >= :since AND wt.kind='charge' THEN -wt.amount_vnd ELSE 0 END), 0) AS period_spent,
               COALESCE((SELECT tier FROM subscriptions s WHERE s.workspace_id = w.id AND s.status='active' ORDER BY s.created_at DESC LIMIT 1), 'free') AS current_tier
        FROM workspaces w
        LEFT JOIN wallet_balances wb ON wb.workspace_id = w.id
        LEFT JOIN wallet_transactions wt ON wt.workspace_id = w.id
        GROUP BY w.id, w.name, wb.balance_vnd, wb.total_spent
        ORDER BY period_spent DESC
    """), {"since": since})).all()

    total_period = sum(float(r[4] or 0) for r in rows)
    return {
        "period_days": days,
        "total_period_revenue_vnd": total_period,
        "workspaces": [
            {"workspace_id": r[0], "name": r[1],
             "balance_vnd": float(r[2] or 0),
             "lifetime_spent_vnd": float(r[3] or 0),
             "period_spent_vnd": float(r[4] or 0),
             "current_tier": r[5]}
            for r in rows
        ],
    }
