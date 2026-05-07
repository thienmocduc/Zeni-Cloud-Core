from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BillingEvent


async def audit_push(
    db: AsyncSession,
    *,
    actor: str | None,
    workspace_id: str | None,
    action: str,
    target: str | None = None,
    severity: str = "info",
    metadata: dict[str, Any] | None = None,
) -> None:
    row = AuditLog(
        actor=actor,
        workspace_id=workspace_id,
        action=action,
        target=target,
        severity=severity,
        metadata_=metadata or {},
    )
    db.add(row)
    await db.flush()


async def billing_push(
    db: AsyncSession,
    *,
    workspace_id: str,
    layer: str,
    action: str,
    cost_usd: float,
) -> None:
    row = BillingEvent(
        workspace_id=workspace_id,
        layer=layer,
        action=action,
        cost_usd=Decimal(str(round(cost_usd, 8))),
    )
    db.add(row)
    await db.flush()
