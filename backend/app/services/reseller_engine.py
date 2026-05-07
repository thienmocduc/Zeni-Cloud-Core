"""
Zeni Cloud Core — Reseller / White-label engine.

Phase 4: agencies bán Zeni Cloud dưới brand riêng.

Public functions:
  - apply_to_become_reseller(...)       — application (insert pending row)
  - approve_reseller(...)               — admin duyệt → status='approved'
  - attribute_customer(...)             — link new signup → reseller
  - compute_monthly_commissions(...)    — cron, tính hoa hồng cho period
  - process_payouts(...)                — cron, batch transfer cho reseller
  - apply_brand_to_request(request,host) — load brand config theo custom_domain
  - validate_promo_code(code, plan_id)  — return discount info

Tables match: backend/migrations/041_whitelabel_reseller.sql
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import audit_push

log = logging.getLogger("zeni.reseller.engine")


# ════════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════════
TIER_COMMISSION = {
    "basic": Decimal("15.00"),
    "pro":   Decimal("25.00"),
    "elite": Decimal("35.00"),
}
TIER_DISCOUNT = {
    "basic": Decimal("0.00"),
    "pro":   Decimal("5.00"),
    "elite": Decimal("10.00"),
}
PAYABLE_GRACE_DAYS = 7   # commission lock window — chống refund clawback
USD_TO_VND = Decimal("25000")


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _gen_cname_token() -> str:
    return secrets.token_urlsafe(24)


# ════════════════════════════════════════════════════════════════════════════
# 1. Apply / approve / suspend
# ════════════════════════════════════════════════════════════════════════════
async def apply_to_become_reseller(
    db: AsyncSession,
    *,
    workspace_id: str,
    reseller_name: str,
    business_name: str | None,
    contact_email: str,
    contact_phone: str | None = None,
    tax_id: str | None = None,
    payout_method: str = "bank_transfer",
    payout_account: str | None = None,
    actor_email: str | None = None,
) -> dict[str, Any]:
    """Tạo application (status=pending). Không trả tier — admin sẽ chọn khi duyệt."""
    existing = (
        await db.execute(
            text(
                "SELECT id, status FROM reseller_accounts WHERE workspace_id = :ws"
            ),
            {"ws": workspace_id},
        )
    ).mappings().first()
    if existing:
        if existing["status"] in ("approved", "pending"):
            raise ValueError(
                f"workspace đã có application reseller (status={existing['status']})"
            )
        # rejected/suspended → re-apply: reset to pending
        await db.execute(
            text(
                """
                UPDATE reseller_accounts SET
                    reseller_name = :n, business_name = :b, contact_email = :ce,
                    contact_phone = :cp, tax_id = :tx,
                    payout_method = :pm, payout_account = :pa,
                    status = 'pending', updated_at = NOW(),
                    approved_at = NULL, approved_by = NULL
                WHERE workspace_id = :ws
                """
            ),
            {
                "ws": workspace_id, "n": reseller_name, "b": business_name,
                "ce": contact_email, "cp": contact_phone, "tx": tax_id,
                "pm": payout_method, "pa": payout_account,
            },
        )
        rid = existing["id"]
    else:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO reseller_accounts
                        (workspace_id, reseller_name, business_name, contact_email,
                         contact_phone, tax_id, payout_method, payout_account, status)
                    VALUES (:ws, :n, :b, :ce, :cp, :tx, :pm, :pa, 'pending')
                    RETURNING id
                    """
                ),
                {
                    "ws": workspace_id, "n": reseller_name, "b": business_name,
                    "ce": contact_email, "cp": contact_phone, "tx": tax_id,
                    "pm": payout_method, "pa": payout_account,
                },
            )
        ).mappings().first()
        rid = int(row["id"]) if row else 0
        # Pre-create empty brand row so reseller can immediately PATCH /brand.
        await db.execute(
            text(
                """
                INSERT INTO reseller_brand_config (reseller_id, domain_cname_token)
                VALUES (:rid, :tok)
                ON CONFLICT (reseller_id) DO NOTHING
                """
            ),
            {"rid": rid, "tok": _gen_cname_token()},
        )

    await audit_push(
        db, actor=actor_email, workspace_id=workspace_id,
        action="reseller.apply", target=str(rid),
        metadata={"name": reseller_name, "email": contact_email},
    )
    return {"reseller_id": rid, "status": "pending"}


async def approve_reseller(
    db: AsyncSession,
    *,
    reseller_id: int,
    admin_email: str,
    tier: str = "basic",
    commission_percent: Decimal | None = None,
    discount_percent: Decimal | None = None,
) -> dict[str, Any]:
    """Admin duyệt application → status=approved + set tier."""
    if tier not in TIER_COMMISSION:
        raise ValueError(f"tier invalid: {tier}")
    cp = commission_percent if commission_percent is not None else TIER_COMMISSION[tier]
    dp = discount_percent if discount_percent is not None else TIER_DISCOUNT[tier]

    row = (
        await db.execute(
            text(
                """
                UPDATE reseller_accounts SET
                    status = 'approved',
                    tier = :t,
                    commission_percent = :cp,
                    discount_percent = :dp,
                    approved_at = NOW(),
                    approved_by = :ae,
                    updated_at = NOW()
                WHERE id = :id AND status IN ('pending','suspended','rejected')
                RETURNING id, workspace_id, status, tier
                """
            ),
            {"id": reseller_id, "t": tier, "cp": cp, "dp": dp, "ae": admin_email},
        )
    ).mappings().first()
    if not row:
        raise ValueError("reseller not found or already approved")

    await audit_push(
        db, actor=admin_email, workspace_id=row["workspace_id"],
        action="reseller.approve", target=str(reseller_id),
        metadata={"tier": tier, "commission_percent": float(cp)},
    )
    return dict(row)


# ════════════════════════════════════════════════════════════════════════════
# 2. Attribute customer
# ════════════════════════════════════════════════════════════════════════════
async def attribute_customer(
    db: AsyncSession,
    *,
    reseller_id: int,
    customer_workspace_id: str,
    customer_email: str,
    source: str = "invite",
    promo_code: str | None = None,
    plan: str = "free",
) -> dict[str, Any]:
    """Link a new signup workspace to its reseller. Idempotent."""
    row = (
        await db.execute(
            text(
                """
                INSERT INTO reseller_customers
                    (reseller_id, customer_workspace_id, customer_email,
                     signed_up_via, promo_code, original_plan, current_plan)
                VALUES (:rid, :ws, :em, :src, :pc, :pl, :pl)
                ON CONFLICT (customer_workspace_id) DO UPDATE
                SET customer_email = EXCLUDED.customer_email
                RETURNING id, status
                """
            ),
            {
                "rid": reseller_id, "ws": customer_workspace_id, "em": customer_email,
                "src": source, "pc": promo_code, "pl": plan,
            },
        )
    ).mappings().first()

    # Bump promo code use count
    if promo_code:
        await db.execute(
            text(
                """
                UPDATE reseller_promo_codes SET
                    current_uses = current_uses + 1,
                    updated_at = NOW()
                WHERE code = :c AND reseller_id = :rid AND enabled = TRUE
                """
            ),
            {"c": promo_code, "rid": reseller_id},
        )

    return dict(row) if row else {}


# ════════════════════════════════════════════════════════════════════════════
# 3. Compute monthly commissions (cron)
# ════════════════════════════════════════════════════════════════════════════
async def compute_monthly_commissions(
    db: AsyncSession,
    *,
    period_end: datetime | None = None,
    period_days: int = 30,
) -> dict[str, Any]:
    """
    For each (reseller, customer) pair, sum customer paid trong period
    rồi insert reseller_commissions. Skip if entry exists.
    """
    end = period_end or _now()
    start = end - timedelta(days=period_days)

    rows = (
        await db.execute(
            text(
                """
                SELECT rc.reseller_id, rc.customer_workspace_id,
                       ra.commission_percent
                FROM reseller_customers rc
                JOIN reseller_accounts ra ON ra.id = rc.reseller_id
                WHERE rc.status = 'active' AND ra.status = 'approved'
                """
            )
        )
    ).mappings().all()

    inserted = 0
    skipped = 0
    total_vnd = Decimal("0")
    for r in rows:
        rid = int(r["reseller_id"])
        ws = r["customer_workspace_id"]
        commission_pct = _to_decimal(r["commission_percent"])

        # Sum customer payment (cost_usd → VND) in period
        paid_usd = _to_decimal(
            (
                await db.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(cost_usd),0) FROM billing_events
                        WHERE workspace_id = :ws AND ts >= :s AND ts < :e
                        """
                    ),
                    {"ws": ws, "s": start, "e": end},
                )
            ).scalar_one()
        )
        if paid_usd <= 0:
            continue

        paid_vnd = (paid_usd * USD_TO_VND).quantize(Decimal("0.01"))
        commission_vnd = (paid_vnd * commission_pct / Decimal("100")).quantize(Decimal("0.01"))

        try:
            inserted_row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO reseller_commissions
                            (reseller_id, customer_workspace_id,
                             billing_period_start, billing_period_end,
                             customer_paid_vnd, commission_percent, commission_vnd,
                             status, payable_at)
                        VALUES (:rid, :ws, :s, :e, :paid, :cp, :cv, 'pending', :pa)
                        ON CONFLICT (reseller_id, customer_workspace_id, billing_period_start) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "rid": rid, "ws": ws, "s": start, "e": end,
                        "paid": paid_vnd, "cp": commission_pct, "cv": commission_vnd,
                        "pa": end + timedelta(days=PAYABLE_GRACE_DAYS),
                    },
                )
            ).first()
            if inserted_row:
                inserted += 1
                total_vnd += commission_vnd
            else:
                skipped += 1
        except Exception as e:
            log.exception("[reseller] compute_monthly_commissions insert err: %s", e)
            skipped += 1

    # Promote pending → payable when grace passed
    promoted = (
        await db.execute(
            text(
                """
                UPDATE reseller_commissions
                SET status = 'payable'
                WHERE status = 'pending' AND payable_at <= NOW()
                RETURNING id
                """
            )
        )
    ).all()

    log.info(
        "[reseller] commissions: inserted=%d skipped=%d promoted=%d total_vnd=%s",
        inserted, skipped, len(promoted), total_vnd,
    )
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "inserted": inserted,
        "skipped": skipped,
        "promoted_to_payable": len(promoted),
        "total_commission_vnd": float(total_vnd),
    }


# ════════════════════════════════════════════════════════════════════════════
# 4. Process payouts (cron)
# ════════════════════════════════════════════════════════════════════════════
async def process_payouts(
    db: AsyncSession,
    *,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    """Group payable commissions per reseller → tạo reseller_payouts row + mark paid."""
    end = period_end or _now()

    grouped = (
        await db.execute(
            text(
                """
                SELECT rc.reseller_id,
                       MIN(rc.billing_period_start) AS pstart,
                       MAX(rc.billing_period_end)   AS pend,
                       SUM(rc.commission_vnd)        AS total_vnd,
                       COUNT(*)                      AS cnt,
                       ra.payout_method, ra.payout_account
                FROM reseller_commissions rc
                JOIN reseller_accounts ra ON ra.id = rc.reseller_id
                WHERE rc.status = 'payable'
                GROUP BY rc.reseller_id, ra.payout_method, ra.payout_account
                """
            )
        )
    ).mappings().all()

    payouts_created = 0
    total_paid_vnd = Decimal("0")
    for g in grouped:
        rid = int(g["reseller_id"])
        total = _to_decimal(g["total_vnd"])
        if total <= 0:
            continue

        payout = (
            await db.execute(
                text(
                    """
                    INSERT INTO reseller_payouts
                        (reseller_id, total_amount_vnd, period_start, period_end,
                         status, payment_method, payout_account, commission_count)
                    VALUES (:rid, :tot, :ps, :pe, 'paid', :pm, :pa, :cnt)
                    RETURNING id
                    """
                ),
                {
                    "rid": rid, "tot": total,
                    "ps": g["pstart"], "pe": g["pend"],
                    "pm": g["payout_method"], "pa": g["payout_account"],
                    "cnt": int(g["cnt"]),
                },
            )
        ).mappings().first()
        if not payout:
            continue
        pid = int(payout["id"])

        # Mark commissions as paid + link to payout
        await db.execute(
            text(
                """
                UPDATE reseller_commissions SET
                    status = 'paid', paid_at = NOW(), payout_id = :pid
                WHERE reseller_id = :rid AND status = 'payable'
                """
            ),
            {"rid": rid, "pid": pid},
        )
        # Mark payout itself as paid (transactional simulation — real impl
        # would set 'processing', call bank API, then mark 'paid' on success)
        await db.execute(
            text(
                "UPDATE reseller_payouts SET paid_at = NOW(), updated_at = NOW() WHERE id = :id"
            ),
            {"id": pid},
        )
        payouts_created += 1
        total_paid_vnd += total

    log.info(
        "[reseller] payouts: created=%d total_vnd=%s",
        payouts_created, total_paid_vnd,
    )
    return {
        "payouts_created": payouts_created,
        "total_paid_vnd": float(total_paid_vnd),
        "processed_at": end.isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# 5. Brand resolution (middleware-style)
# ════════════════════════════════════════════════════════════════════════════
async def apply_brand_to_request(
    db: AsyncSession,
    *,
    host: str,
) -> dict[str, Any] | None:
    """
    Detect reseller by request host. Return brand config dict (or None).
    Cache trong app state recommended; here we do raw lookup.
    """
    host_clean = (host or "").lower().split(":")[0].strip()
    if not host_clean:
        return None
    row = (
        await db.execute(
            text(
                """
                SELECT b.*, ra.id AS reseller_id, ra.reseller_name, ra.status
                FROM reseller_brand_config b
                JOIN reseller_accounts ra ON ra.id = b.reseller_id
                WHERE LOWER(b.custom_domain) = :h
                  AND b.domain_verified_at IS NOT NULL
                  AND ra.status = 'approved'
                """
            ),
            {"h": host_clean},
        )
    ).mappings().first()
    return dict(row) if row else None


# ════════════════════════════════════════════════════════════════════════════
# 6. Promo code validation
# ════════════════════════════════════════════════════════════════════════════
async def validate_promo_code(
    db: AsyncSession,
    *,
    code: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """
    Public-callable. Trả về { valid, reseller_id, discount_type, discount_value, ...}
    hoặc { valid: False, reason: ... }.
    """
    code_clean = (code or "").strip().upper()
    if not code_clean:
        return {"valid": False, "reason": "empty_code"}

    row = (
        await db.execute(
            text(
                """
                SELECT pc.*, ra.status AS reseller_status
                FROM reseller_promo_codes pc
                JOIN reseller_accounts ra ON ra.id = pc.reseller_id
                WHERE UPPER(pc.code) = :c AND pc.enabled = TRUE
                """
            ),
            {"c": code_clean},
        )
    ).mappings().first()
    if not row:
        return {"valid": False, "reason": "not_found"}
    if row["reseller_status"] != "approved":
        return {"valid": False, "reason": "reseller_not_active"}
    if row["expires_at"] and row["expires_at"] < _now():
        return {"valid": False, "reason": "expired"}
    if row["max_uses"] is not None and int(row["current_uses"] or 0) >= int(row["max_uses"]):
        return {"valid": False, "reason": "max_uses_reached"}
    plans = list(row["applies_to_plans"] or [])
    if plans and plan_id and plan_id not in plans:
        return {"valid": False, "reason": "plan_not_eligible"}

    return {
        "valid": True,
        "reseller_id": int(row["reseller_id"]),
        "code": row["code"],
        "discount_type": row["discount_type"],
        "discount_value": float(_to_decimal(row["discount_value"])),
        "applies_to_plans": plans,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


__all__ = [
    "TIER_COMMISSION",
    "TIER_DISCOUNT",
    "PAYABLE_GRACE_DAYS",
    "apply_to_become_reseller",
    "approve_reseller",
    "attribute_customer",
    "compute_monthly_commissions",
    "process_payouts",
    "apply_brand_to_request",
    "validate_promo_code",
]
