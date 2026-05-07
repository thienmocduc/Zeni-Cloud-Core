"""
Zeni Cloud Core — Wallet Engine (Zeni Pay Cấp 2).

Internal wallet system: user nạp tiền 1 lần qua VietQR (Cấp 1) → balance sống
trong wallet_balances → mọi giao dịch (subscription, router, agent run, transfer)
deduct trực tiếp.

Public surface
--------------
    get_balance(db, workspace_id)
    topup(db, workspace_id, amount_vnd, intent_id, ...)
    spend(db, workspace_id, amount_vnd, source_type, source_id, ...)
    lock(db, workspace_id, amount_vnd, reason, ttl_seconds, ...)
    release(db, workspace_id, hold_id, actually_spent)
    transfer(db, from_ws, to_ws, amount_vnd, reason)
    refund(db, workspace_id, amount_vnd, reason, original_tx_id)
    process_recurring_charges(db)         — cron entry
    expire_holds(db)                       — cron entry
    low_balance_check(db, workspace_id)   — trigger alert if < threshold
    admin_adjust(db, workspace_id, amount, reason, actor)

Concurrency: dùng SELECT ... FOR UPDATE trên wallet_balances cho mọi mutation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import audit_push

log = logging.getLogger("zeni.wallet_engine")

# Tunables
DEFAULT_LOW_BALANCE_THRESHOLD_VND = Decimal("50000")
MAX_TRANSFER_VND = Decimal("100000000")  # 100M VND per transfer cap
RECURRING_RETRY_BACKOFF_HOURS = 24


# ─── Exceptions ─────────────────────────────────────────────────────────────


class WalletError(Exception):
    """Base wallet exception."""


class InsufficientFunds(WalletError):
    def __init__(self, workspace_id: str, requested: Decimal, available: Decimal):
        self.workspace_id = workspace_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"Insufficient funds in wallet for ws={workspace_id}: "
            f"requested {requested}, available {available}"
        )


class WalletNotFound(WalletError):
    pass


class HoldNotFound(WalletError):
    pass


# ─── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class WalletSnapshot:
    workspace_id: str
    balance_vnd: Decimal
    balance_locked: Decimal
    available_vnd: Decimal
    escrow_amount: Decimal
    total_topped_up: Decimal
    total_spent: Decimal
    currency: str
    low_balance_threshold: Decimal
    updated_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "balance_vnd": float(self.balance_vnd),
            "balance_locked": float(self.balance_locked),
            "available_vnd": float(self.available_vnd),
            "escrow_amount": float(self.escrow_amount),
            "total_topped_up": float(self.total_topped_up),
            "total_spent": float(self.total_spent),
            "currency": self.currency,
            "low_balance_threshold": float(self.low_balance_threshold),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ─── Helpers ────────────────────────────────────────────────────────────────


def _to_decimal(amount: Any) -> Decimal:
    if isinstance(amount, Decimal):
        return amount
    return Decimal(str(amount))


def _json(obj: Any) -> str:
    def _default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return str(o)
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)


async def _ensure_wallet_row(db: AsyncSession, workspace_id: str) -> None:
    """Create empty wallet row if not exists. Idempotent."""
    await db.execute(text("""
        INSERT INTO wallet_balances (workspace_id, balance_vnd, total_topped_up, total_spent)
        VALUES (:w, 0, 0, 0)
        ON CONFLICT (workspace_id) DO NOTHING
    """), {"w": workspace_id})


async def _lock_wallet_row(db: AsyncSession, workspace_id: str) -> dict:
    """SELECT FOR UPDATE on wallet_balances → race-safe mutation."""
    await _ensure_wallet_row(db, workspace_id)
    row = (await db.execute(text("""
        SELECT workspace_id, balance_vnd, balance_locked, escrow_amount,
               total_topped_up, total_spent, currency, low_balance_threshold,
               updated_at
          FROM wallet_balances
         WHERE workspace_id = :w
         FOR UPDATE
    """), {"w": workspace_id})).mappings().first()
    if row is None:
        raise WalletNotFound(f"Wallet row missing after upsert for ws={workspace_id}")
    return dict(row)


async def _insert_tx(
    db: AsyncSession,
    *,
    workspace_id: str,
    type_: str,
    amount_vnd: Decimal,
    balance_after: Decimal,
    source_type: str | None,
    source_id: str | None,
    description: str,
    actor: str | None = None,
    related_tx_id: int | None = None,
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert wallet_transactions row + map to legacy "kind" col."""
    legacy_kind = {
        "topup": "topup",
        "spend": "charge",
        "refund": "refund",
        "transfer_in": "topup",
        "transfer_out": "charge",
        "lock": "charge",
        "unlock": "topup",
        "escrow": "charge",
        "release": "topup",
        "adjust": "topup" if amount_vnd >= 0 else "charge",
    }.get(type_, "charge")

    new_id = (await db.execute(text("""
        INSERT INTO wallet_transactions
            (workspace_id, kind, amount_vnd, balance_after,
             type, source_type, source_id, status,
             description, ref_id, actor, related_tx_id, metadata)
        VALUES
            (:w, :k, :a, :b,
             :t, :st, :sid, :stat,
             :d, :r, :ac, :rel, :meta::jsonb)
        RETURNING id
    """), {
        "w": workspace_id, "k": legacy_kind, "a": amount_vnd, "b": balance_after,
        "t": type_, "st": source_type, "sid": source_id, "stat": status,
        "d": description[:255], "r": source_id, "ac": actor,
        "rel": related_tx_id,
        "meta": _json(metadata or {}),
    })).scalar()
    return int(new_id)


# ─── Public API ─────────────────────────────────────────────────────────────


async def get_balance(db: AsyncSession, workspace_id: str) -> WalletSnapshot:
    """Read current balance snapshot. Auto-creates wallet if not exists."""
    await _ensure_wallet_row(db, workspace_id)
    row = (await db.execute(text("""
        SELECT workspace_id, balance_vnd, balance_locked, escrow_amount,
               total_topped_up, total_spent, currency, low_balance_threshold,
               updated_at
          FROM wallet_balances
         WHERE workspace_id = :w
    """), {"w": workspace_id})).mappings().first()
    if row is None:
        raise WalletNotFound(workspace_id)

    bal = _to_decimal(row["balance_vnd"])
    locked = _to_decimal(row["balance_locked"])
    return WalletSnapshot(
        workspace_id=row["workspace_id"],
        balance_vnd=bal,
        balance_locked=locked,
        available_vnd=bal - locked,
        escrow_amount=_to_decimal(row["escrow_amount"]),
        total_topped_up=_to_decimal(row["total_topped_up"]),
        total_spent=_to_decimal(row["total_spent"]),
        currency=row["currency"] or "VND",
        low_balance_threshold=_to_decimal(row["low_balance_threshold"]),
        updated_at=row["updated_at"],
    )


async def topup(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    intent_id: int | None = None,
    intent_code: str | None = None,
    payment_method: str = "vietqr",
    actor: str = "system:zeni-pay",
    description: str | None = None,
) -> dict[str, Any]:
    """Credit wallet balance (e.g. after VietQR confirmed). Atomic."""
    amount = _to_decimal(amount_vnd)
    if amount <= 0:
        raise ValueError("topup amount must be > 0")

    cur = await _lock_wallet_row(db, workspace_id)
    new_balance = _to_decimal(cur["balance_vnd"]) + amount
    new_total_topup = _to_decimal(cur["total_topped_up"]) + amount

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd     = :b,
               total_topped_up = :t,
               updated_at      = NOW()
         WHERE workspace_id = :w
    """), {"b": new_balance, "t": new_total_topup, "w": workspace_id})

    tx_id = await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="topup",
        amount_vnd=amount,
        balance_after=new_balance,
        source_type="zeni_pay_intent" if intent_code else "manual",
        source_id=intent_code or (str(intent_id) if intent_id else None),
        description=description or f"Top-up via {payment_method}",
        actor=actor,
        metadata={"intent_id": intent_id, "payment_method": payment_method},
    )

    # Link to wallet_topups (idempotent)
    await db.execute(text("""
        INSERT INTO wallet_topups
            (workspace_id, intent_id, intent_code, amount_vnd,
             payment_method, status, completed_at)
        VALUES (:w, :iid, :ic, :a, :pm, 'completed', NOW())
        ON CONFLICT DO NOTHING
    """), {
        "w": workspace_id, "iid": intent_id, "ic": intent_code,
        "a": amount, "pm": payment_method,
    })

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.topup", target=intent_code or f"tx:{tx_id}",
        severity="ok",
        metadata={"amount_vnd": float(amount), "new_balance": float(new_balance)},
    )

    return {
        "ok": True,
        "tx_id": tx_id,
        "amount_vnd": float(amount),
        "new_balance": float(new_balance),
    }


async def spend(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    source_type: str,
    source_id: str | None,
    description: str,
    actor: str = "system:wallet",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Debit wallet. Raises InsufficientFunds if available < amount.

    source_type ∈ {'subscription','router','agent','blockchain','manual','transfer','recurring'}
    """
    amount = _to_decimal(amount_vnd)
    if amount <= 0:
        raise ValueError("spend amount must be > 0")

    cur = await _lock_wallet_row(db, workspace_id)
    bal = _to_decimal(cur["balance_vnd"])
    locked = _to_decimal(cur["balance_locked"])
    available = bal - locked
    if available < amount:
        raise InsufficientFunds(workspace_id, amount, available)

    new_balance = bal - amount
    new_total_spent = _to_decimal(cur["total_spent"]) + amount

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd     = :b,
               total_spent     = :t,
               last_charged_at = NOW(),
               updated_at      = NOW()
         WHERE workspace_id = :w
    """), {"b": new_balance, "t": new_total_spent, "w": workspace_id})

    tx_id = await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="spend",
        amount_vnd=-amount,  # negative = outflow
        balance_after=new_balance,
        source_type=source_type,
        source_id=source_id,
        description=description,
        actor=actor,
        metadata=metadata,
    )

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.spend", target=source_id or f"tx:{tx_id}",
        severity="info",
        metadata={
            "amount_vnd": float(amount),
            "new_balance": float(new_balance),
            "source_type": source_type,
        },
    )

    # Trigger low-balance alert if needed
    threshold = _to_decimal(cur["low_balance_threshold"])
    if new_balance < threshold and bal >= threshold:
        # Crossing threshold downwards — fire alert (best-effort, don't fail spend)
        try:
            await _maybe_fire_low_balance_alert(db, workspace_id, new_balance, threshold)
        except Exception:
            log.exception("low-balance alert push failed")

    return {
        "ok": True,
        "tx_id": tx_id,
        "amount_vnd": float(amount),
        "new_balance": float(new_balance),
    }


async def lock(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    reason: str,
    ttl_seconds: int = 600,
    source_type: str | None = None,
    source_id: str | None = None,
    actor: str = "system:wallet",
) -> dict[str, Any]:
    """Reserve amount on balance (escrow). Returns hold_id."""
    amount = _to_decimal(amount_vnd)
    if amount <= 0:
        raise ValueError("lock amount must be > 0")

    cur = await _lock_wallet_row(db, workspace_id)
    bal = _to_decimal(cur["balance_vnd"])
    locked_now = _to_decimal(cur["balance_locked"])
    if (bal - locked_now) < amount:
        raise InsufficientFunds(workspace_id, amount, bal - locked_now)

    new_locked = locked_now + amount
    new_escrow = _to_decimal(cur["escrow_amount"]) + amount
    hold_until = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_locked = :l,
               escrow_amount  = :e,
               updated_at     = NOW()
         WHERE workspace_id = :w
    """), {"l": new_locked, "e": new_escrow, "w": workspace_id})

    hold_id = (await db.execute(text("""
        INSERT INTO wallet_holds
            (workspace_id, amount_vnd, reason, source_type, source_id, hold_until)
        VALUES (:w, :a, :r, :st, :sid, :hu)
        RETURNING id
    """), {
        "w": workspace_id, "a": amount, "r": reason[:120],
        "st": source_type, "sid": source_id, "hu": hold_until,
    })).scalar()

    # Log a hold transaction (informational, balance after = bal unchanged)
    await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="lock",
        amount_vnd=Decimal(0),  # lock doesn't move balance, only locks it
        balance_after=bal,
        source_type=source_type or "hold",
        source_id=str(hold_id),
        description=f"Lock {amount} VND — {reason}"[:255],
        actor=actor,
        metadata={"hold_id": int(hold_id), "ttl_seconds": ttl_seconds},
    )

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.lock", target=f"hold:{hold_id}",
        severity="info",
        metadata={"amount_vnd": float(amount), "reason": reason, "ttl": ttl_seconds},
    )
    return {"ok": True, "hold_id": int(hold_id), "amount_vnd": float(amount),
            "hold_until": hold_until.isoformat()}


async def release(
    db: AsyncSession,
    workspace_id: str,
    hold_id: int,
    *,
    actually_spent: int | Decimal | None = None,
    actor: str = "system:wallet",
) -> dict[str, Any]:
    """Release a hold. If actually_spent is given (and ≤ hold amount), spend that
    amount and unlock the rest. If None, fully unlock (no spend)."""
    cur = await _lock_wallet_row(db, workspace_id)
    hold = (await db.execute(text("""
        SELECT id, workspace_id, amount_vnd, source_type, source_id, released
          FROM wallet_holds
         WHERE id = :id AND workspace_id = :w
         FOR UPDATE
    """), {"id": hold_id, "w": workspace_id})).mappings().first()
    if hold is None:
        raise HoldNotFound(f"hold_id={hold_id} for ws={workspace_id}")
    if hold["released"]:
        return {"ok": True, "already_released": True, "hold_id": hold_id}

    hold_amount = _to_decimal(hold["amount_vnd"])
    actual = _to_decimal(actually_spent) if actually_spent is not None else Decimal(0)
    if actual < 0:
        raise ValueError("actually_spent cannot be negative")
    if actual > hold_amount:
        actual = hold_amount  # cap — never spend more than locked

    bal = _to_decimal(cur["balance_vnd"])
    locked_now = _to_decimal(cur["balance_locked"])
    escrow_now = _to_decimal(cur["escrow_amount"])

    new_balance = bal - actual
    new_locked = locked_now - hold_amount
    new_escrow = max(Decimal(0), escrow_now - hold_amount)
    new_total_spent = _to_decimal(cur["total_spent"]) + actual

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd     = :b,
               balance_locked  = :l,
               escrow_amount   = :e,
               total_spent     = :ts,
               last_charged_at = CASE WHEN :actual > 0 THEN NOW() ELSE last_charged_at END,
               updated_at      = NOW()
         WHERE workspace_id = :w
    """), {
        "b": new_balance, "l": new_locked, "e": new_escrow,
        "ts": new_total_spent, "actual": actual, "w": workspace_id,
    })

    await db.execute(text("""
        UPDATE wallet_holds
           SET released = TRUE, released_at = NOW(), actual_spent = :a
         WHERE id = :id
    """), {"a": actual, "id": hold_id})

    await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="release",
        amount_vnd=-actual if actual > 0 else Decimal(0),
        balance_after=new_balance,
        source_type=hold["source_type"] or "hold_release",
        source_id=hold["source_id"] or str(hold_id),
        description=f"Release hold {hold_id} — spent {actual}/{hold_amount}"[:255],
        actor=actor,
        metadata={"hold_id": int(hold_id), "hold_amount": float(hold_amount),
                  "actual_spent": float(actual)},
    )

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.release", target=f"hold:{hold_id}",
        severity="info",
        metadata={"actually_spent": float(actual), "hold_amount": float(hold_amount)},
    )
    return {
        "ok": True,
        "hold_id": hold_id,
        "actually_spent": float(actual),
        "released_amount": float(hold_amount - actual),
        "new_balance": float(new_balance),
    }


async def transfer(
    db: AsyncSession,
    from_workspace_id: str,
    to_workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    reason: str,
    actor: str = "system:wallet",
) -> dict[str, Any]:
    """Internal P2P transfer between workspaces. Atomic (both rows locked)."""
    if from_workspace_id == to_workspace_id:
        raise ValueError("Cannot transfer to same workspace")
    amount = _to_decimal(amount_vnd)
    if amount <= 0:
        raise ValueError("transfer amount must be > 0")
    if amount > MAX_TRANSFER_VND:
        raise ValueError(f"transfer cap exceeded ({MAX_TRANSFER_VND} VND)")

    # Lock both rows in deterministic order to avoid deadlock
    a, b = sorted([from_workspace_id, to_workspace_id])
    await _lock_wallet_row(db, a)
    await _lock_wallet_row(db, b)

    src = await _lock_wallet_row(db, from_workspace_id)
    src_bal = _to_decimal(src["balance_vnd"])
    src_locked = _to_decimal(src["balance_locked"])
    if (src_bal - src_locked) < amount:
        raise InsufficientFunds(from_workspace_id, amount, src_bal - src_locked)

    dst = await _lock_wallet_row(db, to_workspace_id)
    dst_bal = _to_decimal(dst["balance_vnd"])

    src_new = src_bal - amount
    dst_new = dst_bal + amount

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd = :b,
               total_spent = total_spent + :a,
               updated_at  = NOW()
         WHERE workspace_id = :w
    """), {"b": src_new, "a": amount, "w": from_workspace_id})

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd     = :b,
               total_topped_up = total_topped_up + :a,
               updated_at      = NOW()
         WHERE workspace_id = :w
    """), {"b": dst_new, "a": amount, "w": to_workspace_id})

    out_tx = await _insert_tx(
        db,
        workspace_id=from_workspace_id,
        type_="transfer_out",
        amount_vnd=-amount,
        balance_after=src_new,
        source_type="transfer",
        source_id=to_workspace_id,
        description=f"Transfer to {to_workspace_id}: {reason}",
        actor=actor,
        metadata={"to_workspace": to_workspace_id, "reason": reason},
    )
    in_tx = await _insert_tx(
        db,
        workspace_id=to_workspace_id,
        type_="transfer_in",
        amount_vnd=amount,
        balance_after=dst_new,
        source_type="transfer",
        source_id=from_workspace_id,
        description=f"Transfer from {from_workspace_id}: {reason}",
        actor=actor,
        related_tx_id=out_tx,
        metadata={"from_workspace": from_workspace_id, "reason": reason},
    )

    # Link out → in (after we know both ids)
    await db.execute(text("UPDATE wallet_transactions SET related_tx_id = :ri WHERE id = :id"),
                     {"ri": in_tx, "id": out_tx})

    await audit_push(
        db, actor=actor, workspace_id=from_workspace_id,
        action="wallet.transfer.out", target=to_workspace_id,
        severity="ok",
        metadata={"amount_vnd": float(amount), "reason": reason,
                  "out_tx": out_tx, "in_tx": in_tx},
    )
    return {
        "ok": True,
        "out_tx_id": out_tx,
        "in_tx_id": in_tx,
        "amount_vnd": float(amount),
        "from_balance": float(src_new),
        "to_balance": float(dst_new),
    }


async def refund(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    reason: str,
    original_tx_id: int | None = None,
    actor: str = "system:wallet",
) -> dict[str, Any]:
    """Credit wallet back (admin-initiated refund or system error correction)."""
    amount = _to_decimal(amount_vnd)
    if amount <= 0:
        raise ValueError("refund amount must be > 0")

    cur = await _lock_wallet_row(db, workspace_id)
    new_balance = _to_decimal(cur["balance_vnd"]) + amount

    await db.execute(text("""
        UPDATE wallet_balances
           SET balance_vnd = :b,
               updated_at  = NOW()
         WHERE workspace_id = :w
    """), {"b": new_balance, "w": workspace_id})

    tx_id = await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="refund",
        amount_vnd=amount,
        balance_after=new_balance,
        source_type="refund",
        source_id=str(original_tx_id) if original_tx_id else None,
        description=f"Refund: {reason}",
        actor=actor,
        related_tx_id=original_tx_id,
        metadata={"original_tx": original_tx_id, "reason": reason},
    )

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.refund", target=str(original_tx_id) if original_tx_id else f"tx:{tx_id}",
        severity="warning",
        metadata={"amount_vnd": float(amount), "reason": reason},
    )
    return {"ok": True, "tx_id": tx_id, "amount_vnd": float(amount),
            "new_balance": float(new_balance)}


async def admin_adjust(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: int | Decimal,
    *,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Manual adjust by Owner. Can be positive (credit) or negative (debit)."""
    amount = _to_decimal(amount_vnd)
    if amount == 0:
        raise ValueError("adjust amount cannot be 0")

    cur = await _lock_wallet_row(db, workspace_id)
    bal = _to_decimal(cur["balance_vnd"])
    new_balance = bal + amount
    if new_balance < 0:
        raise InsufficientFunds(workspace_id, abs(amount), bal)

    if amount > 0:
        await db.execute(text("""
            UPDATE wallet_balances
               SET balance_vnd     = :b,
                   total_topped_up = total_topped_up + :a,
                   updated_at      = NOW()
             WHERE workspace_id = :w
        """), {"b": new_balance, "a": amount, "w": workspace_id})
    else:
        await db.execute(text("""
            UPDATE wallet_balances
               SET balance_vnd     = :b,
                   total_spent     = total_spent + :a,
                   last_charged_at = NOW(),
                   updated_at      = NOW()
             WHERE workspace_id = :w
        """), {"b": new_balance, "a": -amount, "w": workspace_id})

    tx_id = await _insert_tx(
        db,
        workspace_id=workspace_id,
        type_="adjust",
        amount_vnd=amount,
        balance_after=new_balance,
        source_type="admin",
        source_id=actor,
        description=f"Admin adjust: {reason}",
        actor=actor,
        metadata={"reason": reason},
    )

    await audit_push(
        db, actor=actor, workspace_id=workspace_id,
        action="wallet.admin_adjust", target=f"tx:{tx_id}",
        severity="warning",
        metadata={"amount_vnd": float(amount), "reason": reason,
                  "new_balance": float(new_balance)},
    )
    return {"ok": True, "tx_id": tx_id, "amount_vnd": float(amount),
            "new_balance": float(new_balance)}


# ─── Recurring charges (cron) ───────────────────────────────────────────────


async def process_recurring_charges(db: AsyncSession) -> dict[str, Any]:
    """Scan due recurring charges → deduct → schedule next.

    Result: {"processed": n, "succeeded": n, "failed": n, "skipped": n}
    """
    now = datetime.now(timezone.utc)
    due = (await db.execute(text("""
        SELECT id, workspace_id, plan_id, amount_vnd, billing_cycle,
               next_charge_at, retry_count, max_retries
          FROM wallet_recurring_charges
         WHERE status = 'active' AND next_charge_at <= :now
         ORDER BY next_charge_at ASC
         LIMIT 200
    """), {"now": now})).mappings().all()

    summary = {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    for r in due:
        summary["processed"] += 1
        try:
            await spend(
                db,
                r["workspace_id"],
                _to_decimal(r["amount_vnd"]),
                source_type="recurring",
                source_id=f"plan:{r['plan_id']}",
                description=f"Auto-charge {r['plan_id']} ({r['billing_cycle']})",
                actor="system:recurring",
                metadata={"recurring_id": r["id"], "plan_id": r["plan_id"]},
            )
            # Schedule next
            cycle_days = 30 if r["billing_cycle"] == "monthly" else 365
            next_at = now + timedelta(days=cycle_days)
            await db.execute(text("""
                UPDATE wallet_recurring_charges
                   SET last_charged_at = :now,
                       last_charge_status = 'success',
                       next_charge_at = :nx,
                       retry_count = 0,
                       updated_at = NOW()
                 WHERE id = :id
            """), {"now": now, "nx": next_at, "id": r["id"]})
            summary["succeeded"] += 1
        except InsufficientFunds:
            new_retry = (r["retry_count"] or 0) + 1
            if new_retry >= (r["max_retries"] or 3):
                # Pause subscription
                await db.execute(text("""
                    UPDATE wallet_recurring_charges
                       SET status = 'paused',
                           last_charge_status = 'failed_insufficient',
                           retry_count = :rc,
                           updated_at = NOW()
                     WHERE id = :id
                """), {"rc": new_retry, "id": r["id"]})
                await audit_push(
                    db, actor="system:recurring",
                    workspace_id=r["workspace_id"],
                    action="wallet.recurring.paused",
                    target=f"recurring:{r['id']}",
                    severity="warning",
                    metadata={"plan_id": r["plan_id"], "retries": new_retry},
                )
            else:
                next_at = now + timedelta(hours=RECURRING_RETRY_BACKOFF_HOURS)
                await db.execute(text("""
                    UPDATE wallet_recurring_charges
                       SET next_charge_at = :nx,
                           last_charge_status = 'failed_insufficient',
                           retry_count = :rc,
                           updated_at = NOW()
                     WHERE id = :id
                """), {"nx": next_at, "rc": new_retry, "id": r["id"]})
            summary["failed"] += 1
        except Exception as e:
            log.exception("recurring charge failed for id=%s", r["id"])
            await db.execute(text("""
                UPDATE wallet_recurring_charges
                   SET last_charge_status = 'failed_other',
                       updated_at = NOW()
                 WHERE id = :id
            """), {"id": r["id"]})
            summary["failed"] += 1

    if summary["processed"] > 0:
        await db.commit()
    return summary


async def expire_holds(db: AsyncSession) -> int:
    """Cron: release any expired holds (hold_until < now AND not released).
    Treats them as full unlock (actual_spent=0)."""
    rows = (await db.execute(text("""
        SELECT id, workspace_id FROM wallet_holds
         WHERE NOT released AND hold_until < NOW()
         LIMIT 500
    """))).mappings().all()
    n = 0
    for r in rows:
        try:
            await release(db, r["workspace_id"], r["id"],
                          actually_spent=None, actor="system:hold-expiry")
            n += 1
        except Exception:
            log.exception("expire_holds: release failed for hold_id=%s", r["id"])
    if n:
        await db.commit()
    return n


# ─── Alerts ─────────────────────────────────────────────────────────────────


async def low_balance_check(db: AsyncSession, workspace_id: str) -> bool:
    """Manual check + maybe-fire alert. Returns True if alert was fired."""
    snap = await get_balance(db, workspace_id)
    if snap.balance_vnd >= snap.low_balance_threshold:
        return False
    return await _maybe_fire_low_balance_alert(
        db, workspace_id, snap.balance_vnd, snap.low_balance_threshold)


async def _maybe_fire_low_balance_alert(
    db: AsyncSession,
    workspace_id: str,
    balance_vnd: Decimal,
    threshold: Decimal,
) -> bool:
    """Insert/update alert row + audit. Email/SMS dispatched by separate worker."""
    alert = (await db.execute(text("""
        SELECT id, last_triggered_at, trigger_count, enabled
          FROM wallet_alerts
         WHERE workspace_id = :w AND alert_type = 'low_balance'
    """), {"w": workspace_id})).mappings().first()

    if alert and not alert["enabled"]:
        return False

    # Suppress duplicate alerts within 6 hours
    last = alert["last_triggered_at"] if alert else None
    if last and (datetime.now(timezone.utc) - last) < timedelta(hours=6):
        return False

    if alert:
        await db.execute(text("""
            UPDATE wallet_alerts
               SET last_triggered_at = NOW(),
                   trigger_count = trigger_count + 1
             WHERE id = :id
        """), {"id": alert["id"]})
    else:
        await db.execute(text("""
            INSERT INTO wallet_alerts
                (workspace_id, alert_type, threshold_vnd,
                 last_triggered_at, trigger_count, enabled)
            VALUES (:w, 'low_balance', :th, NOW(), 1, TRUE)
            ON CONFLICT (workspace_id, alert_type) DO UPDATE SET
                last_triggered_at = NOW(),
                trigger_count = wallet_alerts.trigger_count + 1
        """), {"w": workspace_id, "th": threshold})

    await audit_push(
        db, actor="system:wallet", workspace_id=workspace_id,
        action="wallet.alert.low_balance", target=workspace_id,
        severity="warning",
        metadata={"balance_vnd": float(balance_vnd), "threshold": float(threshold)},
    )
    return True


# ─── Convenience getters ────────────────────────────────────────────────────


async def list_transactions(
    db: AsyncSession,
    workspace_id: str,
    *,
    type_filter: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["workspace_id = :w"]
    params: dict[str, Any] = {"w": workspace_id, "lim": limit, "off": offset}
    if type_filter:
        where.append("(type = :t OR kind = :t)")
        params["t"] = type_filter
    if from_date:
        where.append("created_at >= :fd")
        params["fd"] = from_date
    if to_date:
        where.append("created_at <= :td")
        params["td"] = to_date

    rows = (await db.execute(text(f"""
        SELECT id, workspace_id, kind, type, amount_vnd, balance_after,
               source_type, source_id, status, description, ref_id, actor,
               metadata, created_at
          FROM wallet_transactions
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC, id DESC
         LIMIT :lim OFFSET :off
    """), params)).mappings().all()

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "type": r["type"] or r["kind"],
            "amount_vnd": float(r["amount_vnd"]),
            "balance_after": float(r["balance_after"]),
            "source_type": r["source_type"],
            "source_id": r["source_id"] or r["ref_id"],
            "status": r["status"] or "completed",
            "description": r["description"],
            "actor": r["actor"],
            "metadata": r["metadata"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


async def monthly_statement(
    db: AsyncSession, workspace_id: str, year: int, month: int,
) -> dict[str, Any]:
    """Return summary for a calendar month (UTC)."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    summary = (await db.execute(text("""
        SELECT
            COALESCE(SUM(CASE WHEN type='topup' OR kind='topup' THEN amount_vnd ELSE 0 END), 0)        AS total_topup,
            COALESCE(SUM(CASE WHEN type='spend' OR kind='charge' THEN -amount_vnd ELSE 0 END), 0)      AS total_spent,
            COALESCE(SUM(CASE WHEN type='refund' OR kind='refund' THEN amount_vnd ELSE 0 END), 0)      AS total_refund,
            COALESCE(SUM(CASE WHEN type='transfer_out' THEN -amount_vnd ELSE 0 END), 0)                AS transfer_out,
            COALESCE(SUM(CASE WHEN type='transfer_in' THEN amount_vnd ELSE 0 END), 0)                  AS transfer_in,
            COUNT(*) AS tx_count
          FROM wallet_transactions
         WHERE workspace_id = :w
           AND created_at >= :start AND created_at < :end
    """), {"w": workspace_id, "start": start, "end": end})).mappings().first()

    snap = await get_balance(db, workspace_id)
    return {
        "workspace_id": workspace_id,
        "period": f"{year:04d}-{month:02d}",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "current_balance": float(snap.balance_vnd),
        "total_topup": float(summary["total_topup"] or 0),
        "total_spent": float(summary["total_spent"] or 0),
        "total_refund": float(summary["total_refund"] or 0),
        "transfer_out": float(summary["transfer_out"] or 0),
        "transfer_in": float(summary["transfer_in"] or 0),
        "tx_count": int(summary["tx_count"] or 0),
        "currency": snap.currency,
    }
