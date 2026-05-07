"""
Zeni Cloud Core — Pricing & Wallet service.

Cost-to-Price translation:
  GIÁ_KHÁCH_VND = COST_USD × markup_ratio × USD_TO_VND

Wallet flow:
  - Khách top-up trước: credit_vnd += amount
  - Mỗi API call: charge price → balance_vnd -= price
  - Insufficient balance → 402 Payment Required

Subscription flow:
  - Tier có quota (run/image/token) miễn phí
  - Vượt quota → tính trên ví prepaid với markup_ratio
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.pricing")

# ─── FX rate (admin updates periodically) ────────────
USD_TO_VND = Decimal("25000")

# ─── Subscription tiers (server-config) ──────────────
SUBSCRIPTION_TIERS = {
    "free": {
        "price_vnd_month": 0,
        "quota_agent_runs": 5,
        "quota_image_renders": 5,
        "quota_text_tokens_out": 50_000,
    },
    "starter": {
        "price_vnd_month": 500_000,
        "quota_agent_runs": 50,
        "quota_image_renders": 100,
        "quota_text_tokens_out": 500_000,
    },
    "pro": {
        "price_vnd_month": 2_000_000,
        "quota_agent_runs": 300,
        "quota_image_renders": 500,
        "quota_text_tokens_out": 5_000_000,
    },
    "business": {
        "price_vnd_month": 6_000_000,
        "quota_agent_runs": 1500,
        "quota_image_renders": 2000,
        "quota_text_tokens_out": 20_000_000,
    },
    "enterprise": {
        "price_vnd_month": 0,  # contract-based
        "quota_agent_runs": 999_999_999,
        "quota_image_renders": 999_999_999,
        "quota_text_tokens_out": 9_999_999_999,
    },
}


# ─── Convert cost USD → price VND ────────────────────
async def calc_price_vnd(db: AsyncSession, product_key: str, units: float = 1.0) -> tuple[Decimal, dict]:
    """
    Lookup product in price_book → calc price in VND with markup.
    Returns (price_vnd, breakdown_dict).
    """
    row = (await db.execute(
        text("SELECT cost_usd_per_unit, markup_ratio, display_name, unit FROM price_book "
             "WHERE product_key = :k AND active = TRUE"),
        {"k": product_key}
    )).first()
    if row is None:
        # Fallback default markup 4x for unknown products
        return Decimal("0"), {"product_key": product_key, "found": False}

    cost_usd = Decimal(str(row[0])) * Decimal(str(units))
    markup = Decimal(str(row[1]))
    price_usd = cost_usd * markup
    price_vnd = (price_usd * USD_TO_VND).quantize(Decimal("0.01"))

    return price_vnd, {
        "product_key": product_key,
        "found": True,
        "display_name": row[2],
        "unit": row[3],
        "units": float(units),
        "cost_usd": float(cost_usd),
        "markup_ratio": float(markup),
        "price_vnd": float(price_vnd),
    }


# ─── Wallet ───────────────────────────────────────────
async def get_balance(db: AsyncSession, workspace_id: str) -> Decimal:
    row = (await db.execute(
        text("SELECT balance_vnd FROM wallet_balances WHERE workspace_id = :w"),
        {"w": workspace_id}
    )).first()
    if row is None:
        # Auto-create with 0 balance
        await db.execute(
            text("INSERT INTO wallet_balances(workspace_id, balance_vnd) VALUES(:w, 0) "
                 "ON CONFLICT DO NOTHING"),
            {"w": workspace_id}
        )
        await db.commit()
        return Decimal("0")
    return Decimal(str(row[0]))


async def topup(db: AsyncSession, workspace_id: str, amount_vnd: Decimal,
                actor: str, payment_ref: str | None = None) -> Decimal:
    """Add funds to wallet. Returns new balance."""
    if amount_vnd <= 0:
        raise ValueError("amount_vnd phải > 0")
    await db.execute(
        text("""INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up)
                VALUES(:w, :a, :a)
                ON CONFLICT (workspace_id) DO UPDATE SET
                  balance_vnd     = wallet_balances.balance_vnd + :a,
                  total_topped_up = wallet_balances.total_topped_up + :a,
                  updated_at      = NOW()"""),
        {"w": workspace_id, "a": amount_vnd}
    )
    new_bal = await get_balance(db, workspace_id)
    await db.execute(
        text("""INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                description, ref_id, actor)
                VALUES(:w, 'topup', :a, :b, :d, :r, :ac)"""),
        {"w": workspace_id, "a": amount_vnd, "b": new_bal,
         "d": "Wallet top-up", "r": payment_ref, "ac": actor}
    )
    await db.commit()
    log.info("[wallet] topup ws=%s +%s → balance %s", workspace_id, amount_vnd, new_bal)
    return new_bal


async def charge(
    db: AsyncSession, workspace_id: str, product_key: str, units: float,
    actor: str, ref_id: str | None = None, *, allow_negative: bool = False,
) -> dict:
    """
    Charge price for a usage unit. Updates wallet + logs transaction.
    Returns dict with price + new balance + breakdown.
    Raises ValueError if balance insufficient (unless allow_negative).
    """
    price_vnd, breakdown = await calc_price_vnd(db, product_key, units)
    if not breakdown.get("found"):
        log.warning("[wallet] product %s not in price_book — skipping charge", product_key)
        return {"charged": False, "reason": "product_not_in_price_book"}

    current = await get_balance(db, workspace_id)
    new_bal = current - price_vnd

    if new_bal < 0 and not allow_negative:
        raise ValueError(f"Số dư không đủ (cần {price_vnd}đ, có {current}đ). "
                         f"Top-up tại /api/v1/billing/topup")

    await db.execute(
        text("""UPDATE wallet_balances SET
                  balance_vnd     = balance_vnd - :p,
                  total_spent     = total_spent + :p,
                  last_charged_at = NOW(),
                  updated_at      = NOW()
                WHERE workspace_id = :w"""),
        {"w": workspace_id, "p": price_vnd}
    )
    await db.execute(
        text("""INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                cost_usd, description, ref_id, actor, metadata)
                VALUES(:w,'charge',:a,:b,:c,:d,:r,:ac,CAST(:m AS JSONB))"""),
        {"w": workspace_id, "a": -price_vnd, "b": new_bal,
         "c": breakdown.get("cost_usd", 0), "d": breakdown.get("display_name", product_key),
         "r": ref_id, "ac": actor,
         "m": '{"units":%s,"markup":%s}' % (breakdown.get("units", 1), breakdown.get("markup_ratio", 4))}
    )
    await db.commit()
    log.info("[wallet] charge ws=%s -%s → balance %s (product=%s units=%s)",
             workspace_id, price_vnd, new_bal, product_key, units)
    return {
        "charged": True,
        "price_vnd": float(price_vnd),
        "balance_after_vnd": float(new_bal),
        "breakdown": breakdown,
    }


# ─── Subscription helpers ────────────────────────────
async def get_active_sub(db: AsyncSession, workspace_id: str) -> dict | None:
    row = (await db.execute(
        text("""SELECT id, tier, price_vnd_month, quota_agent_runs, quota_image_renders,
                       quota_text_tokens_out, used_agent_runs, used_image_renders,
                       used_text_tokens_out, period_start, period_end, status
                FROM subscriptions WHERE workspace_id = :w AND status = 'active'
                ORDER BY created_at DESC LIMIT 1"""),
        {"w": workspace_id}
    )).first()
    if row is None:
        return None
    return {
        "id": str(row[0]), "tier": row[1], "price_vnd_month": float(row[2]),
        "quota": {"agent_runs": row[3], "image_renders": row[4], "text_tokens_out": row[5]},
        "used":  {"agent_runs": row[6], "image_renders": row[7], "text_tokens_out": row[8]},
        "period_start": row[9].isoformat() if row[9] else None,
        "period_end":   row[10].isoformat() if row[10] else None,
        "status": row[11],
    }


async def consume_subscription_quota(
    db: AsyncSession, workspace_id: str,
    *, agent_runs: int = 0, image_renders: int = 0, text_tokens_out: int = 0,
) -> dict:
    """
    Try to deduct usage from subscription quota first. If quota exhausted,
    return remaining shortfall (caller should charge wallet for that).
    """
    sub = await get_active_sub(db, workspace_id)
    if not sub:
        return {"applied": False, "shortfall": {
            "agent_runs": agent_runs, "image_renders": image_renders, "text_tokens_out": text_tokens_out,
        }}
    quota = sub["quota"]; used = sub["used"]
    avail_runs   = max(0, quota["agent_runs"]      - used["agent_runs"])
    avail_imgs   = max(0, quota["image_renders"]   - used["image_renders"])
    avail_tokens = max(0, quota["text_tokens_out"] - used["text_tokens_out"])

    take_runs   = min(agent_runs,      avail_runs)
    take_imgs   = min(image_renders,   avail_imgs)
    take_tokens = min(text_tokens_out, avail_tokens)

    if take_runs or take_imgs or take_tokens:
        await db.execute(
            text("""UPDATE subscriptions SET
                      used_agent_runs      = used_agent_runs + :r,
                      used_image_renders   = used_image_renders + :i,
                      used_text_tokens_out = used_text_tokens_out + :t
                    WHERE id = CAST(:sid AS UUID)"""),
            {"r": take_runs, "i": take_imgs, "t": take_tokens, "sid": sub["id"]}
        )
        await db.commit()

    return {
        "applied": True,
        "tier": sub["tier"],
        "consumed": {"agent_runs": take_runs, "image_renders": take_imgs, "text_tokens_out": take_tokens},
        "shortfall": {
            "agent_runs":      agent_runs - take_runs,
            "image_renders":   image_renders - take_imgs,
            "text_tokens_out": text_tokens_out - take_tokens,
        },
    }
