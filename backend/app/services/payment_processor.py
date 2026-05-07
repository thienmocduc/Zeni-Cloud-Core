"""
Zeni Cloud Core — Payment Processor (Zeni Pay Cấp 1).

Background job that:
  1. Every 30s: scan ``bank_webhook_events`` WHERE NOT processed
     → match each event against ``payment_intents`` by intent_code
     → if amount matches: mark intent ``paid``, run activation logic
     → mark webhook event processed.
  2. Every 5 min: scan ``payment_intents`` WHERE status='pending' AND expires_at < NOW
     → mark them ``expired``.

The processor exposes:
  - ``run_match_loop(db_factory)``                 — long-running coroutine
  - ``run_expiry_loop(db_factory)``                — long-running coroutine
  - ``match_pending_webhooks_once(db)``            — single scan (testable)
  - ``expire_stale_intents_once(db)``              — single scan (testable)
  - ``activate_after_payment(db, intent_row)``     — shared activation entrypoint

Design notes
------------
- Activation logic kept in this module so both webhook auto-confirm AND admin
  manual-confirm route to the same place.
- Activation purposes supported:
    'subscription_<plan_id>'  → upsert workspace_subscriptions = active, +30d
    'wallet_topup'            → credit wallet balance + log txn
    'custom'                  → just mark paid (manual handling)
- Sends confirmation email if SMTP configured.
- audit_push every action; commits on each loop iteration to avoid blocking.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import audit_push
from app.services.email import is_configured as email_is_configured, send_email

log = logging.getLogger("zeni.payment_processor")


# Tunables (also used by callers as defaults)
MATCH_INTERVAL_SECONDS = 30
EXPIRY_INTERVAL_SECONDS = 300


# ──────────────────────────────────────────────────────────────────────────
# Activation logic — shared between webhook & admin manual-confirm
# ──────────────────────────────────────────────────────────────────────────


async def activate_after_payment(
    db: AsyncSession,
    intent: dict[str, Any],
    *,
    paid_amount_vnd: int,
    bank_tx_ref: str | None = None,
    actor: str = "system:zeni-pay",
) -> dict[str, Any]:
    """Run side-effects after a payment intent is confirmed paid.

    Updates intent row to ``paid``, triggers subscription/topup activation,
    logs audit, and sends a confirmation email (best-effort).
    """
    intent_id = intent["id"]
    intent_code = intent["intent_code"]
    workspace_id = intent["workspace_id"]
    purpose = intent["purpose"]
    user_email = intent["user_email"]

    now = datetime.now(timezone.utc)

    # Mark intent as paid first (idempotent guard)
    upd = (await db.execute(
        text("""UPDATE payment_intents
                   SET status = 'paid',
                       paid_at = :now,
                       paid_amount_vnd = :amt,
                       bank_tx_ref = COALESCE(:tx, bank_tx_ref)
                 WHERE id = :id AND status = 'pending'
                 RETURNING id"""),
        {"now": now, "amt": paid_amount_vnd, "tx": bank_tx_ref, "id": intent_id},
    )).first()

    if upd is None:
        log.info("[zeni-pay] intent_id=%s already settled — skipping activation", intent_id)
        return {"already_settled": True, "intent_id": intent_id}

    activation_detail: dict[str, Any] = {"purpose": purpose}

    # ─── Subscription activation ───
    if purpose.startswith("subscription_"):
        plan_id = purpose.replace("subscription_", "", 1)
        period_end = now + timedelta(days=30)
        await db.execute(text("""
            INSERT INTO workspace_subscriptions
                (workspace_id, plan_id, status, started_at,
                 current_period_start, current_period_end,
                 cancel_at_period_end, payment_method, payment_reference,
                 notes, updated_at)
            VALUES
                (:w, :p, 'active', :now, :now, :pe, FALSE, 'vietqr', :pr, :nt, :now)
            ON CONFLICT (workspace_id) DO UPDATE SET
                plan_id              = EXCLUDED.plan_id,
                status               = 'active',
                current_period_start = EXCLUDED.current_period_start,
                current_period_end   = EXCLUDED.current_period_end,
                cancel_at_period_end = FALSE,
                payment_method       = 'vietqr',
                payment_reference    = EXCLUDED.payment_reference,
                notes                = COALESCE(EXCLUDED.notes, workspace_subscriptions.notes),
                updated_at           = EXCLUDED.updated_at
        """), {
            "w": workspace_id, "p": plan_id, "now": now, "pe": period_end,
            "pr": intent_code,
            "nt": f"Zeni Pay VietQR — bank_tx={bank_tx_ref or 'n/a'}",
        })
        # quota_events
        await db.execute(text("""
            INSERT INTO quota_events (workspace_id, event_type, detail)
            VALUES (:w, 'plan_paid_vietqr', :dt)
        """), {
            "w": workspace_id,
            "dt": f"plan={plan_id} amt={paid_amount_vnd} intent={intent_code}",
        })
        activation_detail.update(plan_id=plan_id, period_end=period_end.isoformat())

    # ─── Wallet top-up activation ───
    elif purpose == "wallet_topup":
        amount = Decimal(str(paid_amount_vnd))
        await db.execute(text("""
            INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up)
            VALUES(:w, :a, :a)
            ON CONFLICT (workspace_id) DO UPDATE SET
              balance_vnd     = wallet_balances.balance_vnd + :a,
              total_topped_up = wallet_balances.total_topped_up + :a,
              updated_at      = NOW()
        """), {"w": workspace_id, "a": amount})
        new_bal_row = (await db.execute(
            text("SELECT balance_vnd FROM wallet_balances WHERE workspace_id = :w"),
            {"w": workspace_id},
        )).first()
        new_bal = Decimal(str(new_bal_row[0])) if new_bal_row else amount
        await db.execute(text("""
            INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                                            description, ref_id, actor)
            VALUES(:w, 'topup', :a, :b, :d, :r, :ac)
        """), {
            "w": workspace_id, "a": amount, "b": new_bal,
            "d": "Zeni Pay VietQR top-up", "r": intent_code, "ac": actor,
        })
        activation_detail.update(new_balance_vnd=str(new_bal))

    # 'custom' or unknown → just mark paid; no auto-activation.

    # ─── Audit ───
    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="zeni_pay.intent.paid", target=intent_code, severity="ok",
        metadata={
            "purpose": purpose,
            "paid_amount_vnd": paid_amount_vnd,
            "bank_tx_ref": bank_tx_ref,
            **activation_detail,
        },
    )

    # ─── Confirmation email (best-effort) ───
    if email_is_configured() and user_email:
        try:
            subject = f"[Zeni Cloud] Da nhan thanh toan VietQR — {intent_code}"
            html = _render_payment_received_email(
                intent_code=intent_code,
                amount_vnd=paid_amount_vnd,
                purpose=purpose,
                workspace_id=workspace_id,
                bank_tx_ref=bank_tx_ref,
                detail=activation_detail,
            )
            await send_email(to=user_email, subject=subject, body_html=html)
        except Exception as e:
            log.warning("payment confirmation email failed: %s", e)

    log.info(
        "[zeni-pay] activated intent=%s ws=%s purpose=%s amount=%d",
        intent_code, workspace_id, purpose, paid_amount_vnd,
    )
    return {"activated": True, "intent_id": intent_id, **activation_detail}


# ──────────────────────────────────────────────────────────────────────────
# Background scanners
# ──────────────────────────────────────────────────────────────────────────


async def match_pending_webhooks_once(db: AsyncSession) -> int:
    """Scan unprocessed bank_webhook_events; match → activate. Returns matched count."""
    rows = (await db.execute(text("""
        SELECT id, bank_code, raw_payload, parsed_amount_vnd, parsed_ref_code,
               parsed_tx_ref, parsed_sender_name
          FROM bank_webhook_events
         WHERE NOT processed
         ORDER BY received_at ASC
         LIMIT 100
    """))).mappings().all()

    matched = 0
    for ev in rows:
        ref = (ev["parsed_ref_code"] or "").strip().upper()
        if not ref:
            await db.execute(text("""
                UPDATE bank_webhook_events
                   SET processed = TRUE, processing_error = 'no parsed_ref_code'
                 WHERE id = :id
            """), {"id": ev["id"]})
            continue

        intent = (await db.execute(text("""
            SELECT id, intent_code, workspace_id, user_email, amount_vnd,
                   purpose, status, expires_at
              FROM payment_intents
             WHERE intent_code = :code AND status = 'pending'
             LIMIT 1
        """), {"code": ref})).mappings().first()

        if intent is None:
            await db.execute(text("""
                UPDATE bank_webhook_events
                   SET processed = TRUE, processing_error = 'no matching pending intent'
                 WHERE id = :id
            """), {"id": ev["id"]})
            continue

        # Amount sanity: must equal or exceed expected (allow over-pay)
        paid = int(ev["parsed_amount_vnd"] or 0)
        if paid < int(intent["amount_vnd"]):
            await db.execute(text("""
                UPDATE bank_webhook_events
                   SET processed = TRUE, processing_error = :err, matched_intent_id = :iid
                 WHERE id = :id
            """), {
                "id": ev["id"], "iid": intent["id"],
                "err": f"underpaid (got {paid}, expected {intent['amount_vnd']})",
            })
            continue

        try:
            await activate_after_payment(
                db, dict(intent),
                paid_amount_vnd=paid,
                bank_tx_ref=ev["parsed_tx_ref"],
                actor=f"webhook:{ev['bank_code']}",
            )
            await db.execute(text("""
                UPDATE bank_webhook_events
                   SET processed = TRUE, matched_intent_id = :iid
                 WHERE id = :id
            """), {"id": ev["id"], "iid": intent["id"]})
            matched += 1
        except Exception as e:
            log.exception("activation failed for intent_code=%s", ref)
            await db.execute(text("""
                UPDATE bank_webhook_events
                   SET processing_error = :err
                 WHERE id = :id
            """), {"id": ev["id"], "err": f"activation error: {e}"})

    if rows:
        await db.commit()
    return matched


async def expire_stale_intents_once(db: AsyncSession) -> int:
    """Mark expired any pending intents past expires_at. Returns count."""
    res = await db.execute(text("""
        UPDATE payment_intents
           SET status = 'expired'
         WHERE status = 'pending' AND expires_at < NOW()
         RETURNING id
    """))
    n = len(res.fetchall())
    if n > 0:
        await db.commit()
        log.info("[zeni-pay] expired %d stale payment_intents", n)
    return n


# ──────────────────────────────────────────────────────────────────────────
# Long-running loops (entrypoints for asyncio.create_task on app startup)
# ──────────────────────────────────────────────────────────────────────────


DbFactory = Callable[[], Awaitable[AsyncSession]]


async def run_match_loop(db_factory: DbFactory, interval: int = MATCH_INTERVAL_SECONDS) -> None:
    """Long-running: every ``interval`` seconds match webhooks → intents."""
    log.info("zeni-pay match loop started (every %ss)", interval)
    while True:
        try:
            db = await db_factory()
            try:
                count = await match_pending_webhooks_once(db)
                if count:
                    log.info("zeni-pay matched %d intent(s)", count)
            finally:
                await db.close()
        except Exception:
            log.exception("zeni-pay match loop iteration failed")
        await asyncio.sleep(interval)


async def run_expiry_loop(db_factory: DbFactory, interval: int = EXPIRY_INTERVAL_SECONDS) -> None:
    """Long-running: every ``interval`` seconds expire stale intents."""
    log.info("zeni-pay expiry loop started (every %ss)", interval)
    while True:
        try:
            db = await db_factory()
            try:
                await expire_stale_intents_once(db)
            finally:
                await db.close()
        except Exception:
            log.exception("zeni-pay expiry loop iteration failed")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────────────────
# Email rendering
# ──────────────────────────────────────────────────────────────────────────


def _render_payment_received_email(
    *, intent_code: str, amount_vnd: int, purpose: str,
    workspace_id: str, bank_tx_ref: str | None, detail: dict,
) -> str:
    amount_fmt = f"{amount_vnd:,}".replace(",", ".")
    detail_lines = "".join(
        f"<li><strong>{k}:</strong> {v}</li>" for k, v in detail.items() if k != "purpose"
    )
    return f"""<!DOCTYPE html>
<html><body style="font-family: system-ui, -apple-system, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1a0938;">
  <div style="background: #08051F; padding: 24px; border-radius: 12px; text-align: center;">
    <h1 style="color: #FAF5FF; margin: 0; font-size: 22px;">Zeni Cloud · Thanh toan thanh cong</h1>
    <p style="color: #C4B5FD; margin: 6px 0 0; font-size: 12px; letter-spacing: 0.1em;">ZENI PAY · VIETQR</p>
  </div>
  <div style="background: white; border: 1px solid #eee; padding: 24px; border-radius: 12px; margin-top: 16px;">
    <p>Chao ban,</p>
    <p>Zeni da nhan duoc khoan thanh toan VietQR cua ban.</p>
    <table style="width:100%; border-collapse: collapse; margin: 16px 0;">
      <tr><td style="padding:6px 0;"><strong>Ma giao dich:</strong></td><td>{intent_code}</td></tr>
      <tr><td style="padding:6px 0;"><strong>So tien:</strong></td><td>{amount_fmt} VND</td></tr>
      <tr><td style="padding:6px 0;"><strong>Muc dich:</strong></td><td>{purpose}</td></tr>
      <tr><td style="padding:6px 0;"><strong>Workspace:</strong></td><td>{workspace_id}</td></tr>
      <tr><td style="padding:6px 0;"><strong>Bank tx ref:</strong></td><td>{bank_tx_ref or '-'}</td></tr>
    </table>
    <ul style="font-size:13px; color:#444;">{detail_lines}</ul>
    <p style="color:#666; font-size:12px;">Goi/dich vu da duoc kich hoat tu dong. Vui long dang nhap Zeni Cloud de kiem tra.</p>
  </div>
  <div style="text-align:center; margin-top:16px; color:#999; font-size:11px;">Zeni Holdings · zenicloud.io</div>
</body></html>"""
