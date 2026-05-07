"""
Zeni Cloud Core — $ZENI Token integration API (Sprint A4).

Endpoints (prefix ``/zeni-token``):

Wallet:
  GET   /wallet?ws=                           — current balance + tx history
  POST  /wallet/link                          — link Ethereum wallet (signed)
  POST  /wallet/nonce                         — request nonce + message to sign
  POST  /wallet/sync?ws=                      — force balance refresh

Transactions:
  POST  /transfer                             — internal/external $ZENI transfer
  GET   /transactions?ws=&type=&from=&to=     — transaction history

Pay with token:
  POST  /pay-with-token                       — settle subscription via $ZENI
  GET   /quote?vnd_amount=&discount=          — preview ZENI required

Rewards:
  GET   /rewards/eligible?ws=                 — list eligible rewards
  POST  /rewards/claim?ws=                    — claim a reward

Badges (Soulbound, ZeniBadge):
  GET   /badges?ws=                           — list badges
  POST  /badges/mint?ws=                      — mint a soulbound badge (admin)

Governance:
  POST  /burn?ws=                             — voluntary burn
  GET   /exchange-rate                        — current ZENI/VND rate (public)
  POST  /exchange-rate                        — update rate (Owner only)

Stack:
  GET   /stack                                — public on-chain stack info
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_workspace_access,
)
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.zeni_token import (
    AFFILIATE_ADDR,
    BADGE_SBT_ADDR,
    DEFAULT_DISCOUNT_PERCENT,
    DISCOUNT_MAX_PERCENT,
    DISCOUNT_MIN_PERCENT,
    VALID_BADGE_TYPES,
    ZENI_TOKEN_ADDR,
    award_reward,
    build_link_message,
    generate_link_nonce,
    get_balance,
    get_exchange_rate,
    get_wallet_row,
    link_wallet,
    list_badges,
    list_eligible_rewards,
    mint_badge,
    pay_with_token,
    record_burn,
    sync_balance,
    transfer_token,
    update_exchange_rate,
)

log = logging.getLogger("zeni.api.zeni_token")
router = APIRouter(prefix="/zeni-token", tags=["zeni-token", "web3", "$ZENI"])


ETH_ADDR_PATTERN = r"^0x[a-fA-F0-9]{40}$"
ACTION_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"
BADGE_TYPE_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"


# ─── Pydantic schemas ───────────────────────────────────────────────────────


class WalletNonceIn(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=32)
    eth_address: str = Field(pattern=ETH_ADDR_PATTERN)


class WalletLinkIn(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=32)
    eth_address: str = Field(pattern=ETH_ADDR_PATTERN)
    signature: str = Field(min_length=8, max_length=200)
    nonce: str = Field(min_length=8, max_length=80)


class TransferIn(BaseModel):
    from_workspace: str = Field(min_length=1, max_length=32)
    to_workspace: str | None = Field(default=None, max_length=32)
    to_address: str | None = Field(default=None, pattern=ETH_ADDR_PATTERN)
    amount_zeni: float = Field(gt=0, le=1_000_000_000)
    reason: str = Field(default="manual_transfer", max_length=80)


class PayWithTokenIn(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=32)
    vnd_amount: int = Field(gt=0, le=2_000_000_000)
    intent_code: str | None = Field(default=None, max_length=40)
    discount_percent: int = Field(
        default=DEFAULT_DISCOUNT_PERCENT,
        ge=DISCOUNT_MIN_PERCENT, le=DISCOUNT_MAX_PERCENT,
    )


class RewardClaimIn(BaseModel):
    action: str = Field(pattern=ACTION_PATTERN)


class BadgeMintIn(BaseModel):
    badge_type: str = Field(pattern=BADGE_TYPE_PATTERN)
    eth_address: str | None = Field(default=None, pattern=ETH_ADDR_PATTERN)
    metadata_uri: str | None = Field(default=None, max_length=500)


class BurnIn(BaseModel):
    amount_zeni: float = Field(gt=0, le=1_000_000_000)
    reason: str = Field(min_length=3, max_length=120)


class UpdateRateIn(BaseModel):
    rate_vnd: float = Field(gt=0, le=10_000_000)
    rate_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    source: str = Field(default="manual", max_length=40)


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _resolve_workspace(db: AsyncSession, ws: str) -> str:
    """Map workspace.code or workspace.id → canonical workspace_id."""
    row = (await db.execute(
        text("SELECT id FROM workspaces WHERE id = :w OR code = :w LIMIT 1"),
        {"w": ws},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Workspace '{ws}' không tồn tại")
    return row[0]


def _require_owner(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role cho action này")


# ─── Public endpoints ───────────────────────────────────────────────────────


@router.get("/stack")
async def stack_info() -> dict:
    """Public summary of $ZENI on-chain stack."""
    return {
        "chain": "polygon",
        "contracts": {
            "ZENI_TOKEN": ZENI_TOKEN_ADDR,
            "AFFILIATE_COMMISSION": AFFILIATE_ADDR,
            "BADGE_SBT": BADGE_SBT_ADDR,
        },
        "explorer": {
            "ZENI_TOKEN": f"https://polygonscan.com/address/{ZENI_TOKEN_ADDR}",
            "AFFILIATE_COMMISSION": f"https://polygonscan.com/address/{AFFILIATE_ADDR}",
            "BADGE_SBT": f"https://polygonscan.com/address/{BADGE_SBT_ADDR}",
        },
        "discount_range_percent": [DISCOUNT_MIN_PERCENT, DISCOUNT_MAX_PERCENT],
        "default_discount_percent": DEFAULT_DISCOUNT_PERCENT,
        "valid_badge_types": sorted(VALID_BADGE_TYPES),
    }


@router.get("/exchange-rate")
async def exchange_rate(db: AsyncSession = Depends(get_db)) -> dict:
    """Current ZENI/VND rate (public; safe for unauthenticated frontend)."""
    return await get_exchange_rate(db)


@router.get("/quote")
async def quote_payment(
    vnd_amount: int = Query(..., gt=0, le=2_000_000_000),
    discount_percent: int = Query(DEFAULT_DISCOUNT_PERCENT,
                                   ge=DISCOUNT_MIN_PERCENT, le=DISCOUNT_MAX_PERCENT),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Preview how much $ZENI is required to pay a given VND amount."""
    rate = await get_exchange_rate(db)
    rate_vnd = rate["rate"]
    discounted_vnd = vnd_amount * (100 - discount_percent) / 100.0
    zeni_required = discounted_vnd / rate_vnd if rate_vnd > 0 else 0
    return {
        "vnd_amount": vnd_amount,
        "vnd_amount_discounted": int(discounted_vnd),
        "discount_percent": discount_percent,
        "savings_vnd": vnd_amount - int(discounted_vnd),
        "rate_zeni_vnd": rate_vnd,
        "zeni_required": round(zeni_required, 8),
    }


# ─── Wallet linking ─────────────────────────────────────────────────────────


@router.post("/wallet/nonce")
async def request_nonce(
    data: WalletNonceIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate a nonce + message that the user will sign with their wallet.

    Frontend flow:
      1. POST /wallet/nonce → receive ``message`` + ``nonce``.
      2. wallet.signMessage(message) → ``signature``.
      3. POST /wallet/link with (eth_address, signature, nonce).
    """
    workspace_id = await _resolve_workspace(db, data.workspace_id)
    await require_workspace_access(workspace_id, me)

    nonce = generate_link_nonce()
    message = build_link_message(data.eth_address, nonce)
    return {
        "workspace_id": workspace_id,
        "eth_address": data.eth_address,
        "nonce": nonce,
        "message": message,
        "instruction_vi": (
            "Mở ví crypto (MetaMask/Privy/...) → ký message này → gửi signature về "
            "endpoint /zeni-token/wallet/link cùng với eth_address và nonce."
        ),
    }


@router.post("/wallet/link")
async def link(
    data: WalletLinkIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Link Ethereum wallet to workspace (verifies EIP-191 signature)."""
    workspace_id = await _resolve_workspace(db, data.workspace_id)
    await require_workspace_access(workspace_id, me)

    try:
        result = await link_wallet(
            db,
            workspace_id=workspace_id,
            eth_address=data.eth_address,
            signature=data.signature,
            nonce=data.nonce,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.wallet.link", target=result["eth_address"], severity="ok",
        metadata={"chain": result["chain"]},
    )
    await db.commit()
    return {"ok": True, **result}


@router.get("/wallet")
async def get_wallet(
    ws: str = Query(..., description="workspace_id or workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current $ZENI balance, totals, and last 30 transactions."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    try:
        bal = await get_balance(db, workspace_id=workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    rows = (await db.execute(text("""
        SELECT id, type, direction, amount_zeni, vnd_value, reason, intent_code,
               counterparty_ws, counterparty_addr, tx_hash, status, settlement,
               created_at
          FROM token_transactions
         WHERE workspace_id = :w
         ORDER BY created_at DESC
         LIMIT 30
    """), {"w": workspace_id})).mappings().all()
    history = [
        {
            "id": int(r["id"]),
            "type": r["type"],
            "direction": r["direction"],
            "amount_zeni": float(r["amount_zeni"]),
            "vnd_value": float(r["vnd_value"]) if r["vnd_value"] else None,
            "reason": r["reason"],
            "intent_code": r["intent_code"],
            "counterparty_ws": r["counterparty_ws"],
            "counterparty_addr": r["counterparty_addr"],
            "tx_hash": r["tx_hash"],
            "status": r["status"],
            "settlement": r["settlement"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows
    ]
    await db.commit()
    return {**bal, "transactions": history}


@router.post("/wallet/sync")
async def sync_wallet(
    ws: str = Query(..., description="workspace_id or workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Force on-chain balance refresh."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    try:
        on_chain = await sync_balance(db, workspace_id=workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        log.exception("sync_balance failed")
        raise HTTPException(status_code=502, detail=f"RPC sync thất bại: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.wallet.sync", target=str(on_chain), severity="info",
    )
    await db.commit()
    return {"ok": True, "workspace_id": workspace_id, "balance_zeni": on_chain}


# ─── Transactions ───────────────────────────────────────────────────────────


@router.post("/transfer")
async def transfer(
    data: TransferIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Transfer $ZENI between workspaces (off-chain) or to an external address."""
    if not data.to_workspace and not data.to_address:
        raise HTTPException(status_code=400, detail="Phải cung cấp to_workspace hoặc to_address")

    src_id = await _resolve_workspace(db, data.from_workspace)
    await require_workspace_access(src_id, me)

    dst_id: str | None = None
    if data.to_workspace:
        dst_id = await _resolve_workspace(db, data.to_workspace)

    try:
        result = await transfer_token(
            db,
            from_workspace=src_id, to_workspace=dst_id, to_address=data.to_address,
            amount=Decimal(str(data.amount_zeni)),
            reason=data.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=src_id,
        action="zeni_token.transfer", target=str(data.amount_zeni),
        severity="info",
        metadata={
            "to_workspace": dst_id,
            "to_address": data.to_address,
            "settlement": result["settlement"],
            "reason": data.reason,
        },
    )
    await db.commit()
    return {"ok": True, **result}


@router.get("/transactions")
async def list_transactions(
    ws: str = Query(...),
    tx_type: str | None = Query(
        None, alias="type", pattern=r"^(earn|spend|transfer|burn|mint|reward)$"
    ),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Filtered transaction history."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    where = ["workspace_id = :w"]
    params: dict[str, Any] = {"w": workspace_id, "n": limit}
    if tx_type:
        where.append("type = :t")
        params["t"] = tx_type
    if from_date:
        where.append("created_at >= :fd")
        params["fd"] = from_date
    if to_date:
        where.append("created_at <= :td")
        params["td"] = to_date

    rows = (await db.execute(text(f"""
        SELECT id, type, direction, amount_zeni, vnd_value, reason, intent_code,
               counterparty_ws, counterparty_addr, tx_hash, status, settlement,
               created_at
          FROM token_transactions
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT :n
    """), params)).mappings().all()
    return {
        "workspace_id": workspace_id,
        "count": len(rows),
        "transactions": [
            {
                **dict(r),
                "amount_zeni": float(r["amount_zeni"]),
                "vnd_value": float(r["vnd_value"]) if r["vnd_value"] else None,
                "id": int(r["id"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            } for r in rows
        ],
    }


# ─── Pay with token ─────────────────────────────────────────────────────────


@router.post("/pay-with-token")
async def pay_with_zeni(
    data: PayWithTokenIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pay subscription/services with $ZENI Token (5-15% discount applied)."""
    workspace_id = await _resolve_workspace(db, data.workspace_id)
    await require_workspace_access(workspace_id, me)

    try:
        result = await pay_with_token(
            db,
            workspace_id=workspace_id,
            vnd_amount=data.vnd_amount,
            intent_code=data.intent_code,
            discount_percent=data.discount_percent,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.pay", target=data.intent_code or str(data.vnd_amount),
        severity="ok",
        metadata={
            "vnd_amount_original": data.vnd_amount,
            "vnd_amount_discounted": result["vnd_amount_discounted"],
            "zeni_paid": result["zeni_paid"],
            "discount_percent": data.discount_percent,
        },
    )
    await db.commit()
    return {"ok": True, **result}


# ─── Rewards ────────────────────────────────────────────────────────────────


@router.get("/rewards/eligible")
async def rewards_eligible(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List rewards the workspace can still claim (or reasons why not)."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    rules = await list_eligible_rewards(db, workspace_id=workspace_id)
    return {
        "workspace_id": workspace_id,
        "count": len(rules),
        "rewards": rules,
    }


@router.post("/rewards/claim")
async def rewards_claim(
    data: RewardClaimIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Claim a configured reward (idempotent against caps & cooldowns)."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    try:
        result = await award_reward(db, workspace_id=workspace_id, action=data.action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.reward.claim", target=data.action, severity="ok",
        metadata={"amount_zeni": result["amount_zeni"]},
    )
    await db.commit()
    return {"ok": True, **result}


# ─── Badges (Soulbound) ─────────────────────────────────────────────────────


@router.get("/badges")
async def badges(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    items = await list_badges(db, workspace_id=workspace_id)
    return {
        "workspace_id": workspace_id,
        "contract_address": BADGE_SBT_ADDR,
        "count": len(items),
        "badges": items,
    }


@router.post("/badges/mint")
async def badges_mint(
    data: BadgeMintIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: mint a soulbound badge to a workspace's wallet."""
    _require_owner(me)
    workspace_id = await _resolve_workspace(db, ws)

    if data.badge_type not in VALID_BADGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"badge_type không hợp lệ. Hỗ trợ: {sorted(VALID_BADGE_TYPES)}",
        )

    try:
        result = await mint_badge(
            db,
            workspace_id=workspace_id, badge_type=data.badge_type,
            eth_address=data.eth_address, metadata_uri=data.metadata_uri,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.badge.mint", target=data.badge_type, severity="ok",
        metadata={
            "badge_id": result["badge_id"],
            "eth_address": result["eth_address"],
            "metadata_uri": data.metadata_uri,
        },
    )
    await db.commit()
    return {"ok": True, **result}


# ─── Burn ───────────────────────────────────────────────────────────────────


@router.post("/burn")
async def burn(
    data: BurnIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Voluntarily burn $ZENI (governance, milestone, commitment)."""
    workspace_id = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    try:
        result = await record_burn(
            db, workspace_id=workspace_id,
            amount=Decimal(str(data.amount_zeni)), reason=data.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="zeni_token.burn", target=str(data.amount_zeni), severity="warning",
        metadata={"reason": data.reason, "burn_id": result["burn_id"]},
    )
    await db.commit()
    return {"ok": True, **result}


# ─── Exchange rate (admin) ──────────────────────────────────────────────────


@router.post("/exchange-rate")
async def update_rate(
    data: UpdateRateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: insert a new ZENI/VND rate snapshot."""
    _require_owner(me)
    result = await update_exchange_rate(
        db, rate_vnd=data.rate_vnd, rate_usd=data.rate_usd, source=data.source,
    )
    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="zeni_token.rate.update", target=str(data.rate_vnd), severity="info",
        metadata={"rate_vnd": data.rate_vnd, "rate_usd": data.rate_usd, "source": data.source},
    )
    await db.commit()
    return {"ok": True, **result}
