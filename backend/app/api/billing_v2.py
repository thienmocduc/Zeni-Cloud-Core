"""
Zeni Cloud Core — Billing v2 API (wallet + subscription + price book).

Endpoints:
  GET  /billing/wallet?ws=X         — balance + recent transactions
  POST /billing/wallet/topup        — admin top-up (manual, payment integration later)
  GET  /billing/subscription?ws=X   — current subscription tier + quota usage
  POST /billing/subscribe?ws=X      — change tier (charges wallet for first month)
  GET  /billing/price-book          — price book (cho khách xem giá)
  GET  /billing/transactions?ws=X   — wallet transaction history
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import pricing
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.billing_v2")
router = APIRouter(prefix="/billing", tags=["billing", "wallet"])


class TopupIn(BaseModel):
    amount_vnd: Decimal = Field(gt=0, le=100_000_000)
    payment_ref: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)


class SubscribeIn(BaseModel):
    tier: str = Field(pattern=r"^(free|starter|pro|business|enterprise)$")


class AdminGrantIn(BaseModel):
    """Admin grant subscription cho doanh nghiệp VIP (không qua thanh toán)."""
    workspace_id: str = Field(min_length=2, max_length=32)
    tier: str = Field(pattern=r"^(starter|pro|business|enterprise)$")
    duration_months: int = Field(default=12, ge=1, le=120)
    reason: str = Field(min_length=5, max_length=500,
                         description="Lý do grant: 'VIP customer · Founder partnership' …")
    custom_quota_runs: int | None = None
    custom_quota_renders: int | None = None
    custom_quota_tokens: int | None = None


class AdminBulkGrantIn(BaseModel):
    """Bulk grant tier cho nhiều workspaces cùng lúc."""
    workspace_ids: list[str] = Field(min_length=1, max_length=50)
    tier: str = Field(pattern=r"^(starter|pro|business|enterprise)$")
    duration_months: int = Field(default=12, ge=1, le=120)
    reason: str = Field(min_length=5, max_length=500)


@router.get("/wallet")
async def get_wallet(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Số dư + tổng nạp + tổng chi của workspace."""
    await require_workspace_access(ws, me)
    row = (await db.execute(
        text("""SELECT balance_vnd, total_topped_up, total_spent, last_charged_at, updated_at
                FROM wallet_balances WHERE workspace_id = :w"""),
        {"w": ws}
    )).first()
    if row is None:
        return {"workspace_id": ws, "balance_vnd": 0, "total_topped_up": 0,
                "total_spent": 0, "last_charged_at": None, "currency": "VND"}
    return {
        "workspace_id": ws,
        "balance_vnd":     float(row[0] or 0),
        "total_topped_up": float(row[1] or 0),
        "total_spent":     float(row[2] or 0),
        "last_charged_at": row[3].isoformat() if row[3] else None,
        "updated_at":      row[4].isoformat() if row[4] else None,
        "currency": "VND",
    }


@router.post("/wallet/topup")
async def topup_wallet(
    ws: str,
    data: TopupIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Nạp tiền vào ví workspace. Hiện cần Admin/Owner; sau sẽ hook VNPay."""
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để top-up")
    new_bal = await pricing.topup(
        db, ws, data.amount_vnd, actor=me.email, payment_ref=data.payment_ref,
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="billing.topup",
        target=str(data.amount_vnd), severity="ok",
        metadata={"payment_ref": data.payment_ref, "note": data.note},
    )
    await db.commit()
    return {
        "workspace_id": ws,
        "topped_up_vnd": float(data.amount_vnd),
        "new_balance_vnd": float(new_bal),
    }


@router.get("/subscription")
async def get_subscription(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Subscription hiện tại + usage trong period."""
    await require_workspace_access(ws, me)
    sub = await pricing.get_active_sub(db, ws)
    if sub is None:
        return {"workspace_id": ws, "tier": "free", "active": False,
                "quota": pricing.SUBSCRIPTION_TIERS["free"],
                "used": {"agent_runs": 0, "image_renders": 0, "text_tokens_out": 0}}
    return {**sub, "workspace_id": ws, "active": True}


@router.post("/subscribe")
async def subscribe(
    ws: str,
    data: SubscribeIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Đăng ký / đổi tier. Charge wallet cho tháng đầu."""
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để subscribe")

    tier_cfg = pricing.SUBSCRIPTION_TIERS.get(data.tier)
    if not tier_cfg:
        raise HTTPException(status_code=400, detail="tier không hợp lệ")

    # Charge wallet for tier price (skip free + enterprise)
    if data.tier not in ("free", "enterprise"):
        price = Decimal(str(tier_cfg["price_vnd_month"]))
        balance = await pricing.get_balance(db, ws)
        if balance < price:
            raise HTTPException(status_code=402,
                                detail=f"Số dư không đủ (cần {price}đ, có {balance}đ). Top-up trước.")
        new_bal = balance - price
        await db.execute(
            text("""UPDATE wallet_balances SET
                      balance_vnd = balance_vnd - :p,
                      total_spent = total_spent + :p,
                      updated_at  = NOW()
                    WHERE workspace_id = :w"""),
            {"w": ws, "p": price}
        )
        await db.execute(
            text("""INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                    description, actor)
                    VALUES(:w,'sub_payment',:a,:b,:d,:ac)"""),
            {"w": ws, "a": -price, "b": new_bal,
             "d": f"Subscription {data.tier} (1 month)", "ac": me.email}
        )

    # Cancel any existing active sub
    await db.execute(
        text("UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
             "WHERE workspace_id=:w AND status='active'"),
        {"w": ws}
    )
    # Insert new sub
    period_end = datetime.now(timezone.utc) + timedelta(days=30)
    await db.execute(
        text("""INSERT INTO subscriptions(workspace_id, tier, price_vnd_month,
                  quota_agent_runs, quota_image_renders, quota_text_tokens_out, period_end)
                VALUES(:w,:t,:p,:r,:i,:o,:e)"""),
        {"w": ws, "t": data.tier,
         "p": tier_cfg["price_vnd_month"],
         "r": tier_cfg["quota_agent_runs"],
         "i": tier_cfg["quota_image_renders"],
         "o": tier_cfg["quota_text_tokens_out"],
         "e": period_end}
    )
    await audit_push(db, actor=me.email, workspace_id=ws, action="billing.subscribe",
                     target=data.tier, severity="ok",
                     metadata={"price": tier_cfg["price_vnd_month"]})
    await db.commit()

    return {
        "workspace_id": ws,
        "tier": data.tier,
        "price_vnd_month": tier_cfg["price_vnd_month"],
        "quota": {k: v for k, v in tier_cfg.items() if k.startswith("quota")},
        "period_end": period_end.isoformat(),
        "active": True,
    }


@router.post("/admin/grant-tier")
async def admin_grant_tier(
    data: AdminGrantIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Admin grant subscription cho 1 workspace KHÔNG qua hệ thống thanh toán.
    Chỉ Owner role mới gọi được. Phù hợp cho:
      - Founder partner companies
      - VIP doanh nghiệp lớn
      - Internal Zeni Holdings workspaces
      - Promotional / testing accounts
    """
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role để grant tier")

    tier_cfg = pricing.SUBSCRIPTION_TIERS.get(data.tier)
    if not tier_cfg:
        raise HTTPException(status_code=400, detail="tier không hợp lệ")

    # Validate workspace exists
    ws_row = (await db.execute(
        text("SELECT id, name FROM workspaces WHERE id = :w"), {"w": data.workspace_id}
    )).first()
    if ws_row is None:
        raise HTTPException(status_code=404, detail=f"workspace {data.workspace_id} không tồn tại")

    quota_runs = data.custom_quota_runs or tier_cfg["quota_agent_runs"]
    quota_renders = data.custom_quota_renders or tier_cfg["quota_image_renders"]
    quota_tokens = data.custom_quota_tokens or tier_cfg["quota_text_tokens_out"]

    # Cancel existing active sub
    await db.execute(
        text("UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
             "WHERE workspace_id=:w AND status='active'"),
        {"w": data.workspace_id}
    )
    period_end = datetime.now(timezone.utc) + timedelta(days=30 * data.duration_months)
    await db.execute(
        text("""INSERT INTO subscriptions(workspace_id, tier, price_vnd_month,
                  quota_agent_runs, quota_image_renders, quota_text_tokens_out,
                  period_end, auto_renew)
                VALUES(:w,:t,0,:r,:i,:o,:e,FALSE)"""),  # price=0 (granted), no auto-renew
        {"w": data.workspace_id, "t": data.tier,
         "r": quota_runs, "i": quota_renders, "o": quota_tokens, "e": period_end}
    )

    # Get current balance first (asyncpg doesn't allow param re-use in subquery)
    bal_row = (await db.execute(
        text("SELECT COALESCE(balance_vnd, 0) FROM wallet_balances WHERE workspace_id = :w"),
        {"w": data.workspace_id}
    )).first()
    balance_after = float(bal_row[0]) if bal_row else 0

    # Log to wallet ledger as 'grant' transaction
    await db.execute(
        text("""INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                description, ref_id, actor, metadata)
                VALUES(:w,'sub_payment',0,:b,:d,NULL,:ac,CAST(:m AS JSONB))"""),
        {"w": data.workspace_id, "b": balance_after,
         "d": f"VIP grant {data.tier} ({data.duration_months}mo) - {data.reason[:120]}",
         "ac": me.email,
         "m": '{"granted_by_admin":true,"tier":"%s","duration_months":%d}' % (data.tier, data.duration_months)}
    )
    await audit_push(
        db, actor=me.email, workspace_id=data.workspace_id, action="billing.admin_grant_tier",
        target=f"{data.tier} {data.duration_months}mo", severity="info",
        metadata={"reason": data.reason, "quotas": {"runs": quota_runs, "renders": quota_renders, "tokens": quota_tokens}},
    )
    await db.commit()
    log.info("[admin_grant] %s → %s tier=%s for %d months by %s",
             data.workspace_id, ws_row[1], data.tier, data.duration_months, me.email)

    return {
        "ok": True,
        "workspace_id": data.workspace_id,
        "workspace_name": ws_row[1],
        "tier_granted": data.tier,
        "duration_months": data.duration_months,
        "period_end": period_end.isoformat(),
        "quota": {"runs": quota_runs, "renders": quota_renders, "tokens": quota_tokens},
        "reason": data.reason,
        "granted_by": me.email,
    }


@router.post("/admin/bulk-grant")
async def admin_bulk_grant(
    data: AdminBulkGrantIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Bulk grant tier cho nhiều workspaces cùng lúc (Owner only).
    Phù hợp khi launch: cấp Pro tier cho 5-10 doanh nghiệp internal partners.
    """
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role")

    tier_cfg = pricing.SUBSCRIPTION_TIERS.get(data.tier)
    if not tier_cfg:
        raise HTTPException(status_code=400, detail="tier không hợp lệ")

    period_end = datetime.now(timezone.utc) + timedelta(days=30 * data.duration_months)
    quota_runs = tier_cfg["quota_agent_runs"]
    quota_renders = tier_cfg["quota_image_renders"]
    quota_tokens = tier_cfg["quota_text_tokens_out"]

    success: list[str] = []
    failed: list[dict] = []

    for ws_id in data.workspace_ids:
        try:
            # Validate workspace exists
            ws_row = (await db.execute(
                text("SELECT id, name FROM workspaces WHERE id = :w"), {"w": ws_id}
            )).first()
            if ws_row is None:
                failed.append({"workspace_id": ws_id, "error": "workspace not found"})
                continue

            # Cancel existing active sub
            await db.execute(
                text("UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
                     "WHERE workspace_id=:w AND status='active'"),
                {"w": ws_id}
            )
            # Insert new sub
            await db.execute(
                text("""INSERT INTO subscriptions(workspace_id, tier, price_vnd_month,
                          quota_agent_runs, quota_image_renders, quota_text_tokens_out,
                          period_end, auto_renew)
                        VALUES(:w,:t,0,:r,:i,:o,:e,FALSE)"""),
                {"w": ws_id, "t": data.tier, "r": quota_runs,
                 "i": quota_renders, "o": quota_tokens, "e": period_end}
            )
            # Get balance
            bal_row = (await db.execute(
                text("SELECT COALESCE(balance_vnd,0) FROM wallet_balances WHERE workspace_id=:w"),
                {"w": ws_id}
            )).first()
            balance_after = float(bal_row[0]) if bal_row else 0
            # Log transaction
            await db.execute(
                text("""INSERT INTO wallet_transactions(workspace_id, kind, amount_vnd, balance_after,
                        description, ref_id, actor, metadata)
                        VALUES(:w,'sub_payment',0,:b,:d,NULL,:ac,CAST(:m AS JSONB))"""),
                {"w": ws_id, "b": balance_after,
                 "d": f"BULK VIP grant {data.tier} ({data.duration_months}mo) - {data.reason[:80]}",
                 "ac": me.email,
                 "m": '{"granted_by_admin":true,"bulk":true,"tier":"%s","months":%d}' % (data.tier, data.duration_months)}
            )
            await audit_push(
                db, actor=me.email, workspace_id=ws_id, action="billing.admin_bulk_grant",
                target=f"{data.tier} {data.duration_months}mo", severity="info",
                metadata={"reason": data.reason},
            )
            success.append(ws_id)
        except Exception as e:
            log.exception("bulk grant failed for %s", ws_id)
            failed.append({"workspace_id": ws_id, "error": f"{type(e).__name__}: {e}"})

    await db.commit()

    return {
        "ok": True,
        "tier_granted": data.tier,
        "duration_months": data.duration_months,
        "period_end": period_end.isoformat(),
        "success_count": len(success),
        "success_workspaces": success,
        "failed_count": len(failed),
        "failed_workspaces": failed,
    }


@router.get("/admin/granted-list")
async def admin_list_grants(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all VIP grants (subscriptions với price=0)."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role")
    rows = (await db.execute(
        text("""SELECT s.workspace_id, w.name, s.tier, s.period_end, s.created_at, s.status,
                       s.used_agent_runs, s.quota_agent_runs
                FROM subscriptions s
                JOIN workspaces w ON w.id = s.workspace_id
                WHERE s.price_vnd_month = 0 AND s.tier != 'free'
                ORDER BY s.created_at DESC""")
    )).all()
    return {
        "count": len(rows),
        "granted_workspaces": [
            {"workspace_id": r[0], "workspace_name": r[1], "tier": r[2],
             "period_end": r[3].isoformat() if r[3] else None,
             "granted_at": r[4].isoformat() if r[4] else None,
             "status": r[5], "usage": f"{r[6]}/{r[7]} runs"}
            for r in rows
        ],
    }


@router.get("/price-book")
async def get_price_book(db: AsyncSession = Depends(get_db)) -> dict:
    """Bảng giá public — anyone can see."""
    rows = (await db.execute(
        text("""SELECT product_key, display_name, cost_usd_per_unit, markup_ratio, unit
                FROM price_book WHERE active = TRUE
                ORDER BY product_key""")
    )).all()
    items = []
    for r in rows:
        cost = float(r[2] or 0)
        markup = float(r[3] or 4)
        price_usd = cost * markup
        price_vnd = price_usd * float(pricing.USD_TO_VND)
        items.append({
            "product_key": r[0],
            "display_name": r[1],
            "unit": r[4],
            "cost_usd": cost,
            "markup_ratio": markup,
            "price_usd": price_usd,
            "price_vnd": round(price_vnd, 2),
        })
    return {
        "currency": "VND",
        "fx_rate": float(pricing.USD_TO_VND),
        "items": items,
        "subscription_tiers": pricing.SUBSCRIPTION_TIERS,
    }


@router.get("/transactions")
async def list_transactions(
    ws: str,
    limit: int = Query(default=50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lịch sử giao dịch wallet."""
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        text("""SELECT id, kind, amount_vnd, balance_after, cost_usd, description,
                       ref_id, actor, created_at
                FROM wallet_transactions WHERE workspace_id = :w
                ORDER BY id DESC LIMIT :lim"""),
        {"w": ws, "lim": limit}
    )).all()
    return {
        "workspace_id": ws,
        "count": len(rows),
        "transactions": [
            {"id": r[0], "kind": r[1], "amount_vnd": float(r[2]),
             "balance_after_vnd": float(r[3]),
             "cost_usd": float(r[4]) if r[4] else None,
             "description": r[5], "ref_id": r[6], "actor": r[7],
             "created_at": r[8].isoformat() if r[8] else None}
            for r in rows
        ],
    }
