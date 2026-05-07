"""
Zeni Cloud Core — L6 Web3 API (REAL Polygon RPC reads + templated writes).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import Contract
from app.schemas.resources import ContractOut, Web3ExecIn
from app.services.audit import audit_push, billing_push
from app.services.blockchain import (
    BLOCK_TIME_MS,
    EXPLORERS,
    RPC_ENDPOINTS,
    ZENI_CONTRACTS,
    build_transfer_tx,
    get_chain_status,
    get_native_balance,
    get_tx_receipt,
    read_erc20,
    read_erc721,
)

log = logging.getLogger("zeni.api.web3")
router = APIRouter(prefix="/web3", tags=["web3"])


class ReadTokenIn(BaseModel):
    chain: str = Field(default="polygon", pattern=r"^(polygon|polygon_amoy|base|arbitrum)$")
    address: str = Field(min_length=42, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$")
    owner: str | None = Field(default=None, pattern=r"^0x[a-fA-F0-9]{40}$")
    kind: str = Field(default="erc20", pattern=r"^(erc20|erc721|native)$")


# ─── Read-only routes (REAL via RPC) ──────────────
@router.get("/chains")
async def list_chains() -> dict:
    """List supported chains + live status (block number, gas)."""
    return {
        "supported": list(RPC_ENDPOINTS.keys()) + ["zeni_chain"],
        "status": [get_chain_status(c) for c in RPC_ENDPOINTS.keys()],
        "zeni_native_contracts": ZENI_CONTRACTS,
    }


@router.get("/zeni-stack")
async def zeni_stack_status() -> dict:
    """Live read of all 3 Zeni contracts on Polygon Mainnet."""
    chain = "polygon"
    return {
        "chain_status": get_chain_status(chain),
        "ZENI_TOKEN": read_erc20(chain, ZENI_CONTRACTS["ZENI_TOKEN"], owner=ZENI_CONTRACTS["DEPLOYER"]),
        "AFFILIATE": {"address": ZENI_CONTRACTS["AFFILIATE"], "note": "AffiliateCommission contract"},
        "BADGE_SBT": read_erc721(chain, ZENI_CONTRACTS["BADGE_SBT"], owner=ZENI_CONTRACTS["DEPLOYER"]),
        "DEPLOYER_BALANCE": get_native_balance(chain, ZENI_CONTRACTS["DEPLOYER"]),
    }


@router.post("/read")
async def read_contract(
    data: ReadTokenIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """REAL read of any ERC20 / ERC721 / native balance."""
    if data.kind == "erc20":
        return read_erc20(data.chain, data.address, owner=data.owner)
    elif data.kind == "erc721":
        return read_erc721(data.chain, data.address, owner=data.owner)
    elif data.kind == "native":
        return get_native_balance(data.chain, data.address)
    else:
        raise HTTPException(status_code=400, detail="kind không hợp lệ")


@router.get("/tx/{chain}/{tx_hash}")
async def lookup_tx(chain: str, tx_hash: str) -> dict:
    """Look up real transaction receipt on chain."""
    if chain not in RPC_ENDPOINTS:
        raise HTTPException(status_code=400, detail=f"chain {chain} không support")
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise HTTPException(status_code=400, detail="tx_hash invalid format")
    return get_tx_receipt(chain, tx_hash)


# ─── Workspace contracts (DB-backed, real list) ──
@router.get("/contracts", response_model=list[ContractOut])
async def list_contracts(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ContractOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Contract).where(Contract.workspace_id == ws).order_by(Contract.created_at.desc())
    )).scalars().all()
    return [ContractOut.model_validate(r) for r in rows]


# ─── Write operations (templated, no key custody) ──
@router.post("/build-transfer")
async def build_transfer(
    ws: str,
    chain: str = Query(default="polygon", pattern=r"^(polygon|polygon_amoy|base|arbitrum)$"),
    token: str = Query(min_length=42, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$"),
    from_addr: str = Query(alias="from", min_length=42, max_length=42),
    to_addr: str = Query(alias="to", min_length=42, max_length=42),
    amount: float = Query(gt=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Build unsigned ERC20 transfer transaction. User signs locally + submits.
    REAL gas estimate + nonce from chain."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được build transaction")
    try:
        tx = build_transfer_tx(chain, token, from_addr, to_addr, amount)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"build tx failed: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="web3.build_transfer",
        target=f"{from_addr[:10]}->{to_addr[:10]}", severity="info",
        metadata={"chain": chain, "amount": amount, "token": token},
    )
    await billing_push(db, workspace_id=ws, layer="L6", action="web3.build_tx", cost_usd=0.000001)
    await db.commit()
    return tx


@router.post("/execute")
async def execute_web3(
    ws: str,
    data: Web3ExecIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Execute a Web3 action.

    Read actions (`read.*`) use real RPC.
    Write actions return a TEMPLATED transaction object — caller must sign +
    submit via their own wallet (Privy / WalletConnect / MetaMask). We do NOT
    custody private keys server-side.
    """
    await require_workspace_access(ws, me)
    if me.role == "Viewer" and not data.action.startswith("read"):
        raise HTTPException(status_code=403, detail="Viewer chỉ được read")

    if data.chain not in RPC_ENDPOINTS:
        raise HTTPException(status_code=400, detail=f"chain {data.chain} chưa hỗ trợ")

    chain_status = get_chain_status(data.chain)
    explorer = EXPLORERS.get(data.chain, "")
    note = "Templated transaction — user phải sign + submit qua wallet (Privy / MetaMask). Backend không custody private key."

    result: dict = {
        "action": data.action,
        "chain": data.chain,
        "wallet_mode": data.wallet.upper(),
        "block_time_ms": BLOCK_TIME_MS.get(data.chain),
        "current_block": chain_status.get("block_number"),
        "gas_price_gwei": chain_status.get("gas_price_gwei"),
        "explorer_base": explorer,
        "note": note,
    }

    # Real read action
    if data.action == "read":
        addr = data.params.get("address")
        kind = data.params.get("kind", "erc20")
        owner = data.params.get("owner")
        if not addr:
            raise HTTPException(status_code=400, detail="params.address required")
        if kind == "erc20":
            result["data"] = read_erc20(data.chain, addr, owner=owner)
        elif kind == "erc721":
            result["data"] = read_erc721(data.chain, addr, owner=owner)
        elif kind == "native":
            result["data"] = get_native_balance(data.chain, addr)
    elif data.action == "transfer":
        # Build templated transfer
        token = data.params.get("token") or ZENI_CONTRACTS["ZENI_TOKEN"]
        from_addr = data.params.get("from") or ZENI_CONTRACTS["DEPLOYER"]
        to_addr = data.params.get("to")
        amount = data.params.get("amount")
        if not to_addr or amount is None:
            raise HTTPException(status_code=400, detail="params.to và params.amount required")
        try:
            result["templated_tx"] = build_transfer_tx(data.chain, token, from_addr, to_addr, float(amount))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"build tx failed: {e}")
    else:
        # Generic templated deploy / mint / etc.
        result["templated_tx"] = {
            "to": None,  # contract creation
            "data": data.params.get("bytecode", "<bytecode required>"),
            "gas_estimate": 1_500_000,
            "gas_price_gwei": chain_status.get("gas_price_gwei"),
        }

    await audit_push(
        db, actor=me.email, workspace_id=ws, action=f"web3.{data.action}",
        target=data.action, severity="ok",
        metadata={"chain": data.chain, "wallet": data.wallet, "params_keys": list(data.params.keys())},
    )
    await billing_push(db, workspace_id=ws, layer="L6", action=f"web3.{data.action}", cost_usd=0.000005)
    await db.commit()
    return result
