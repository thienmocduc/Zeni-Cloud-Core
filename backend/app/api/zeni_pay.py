"""
Zeni Cloud Core — Zeni Pay Cấp 1 API (VietQR direct payment).

Strategy: KHÔNG qua VNPay/MoMo. Khách scan VietQR → chuyển khoản trực tiếp đến TK
Zeni Holdings → Zeni listen webhook ngân hàng (TPB/MB/VCB Open API) → match
``intent_code`` (set as remittance memo) → activate subscription / wallet topup
tự động.

Endpoints
---------
Auth (workspace member):
  POST /api/v1/zeni-pay/intent                    — create intent (sinh QR)
  GET  /api/v1/zeni-pay/intent/{intent_code}      — poll status
  POST /api/v1/zeni-pay/intent/{intent_code}/cancel
  GET  /api/v1/zeni-pay/intents?ws=&status=       — list history

Public (HMAC-secured):
  POST /api/v1/zeni-pay/webhook/{bank_code}       — bank callbacks

Owner-only:
  POST /api/v1/zeni-pay/admin/manual-confirm      — fallback if webhook fails
  POST /api/v1/zeni-pay/refund                    — refund a paid intent
  GET  /api/v1/zeni-pay/bank-accounts             — list Zeni Holdings banks
  POST /api/v1/zeni-pay/bank-accounts             — add a new bank
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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
from app.services.payment_processor import activate_after_payment
from app.services.vietqr import (
    BANK_BINS,
    generate_qr_image_b64,
    generate_qr_payload,
    make_intent_code,
)

log = logging.getLogger("zeni.api.zeni_pay")
router = APIRouter(prefix="/zeni-pay", tags=["zeni-pay", "payments", "vietqr"])

# ─── Constants ──────────────────────────────────────────────────────────────
INTENT_TTL_MINUTES = 30
PURPOSE_PATTERN = r"^(subscription_(free|starter|pro|business|enterprise)|wallet_topup|custom)$"


# ─── Pydantic schemas ───────────────────────────────────────────────────────


class CreateIntentIn(BaseModel):
    workspace_code: str = Field(min_length=1, max_length=32)
    amount_vnd: int = Field(gt=0, le=2_000_000_000)
    purpose: str = Field(pattern=PURPOSE_PATTERN)
    purpose_ref: str | None = Field(default=None, max_length=80)
    bank_code: str | None = Field(default=None, max_length=20,
                                   description="Override default bank; else use is_default=true")


class IntentOut(BaseModel):
    intent_code: str
    workspace_id: str
    amount_vnd: int
    purpose: str
    purpose_ref: str | None
    status: str
    expires_at: str
    paid_at: str | None
    paid_amount_vnd: int | None
    bank_tx_ref: str | None
    qr_image_data: str | None = None
    qr_payload: str | None = None
    bank_account: dict | None = None


class ManualConfirmIn(BaseModel):
    intent_code: str = Field(min_length=4, max_length=40)
    amount_vnd: int = Field(gt=0)
    bank_tx_ref: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=500)


class RefundIn(BaseModel):
    intent_code: str = Field(min_length=4, max_length=40)
    reason: str = Field(min_length=3, max_length=500)


class AddBankIn(BaseModel):
    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str = Field(min_length=2, max_length=80)
    account_number: str = Field(min_length=4, max_length=40)
    account_holder: str = Field(min_length=2, max_length=120)
    branch: str | None = Field(default=None, max_length=120)
    is_default: bool = False
    webhook_secret: str | None = Field(default=None, max_length=256)


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _resolve_workspace(db: AsyncSession, workspace_code: str) -> tuple[str, str, str]:
    row = (await db.execute(
        text("""SELECT id, code, name FROM workspaces
                WHERE id = :w OR code = :w
                LIMIT 1"""),
        {"w": workspace_code},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_code}' không tồn tại")
    return row[0], row[1], row[2]


def _require_owner(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role cho action này")


async def _get_active_bank(db: AsyncSession, bank_code: str | None) -> dict:
    """Pick the bank account to receive payment.

    - If ``bank_code`` provided: use is_active row for that bank.
    - Else: use is_default=true is_active row.
    - Falls back to any is_active row if no default flagged.
    """
    if bank_code:
        row = (await db.execute(text("""
            SELECT id, bank_code, bank_name, account_number, account_holder, branch, webhook_secret
              FROM payment_bank_accounts
             WHERE bank_code = :bc AND is_active = TRUE
             ORDER BY is_default DESC, id ASC
             LIMIT 1
        """), {"bc": bank_code.upper()})).mappings().first()
    else:
        row = (await db.execute(text("""
            SELECT id, bank_code, bank_name, account_number, account_holder, branch, webhook_secret
              FROM payment_bank_accounts
             WHERE is_active = TRUE
             ORDER BY is_default DESC, id ASC
             LIMIT 1
        """))).mappings().first()
    if row is None:
        raise HTTPException(status_code=503, detail="Chưa cấu hình tài khoản nhận tiền")
    return dict(row)


def _bank_public_view(bank: dict) -> dict:
    return {
        "id": bank.get("id"),
        "bank_code": bank["bank_code"],
        "bank_name": bank["bank_name"],
        "account_number": bank["account_number"],
        "account_holder": bank["account_holder"],
        "branch": bank.get("branch"),
    }


def _intent_to_out(row: dict) -> dict:
    return {
        "intent_code": row["intent_code"],
        "workspace_id": row["workspace_id"],
        "amount_vnd": int(row["amount_vnd"]),
        "purpose": row["purpose"],
        "purpose_ref": row.get("purpose_ref"),
        "status": row["status"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "paid_at": row["paid_at"].isoformat() if row.get("paid_at") else None,
        "paid_amount_vnd": int(row["paid_amount_vnd"]) if row.get("paid_amount_vnd") else None,
        "bank_tx_ref": row.get("bank_tx_ref"),
    }


# ─── Endpoints — auth required ──────────────────────────────────────────────


@router.post("/intent")
async def create_intent(
    data: CreateIntentIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create payment intent + generate VietQR. 30-minute TTL."""
    workspace_id, code, name = await _resolve_workspace(db, data.workspace_code)
    await require_workspace_access(workspace_id, me)

    bank = await _get_active_bank(db, data.bank_code)

    # Generate deterministic intent code (limited to 25 chars for QR tag 62/05).
    ts = int(time.time())
    intent_code = make_intent_code(workspace_id, ts)

    # Build VietQR payload + image
    add_info = f"Zeni {data.purpose}"[:25]
    qr_payload = generate_qr_payload(
        bank_code=bank["bank_code"],
        account_number=bank["account_number"],
        amount_vnd=data.amount_vnd,
        add_info=add_info,
        ref=intent_code,
    )
    qr_image_b64 = generate_qr_image_b64(qr_payload, size=320)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=INTENT_TTL_MINUTES)

    try:
        await db.execute(text("""
            INSERT INTO payment_intents
                (intent_code, workspace_id, user_email, amount_vnd, purpose, purpose_ref,
                 bank_account_id, qr_image_data, qr_payload,
                 status, expires_at, metadata)
            VALUES
                (:ic, :w, :email, :amt, :p, :pr, :bid, :img, :qp,
                 'pending', :exp, CAST(:meta AS jsonb))
        """), {
            "ic": intent_code, "w": workspace_id, "email": me.email,
            "amt": data.amount_vnd, "p": data.purpose, "pr": data.purpose_ref,
            "bid": bank["id"], "img": qr_image_b64, "qp": qr_payload,
            "exp": expires_at,
            "meta": _json({
                "workspace_code": code,
                "workspace_name": name,
                "bank_code": bank["bank_code"],
                "account_holder": bank["account_holder"],
                "created_by": me.email,
            }),
        })
        await audit_push(
            db, actor=me.email, workspace_id=workspace_id,
            action="zeni_pay.intent.create", target=intent_code, severity="info",
            metadata={
                "amount_vnd": data.amount_vnd,
                "purpose": data.purpose,
                "bank_code": bank["bank_code"],
            },
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_intent failed")
        raise HTTPException(status_code=500, detail=f"Tạo intent thất bại: {type(e).__name__}: {e}")

    return {
        "ok": True,
        "intent_code": intent_code,
        "workspace_id": workspace_id,
        "amount_vnd": data.amount_vnd,
        "purpose": data.purpose,
        "purpose_ref": data.purpose_ref,
        "status": "pending",
        "expires_at": expires_at.isoformat(),
        "qr_image_data": qr_image_b64,
        "qr_payload": qr_payload,
        "bank_account": _bank_public_view(bank),
        "memo_text": intent_code,
        "message_vi": (
            f"Mở app ngân hàng → quét QR → nội dung chuyển khoản BẮT BUỘC: {intent_code}. "
            f"Hệ thống sẽ tự kích hoạt sau khi nhận tiền (thường < 1 phút)."
        ),
    }


@router.get("/intent/{intent_code}")
async def get_intent(
    intent_code: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poll intent status (frontend polls every 3-5s while pending)."""
    row = (await db.execute(text("""
        SELECT i.intent_code, i.workspace_id, i.user_email, i.amount_vnd,
               i.purpose, i.purpose_ref, i.status, i.expires_at, i.paid_at,
               i.paid_amount_vnd, i.bank_tx_ref, i.qr_image_data, i.qr_payload,
               b.bank_code, b.bank_name, b.account_number, b.account_holder, b.branch
          FROM payment_intents i
          LEFT JOIN payment_bank_accounts b ON b.id = i.bank_account_id
         WHERE i.intent_code = :ic
    """), {"ic": intent_code})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Intent '{intent_code}' không tồn tại")
    await require_workspace_access(row["workspace_id"], me)

    out = _intent_to_out(dict(row))
    out["qr_image_data"] = row["qr_image_data"]
    out["qr_payload"] = row["qr_payload"]
    out["bank_account"] = (
        _bank_public_view({
            "id": None,
            "bank_code": row["bank_code"], "bank_name": row["bank_name"],
            "account_number": row["account_number"],
            "account_holder": row["account_holder"], "branch": row["branch"],
        }) if row["bank_code"] else None
    )
    return out


@router.post("/intent/{intent_code}/cancel")
async def cancel_intent(
    intent_code: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (await db.execute(text("""
        SELECT id, workspace_id, status FROM payment_intents WHERE intent_code = :ic
    """), {"ic": intent_code})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Intent không tồn tại")
    await require_workspace_access(row[1], me)
    if row[2] != "pending":
        raise HTTPException(status_code=400, detail=f"Intent đã ở trạng thái '{row[2]}'")

    await db.execute(text("""
        UPDATE payment_intents SET status = 'cancelled' WHERE id = :id
    """), {"id": row[0]})
    await audit_push(
        db, actor=me.email, workspace_id=row[1],
        action="zeni_pay.intent.cancel", target=intent_code, severity="info",
    )
    await db.commit()
    return {"ok": True, "intent_code": intent_code, "status": "cancelled"}


@router.get("/intents")
async def list_intents(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    status: str | None = Query(None, pattern=r"^(pending|paid|expired|cancelled|refunded)$"),
    limit: int = Query(50, ge=1, le=200),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    where = ["workspace_id = :w"]
    params: dict[str, Any] = {"w": workspace_id, "n": limit}
    if status:
        where.append("status = :s")
        params["s"] = status

    rows = (await db.execute(text(f"""
        SELECT intent_code, workspace_id, amount_vnd, purpose, purpose_ref,
               status, expires_at, paid_at, paid_amount_vnd, bank_tx_ref
          FROM payment_intents
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT :n
    """), params)).mappings().all()
    return {
        "workspace_id": workspace_id,
        "count": len(rows),
        "intents": [_intent_to_out(dict(r)) for r in rows],
    }


# ─── Webhook (public, HMAC-secured) ─────────────────────────────────────────


@router.post("/webhook/{bank_code}")
async def bank_webhook(
    bank_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_zeni_signature: str | None = Header(default=None, alias="X-Zeni-Signature"),
) -> dict:
    """Generic bank webhook receiver.

    Each bank has its own JSON shape — we store the raw payload and best-effort
    parse common fields (amount, ref code, tx ref, sender name).

    HMAC verification: header ``X-Signature`` or ``X-Zeni-Signature`` containing
    hex-encoded HMAC-SHA256 of the raw body using the bank's webhook_secret
    (configured per ``payment_bank_accounts.webhook_secret``). If the bank has
    no secret stored, signature verification is skipped (DEV mode).
    """
    bank_code = bank_code.upper()
    raw_body = await request.body()
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")

    # Look up bank account (use first active for that bank_code)
    bank = (await db.execute(text("""
        SELECT id, bank_code, account_number, webhook_secret
          FROM payment_bank_accounts
         WHERE bank_code = :bc AND is_active = TRUE
         ORDER BY is_default DESC, id ASC
         LIMIT 1
    """), {"bc": bank_code})).mappings().first()
    if bank is None:
        raise HTTPException(status_code=404, detail=f"Bank {bank_code} chưa cấu hình")

    # ─ HMAC verification (if secret stored) ─
    secret = bank.get("webhook_secret")
    sig = x_zeni_signature or x_signature
    if secret:
        if not sig:
            raise HTTPException(status_code=401, detail="Missing X-Signature")
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig.lower()):
            log.warning("[zeni-pay] webhook HMAC mismatch bank=%s", bank_code)
            raise HTTPException(status_code=401, detail="Invalid signature")

    # ─ Best-effort parse of common bank Open API shapes ─
    amount, ref, tx_ref, sender = _parse_bank_payload(bank_code, payload)

    # Always log raw event
    ev_id = (await db.execute(text("""
        INSERT INTO bank_webhook_events
            (bank_code, bank_account_id, raw_payload,
             parsed_amount_vnd, parsed_ref_code, parsed_tx_ref, parsed_sender_name)
        VALUES (:bc, :bid, :rp::jsonb, :amt, :ref, :tx, :sender)
        RETURNING id
    """), {
        "bc": bank_code, "bid": bank["id"], "rp": _json(payload),
        "amt": amount, "ref": (ref or "").upper() or None,
        "tx": tx_ref, "sender": sender,
    })).scalar()

    activated = False
    if ref and amount:
        intent = (await db.execute(text("""
            SELECT id, intent_code, workspace_id, user_email, amount_vnd,
                   purpose, status, expires_at
              FROM payment_intents
             WHERE intent_code = :code AND status = 'pending'
             LIMIT 1
        """), {"code": ref.upper()})).mappings().first()

        if intent and amount >= int(intent["amount_vnd"]):
            try:
                await activate_after_payment(
                    db, dict(intent),
                    paid_amount_vnd=int(amount),
                    bank_tx_ref=tx_ref,
                    actor=f"webhook:{bank_code}",
                )
                await db.execute(text("""
                    UPDATE bank_webhook_events
                       SET processed = TRUE, matched_intent_id = :iid
                     WHERE id = :id
                """), {"id": ev_id, "iid": intent["id"]})
                activated = True
            except Exception as e:
                log.exception("inline activate failed")
                await db.execute(text("""
                    UPDATE bank_webhook_events SET processing_error = :err WHERE id = :id
                """), {"id": ev_id, "err": str(e)})

    await db.commit()
    return {"ok": True, "received": True, "activated": activated, "event_id": ev_id}


def _parse_bank_payload(bank_code: str, payload: dict) -> tuple[int | None, str | None, str | None, str | None]:
    """Best-effort extraction of amount/ref/tx/sender from various bank shapes.

    Each bank's Open API differs; we try several known field names.
    """
    def _g(*keys):
        for k in keys:
            for src in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {},
                        payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}):
                if isinstance(src, dict) and k in src and src[k] is not None:
                    return src[k]
        return None

    amount_raw = _g("amount", "creditAmount", "transAmount", "amountVnd", "amount_vnd")
    try:
        amount = int(float(str(amount_raw))) if amount_raw is not None else None
    except (TypeError, ValueError):
        amount = None

    description = _g("description", "remark", "memo", "content", "addInfo", "note")
    ref = None
    if description:
        # Find first ZP-XXXX-XXXX token in description
        import re
        m = re.search(r"ZP-[A-Z0-9]{2,12}-[A-Z0-9]{4,16}", str(description).upper())
        if m:
            ref = m.group(0)
    if not ref:
        ref = _g("refCode", "ref_code", "billNumber", "bill_number")
    if ref:
        ref = str(ref).strip().upper()

    tx_ref = _g("transactionId", "transaction_id", "txId", "tx_id", "ftCode", "id")
    if tx_ref is not None:
        tx_ref = str(tx_ref)[:80]

    sender = _g("senderName", "sender_name", "fromAccount", "payerName")
    if sender is not None:
        sender = str(sender)[:120]

    return amount, ref, tx_ref, sender


# ─── Admin endpoints (Owner only) ───────────────────────────────────────────


@router.post("/admin/manual-confirm")
async def admin_manual_confirm(
    data: ManualConfirmIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner backup: manually confirm a payment when webhook fails or not configured."""
    _require_owner(me)

    intent = (await db.execute(text("""
        SELECT id, intent_code, workspace_id, user_email, amount_vnd,
               purpose, status, expires_at
          FROM payment_intents
         WHERE intent_code = :ic
    """), {"ic": data.intent_code})).mappings().first()
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent không tồn tại")
    if intent["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Intent đã ở trạng thái '{intent['status']}'")

    if data.amount_vnd < int(intent["amount_vnd"]):
        raise HTTPException(
            status_code=400,
            detail=f"Số tiền {data.amount_vnd} thấp hơn yêu cầu {int(intent['amount_vnd'])}",
        )

    result = await activate_after_payment(
        db, dict(intent),
        paid_amount_vnd=data.amount_vnd,
        bank_tx_ref=data.bank_tx_ref,
        actor=f"admin:{me.email}",
    )
    await audit_push(
        db, actor=me.email, workspace_id=intent["workspace_id"],
        action="zeni_pay.admin.manual_confirm", target=data.intent_code, severity="ok",
        metadata={"amount_vnd": data.amount_vnd, "tx_ref": data.bank_tx_ref, "notes": data.notes},
    )
    await db.commit()
    return {"ok": True, "intent_code": data.intent_code, "status": "paid", **result}


@router.post("/refund")
async def refund_intent(
    data: RefundIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: log refund request for a paid intent.

    Note: actual bank-side refund is performed manually by Zeni Holdings finance
    team (no automated bank payouts in Cấp 1).
    """
    _require_owner(me)

    intent = (await db.execute(text("""
        SELECT id, intent_code, workspace_id, paid_amount_vnd, status, purpose
          FROM payment_intents
         WHERE intent_code = :ic
    """), {"ic": data.intent_code})).mappings().first()
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent không tồn tại")
    if intent["status"] != "paid":
        raise HTTPException(status_code=400, detail=f"Chỉ refund được intent đã 'paid' (hiện: '{intent['status']}')")

    refund_id = (await db.execute(text("""
        INSERT INTO payment_refunds
            (intent_id, workspace_id, amount_vnd, reason, refunded_by_user, status)
        VALUES (:iid, :w, :amt, :reason, :user, 'pending')
        RETURNING id
    """), {
        "iid": intent["id"], "w": intent["workspace_id"],
        "amt": int(intent["paid_amount_vnd"] or 0), "reason": data.reason, "user": me.email,
    })).scalar()

    await db.execute(text("""
        UPDATE payment_intents SET status = 'refunded' WHERE id = :id
    """), {"id": intent["id"]})

    await audit_push(
        db, actor=me.email, workspace_id=intent["workspace_id"],
        action="zeni_pay.refund.create", target=data.intent_code, severity="warning",
        metadata={"amount_vnd": int(intent["paid_amount_vnd"] or 0), "reason": data.reason},
    )
    await db.commit()
    return {
        "ok": True,
        "refund_id": refund_id,
        "intent_code": data.intent_code,
        "amount_vnd": int(intent["paid_amount_vnd"] or 0),
        "status": "pending",
        "message_vi": "Đã ghi nhận yêu cầu hoàn tiền. Tài chính Zeni Holdings sẽ chuyển khoản thủ công.",
    }


@router.get("/bank-accounts")
async def list_bank_accounts(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: list configured Zeni Holdings bank accounts."""
    _require_owner(me)
    rows = (await db.execute(text("""
        SELECT id, bank_code, bank_name, account_number, account_holder, branch,
               is_active, is_default, created_at,
               CASE WHEN webhook_secret IS NOT NULL THEN TRUE ELSE FALSE END AS has_webhook_secret
          FROM payment_bank_accounts
         ORDER BY is_default DESC, id ASC
    """))).mappings().all()
    return {
        "count": len(rows),
        "supported_bank_bins": BANK_BINS,
        "accounts": [
            {
                **dict(r),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/bank-accounts")
async def add_bank_account(
    data: AddBankIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: add a new Zeni Holdings bank account."""
    _require_owner(me)
    if data.bank_code.upper() not in BANK_BINS:
        raise HTTPException(
            status_code=400,
            detail=f"bank_code '{data.bank_code}' chưa có BIN. Hỗ trợ: {sorted(BANK_BINS.keys())}",
        )

    # If is_default → un-default others
    if data.is_default:
        await db.execute(text("UPDATE payment_bank_accounts SET is_default = FALSE"))

    new_id = (await db.execute(text("""
        INSERT INTO payment_bank_accounts
            (bank_code, bank_name, account_number, account_holder, branch,
             is_default, is_active, webhook_secret)
        VALUES (:bc, :bn, :an, :ah, :br, :idef, TRUE, :ws)
        ON CONFLICT (bank_code, account_number) DO UPDATE SET
            bank_name      = EXCLUDED.bank_name,
            account_holder = EXCLUDED.account_holder,
            branch         = EXCLUDED.branch,
            is_default     = EXCLUDED.is_default,
            is_active      = TRUE,
            webhook_secret = COALESCE(EXCLUDED.webhook_secret, payment_bank_accounts.webhook_secret)
        RETURNING id
    """), {
        "bc": data.bank_code.upper(), "bn": data.bank_name,
        "an": data.account_number, "ah": data.account_holder, "br": data.branch,
        "idef": data.is_default, "ws": data.webhook_secret,
    })).scalar()

    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="zeni_pay.bank.add", target=f"{data.bank_code}:{data.account_number}",
        severity="ok",
        metadata={"is_default": data.is_default, "has_secret": bool(data.webhook_secret)},
    )
    await db.commit()
    return {"ok": True, "id": new_id, "bank_code": data.bank_code.upper()}


# ─── Internal helpers ───────────────────────────────────────────────────────


def _json(obj: Any) -> str:
    """JSON serialize for ::jsonb cast (handles datetime → iso)."""
    import json
    def _default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)
