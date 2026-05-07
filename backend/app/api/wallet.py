"""
Zeni Cloud Core — Wallet API (Zeni Pay Cấp 2).

Endpoints (prefix ``/wallet``):

  Member-scoped
  --------------
    GET  /balance?ws=                   — current balance (available/locked/total)
    GET  /transactions?ws=&type=...     — history
    POST /topup?ws=                     — create VietQR intent + return QR
    POST /transfer?ws=                  — internal P2P
    GET  /recurring?ws=                 — list recurring charges
    POST /recurring?ws=                 — create recurring charge
    POST /recurring/cancel?ws=          — cancel
    GET  /alerts?ws=                    — list alerts
    POST /alerts?ws=                    — create/update alert
    GET  /statement?ws=&month=YYYY-MM   — monthly statement
    GET  /holds?ws=                     — list active holds

  Owner-only
  ----------
    POST /refund?ws=                    — credit balance back
    POST /admin/adjust?ws=              — manual balance adjust
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
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
from app.services.vietqr import (
    generate_qr_image_b64,
    generate_qr_payload,
    make_intent_code,
)
from app.services.wallet_engine import (
    HoldNotFound,
    InsufficientFunds,
    WalletError,
    admin_adjust as engine_admin_adjust,
    get_balance as engine_get_balance,
    list_transactions,
    monthly_statement,
    refund as engine_refund,
    transfer as engine_transfer,
)

log = logging.getLogger("zeni.api.wallet")
router = APIRouter(prefix="/wallet", tags=["wallet", "zeni-pay-v2"])


# ─── Constants ──────────────────────────────────────────────────────────────
TOPUP_INTENT_TTL_MINUTES = 30
TOPUP_MIN_VND = 10_000
TOPUP_MAX_VND = 2_000_000_000


# ─── Pydantic schemas ───────────────────────────────────────────────────────


class TopupIn(BaseModel):
    amount_vnd: int = Field(ge=TOPUP_MIN_VND, le=TOPUP_MAX_VND)
    payment_method: str = Field(default="vietqr", pattern=r"^(vietqr|crypto|usd_wire)$")
    bank_code: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=200)


class TransferIn(BaseModel):
    to_workspace: str = Field(min_length=1, max_length=32)
    amount_vnd: int = Field(gt=0, le=100_000_000)
    note: str = Field(min_length=1, max_length=200)


class RefundIn(BaseModel):
    amount_vnd: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    original_tx_id: int | None = Field(default=None, gt=0)


class RecurringCreateIn(BaseModel):
    plan_id: str = Field(min_length=1, max_length=40)
    amount_vnd: int = Field(gt=0, le=2_000_000_000)
    billing_cycle: str = Field(default="monthly", pattern=r"^(monthly|yearly)$")
    next_charge_at: datetime | None = None  # default: NOW + cycle


class RecurringCancelIn(BaseModel):
    id: int = Field(gt=0)


class AlertIn(BaseModel):
    alert_type: str = Field(pattern=r"^(low_balance|charge_failed|refund|topup_received)$")
    threshold_vnd: int | None = Field(default=None, ge=0)
    email_enabled: bool = True
    sms_enabled: bool = False


class AdminAdjustIn(BaseModel):
    amount_vnd: int = Field(description="Positive=credit, negative=debit", ge=-2_000_000_000, le=2_000_000_000)
    reason: str = Field(min_length=3, max_length=500)


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _resolve_workspace(db: AsyncSession, ws: str) -> tuple[str, str, str]:
    row = (await db.execute(
        text("SELECT id, code, name FROM workspaces WHERE id = :w OR code = :w LIMIT 1"),
        {"w": ws},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{ws}' không tồn tại")
    return row[0], row[1], row[2]


def _require_owner(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role cho action này")


async def _get_default_bank(db: AsyncSession, bank_code: str | None) -> dict:
    if bank_code:
        row = (await db.execute(text("""
            SELECT id, bank_code, bank_name, account_number, account_holder, branch
              FROM payment_bank_accounts
             WHERE bank_code = :bc AND is_active = TRUE
             ORDER BY is_default DESC, id ASC
             LIMIT 1
        """), {"bc": bank_code.upper()})).mappings().first()
    else:
        row = (await db.execute(text("""
            SELECT id, bank_code, bank_name, account_number, account_holder, branch
              FROM payment_bank_accounts
             WHERE is_active = TRUE
             ORDER BY is_default DESC, id ASC
             LIMIT 1
        """))).mappings().first()
    if row is None:
        raise HTTPException(status_code=503, detail="Chưa cấu hình tài khoản nhận tiền")
    return dict(row)


# ─── Endpoints — balance & transactions ─────────────────────────────────────


@router.get("/balance")
async def get_balance(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current wallet balance snapshot."""
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    snap = await engine_get_balance(db, workspace_id)
    return {"ok": True, **snap.as_dict()}


@router.get("/transactions")
async def transactions(
    ws: str = Query(...),
    type: str | None = Query(None, description="Filter type"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    fd = _parse_date(from_) if from_ else None
    td = _parse_date(to) if to else None
    rows = await list_transactions(
        db, workspace_id,
        type_filter=type, from_date=fd, to_date=td,
        limit=limit, offset=offset,
    )
    return {"ok": True, "workspace_id": workspace_id, "count": len(rows), "transactions": rows}


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or full ISO."""
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {s}")


# ─── Top-up ─────────────────────────────────────────────────────────────────


@router.post("/topup")
async def topup(
    body: TopupIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a top-up payment intent. Returns VietQR.

    The actual balance credit happens via the existing payment_processor when
    the bank webhook arrives (purpose='wallet_topup').
    """
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    if body.payment_method != "vietqr":
        raise HTTPException(status_code=501, detail=f"payment_method '{body.payment_method}' chưa hỗ trợ")

    bank = await _get_default_bank(db, body.bank_code)

    ts = int(time.time())
    intent_code = make_intent_code(workspace_id, ts)
    add_info = "Zeni wallet topup"[:25]
    qr_payload = generate_qr_payload(
        bank_code=bank["bank_code"],
        account_number=bank["account_number"],
        amount_vnd=body.amount_vnd,
        add_info=add_info,
        ref=intent_code,
    )
    qr_image = generate_qr_image_b64(qr_payload, size=320)

    from datetime import timedelta
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOPUP_INTENT_TTL_MINUTES)

    try:
        intent_id = (await db.execute(text("""
            INSERT INTO payment_intents
                (intent_code, workspace_id, user_email, amount_vnd, purpose,
                 bank_account_id, qr_image_data, qr_payload,
                 status, expires_at, metadata)
            VALUES (:ic, :w, :email, :amt, 'wallet_topup',
                    :bid, :img, :qp, 'pending', :exp, :meta::jsonb)
            RETURNING id
        """), {
            "ic": intent_code, "w": workspace_id, "email": me.email,
            "amt": body.amount_vnd, "bid": bank["id"],
            "img": qr_image, "qp": qr_payload, "exp": expires_at,
            "meta": _json({
                "workspace_code": code,
                "workspace_name": name,
                "bank_code": bank["bank_code"],
                "created_by": me.email,
                "note": body.note,
                "wallet_topup": True,
            }),
        })).scalar()

        # Pre-record wallet_topups row in pending status
        await db.execute(text("""
            INSERT INTO wallet_topups
                (workspace_id, intent_id, intent_code, amount_vnd,
                 payment_method, status)
            VALUES (:w, :iid, :ic, :amt, :pm, 'pending')
        """), {
            "w": workspace_id, "iid": intent_id, "ic": intent_code,
            "amt": body.amount_vnd, "pm": body.payment_method,
        })

        await audit_push(
            db, actor=me.email, workspace_id=workspace_id,
            action="wallet.topup.intent", target=intent_code, severity="info",
            metadata={"amount_vnd": body.amount_vnd, "bank": bank["bank_code"]},
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("wallet.topup intent create failed")
        raise HTTPException(status_code=500, detail=f"Tạo top-up thất bại: {type(e).__name__}: {e}")

    return {
        "ok": True,
        "intent_code": intent_code,
        "intent_id": intent_id,
        "workspace_id": workspace_id,
        "amount_vnd": body.amount_vnd,
        "payment_method": body.payment_method,
        "qr_image_data": qr_image,
        "qr_payload": qr_payload,
        "bank_account": {
            "bank_code": bank["bank_code"],
            "bank_name": bank["bank_name"],
            "account_number": bank["account_number"],
            "account_holder": bank["account_holder"],
            "branch": bank.get("branch"),
        },
        "expires_at": expires_at.isoformat(),
        "memo_text": intent_code,
        "message_vi": (
            f"Quét QR → chuyển khoản với nội dung BẮT BUỘC: {intent_code}. "
            f"Hệ thống tự nạp ví trong < 1 phút sau khi tiền về."
        ),
    }


# ─── Transfer ───────────────────────────────────────────────────────────────


@router.post("/transfer")
async def transfer(
    body: TransferIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Internal P2P transfer between two workspaces.

    Caller must have access to ``ws`` (the sender). Recipient validated by lookup.
    """
    src_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(src_id, me)
    dst_id, _, _ = await _resolve_workspace(db, body.to_workspace)
    if src_id == dst_id:
        raise HTTPException(status_code=400, detail="Không thể chuyển cho chính workspace mình")

    try:
        result = await engine_transfer(
            db, src_id, dst_id, body.amount_vnd,
            reason=body.note, actor=me.email,
        )
        await db.commit()
    except InsufficientFunds as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except (ValueError, WalletError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        log.exception("wallet.transfer failed")
        raise HTTPException(status_code=500, detail="Transfer failed")

    return result


# ─── Refund (Owner only) ────────────────────────────────────────────────────


@router.post("/refund")
async def refund(
    body: RefundIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_owner(me)
    workspace_id, _, _ = await _resolve_workspace(db, ws)

    try:
        result = await engine_refund(
            db, workspace_id, body.amount_vnd,
            reason=body.reason, original_tx_id=body.original_tx_id,
            actor=me.email,
        )
        await db.commit()
    except (ValueError, WalletError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        log.exception("wallet.refund failed")
        raise HTTPException(status_code=500, detail="Refund failed")
    return result


# ─── Recurring charges ──────────────────────────────────────────────────────


@router.get("/recurring")
async def list_recurring(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    rows = (await db.execute(text("""
        SELECT id, plan_id, amount_vnd, billing_cycle,
               next_charge_at, last_charged_at, last_charge_status,
               retry_count, max_retries, status, created_at
          FROM wallet_recurring_charges
         WHERE workspace_id = :w
         ORDER BY status='active' DESC, next_charge_at ASC
    """), {"w": workspace_id})).mappings().all()
    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {
                **dict(r),
                "amount_vnd": float(r["amount_vnd"]),
                "next_charge_at": r["next_charge_at"].isoformat() if r["next_charge_at"] else None,
                "last_charged_at": r["last_charged_at"].isoformat() if r["last_charged_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/recurring")
async def create_recurring(
    body: RecurringCreateIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    from datetime import timedelta
    cycle_days = 30 if body.billing_cycle == "monthly" else 365
    next_at = body.next_charge_at or (datetime.now(timezone.utc) + timedelta(days=cycle_days))

    new_id = (await db.execute(text("""
        INSERT INTO wallet_recurring_charges
            (workspace_id, plan_id, amount_vnd, billing_cycle, next_charge_at)
        VALUES (:w, :p, :a, :bc, :nx)
        RETURNING id
    """), {
        "w": workspace_id, "p": body.plan_id, "a": body.amount_vnd,
        "bc": body.billing_cycle, "nx": next_at,
    })).scalar()

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="wallet.recurring.create", target=f"recurring:{new_id}",
        severity="info",
        metadata={"plan_id": body.plan_id, "amount_vnd": body.amount_vnd,
                  "cycle": body.billing_cycle},
    )
    await db.commit()
    return {"ok": True, "id": int(new_id), "next_charge_at": next_at.isoformat()}


@router.post("/recurring/cancel")
async def cancel_recurring(
    body: RecurringCancelIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    row = (await db.execute(text("""
        SELECT id, status FROM wallet_recurring_charges
         WHERE id = :id AND workspace_id = :w
    """), {"id": body.id, "w": workspace_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Recurring không tồn tại")
    if row[1] == "cancelled":
        raise HTTPException(status_code=400, detail="Đã bị huỷ")

    await db.execute(text("""
        UPDATE wallet_recurring_charges
           SET status = 'cancelled', cancelled_at = NOW(), updated_at = NOW()
         WHERE id = :id
    """), {"id": body.id})
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="wallet.recurring.cancel", target=f"recurring:{body.id}",
        severity="info",
    )
    await db.commit()
    return {"ok": True, "id": body.id, "status": "cancelled"}


# ─── Alerts ─────────────────────────────────────────────────────────────────


@router.get("/alerts")
async def list_alerts(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)
    rows = (await db.execute(text("""
        SELECT id, alert_type, threshold_vnd, email_enabled, sms_enabled,
               last_triggered_at, trigger_count, enabled, created_at
          FROM wallet_alerts WHERE workspace_id = :w
         ORDER BY id ASC
    """), {"w": workspace_id})).mappings().all()
    return {
        "ok": True,
        "count": len(rows),
        "alerts": [
            {
                **dict(r),
                "threshold_vnd": float(r["threshold_vnd"]) if r["threshold_vnd"] is not None else None,
                "last_triggered_at": r["last_triggered_at"].isoformat() if r["last_triggered_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/alerts")
async def upsert_alert(
    body: AlertIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    if body.alert_type == "low_balance" and body.threshold_vnd is None:
        raise HTTPException(status_code=400, detail="threshold_vnd is required for low_balance")

    new_id = (await db.execute(text("""
        INSERT INTO wallet_alerts
            (workspace_id, alert_type, threshold_vnd, email_enabled, sms_enabled, enabled)
        VALUES (:w, :t, :th, :em, :sm, TRUE)
        ON CONFLICT (workspace_id, alert_type) DO UPDATE SET
            threshold_vnd  = EXCLUDED.threshold_vnd,
            email_enabled  = EXCLUDED.email_enabled,
            sms_enabled    = EXCLUDED.sms_enabled,
            enabled        = TRUE
        RETURNING id
    """), {
        "w": workspace_id, "t": body.alert_type, "th": body.threshold_vnd,
        "em": body.email_enabled, "sm": body.sms_enabled,
    })).scalar()

    # If threshold low_balance changed, also update wallet_balances.low_balance_threshold
    if body.alert_type == "low_balance" and body.threshold_vnd is not None:
        await db.execute(text("""
            UPDATE wallet_balances SET low_balance_threshold = :th WHERE workspace_id = :w
        """), {"th": body.threshold_vnd, "w": workspace_id})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="wallet.alert.upsert", target=body.alert_type, severity="info",
        metadata={"threshold_vnd": body.threshold_vnd},
    )
    await db.commit()
    return {"ok": True, "id": int(new_id), "alert_type": body.alert_type}


# ─── Statement ──────────────────────────────────────────────────────────────


@router.get("/statement")
async def statement(
    ws: str = Query(...),
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    try:
        y, m = month.split("-")
        year, mn = int(y), int(m)
        if not (1 <= mn <= 12):
            raise ValueError("month")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid month: {month}")

    summary = await monthly_statement(db, workspace_id, year, mn)
    summary["pdf_url"] = None  # placeholder — PDF generation in future sprint
    return {"ok": True, **summary}


# ─── Holds ──────────────────────────────────────────────────────────────────


@router.get("/holds")
async def list_holds(
    ws: str = Query(...),
    active_only: bool = Query(default=True),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, _, _ = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    where = ["workspace_id = :w"]
    if active_only:
        where.append("NOT released")
    rows = (await db.execute(text(f"""
        SELECT id, amount_vnd, reason, source_type, source_id,
               hold_until, released, released_at, actual_spent, created_at
          FROM wallet_holds
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT 100
    """), {"w": workspace_id})).mappings().all()

    return {
        "ok": True,
        "count": len(rows),
        "holds": [
            {
                **dict(r),
                "amount_vnd": float(r["amount_vnd"]),
                "actual_spent": float(r["actual_spent"]) if r["actual_spent"] is not None else None,
                "hold_until": r["hold_until"].isoformat() if r["hold_until"] else None,
                "released_at": r["released_at"].isoformat() if r["released_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


# ─── Admin: manual adjust (Owner only) ──────────────────────────────────────


@router.post("/admin/adjust")
async def admin_adjust(
    body: AdminAdjustIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_owner(me)
    workspace_id, _, _ = await _resolve_workspace(db, ws)

    try:
        result = await engine_admin_adjust(
            db, workspace_id, body.amount_vnd,
            reason=body.reason, actor=me.email,
        )
        await db.commit()
    except InsufficientFunds as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except (ValueError, WalletError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        await db.rollback()
        log.exception("admin_adjust failed")
        raise HTTPException(status_code=500, detail="Adjust failed")

    return result


# ─── Local helpers ──────────────────────────────────────────────────────────


def _json(obj: Any) -> str:
    import json
    def _default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)
