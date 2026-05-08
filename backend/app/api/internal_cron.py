"""
Internal cron endpoints — invoked by Cloud Scheduler with shared secret token.
Auth via X-Zeni-Cron-Token header (no JWT — cron job is server-to-server).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.services import quota_alerts

log = logging.getLogger("zeni.api.internal_cron")
router = APIRouter(prefix="/internal/cron", tags=["internal"], include_in_schema=False)

CRON_SECRET = os.environ.get("ZENI_CRON_SECRET", "change-me-via-secret-manager")


def _check_cron_token(x_zeni_cron_token: str | None = Header(default=None)):
    if not x_zeni_cron_token or x_zeni_cron_token != CRON_SECRET:
        raise HTTPException(status_code=401, detail="invalid cron token")


@router.post("/quota-check")
async def cron_quota_check(
    _auth: None = Depends(_check_cron_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Daily check: workspaces có quota >80% hoặc wallet thấp → email alert Owner."""
    result = await quota_alerts.check_and_alert(db)
    log.info("[cron.quota-check] sent %d emails (near_quota=%d, low_wallet=%d)",
             result["emails_sent"], result["near_quota_count"], result["low_wallet_count"])
    return result


@router.post("/billing-period-rollover")
async def cron_billing_rollover(
    _auth: None = Depends(_check_cron_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Monthly: reset used quotas, expire old subs, renew if auto_renew=true."""
    expired = (await db.execute(text("""
        SELECT id, workspace_id, tier, auto_renew, price_vnd_month
        FROM subscriptions
        WHERE status = 'active' AND period_end < NOW()
    """))).all()

    renewed: list = []
    cancelled: list = []
    for s in expired:
        sid, ws, tier, auto_renew, price = s
        price = Decimal(str(price))
        if auto_renew and price > 0:
            balance_row = (await db.execute(
                text("SELECT balance_vnd FROM wallet_balances WHERE workspace_id = :w"),
                {"w": ws}
            )).first()
            balance = Decimal(str(balance_row[0])) if balance_row else Decimal("0")
            if balance >= price:
                new_end = datetime.now(timezone.utc) + timedelta(days=30)
                await db.execute(text("""
                    UPDATE subscriptions SET
                      used_agent_runs = 0, used_image_renders = 0, used_text_tokens_out = 0,
                      period_start = NOW(), period_end = :e
                    WHERE id = :id
                """), {"e": new_end, "id": sid})
                await db.execute(text("""
                    UPDATE wallet_balances SET
                      balance_vnd = balance_vnd - :p, total_spent = total_spent + :p, updated_at = NOW()
                    WHERE workspace_id = :w
                """), {"w": ws, "p": price})
                renewed.append({"workspace_id": ws, "tier": tier, "charged_vnd": float(price)})
            else:
                await db.execute(text(
                    "UPDATE subscriptions SET status = 'cancelled', cancelled_at = NOW() WHERE id = :id"
                ), {"id": sid})
                cancelled.append({"workspace_id": ws, "tier": tier, "reason": "insufficient_balance"})
        else:
            await db.execute(text(
                "UPDATE subscriptions SET status = 'cancelled', cancelled_at = NOW() WHERE id = :id"
            ), {"id": sid})
            cancelled.append({"workspace_id": ws, "tier": tier, "reason": "no_auto_renew"})
    await db.commit()
    return {"expired_count": len(expired), "renewed": renewed, "cancelled": cancelled}


@router.post("/webhook-dispatch")
async def cron_webhook_dispatch(
    _auth: None = Depends(_check_cron_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Process due webhook retry queue. Run mỗi 1 phút."""
    from app.services import webhook_retry
    result = await webhook_retry.process_due_batch(db)
    log.info("[cron.webhook-dispatch] processed=%d", result.get("processed", 0))
    return result


@router.post("/agents-warmup")
async def cron_agents_warmup(
    _auth: None = Depends(_check_cron_token),
) -> dict:
    """Warm-up Vertex AI Gemini connection (avoid cold start on first user request)."""
    try:
        from app.services import ai_core
        ai_core._ensure_init()
        return {"warmed": True}
    except Exception as e:
        log.warning("warmup failed: %s", e)
        return {"warmed": False, "error": str(e)}


@router.post("/trial-expire")
async def cron_trial_expire(
    _auth: None = Depends(_check_cron_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hourly: disable workspaces với trial_ends_at < NOW() and trial_status='active'.

    Idempotent — chỉ update rows đang 'active'. Insert audit_log per expired workspace.
    """
    rows = (await db.execute(text(
        "UPDATE workspaces SET trial_status = 'expired' "
        "WHERE trial_ends_at IS NOT NULL "
        "AND trial_ends_at < NOW() "
        "AND trial_status = 'active' "
        "RETURNING id, trial_ends_at"
    ))).all()

    expired_count = len(rows)
    if expired_count > 0:
        log.info("[cron.trial-expire] expired %d workspaces", expired_count)
        for ws_id, ends_at in rows:
            await db.execute(text(
                "INSERT INTO audit_log (workspace_id, action, target, severity, metadata) "
                "VALUES (:ws, 'trial.expired', :ws, 'warning', CAST(:meta AS jsonb))"
            ), {"ws": ws_id, "meta": '{"trial_ends_at":"' + str(ends_at) + '"}'})
    await db.commit()
    return {"status": "ok", "expired_count": expired_count}
