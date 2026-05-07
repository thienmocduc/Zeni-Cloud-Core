"""
Zeni Cloud Core — Outgoing Payouts API (P2#11 ClawWits).

Unified outgoing payment endpoint cho:
  - bank          : VND transfer (queued chờ chairman đấu bank API)
  - zeni_token    : $ZENI Polygon transfer (wraps services/zeni_token.transfer_token)
  - usdt          : USDT-Polygon transfer (Phase 2)
  - stripe/paypal : opt-in tier (Phase 3)

Endpoints (prefix /payouts):
  POST   /                   — Create payout (queued or instant for zeni_token)
  GET    /                   — List recent payouts
  GET    /{payout_id}        — Get payout detail
  POST   /{payout_id}/approve — Approve high-value payout
  POST   /{payout_id}/cancel  — Cancel pending payout
  GET    /settings           — Get per-workspace payout settings
  PUT    /settings           — Update settings (auto-approval threshold, methods, limits)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db, SessionLocal

log = logging.getLogger("zeni.payouts")

router = APIRouter(prefix="/payouts", tags=["payouts"])

ALLOWED_METHODS = {"bank", "zeni_token", "usdt", "stripe", "paypal"}
ALLOWED_PURPOSES = {"maker_commission", "refund", "salary", "affiliate", "other"}


# ===== Schemas =====

class PayoutCreate(BaseModel):
    method: str = Field(..., description="bank | zeni_token | usdt | stripe | paypal")
    # Recipient
    recipient_id: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_email: Optional[str] = None
    # Amount (one of these required based on method)
    amount_vnd: Optional[int] = Field(None, ge=10000, description="VND amount for bank/stripe/paypal")
    amount_zeni: Optional[Decimal] = Field(None, gt=0, description="$ZENI amount for zeni_token")
    amount_usdt: Optional[Decimal] = Field(None, gt=0, description="USDT amount")
    # Method-specific
    bank_code: Optional[str] = Field(None, description="VCB | TPB | MBB | BIDV | ACB | TCB")
    bank_account_number: Optional[str] = None
    bank_account_name: Optional[str] = None
    recipient_wallet_address: Optional[str] = Field(None, description="0x... for crypto methods")
    stripe_account_id: Optional[str] = None
    # Meta
    purpose: str = Field("other", description="maker_commission | refund | salary | affiliate | other")
    reference: Optional[str] = Field(None, max_length=120)
    notes: Optional[str] = None

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        if v not in ALLOWED_METHODS:
            raise ValueError(f"method must be one of {ALLOWED_METHODS}")
        return v


class PayoutOut(BaseModel):
    id: str
    workspace_id: str
    method: str
    status: str
    amount_vnd: Optional[int] = None
    amount_zeni: Optional[float] = None
    amount_usdt: Optional[float] = None
    recipient_name: Optional[str] = None
    recipient_id: Optional[str] = None
    bank_code: Optional[str] = None
    bank_account_number: Optional[str] = None
    recipient_wallet_address: Optional[str] = None
    purpose: Optional[str] = None
    reference: Optional[str] = None
    requires_approval: bool = False
    approved_at: Optional[str] = None
    zeni_token_tx_hash: Optional[str] = None
    bank_provider_ref: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    processed_at: Optional[str] = None
    completed_at: Optional[str] = None


class SettingsIn(BaseModel):
    auto_approval_threshold_vnd: Optional[int] = Field(None, ge=0)
    enabled_methods: Optional[list[str]] = None
    daily_limit_vnd: Optional[int] = Field(None, ge=0)
    monthly_limit_vnd: Optional[int] = Field(None, ge=0)
    default_bank_code: Optional[str] = None
    notify_email: Optional[str] = None


# ===== Endpoints =====

@router.post("/", response_model=PayoutOut, status_code=202)
async def create_payout(
    data: PayoutCreate,
    bg: BackgroundTasks,
    ws: str = Query(..., description="workspace_id"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Create payout request. zeni_token = instant on-chain. bank = queued for chairman API.

    Examples:
      # Maker commission via $ZENI (instant)
      { "method": "zeni_token", "recipient_wallet_address": "0xABC...",
        "amount_zeni": 1000, "purpose": "maker_commission", "reference": "WL-2026-001" }

      # Salary via bank transfer (queued)
      { "method": "bank", "amount_vnd": 5000000,
        "bank_code": "VCB", "bank_account_number": "1234567890",
        "bank_account_name": "Nguyen Van A", "purpose": "salary" }
    """
    await require_workspace_access(ws, me)

    # Validate method-specific fields
    if data.method == "zeni_token":
        if not data.amount_zeni or not data.recipient_wallet_address:
            raise HTTPException(422, "zeni_token method requires amount_zeni + recipient_wallet_address")
    elif data.method == "usdt":
        if not data.amount_usdt or not data.recipient_wallet_address:
            raise HTTPException(422, "usdt method requires amount_usdt + recipient_wallet_address")
    elif data.method == "bank":
        if not (data.amount_vnd and data.bank_code and data.bank_account_number and data.bank_account_name):
            raise HTTPException(422, "bank method requires amount_vnd, bank_code, bank_account_number, bank_account_name")
    elif data.method == "stripe":
        if not (data.amount_vnd and data.stripe_account_id):
            raise HTTPException(422, "stripe method requires amount_vnd + stripe_account_id")

    # Get settings + check approval threshold
    settings = (await db.execute(text(
        "SELECT auto_approval_threshold_vnd, enabled_methods FROM payout_settings WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    threshold = (settings["auto_approval_threshold_vnd"] if settings else 10_000_000)
    enabled = settings["enabled_methods"] if settings and settings["enabled_methods"] else ["bank", "zeni_token"]
    if isinstance(enabled, str):
        enabled = json.loads(enabled)
    if data.method not in enabled:
        raise HTTPException(403, f"Method '{data.method}' not enabled for this workspace. Enabled: {enabled}")

    # Determine if requires approval
    requires_approval = False
    if data.amount_vnd and data.amount_vnd > threshold:
        requires_approval = True

    payout_id = uuid.uuid4()
    initial_status = "pending" if requires_approval else "approved"

    await db.execute(text(
        "INSERT INTO payouts (id, workspace_id, user_id, recipient_id, recipient_name, recipient_email, "
        "method, amount_vnd, amount_zeni, amount_usdt, bank_code, bank_account_number, bank_account_name, "
        "recipient_wallet_address, stripe_account_id, status, purpose, reference, notes, requires_approval) "
        "VALUES (:id, :ws, :uid, :rid, :rn, :re, :m, :avnd, :azeni, :ausdt, :bc, :ban, :bn, "
        ":rwa, :sai, :st, :pu, :ref, :nt, :ra)"
    ), {
        "id": str(payout_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "rid": data.recipient_id,
        "rn": data.recipient_name,
        "re": data.recipient_email,
        "m": data.method,
        "avnd": data.amount_vnd,
        "azeni": float(data.amount_zeni) if data.amount_zeni else None,
        "ausdt": float(data.amount_usdt) if data.amount_usdt else None,
        "bc": data.bank_code,
        "ban": data.bank_account_number,
        "bn": data.bank_account_name,
        "rwa": data.recipient_wallet_address,
        "sai": data.stripe_account_id,
        "st": initial_status,
        "pu": data.purpose,
        "ref": data.reference,
        "nt": data.notes,
        "ra": requires_approval,
    })
    await db.commit()

    # If auto-approved, schedule processing
    if not requires_approval:
        bg.add_task(_process_payout, str(payout_id))

    return await _fetch_payout(db, str(payout_id), ws)


@router.get("/", response_model=list[PayoutOut])
async def list_payouts(
    ws: str = Query(...),
    status: Optional[str] = Query(None),
    method: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    sql = "SELECT * FROM payouts WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws}
    if status:
        sql += " AND status = :st"
        params["st"] = status
    if method:
        sql += " AND method = :m"
        params["m"] = method
    sql += " ORDER BY created_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_payout(r) for r in rows]


@router.get("/{payout_id}", response_model=PayoutOut)
async def get_payout(
    payout_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    return await _fetch_payout(db, payout_id, ws)


@router.post("/{payout_id}/approve", response_model=PayoutOut)
async def approve_payout(
    payout_id: str,
    bg: BackgroundTasks,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    if me.role not in ("Admin", "Owner"):
        raise HTTPException(403, "Only Admin/Owner can approve payouts")
    r = (await db.execute(text(
        "UPDATE payouts SET status = 'approved', approved_by = :by, approved_at = NOW() "
        "WHERE id = :id AND workspace_id = :ws AND status = 'pending' RETURNING id"
    ), {"by": str(me.id), "id": payout_id, "ws": ws})).first()
    await db.commit()
    if not r:
        raise HTTPException(404, "Payout not found or not in pending state")
    bg.add_task(_process_payout, payout_id)
    return await _fetch_payout(db, payout_id, ws)


@router.post("/{payout_id}/cancel", response_model=PayoutOut)
async def cancel_payout(
    payout_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "UPDATE payouts SET status = 'cancelled' WHERE id = :id AND workspace_id = :ws "
        "AND status IN ('pending','approved') RETURNING id"
    ), {"id": payout_id, "ws": ws})).first()
    await db.commit()
    if not r:
        raise HTTPException(404, "Payout not found or already processed")
    return await _fetch_payout(db, payout_id, ws)


@router.get("/settings/get")
async def get_settings(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT auto_approval_threshold_vnd, enabled_methods, daily_limit_vnd, monthly_limit_vnd, "
        "default_bank_code, notify_email, updated_at FROM payout_settings WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not r:
        return {
            "workspace_id": ws,
            "auto_approval_threshold_vnd": 10_000_000,
            "enabled_methods": ["bank", "zeni_token"],
            "daily_limit_vnd": 100_000_000,
            "monthly_limit_vnd": 1_000_000_000,
            "default_bank_code": None,
            "notify_email": None,
        }
    enabled = r["enabled_methods"] if isinstance(r["enabled_methods"], list) else json.loads(r["enabled_methods"] or "[]")
    return {
        "workspace_id": ws,
        "auto_approval_threshold_vnd": r["auto_approval_threshold_vnd"],
        "enabled_methods": enabled,
        "daily_limit_vnd": r["daily_limit_vnd"],
        "monthly_limit_vnd": r["monthly_limit_vnd"],
        "default_bank_code": r["default_bank_code"],
        "notify_email": r["notify_email"],
    }


@router.put("/settings/update")
async def update_settings(
    data: SettingsIn,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    if me.role not in ("Admin", "Owner"):
        raise HTTPException(403, "Only Admin/Owner can update payout settings")
    # Upsert
    await db.execute(text(
        "INSERT INTO payout_settings (workspace_id, auto_approval_threshold_vnd, enabled_methods, "
        "daily_limit_vnd, monthly_limit_vnd, default_bank_code, notify_email) "
        "VALUES (:ws, :tr, CAST(:em AS jsonb), :dl, :ml, :bc, :ne) "
        "ON CONFLICT (workspace_id) DO UPDATE SET "
        "auto_approval_threshold_vnd = COALESCE(EXCLUDED.auto_approval_threshold_vnd, payout_settings.auto_approval_threshold_vnd), "
        "enabled_methods = COALESCE(EXCLUDED.enabled_methods, payout_settings.enabled_methods), "
        "daily_limit_vnd = COALESCE(EXCLUDED.daily_limit_vnd, payout_settings.daily_limit_vnd), "
        "monthly_limit_vnd = COALESCE(EXCLUDED.monthly_limit_vnd, payout_settings.monthly_limit_vnd), "
        "default_bank_code = COALESCE(EXCLUDED.default_bank_code, payout_settings.default_bank_code), "
        "notify_email = COALESCE(EXCLUDED.notify_email, payout_settings.notify_email), "
        "updated_at = NOW()"
    ), {
        "ws": ws,
        "tr": data.auto_approval_threshold_vnd,
        "em": json.dumps(data.enabled_methods) if data.enabled_methods else None,
        "dl": data.daily_limit_vnd,
        "ml": data.monthly_limit_vnd,
        "bc": data.default_bank_code,
        "ne": data.notify_email,
    })
    await db.commit()
    return {"status": "updated"}


# ===== Helpers =====

async def _fetch_payout(db: AsyncSession, payout_id: str, ws: str) -> PayoutOut:
    r = (await db.execute(text(
        "SELECT * FROM payouts WHERE id = :id AND workspace_id = :ws"
    ), {"id": payout_id, "ws": ws})).mappings().first()
    if not r:
        raise HTTPException(404, "Payout not found")
    return _row_to_payout(r)


def _row_to_payout(r) -> PayoutOut:
    return PayoutOut(
        id=str(r["id"]),
        workspace_id=r["workspace_id"],
        method=r["method"],
        status=r["status"],
        amount_vnd=r["amount_vnd"],
        amount_zeni=float(r["amount_zeni"]) if r["amount_zeni"] is not None else None,
        amount_usdt=float(r["amount_usdt"]) if r["amount_usdt"] is not None else None,
        recipient_name=r["recipient_name"],
        recipient_id=r["recipient_id"],
        bank_code=r["bank_code"],
        bank_account_number=r["bank_account_number"],
        recipient_wallet_address=r["recipient_wallet_address"],
        purpose=r["purpose"],
        reference=r["reference"],
        requires_approval=r["requires_approval"],
        approved_at=r["approved_at"].isoformat() if r["approved_at"] else None,
        zeni_token_tx_hash=r["zeni_token_tx_hash"],
        bank_provider_ref=r["bank_provider_ref"],
        error_message=r["error_message"],
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
        processed_at=r["processed_at"].isoformat() if r["processed_at"] else None,
        completed_at=r["completed_at"].isoformat() if r["completed_at"] else None,
    )


# ===== Worker =====

async def _process_payout(payout_id: str) -> None:
    """Background processor: dispatch payout based on method."""
    async with SessionLocal() as db:
        row = (await db.execute(text(
            "SELECT * FROM payouts WHERE id = :id AND status = 'approved'"
        ), {"id": payout_id})).mappings().first()
        if not row:
            log.warning("Payout %s not found or not approved", payout_id)
            return

        await db.execute(text(
            "UPDATE payouts SET status = 'processing', processed_at = NOW() WHERE id = :id"
        ), {"id": payout_id})
        await db.commit()

        method = row["method"]
        try:
            if method == "zeni_token":
                # Wrap into existing zeni_token.transfer_token service
                from decimal import Decimal as _D
                from app.services.zeni_token import transfer_token
                tx = await transfer_token(
                    db,
                    from_workspace=row["workspace_id"],
                    to_workspace=None,
                    to_address=row["recipient_wallet_address"],
                    amount=_D(str(row["amount_zeni"])),
                    reason=f"payout/{row['reference'] or payout_id[:8]}",
                )
                tx_hash = tx.get("tx_hash") if isinstance(tx, dict) else None
                # External transfer returns unsigned tx — for true on-chain dispatch,
                # backend signer service will sign + submit. For now, mark queued.
                final_status = "success" if tx_hash else "processing"
                await db.execute(text(
                    "UPDATE payouts SET status = :st, completed_at = NOW(), zeni_token_tx_hash = :h WHERE id = :id"
                ), {"st": final_status, "h": tx_hash or "pending_sign", "id": payout_id})
                await db.commit()
                log.info("Payout %s → $ZENI %s tx_hash=%s", payout_id, final_status, tx_hash)

            elif method == "bank":
                # Bank API not yet wired — chairman will plug in (VCB/TPB/MBB)
                # For now: mark as 'processing' indefinitely with notes
                await db.execute(text(
                    "UPDATE payouts SET status = 'processing', "
                    "notes = COALESCE(notes,'') || ' | bank_api_pending' WHERE id = :id"
                ), {"id": payout_id})
                await db.commit()
                log.info("Payout %s → bank queued (waiting for chairman bank API integration)", payout_id)

            elif method == "usdt":
                # Phase 2: USDT-Polygon contract transfer (need USDT contract address from chairman)
                await db.execute(text(
                    "UPDATE payouts SET status = 'processing', "
                    "notes = COALESCE(notes,'') || ' | usdt_phase2_pending' WHERE id = :id"
                ), {"id": payout_id})
                await db.commit()
                log.info("Payout %s → USDT queued (Phase 2 wiring pending)", payout_id)

            else:
                # stripe / paypal — opt-in for ClawWits clients (not Zeni Holdings core)
                await db.execute(text(
                    "UPDATE payouts SET status = 'failed', error_message = :err, completed_at = NOW() WHERE id = :id"
                ), {"err": f"Method '{method}' requires opt-in vendor connector (not enabled by default)", "id": payout_id})
                await db.commit()

        except Exception as e:
            log.exception("Payout %s failed: %s", payout_id, e)
            async with SessionLocal() as db2:
                await db2.execute(text(
                    "UPDATE payouts SET status = 'failed', error_message = :err, completed_at = NOW() WHERE id = :id"
                ), {"err": str(e)[:500], "id": payout_id})
                await db2.commit()
