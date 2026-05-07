"""
ZeniRouter quota layer — per-tenant monthly cost ceiling.

Default: 5.00 USD / workspace / month. Auto-creates a row on first call so
brand-new workspaces still hit the cheap default until the operator raises it.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


DEFAULT_MONTHLY_QUOTA_USD = 5.00


async def check_quota(
    db: AsyncSession, workspace_id: str
) -> tuple[bool, float, float]:
    """Returns ``(within_quota, current_usage_usd, monthly_quota_usd)``.

    If no row exists, this seeds the default quota and returns 0/5.00 USD.
    """
    row = (await db.execute(text("""
        SELECT current_month_usage_usd, monthly_quota_usd
        FROM router_tenant_quotas
        WHERE workspace_id = :ws
    """), {"ws": workspace_id})).mappings().first()

    if not row:
        # Auto-create with default 5 USD/month.
        await db.execute(text("""
            INSERT INTO router_tenant_quotas (workspace_id, monthly_quota_usd)
            VALUES (:ws, :q)
            ON CONFLICT (workspace_id) DO NOTHING
        """), {"ws": workspace_id, "q": DEFAULT_MONTHLY_QUOTA_USD})
        await db.commit()
        return True, 0.0, DEFAULT_MONTHLY_QUOTA_USD

    current = float(row["current_month_usage_usd"])
    limit = float(row["monthly_quota_usd"])
    return current < limit, current, limit


async def increment_usage(
    db: AsyncSession, workspace_id: str, cost_usd: float
) -> None:
    """Add ``cost_usd`` to the workspace's running total. Safe on missing row."""
    await db.execute(text("""
        UPDATE router_tenant_quotas
        SET current_month_usage_usd = current_month_usage_usd + :c,
            updated_at = NOW()
        WHERE workspace_id = :ws
    """), {"ws": workspace_id, "c": float(cost_usd)})
    await db.commit()


async def reset_if_due(db: AsyncSession, workspace_id: str) -> bool:
    """Roll the counter back to 0 if `quota_reset_at` is in the past.
    Returns True when a reset happened."""
    res = await db.execute(text("""
        UPDATE router_tenant_quotas
        SET current_month_usage_usd = 0,
            quota_reset_at = NOW() + INTERVAL '30 days',
            updated_at = NOW()
        WHERE workspace_id = :ws AND quota_reset_at <= NOW()
    """), {"ws": workspace_id})
    await db.commit()
    return (res.rowcount or 0) > 0


async def set_quota(
    db: AsyncSession, workspace_id: str, monthly_quota_usd: float
) -> None:
    """Operator helper — set or upsert a workspace's monthly cap."""
    await db.execute(text("""
        INSERT INTO router_tenant_quotas (workspace_id, monthly_quota_usd)
        VALUES (:ws, :q)
        ON CONFLICT (workspace_id) DO UPDATE
            SET monthly_quota_usd = EXCLUDED.monthly_quota_usd,
                updated_at = NOW()
    """), {"ws": workspace_id, "q": float(monthly_quota_usd)})
    await db.commit()
