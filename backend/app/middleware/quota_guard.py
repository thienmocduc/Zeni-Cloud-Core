"""
Zeni Cloud Core — Quota enforcement middleware.

Used as a FastAPI dependency from any router that needs to enforce a workspace's
monthly plan quota (requests, ai_tokens, storage_gb, router_usd).

Pattern (from another router)::

    from fastapi import Depends
    from app.middleware.quota_guard import enforce_quota, increment_usage

    @router.post("/some-action")
    async def do_thing(
        ws: str,
        me: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        await enforce_quota(ws, db, resource="requests")
        # ... do the thing ...
        await increment_usage(db, workspace_id=ws_resolved, resource="requests", amount=1)
        return {"ok": True}

Notes
-----
- A workspace with NO subscription row is allowed (will be back-filled to 'free'
  on first usage by the seed migration; new workspaces should always have a row).
- ``limit < 0`` means unlimited — never blocks.
- A 'quota_warning_80' event is logged once per period when usage crosses 80 %.
- ``increment_usage`` is atomic via ``ON CONFLICT DO UPDATE`` and commits the
  session; do not call inside a larger transaction unless you understand that.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.middleware.quota_guard")

ResourceKind = Literal["requests", "ai_tokens", "storage_gb", "router_usd"]

# (usage_column, plan_quota_column)
_RESOURCE_COLUMNS: dict[str, tuple[str, str]] = {
    "requests":   ("requests_count",   "quota_requests_per_month"),
    "ai_tokens":  ("ai_tokens_count",  "quota_ai_tokens_per_month"),
    "storage_gb": ("storage_gb_avg",   "quota_storage_gb"),
    "router_usd": ("router_cost_usd",  "quota_router_usd_per_month"),
}


async def _resolve_workspace_id(db: AsyncSession, workspace_code: str) -> str | None:
    """Accept either workspaces.id (VARCHAR(32)) or workspaces.code (VARCHAR(8))."""
    row = (await db.execute(
        text("SELECT id FROM workspaces WHERE id = :w OR code = :w LIMIT 1"),
        {"w": workspace_code},
    )).first()
    return row[0] if row else None


async def enforce_quota(
    workspace_code: str,
    db: AsyncSession,
    resource: ResourceKind = "requests",
) -> None:
    """Raise HTTP 429 if the workspace has exceeded its monthly quota.

    Parameters
    ----------
    workspace_code : workspaces.id or workspaces.code
    db             : async SQLAlchemy session
    resource       : 'requests' | 'ai_tokens' | 'storage_gb' | 'router_usd'
    """
    if resource not in _RESOURCE_COLUMNS:
        raise ValueError(f"unknown resource: {resource}")
    usage_col, plan_col = _RESOURCE_COLUMNS[resource]

    row = (await db.execute(text(f"""
        SELECT
          COALESCE(u.{usage_col}, 0) AS current_value,
          p.{plan_col}                AS limit_value,
          p.id                        AS plan_id,
          p.name                      AS plan_name,
          w.id                        AS workspace_id,
          s.status                    AS sub_status
        FROM workspaces w
        JOIN workspace_subscriptions s ON s.workspace_id = w.id
        JOIN pricing_plans p ON p.id = s.plan_id
        LEFT JOIN workspace_usage u
               ON u.workspace_id = w.id
              AND u.period_start = DATE_TRUNC('month', NOW())::DATE
        WHERE (w.id = :w OR w.code = :w)
          AND s.status IN ('active','trial')
        LIMIT 1
    """), {"w": workspace_code})).mappings().first()

    if row is None:
        # No active subscription found → allow (subscription will be auto-created
        # by seed migration; new workspaces should have one).
        log.warning("[quota_guard] no active subscription for ws=%s — allowing", workspace_code)
        return

    current = float(row["current_value"] or 0)
    limit = float(row["limit_value"] or 0)
    plan_id = row["plan_id"]
    plan_name = row["plan_name"]
    workspace_id = row["workspace_id"]

    # Unlimited (Pro+ projects, etc.)
    if limit < 0:
        return

    # 80 % warning event (idempotent per period via uniqueness on event_type+period)
    if limit > 0 and current >= 0.8 * limit and current < limit:
        try:
            await db.execute(text("""
                INSERT INTO quota_events (workspace_id, event_type, detail)
                SELECT :w, 'quota_warning_80', :dt
                WHERE NOT EXISTS (
                    SELECT 1 FROM quota_events
                     WHERE workspace_id = :w
                       AND event_type = 'quota_warning_80'
                       AND triggered_at >= DATE_TRUNC('month', NOW())
                )
            """), {
                "w": workspace_id,
                "dt": f"resource={resource} current={current} limit={limit} plan={plan_id}",
            })
        except Exception:
            log.exception("[quota_guard] failed to log warning event")

    # Hard quota
    if limit > 0 and current >= limit:
        # Log exceeded event (best-effort)
        try:
            await db.execute(text("""
                INSERT INTO quota_events (workspace_id, event_type, detail)
                VALUES (:w, 'quota_exceeded', :dt)
            """), {
                "w": workspace_id,
                "dt": f"resource={resource} current={current} limit={limit} plan={plan_id}",
            })
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception("[quota_guard] failed to log exceeded event")

        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "resource": resource,
                "current": current,
                "limit": limit,
                "plan": plan_id,
                "upgrade_url": "/app#pricing",
                "message_vi": (
                    f"Đã vượt quota {resource} của gói {plan_name} "
                    f"({int(current)}/{int(limit)}). Vui lòng nâng cấp."
                ),
            },
        )


async def increment_usage(
    db: AsyncSession,
    workspace_id: str,
    resource: ResourceKind = "requests",
    amount: int | float = 1,
) -> None:
    """Atomically increment a usage counter for the current month.

    `workspace_id` should be the resolved primary-key id (VARCHAR(32)). If you
    only have a workspace_code, call :func:`_resolve_workspace_id` first.

    Commits the session on success.
    """
    if resource not in _RESOURCE_COLUMNS:
        raise ValueError(f"unknown resource: {resource}")
    usage_col, _ = _RESOURCE_COLUMNS[resource]

    # If a workspace_code (e.g. workspaces.code) was passed, resolve to id.
    # Cheap branch: try a lookup if the value isn't a known id.
    resolved = workspace_id
    try:
        check = (await db.execute(
            text("SELECT 1 FROM workspaces WHERE id = :w LIMIT 1"), {"w": workspace_id},
        )).first()
        if check is None:
            r = await _resolve_workspace_id(db, workspace_id)
            if r:
                resolved = r
    except Exception:
        # If the check fails, fall through and let the INSERT FK fail loudly
        pass

    try:
        await db.execute(text(f"""
            INSERT INTO workspace_usage (workspace_id, period_start, {usage_col}, last_request_at)
            VALUES (:w, DATE_TRUNC('month', NOW())::DATE, :amt, NOW())
            ON CONFLICT (workspace_id, period_start) DO UPDATE SET
                {usage_col}     = workspace_usage.{usage_col} + EXCLUDED.{usage_col},
                last_request_at = NOW()
        """), {"w": resolved, "amt": amount})
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("[quota_guard] increment_usage failed ws=%s resource=%s", resolved, resource)
        raise
