"""
Zeni Cloud Core — L6 Web3 service: real Polygon RPC + ERC20/ERC721 reads.

Read-only operations are real (via public Polygon RPC).
Write operations are templated — return a transaction object that the caller
can sign + submit via their own wallet. We do NOT custody private keys here
(that's for a future custodial-wallet milestone with KMS-backed signing).

Pre-deployed contracts (Polygon Mainnet):
  $ZENI Token:        0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1
  AffiliateCommission:0x1d5963FcCfC548275293e51f0F6C7aC482E0b714
  ZeniBadge SBT:      0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D
  Deployer:           0x76ABe9d6252e1e151c039F66de19DEa5d8E7CE91
"""
from __future__ import annotations

import logging
from typing import Any
from functools import lru_cache

from web3 import Web3
from web3.exceptions import ContractLogicError, BadFunctionCallOutput

log = logging.getLogger("zeni.blockchain")


# ─── RPC endpoints (public, no API key) ─────────────────
RPC_ENDPOINTS = {
    "polygon":      "https://polygon.publicnode.com",  # publicnode is more stable than polygon-rpc.com
    "polygon_amoy": "https://rpc-amoy.polygon.technology",
    "base":         "https://mainnet.base.org",
    "arbitrum":     "https://arb1.arbitrum.io/rpc",
    # Zeni Chain not yet GA
}

# Fallback RPCs (try in order if primary fails)
RPC_FALLBACKS = {
    "polygon": [
        "https://polygon.publicnode.com",
        "https://rpc.ankr.com/polygon",
        "https://polygon.llamarpc.com",
        "https://polygon-rpc.com",
    ],
}

EXPLORERS = {
    "polygon":      "https://polygonscan.com",
    "polygon_amoy": "https://amoy.polygonscan.com",
    "base":         "https://basescan.org",
    "arbitrum":     "https://arbiscan.io",
    "zeni_chain":   "https://explorer.zenichain.io",
}

# Block times (ms) for cost estimation
BLOCK_TIME_MS = {
    "polygon": 2100, "polygon_amoy": 2100,
    "base": 2000, "arbitrum": 250, "zeni_chain": 400,
}

# ─── Pre-deployed Zeni contracts on Polygon Mainnet ─────
ZENI_CONTRACTS = {
    "ZENI_TOKEN":    "0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1",
    "AFFILIATE":     "0x1d5963FcCfC548275293e51f0F6C7aC482E0b714",
    "BADGE_SBT":     "0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D",
    "DEPLOYER":      "0x76ABe9d6252e1e151c039F66de19DEa5d8E7CE91",
}

# ─── Standard ABIs ───────────────────────────────────────
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "name",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "totalSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "transfer",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

ERC721_ABI = [
    {"constant": True, "inputs": [], "name": "name",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "totalSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


_w3_cache: dict[str, Web3] = {}


def _w3(chain: str) -> Web3:
    """Get Web3 client with fallback to alternate RPCs if primary fails."""
    if chain in _w3_cache:
        cached = _w3_cache[chain]
        try:
            if cached.is_connected():
                return cached
        except Exception:
            pass
    rpcs = RPC_FALLBACKS.get(chain) or [RPC_ENDPOINTS.get(chain)]
    rpcs = [r for r in rpcs if r]
    if not rpcs:
        raise ValueError(f"Chain '{chain}' không support / chưa cấu hình RPC")
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                _w3_cache[chain] = w3
                log.info("Web3 connected to %s for %s", rpc, chain)
                return w3
        except Exception as e:
            log.warning("RPC %s failed for %s: %s", rpc, chain, e)
    raise ConnectionError(f"All RPCs failed for chain {chain}")


def get_chain_status(chain: str) -> dict[str, Any]:
    """Live RPC connectivity + block info."""
    try:
        w3 = _w3(chain)
        connected = w3.is_connected()
        info: dict[str, Any] = {"chain": chain, "rpc": RPC_ENDPOINTS.get(chain), "connected": connected}
        if connected:
            block = w3.eth.block_number
            gas_price_wei = w3.eth.gas_price
            info.update({
                "block_number": block,
                "gas_price_gwei": round(gas_price_wei / 1e9, 4),
                "chain_id": w3.eth.chain_id,
                "block_time_ms": BLOCK_TIME_MS.get(chain),
                "explorer": EXPLORERS.get(chain),
            })
        return info
    except Exception as e:
        return {"chain": chain, "connected": False, "error": str(e)}


def read_erc20(chain: str, address: str, owner: str | None = None) -> dict[str, Any]:
    """Read ERC20 token state from chain. Returns name/symbol/decimals/totalSupply (+ balance if owner given)."""
    w3 = _w3(chain)
    addr = Web3.to_checksum_address(address)
    contract = w3.eth.contract(address=addr, abi=ERC20_ABI)

    out: dict[str, Any] = {"chain": chain, "address": addr}
    try:
        out["name"] = contract.functions.name().call()
        out["symbol"] = contract.functions.symbol().call()
        out["decimals"] = contract.functions.decimals().call()
        ts = contract.functions.totalSupply().call()
        out["total_supply_raw"] = str(ts)
        out["total_supply"] = ts / (10 ** out["decimals"])
        if owner:
            owner_addr = Web3.to_checksum_address(owner)
            bal = contract.functions.balanceOf(owner_addr).call()
            out["balance_of_raw"] = str(bal)
            out["balance_of"] = bal / (10 ** out["decimals"])
            out["owner"] = owner_addr
    except (ContractLogicError, BadFunctionCallOutput) as e:
        out["error"] = f"contract_read_failed: {e}"
    return out


def read_erc721(chain: str, address: str, owner: str | None = None) -> dict[str, Any]:
    """Read ERC721/SBT collection state."""
    w3 = _w3(chain)
    addr = Web3.to_checksum_address(address)
    contract = w3.eth.contract(address=addr, abi=ERC721_ABI)

    out: dict[str, Any] = {"chain": chain, "address": addr}
    try:
        out["name"] = contract.functions.name().call()
        out["symbol"] = contract.functions.symbol().call()
        try:
            out["total_supply"] = contract.functions.totalSupply().call()
        except Exception:
            out["total_supply"] = None  # not all ERC721 implement totalSupply
        if owner:
            owner_addr = Web3.to_checksum_address(owner)
            out["balance_of"] = contract.functions.balanceOf(owner_addr).call()
            out["owner"] = owner_addr
    except (ContractLogicError, BadFunctionCallOutput) as e:
        out["error"] = f"contract_read_failed: {e}"
    return out


def get_native_balance(chain: str, address: str) -> dict[str, Any]:
    """Get native token (MATIC/ETH) balance of an address."""
    w3 = _w3(chain)
    addr = Web3.to_checksum_address(address)
    bal_wei = w3.eth.get_balance(addr)
    return {
        "chain": chain, "address": addr,
        "balance_wei": str(bal_wei),
        "balance": bal_wei / 1e18,
        "symbol": "MATIC" if chain.startswith("polygon") else "ETH",
    }


def build_transfer_tx(chain: str, token: str, from_addr: str, to_addr: str,
                      amount: float) -> dict[str, Any]:
    """Build (but do NOT sign) ERC20 transfer transaction. Caller signs offline."""
    w3 = _w3(chain)
    token_addr = Web3.to_checksum_address(token)
    contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    decimals = contract.functions.decimals().call()
    amount_raw = int(amount * (10 ** decimals))

    fn = contract.functions.transfer(Web3.to_checksum_address(to_addr), amount_raw)

    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(from_addr))
    gas_price = w3.eth.gas_price
    try:
        gas_estimate = fn.estimate_gas({"from": Web3.to_checksum_address(from_addr)})
    except Exception:
        gas_estimate = 100_000

    tx = fn.build_transaction({
        "from": Web3.to_checksum_address(from_addr),
        "nonce": nonce,
        "gas": int(gas_estimate * 1.2),
        "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    })
    return {
        "transaction": {k: (str(v) if isinstance(v, (int, bytes)) else v) for k, v in tx.items()},
        "amount_raw": str(amount_raw),
        "decimals": decimals,
        "estimated_gas_price_gwei": round(gas_price / 1e9, 4),
        "estimated_cost_native": (gas_estimate * gas_price) / 1e18,
    }


def get_tx_receipt(chain: str, tx_hash: str) -> dict[str, Any]:
    """Look up real transaction receipt on chain."""
    w3 = _w3(chain)
    try:
        rcpt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        return {"chain": chain, "tx_hash": tx_hash, "found": False, "error": str(e)}
    return {
        "chain": chain,
        "tx_hash": tx_hash,
        "found": True,
        "status": rcpt.status,
        "block_number": rcpt.blockNumber,
        "from": rcpt["from"],
        "to": rcpt.to,
        "gas_used": rcpt.gasUsed,
        "explorer_url": f"{EXPLORERS.get(chain, '')}/tx/{tx_hash}",
    }
