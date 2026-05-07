"""
Zeni Cloud Core — $ZENI Token integration service (Sprint A4).

Bridges Zeni Cloud workspaces to the on-chain $ZENI Token deployed on Polygon
Mainnet. Off-chain accounting (fast, gas-free) for day-to-day spend / earn,
with periodic on-chain settlement for large transfers and badge minting.

Pre-deployed contracts (Polygon Mainnet):
    $ZENI Token        : 0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1
    AffiliateCommission: 0x1d5963FcCfC548275293e51f0F6C7aC482E0b714
    ZeniBadge SBT      : 0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D

Public functions
----------------
- ``link_wallet(db, ws, eth_address, signature, nonce)``
- ``get_balance(db, ws)``                          — cached + on-demand RPC sync
- ``transfer_token(db, from_ws, to_ws, amount, reason)``
- ``pay_with_token(db, ws, vnd_amount, intent_code, discount_percent)``
- ``award_reward(db, ws, action)``
- ``mint_badge(db, ws, badge_type, eth_address)``
- ``sync_balance(db, ws)``                          — refresh from chain
- ``get_exchange_rate(db)``                          — current ZENI/VND
- ``record_burn(db, ws, amount, reason)``
- ``verify_eth_signature(eth_address, signature, nonce)``

Design notes
------------
- We never custody private keys. ``transfer_token`` to an EXTERNAL on-chain
  address returns an unsigned transaction the wallet must sign client-side.
- Internal workspace-to-workspace ``transfer`` is a pure off-chain ledger
  update — fast, no gas. Periodic batch settlement (cron) is out of scope here.
- Reward grants are mint-equivalent (ledger only). On-chain mint of rewards is
  a future on-chain operation by an authorized signer using ``ZeniAccessControl``.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.zeni_token")


# ─── Constants ──────────────────────────────────────────────────────────────
ZENI_TOKEN_ADDR = "0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1"
AFFILIATE_ADDR = "0x1d5963FcCfC548275293e51f0F6C7aC482E0b714"
BADGE_SBT_ADDR = "0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D"
DEFAULT_CHAIN = "polygon"

# Discount range allowed when paying with $ZENI (5%-15%)
DISCOUNT_MIN_PERCENT = 5
DISCOUNT_MAX_PERCENT = 15
DEFAULT_DISCOUNT_PERCENT = 10

# How long a cached balance is considered fresh
BALANCE_CACHE_TTL_SECONDS = 60

VALID_BADGE_TYPES = {
    "early_adopter",
    "loyal_customer",
    "top_referrer",
    "founder",
    "investor",
    "ambassador",
}


# ─── Helpers: address validation + signature verification ───────────────────


def _checksum_address(addr: str) -> str:
    """Return EIP-55 checksum address. Raises on invalid input."""
    try:
        from web3 import Web3  # local import — keeps import cost off cold start
    except ImportError as e:
        raise RuntimeError(f"web3 not installed: {e}")
    if not addr:
        raise ValueError("eth_address rỗng")
    addr = addr.strip()
    if not Web3.is_address(addr):
        raise ValueError(f"Địa chỉ không hợp lệ: {addr}")
    return Web3.to_checksum_address(addr)


def verify_eth_signature(eth_address: str, signature: str, nonce: str) -> bool:
    """Verify an EIP-191 personal_sign signature.

    The user signs the literal message::

        f"Zeni Cloud wallet link\\nAddress: {eth_address}\\nNonce: {nonce}"

    Returns True if recovered address matches ``eth_address``.
    """
    try:
        from eth_account.messages import encode_defunct
        from eth_account import Account
        from web3 import Web3
    except ImportError as e:
        log.error("eth-account not available: %s", e)
        return False

    if not signature or not nonce:
        return False

    try:
        addr_cs = Web3.to_checksum_address(eth_address)
    except Exception:
        return False

    message = (
        "Zeni Cloud wallet link\n"
        f"Address: {addr_cs}\n"
        f"Nonce: {nonce}"
    )
    msg = encode_defunct(text=message)

    try:
        recovered = Account.recover_message(msg, signature=signature)
        return Web3.to_checksum_address(recovered) == addr_cs
    except Exception as e:
        log.warning("signature recovery failed: %s", e)
        return False


def generate_link_nonce() -> str:
    """Create a fresh random nonce the user must sign for wallet linking."""
    return secrets.token_hex(16)


def build_link_message(eth_address: str, nonce: str) -> str:
    """Public helper: deterministic message to display in wallet for signing."""
    addr = _checksum_address(eth_address)
    return (
        "Zeni Cloud wallet link\n"
        f"Address: {addr}\n"
        f"Nonce: {nonce}"
    )


# ─── Wallet linking ─────────────────────────────────────────────────────────


async def link_wallet(
    db: AsyncSession,
    *,
    workspace_id: str,
    eth_address: str,
    signature: str,
    nonce: str,
) -> dict[str, Any]:
    """Link an Ethereum wallet to a workspace (1-1).

    Verifies EIP-191 signature, ensures uniqueness, persists the wallet, and
    triggers an initial on-chain balance sync (best-effort).
    """
    addr = _checksum_address(eth_address)

    if not verify_eth_signature(addr, signature, nonce):
        raise ValueError("Chữ ký không hợp lệ — không thể xác minh quyền sở hữu ví")

    # Reject if already linked to a different workspace
    existing = (await db.execute(text("""
        SELECT workspace_id FROM token_wallets WHERE eth_address = :a
    """), {"a": addr})).first()
    if existing and existing[0] != workspace_id:
        raise ValueError(f"Ví {addr} đã liên kết với workspace khác")

    # Upsert wallet row
    await db.execute(text("""
        INSERT INTO token_wallets
            (workspace_id, eth_address, chain, signature, signature_nonce, linked_at)
        VALUES (:w, :a, :c, :s, :n, NOW())
        ON CONFLICT (workspace_id) DO UPDATE SET
            eth_address     = EXCLUDED.eth_address,
            chain           = EXCLUDED.chain,
            signature       = EXCLUDED.signature,
            signature_nonce = EXCLUDED.signature_nonce,
            linked_at       = NOW()
    """), {
        "w": workspace_id,
        "a": addr,
        "c": DEFAULT_CHAIN,
        "s": signature,
        "n": nonce,
    })

    # Best-effort initial balance sync (don't fail link if RPC down)
    on_chain_balance: float | None = None
    try:
        on_chain_balance = await sync_balance(db, workspace_id=workspace_id)
    except Exception as e:
        log.warning("initial balance sync failed for %s: %s", addr, e)

    return {
        "workspace_id": workspace_id,
        "eth_address": addr,
        "chain": DEFAULT_CHAIN,
        "balance_zeni": on_chain_balance,
    }


# ─── Balance / sync ─────────────────────────────────────────────────────────


async def get_wallet_row(db: AsyncSession, workspace_id: str) -> dict[str, Any] | None:
    row = (await db.execute(text("""
        SELECT workspace_id, eth_address, chain, balance_zeni, total_earned,
               total_spent, total_burned, last_synced_at, linked_at
          FROM token_wallets
         WHERE workspace_id = :w
    """), {"w": workspace_id})).mappings().first()
    return dict(row) if row else None


async def get_balance(
    db: AsyncSession, *, workspace_id: str, force_refresh: bool = False
) -> dict[str, Any]:
    """Return cached balance; refresh from RPC if stale or force_refresh."""
    wallet = await get_wallet_row(db, workspace_id)
    if not wallet:
        raise ValueError("Workspace chưa liên kết ví")

    now = datetime.now(timezone.utc)
    last_sync = wallet.get("last_synced_at")
    is_stale = (
        force_refresh
        or last_sync is None
        or (now - last_sync) > timedelta(seconds=BALANCE_CACHE_TTL_SECONDS)
    )

    if is_stale:
        try:
            await sync_balance(db, workspace_id=workspace_id)
            wallet = await get_wallet_row(db, workspace_id) or wallet
        except Exception as e:
            log.warning("balance refresh failed: %s", e)

    return {
        "workspace_id": workspace_id,
        "eth_address": wallet["eth_address"],
        "chain": wallet["chain"],
        "balance_zeni": float(wallet["balance_zeni"] or 0),
        "total_earned": float(wallet["total_earned"] or 0),
        "total_spent": float(wallet["total_spent"] or 0),
        "total_burned": float(wallet["total_burned"] or 0),
        "last_synced_at": last_sync.isoformat() if last_sync else None,
    }


async def sync_balance(db: AsyncSession, *, workspace_id: str) -> float:
    """Refresh on-chain balance via Polygon RPC (synchronous web3 call)."""
    wallet = await get_wallet_row(db, workspace_id)
    if not wallet:
        raise ValueError("Workspace chưa liên kết ví")

    # Lazy import to avoid hard dependency on web3 at module load
    from app.services.blockchain import read_erc20

    info = read_erc20(wallet["chain"] or DEFAULT_CHAIN, ZENI_TOKEN_ADDR, owner=wallet["eth_address"])
    on_chain = float(info.get("balance_of") or 0)

    await db.execute(text("""
        UPDATE token_wallets
           SET balance_zeni = :b, last_synced_at = NOW()
         WHERE workspace_id = :w
    """), {"b": Decimal(str(on_chain)), "w": workspace_id})
    return on_chain


# ─── Transactions / ledger ──────────────────────────────────────────────────


async def _record_tx(
    db: AsyncSession,
    *,
    workspace_id: str,
    type_: str,
    direction: str,
    amount_zeni: Decimal,
    reason: str,
    counterparty_ws: str | None = None,
    counterparty_addr: str | None = None,
    intent_code: str | None = None,
    tx_hash: str | None = None,
    settlement: str = "offchain",
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert a token_transactions row + update wallet aggregate counters."""
    rate = await get_exchange_rate(db)
    vnd_value = float(amount_zeni) * rate["rate"]

    wallet = await get_wallet_row(db, workspace_id)
    wallet_addr = wallet["eth_address"] if wallet else None

    new_id = (await db.execute(text("""
        INSERT INTO token_transactions
            (workspace_id, wallet_address, type, direction, amount_zeni, vnd_value,
             counterparty_ws, counterparty_addr, reason, intent_code, tx_hash,
             status, settlement, metadata)
        VALUES
            (:w, :wa, :t, :d, :a, :v, :cw, :ca, :r, :ic, :h,
             'confirmed', :s, :m::jsonb)
        RETURNING id
    """), {
        "w": workspace_id, "wa": wallet_addr, "t": type_, "d": direction,
        "a": amount_zeni, "v": Decimal(str(round(vnd_value, 2))),
        "cw": counterparty_ws, "ca": counterparty_addr,
        "r": reason, "ic": intent_code, "h": tx_hash,
        "s": settlement, "m": _json(metadata or {}),
    })).scalar()

    # Update aggregate counters on wallet
    if wallet:
        if direction == "in" and type_ in ("earn", "reward", "mint"):
            await db.execute(text("""
                UPDATE token_wallets
                   SET balance_zeni = balance_zeni + :a,
                       total_earned = total_earned + :a
                 WHERE workspace_id = :w
            """), {"w": workspace_id, "a": amount_zeni})
        elif direction == "in":
            await db.execute(text("""
                UPDATE token_wallets
                   SET balance_zeni = balance_zeni + :a
                 WHERE workspace_id = :w
            """), {"w": workspace_id, "a": amount_zeni})
        elif direction == "out" and type_ == "burn":
            await db.execute(text("""
                UPDATE token_wallets
                   SET balance_zeni = balance_zeni - :a,
                       total_burned = total_burned + :a
                 WHERE workspace_id = :w
            """), {"w": workspace_id, "a": amount_zeni})
        elif direction == "out" and type_ in ("spend", "transfer"):
            await db.execute(text("""
                UPDATE token_wallets
                   SET balance_zeni = balance_zeni - :a,
                       total_spent  = total_spent  + :a
                 WHERE workspace_id = :w
            """), {"w": workspace_id, "a": amount_zeni})
        else:
            await db.execute(text("""
                UPDATE token_wallets
                   SET balance_zeni = balance_zeni - :a
                 WHERE workspace_id = :w
            """), {"w": workspace_id, "a": amount_zeni})
    return int(new_id)


async def transfer_token(
    db: AsyncSession,
    *,
    from_workspace: str,
    to_workspace: str | None,
    to_address: str | None,
    amount: Decimal | float,
    reason: str = "manual_transfer",
) -> dict[str, Any]:
    """Transfer $ZENI between workspaces (off-chain) or to an external address.

    Internal (to_workspace given) → instantly debit/credit the off-chain ledger.
    External (to_address given)   → returns an unsigned tx for the user to sign.
    """
    amount_d = Decimal(str(amount))
    if amount_d <= 0:
        raise ValueError("Số lượng phải > 0")

    src = await get_wallet_row(db, from_workspace)
    if not src:
        raise ValueError("Workspace nguồn chưa liên kết ví")

    if Decimal(str(src["balance_zeni"] or 0)) < amount_d:
        raise ValueError("Số dư không đủ")

    # ── Internal off-chain transfer ──
    if to_workspace and not to_address:
        dst = await get_wallet_row(db, to_workspace)
        if not dst:
            raise ValueError("Workspace đích chưa liên kết ví")

        out_id = await _record_tx(
            db, workspace_id=from_workspace, type_="transfer", direction="out",
            amount_zeni=amount_d, reason=reason,
            counterparty_ws=to_workspace, counterparty_addr=dst["eth_address"],
            settlement="offchain",
        )
        in_id = await _record_tx(
            db, workspace_id=to_workspace, type_="transfer", direction="in",
            amount_zeni=amount_d, reason=reason,
            counterparty_ws=from_workspace, counterparty_addr=src["eth_address"],
            settlement="offchain",
        )
        return {
            "settlement": "offchain",
            "from_workspace": from_workspace,
            "to_workspace": to_workspace,
            "amount_zeni": float(amount_d),
            "out_tx_id": out_id,
            "in_tx_id": in_id,
        }

    # ── External transfer: build unsigned tx for client to sign ──
    if not to_address:
        raise ValueError("Phải cung cấp to_workspace hoặc to_address")

    addr_dst = _checksum_address(to_address)
    from app.services.blockchain import build_transfer_tx
    tx = build_transfer_tx(
        chain=src["chain"] or DEFAULT_CHAIN, token=ZENI_TOKEN_ADDR,
        from_addr=src["eth_address"], to_addr=addr_dst, amount=float(amount_d),
    )

    # Record as pending on-chain (will flip to confirmed after tx_hash submitted)
    pending_id = await _record_tx(
        db, workspace_id=from_workspace, type_="transfer", direction="out",
        amount_zeni=amount_d, reason=reason,
        counterparty_addr=addr_dst, settlement="onchain",
        metadata={"unsigned_tx": tx},
    )
    return {
        "settlement": "onchain_pending",
        "from_workspace": from_workspace,
        "to_address": addr_dst,
        "amount_zeni": float(amount_d),
        "tx_id": pending_id,
        "unsigned_transaction": tx,
        "message_vi": "Hệ thống đã tạo tx chưa ký. Vui lòng ký bằng ví và gửi tx_hash về để xác nhận.",
    }


# ─── Pay subscription with $ZENI (with discount) ────────────────────────────


async def pay_with_token(
    db: AsyncSession,
    *,
    workspace_id: str,
    vnd_amount: int,
    intent_code: str | None = None,
    discount_percent: int = DEFAULT_DISCOUNT_PERCENT,
) -> dict[str, Any]:
    """Convert VND price → ZENI using current rate with a discount applied.

    discount_percent must be in [DISCOUNT_MIN_PERCENT, DISCOUNT_MAX_PERCENT].
    Deducts ZENI from wallet, marks the linked payment_intent (if any) as paid.
    """
    if vnd_amount <= 0:
        raise ValueError("Số tiền VND phải > 0")
    if not (DISCOUNT_MIN_PERCENT <= discount_percent <= DISCOUNT_MAX_PERCENT):
        raise ValueError(
            f"discount_percent phải nằm trong [{DISCOUNT_MIN_PERCENT}, {DISCOUNT_MAX_PERCENT}]"
        )

    wallet = await get_wallet_row(db, workspace_id)
    if not wallet:
        raise ValueError("Workspace chưa liên kết ví")

    rate = await get_exchange_rate(db)
    rate_vnd = rate["rate"]
    if rate_vnd <= 0:
        raise ValueError("Tỷ giá ZENI/VND không hợp lệ")

    discounted_vnd = vnd_amount * (100 - discount_percent) / 100.0
    zeni_required = Decimal(str(discounted_vnd / rate_vnd)).quantize(Decimal("0.00000001"))

    if Decimal(str(wallet["balance_zeni"] or 0)) < zeni_required:
        raise ValueError(
            f"Số dư không đủ: cần {zeni_required} ZENI, hiện có {wallet['balance_zeni']}"
        )

    tx_id = await _record_tx(
        db, workspace_id=workspace_id, type_="spend", direction="out",
        amount_zeni=zeni_required, reason="pay_with_token",
        intent_code=intent_code,
        metadata={
            "vnd_amount_original": vnd_amount,
            "vnd_amount_discounted": discounted_vnd,
            "discount_percent": discount_percent,
            "rate_zeni_to_vnd": rate_vnd,
        },
    )

    # If linked to a payment_intent, mark it paid (token settlement)
    if intent_code:
        await db.execute(text("""
            UPDATE payment_intents
               SET status = 'paid',
                   paid_at = NOW(),
                   paid_amount_vnd = :amt,
                   bank_tx_ref = CONCAT('zeni-token:', :tx)
             WHERE intent_code = :ic
               AND status = 'pending'
        """), {"amt": int(discounted_vnd), "tx": tx_id, "ic": intent_code})

    return {
        "transaction_id": tx_id,
        "workspace_id": workspace_id,
        "vnd_amount_original": vnd_amount,
        "vnd_amount_discounted": int(discounted_vnd),
        "zeni_paid": float(zeni_required),
        "discount_percent": discount_percent,
        "rate_zeni_vnd": rate_vnd,
        "intent_code": intent_code,
    }


# ─── Reward system ──────────────────────────────────────────────────────────


async def get_active_reward_rules(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(text("""
        SELECT id, action, amount_zeni, max_per_user, cooldown_seconds,
               description, active
          FROM token_reward_rules
         WHERE active = TRUE
         ORDER BY id ASC
    """))).mappings().all()
    return [dict(r) for r in rows]


async def list_eligible_rewards(
    db: AsyncSession, *, workspace_id: str
) -> list[dict[str, Any]]:
    """For each active rule, indicate whether the workspace can still claim it."""
    rules = await get_active_reward_rules(db)

    counts = {}
    last_at = {}
    rows = (await db.execute(text("""
        SELECT action, COUNT(*) AS n, MAX(claimed_at) AS last
          FROM token_reward_claims
         WHERE workspace_id = :w
         GROUP BY action
    """), {"w": workspace_id})).mappings().all()
    for r in rows:
        counts[r["action"]] = int(r["n"])
        last_at[r["action"]] = r["last"]

    out = []
    for rule in rules:
        used = counts.get(rule["action"], 0)
        cap = int(rule["max_per_user"] or 0)
        last = last_at.get(rule["action"])
        cooldown = int(rule["cooldown_seconds"] or 0)
        eligible = True
        reason = None
        if cap > 0 and used >= cap:
            eligible = False
            reason = "reached_max_per_user"
        elif cooldown > 0 and last is not None:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < cooldown:
                eligible = False
                reason = f"cooldown_active_{int(cooldown - elapsed)}s_remaining"
        out.append({
            **rule,
            "amount_zeni": float(rule["amount_zeni"]),
            "claims_used": used,
            "claims_remaining": (cap - used) if cap > 0 else None,
            "eligible": eligible,
            "ineligible_reason": reason,
        })
    return out


async def award_reward(
    db: AsyncSession, *, workspace_id: str, action: str
) -> dict[str, Any]:
    """Grant a reward if the workspace is eligible. Idempotent against caps."""
    rule = (await db.execute(text("""
        SELECT id, action, amount_zeni, max_per_user, cooldown_seconds, active
          FROM token_reward_rules
         WHERE action = :a
    """), {"a": action})).mappings().first()
    if rule is None:
        raise ValueError(f"Reward action '{action}' không tồn tại")
    if not rule["active"]:
        raise ValueError(f"Reward action '{action}' đang bị tắt")

    wallet = await get_wallet_row(db, workspace_id)
    if not wallet:
        raise ValueError("Workspace chưa liên kết ví")

    cap = int(rule["max_per_user"] or 0)
    if cap > 0:
        used = (await db.execute(text("""
            SELECT COUNT(*) FROM token_reward_claims
             WHERE workspace_id = :w AND action = :a
        """), {"w": workspace_id, "a": action})).scalar() or 0
        if int(used) >= cap:
            raise ValueError(f"Đã đạt giới hạn {cap} lần cho action '{action}'")

    cooldown = int(rule["cooldown_seconds"] or 0)
    if cooldown > 0:
        last = (await db.execute(text("""
            SELECT MAX(claimed_at) FROM token_reward_claims
             WHERE workspace_id = :w AND action = :a
        """), {"w": workspace_id, "a": action})).scalar()
        if last and (datetime.now(timezone.utc) - last).total_seconds() < cooldown:
            raise ValueError(f"Reward '{action}' đang trong cooldown")

    amount = Decimal(str(rule["amount_zeni"]))
    tx_id = await _record_tx(
        db, workspace_id=workspace_id, type_="reward", direction="in",
        amount_zeni=amount, reason=f"reward:{action}",
        metadata={"rule_id": int(rule["id"])},
    )
    claim_id = (await db.execute(text("""
        INSERT INTO token_reward_claims
            (workspace_id, rule_id, action, amount_zeni, transaction_id, metadata)
        VALUES (:w, :rid, :a, :amt, :tx, :m::jsonb)
        RETURNING id
    """), {
        "w": workspace_id, "rid": int(rule["id"]),
        "a": action, "amt": amount, "tx": tx_id,
        "m": _json({}),
    })).scalar()

    return {
        "claim_id": int(claim_id),
        "transaction_id": tx_id,
        "workspace_id": workspace_id,
        "action": action,
        "amount_zeni": float(amount),
    }


# ─── Soulbound badges (ZeniBadge SBT) ───────────────────────────────────────


async def mint_badge(
    db: AsyncSession, *, workspace_id: str, badge_type: str,
    eth_address: str | None = None, metadata_uri: str | None = None,
) -> dict[str, Any]:
    """Mint a soulbound badge to the workspace's linked wallet.

    Records the mint as 'pending' until the on-chain tx is confirmed by an
    authorized signer. The actual mint() call lives in a future cron / signer
    service that consumes ``status='pending'`` rows.
    """
    if badge_type not in VALID_BADGE_TYPES:
        raise ValueError(
            f"badge_type không hợp lệ. Hỗ trợ: {sorted(VALID_BADGE_TYPES)}"
        )

    if eth_address is None:
        wallet = await get_wallet_row(db, workspace_id)
        if not wallet:
            raise ValueError("Workspace chưa liên kết ví")
        addr = wallet["eth_address"]
    else:
        addr = _checksum_address(eth_address)

    existing = (await db.execute(text("""
        SELECT id, status FROM token_badges
         WHERE workspace_id = :w AND badge_type = :b
    """), {"w": workspace_id, "b": badge_type})).first()
    if existing:
        raise ValueError(
            f"Workspace đã có badge '{badge_type}' (status='{existing[1]}')"
        )

    new_id = (await db.execute(text("""
        INSERT INTO token_badges
            (workspace_id, badge_type, eth_address, metadata_uri, status, created_at)
        VALUES (:w, :b, :a, :u, 'pending', NOW())
        RETURNING id
    """), {"w": workspace_id, "b": badge_type, "a": addr, "u": metadata_uri})).scalar()

    return {
        "badge_id": int(new_id),
        "workspace_id": workspace_id,
        "badge_type": badge_type,
        "eth_address": addr,
        "status": "pending",
        "metadata_uri": metadata_uri,
        "contract_address": BADGE_SBT_ADDR,
        "message_vi": "Badge đã được đăng ký. Mint on-chain sẽ thực hiện bởi signer service.",
    }


async def list_badges(db: AsyncSession, *, workspace_id: str) -> list[dict[str, Any]]:
    rows = (await db.execute(text("""
        SELECT id, badge_type, eth_address, token_id, tx_hash, metadata_uri,
               status, minted_at, created_at
          FROM token_badges
         WHERE workspace_id = :w
         ORDER BY created_at DESC
    """), {"w": workspace_id})).mappings().all()
    return [
        {
            **dict(r),
            "minted_at": r["minted_at"].isoformat() if r["minted_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ─── Burn ───────────────────────────────────────────────────────────────────


async def record_burn(
    db: AsyncSession, *, workspace_id: str, amount: Decimal | float, reason: str
) -> dict[str, Any]:
    """Record a voluntary burn (debit ledger). On-chain burn() call by signer."""
    amount_d = Decimal(str(amount))
    if amount_d <= 0:
        raise ValueError("amount phải > 0")
    if not reason or len(reason) < 3:
        raise ValueError("reason cần >= 3 ký tự")

    wallet = await get_wallet_row(db, workspace_id)
    if not wallet:
        raise ValueError("Workspace chưa liên kết ví")
    if Decimal(str(wallet["balance_zeni"] or 0)) < amount_d:
        raise ValueError("Số dư không đủ để burn")

    tx_id = await _record_tx(
        db, workspace_id=workspace_id, type_="burn", direction="out",
        amount_zeni=amount_d, reason=f"burn:{reason}",
        metadata={"reason": reason},
    )
    burn_id = (await db.execute(text("""
        INSERT INTO token_burn_records
            (workspace_id, amount_zeni, reason, status, metadata)
        VALUES (:w, :amt, :r, 'pending', :m::jsonb)
        RETURNING id
    """), {
        "w": workspace_id, "amt": amount_d, "r": reason,
        "m": _json({"transaction_id": tx_id}),
    })).scalar()

    return {
        "burn_id": int(burn_id),
        "transaction_id": tx_id,
        "amount_zeni": float(amount_d),
        "reason": reason,
        "status": "pending",
    }


# ─── Exchange rate ──────────────────────────────────────────────────────────


async def get_exchange_rate(db: AsyncSession) -> dict[str, Any]:
    """Latest ZENI/VND rate. Falls back to default if oracle empty."""
    row = (await db.execute(text("""
        SELECT rate, rate_usd, source, captured_at
          FROM token_exchange_rates
         WHERE base_currency = 'ZENI' AND quote_currency = 'VND'
         ORDER BY captured_at DESC
         LIMIT 1
    """))).mappings().first()
    if row is None:
        return {"rate": 25000.0, "rate_usd": 1.0, "source": "default", "captured_at": None}
    return {
        "rate": float(row["rate"]),
        "rate_usd": float(row["rate_usd"] or 0),
        "source": row["source"],
        "captured_at": row["captured_at"].isoformat() if row["captured_at"] else None,
    }


async def update_exchange_rate(
    db: AsyncSession, *, rate_vnd: float, rate_usd: float | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    if rate_vnd <= 0:
        raise ValueError("rate_vnd phải > 0")
    new_id = (await db.execute(text("""
        INSERT INTO token_exchange_rates (base_currency, quote_currency, rate, rate_usd, source)
        VALUES ('ZENI', 'VND', :r, :u, :s)
        RETURNING id
    """), {"r": Decimal(str(rate_vnd)), "u": Decimal(str(rate_usd)) if rate_usd else None, "s": source})).scalar()
    return {"id": int(new_id), "rate": rate_vnd, "rate_usd": rate_usd, "source": source}


# ─── Internal helpers ───────────────────────────────────────────────────────


def _json(obj: Any) -> str:
    import json
    def _default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)
