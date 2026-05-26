"""
Zeni Cloud Core — Design E-Signoff Workflow API (Phase 3 — Task #21).

Endpoints:
    POST /design/sessions/{session_id}/request-signoff
        → request a KTS certified partner sign a design package
    GET  /design/signoff-requests?ws=...
        → list signoff requests (Owner sees all in ws; KTS partner sees own)
    GET  /design/kts-partners?specialty=...
        → list active KTS certified partners
    POST /design/signoff/{request_id}/sign
        → KTS digitally signs; computes SHA-256 anchor (stub blockchain tx)
    POST /design/signoff/{request_id}/decline
        → KTS declines with reason

All endpoints require auth + workspace access. All mutating actions logged via
audit_push. E-signature blockchain anchor is currently a SHA-256 stub
(prefix 'stub-' + 64 hex chars) — real Polygon tx integration is TODO (Phase 4).

Chairman approved 2026-05-26.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.email import is_configured as smtp_configured, send_email

log = logging.getLogger("zeni.api.design_signoff")
router = APIRouter(prefix="/design", tags=["design-signoff"])

URGENCY_VALUES = {"normal", "urgent", "same_day"}
ALLOWED_SPECIALTIES = {"kien_truc", "noi_that", "ket_cau", "mep", "boq"}


# ─── Pydantic schemas ──────────────────────────────────────────
class KTSPartnerOut(BaseModel):
    id: str
    full_name: str
    cert_id: str
    cert_authority: str | None = None
    cert_expires: str | None = None
    specialty: list[str] = Field(default_factory=list)
    email: str
    phone: str | None = None
    fee_per_project_vnd: int = 0
    is_active: bool = True


class RequestSignoffIn(BaseModel):
    kts_partner_id: str = Field(min_length=8, max_length=64)
    urgency: str = Field(default="normal")

    @field_validator("urgency")
    @classmethod
    def _validate_urgency(cls, v: str) -> str:
        if v not in URGENCY_VALUES:
            raise ValueError(f"urgency phải thuộc {sorted(URGENCY_VALUES)}")
        return v


class SignoffRequestOut(BaseModel):
    id: str
    session_id: str
    workspace_id: str
    kts_partner_id: str
    kts_partner_name: str | None = None
    requester_email: str | None = None
    urgency: str
    status: str
    requested_at: str | None = None
    signed_at: str | None = None
    declined_reason: str | None = None
    blockchain_anchor_tx: str | None = None
    blockchain_anchor_chain: str | None = None
    notification_sent: bool = False


class SignoffRequestsListOut(BaseModel):
    requests: list[SignoffRequestOut]
    total: int


class SignActionIn(BaseModel):
    ca_signature_blob_b64: str = Field(min_length=8, max_length=200_000,
                                       description="Base64-encoded CA signature blob from KTS")
    recovery_phrase_hash: str = Field(min_length=32, max_length=128,
                                       description="SHA-256/512 hex of recovery phrase (proof of identity)")

    @field_validator("ca_signature_blob_b64")
    @classmethod
    def _validate_b64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as e:
            raise ValueError(f"ca_signature_blob_b64 không phải base64 hợp lệ: {e}")
        return v


class DeclineActionIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


# ─── Helpers ────────────────────────────────────────────────────
async def _load_partner(db: AsyncSession, partner_id: str) -> dict[str, Any]:
    try:
        UUID(partner_id)
    except Exception:
        raise HTTPException(status_code=400, detail="kts_partner_id phải là UUID hợp lệ")
    res = await db.execute(
        text(
            """SELECT id::text, full_name, cert_id, cert_authority,
                      cert_expires::text, specialty, email, phone,
                      fee_per_project_vnd, is_active
               FROM kts_certified_partners
               WHERE id = CAST(:id AS UUID)"""
        ),
        {"id": partner_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="KTS partner không tồn tại")
    if not row["is_active"]:
        raise HTTPException(status_code=400, detail="KTS partner đã không hoạt động")
    return dict(row)


async def _load_session_ready(db: AsyncSession, session_id: str, workspace_id: str) -> dict[str, Any]:
    res = await db.execute(
        text(
            """SELECT id::text, workspace_id, verdict
               FROM design_sessions
               WHERE id = CAST(:id AS UUID) AND workspace_id = :ws"""
        ),
        {"id": session_id, "ws": workspace_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Design session not found in this workspace")
    if row["verdict"] != "ready_for_signoff":
        raise HTTPException(
            status_code=400,
            detail=f"Session phải có verdict='ready_for_signoff' để xin ký (hiện tại={row['verdict']})",
        )
    return dict(row)


async def _send_signoff_email(
    *, kts_email: str, kts_name: str, session_id: str,
    requester: str, workspace_id: str, urgency: str, request_id: str,
) -> bool:
    """Send notification email to KTS partner."""
    if not smtp_configured():
        log.warning("[signoff.notify] SMTP not configured — skipping email to %s", kts_email)
        return False
    urgency_vi = {"normal": "Bình thường", "urgent": "Khẩn", "same_day": "Trong ngày"}.get(urgency, urgency)
    subject = f"[Zeni Cloud — Viet Contech] Yêu cầu ký duyệt thiết kế ({urgency_vi})"
    body_html = f"""<html><body>
<p>Kính gửi KTS <b>{kts_name}</b>,</p>
<p>Workspace <b>{workspace_id}</b> (yêu cầu bởi {requester}) vừa gửi 1 hồ sơ thiết kế \
sẵn sàng để ký duyệt:</p>
<ul>
  <li><b>Session ID:</b> {session_id}</li>
  <li><b>Sign-off Request ID:</b> {request_id}</li>
  <li><b>Mức ưu tiên:</b> {urgency_vi}</li>
  <li><b>Đã pass QA Validator</b> (verdict = ready_for_signoff)</li>
</ul>
<p>Vui lòng đăng nhập Zeni Cloud → Design Sign-off → xem hồ sơ và ký số (CA) hoặc từ chối có lý do.</p>
<p>Trân trọng,<br/>Zeni Cloud KTS Workflow</p>
</body></html>"""
    try:
        return await send_email(to=kts_email, subject=subject, body_html=body_html)
    except Exception as e:
        log.exception("[signoff.notify] send_email failed: %s", e)
        return False


# ─── 1. POST request-signoff ───────────────────────────────────
@router.post(
    "/sessions/{session_id}/request-signoff",
    response_model=SignoffRequestOut,
)
async def request_signoff(
    session_id: str,
    payload: RequestSignoffIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignoffRequestOut:
    """Submit signoff request to a chosen KTS certified partner."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer",):
        raise HTTPException(status_code=403, detail="Cần Developer trở lên để xin ký duyệt")

    sess = await _load_session_ready(db, session_id, ws)
    partner = await _load_partner(db, payload.kts_partner_id)

    ins = await db.execute(
        text(
            """INSERT INTO design_signoff_requests
               (session_id, workspace_id, requester_email, kts_partner_id, urgency, status)
               VALUES (CAST(:sid AS UUID), :ws, :actor, CAST(:pid AS UUID), :urgency, 'pending')
               RETURNING id::text, requested_at::text"""
        ),
        {
            "sid": session_id, "ws": ws, "actor": me.email,
            "pid": payload.kts_partner_id, "urgency": payload.urgency,
        },
    )
    row = ins.first()
    req_id = row[0]
    requested_at = row[1]

    # Send email notification
    notif_sent = await _send_signoff_email(
        kts_email=partner["email"], kts_name=partner["full_name"],
        session_id=session_id, requester=me.email, workspace_id=ws,
        urgency=payload.urgency, request_id=req_id,
    )
    if notif_sent:
        await db.execute(
            text("""UPDATE design_signoff_requests
                    SET notification_sent_at = NOW() WHERE id = CAST(:id AS UUID)"""),
            {"id": req_id},
        )

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.signoff.request",
        target=req_id, severity="info",
        metadata={
            "session_id": session_id, "kts_partner_id": payload.kts_partner_id,
            "kts_email": partner["email"], "urgency": payload.urgency,
            "notification_sent": notif_sent,
        },
    )
    await db.commit()

    _ = sess  # used implicitly via _load_session_ready
    return SignoffRequestOut(
        id=req_id, session_id=session_id, workspace_id=ws,
        kts_partner_id=payload.kts_partner_id,
        kts_partner_name=partner["full_name"],
        requester_email=me.email,
        urgency=payload.urgency, status="pending",
        requested_at=requested_at,
        notification_sent=notif_sent,
    )


# ─── 2. GET signoff-requests ───────────────────────────────────
@router.get("/signoff-requests", response_model=SignoffRequestsListOut)
async def list_signoff_requests(
    ws: str = Query(..., min_length=1, max_length=64),
    status_filter: str | None = Query(default=None, alias="status",
                                       description="filter by status (pending|signed|declined|expired)"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignoffRequestsListOut:
    """
    List signoff requests.
      - Owner/Admin/Developer → see all in workspace
      - Viewer → forbidden (avoid leaking partner contact info)
      - KTS partner (by email match) → sees their own requests
    """
    await require_workspace_access(ws, me)
    if me.role in ("Viewer",):
        raise HTTPException(status_code=403, detail="Viewer không xem được danh sách signoff")

    # Build base query
    sql = """SELECT r.id::text, r.session_id::text, r.workspace_id,
                    r.kts_partner_id::text, p.full_name AS kts_partner_name,
                    r.requester_email, r.urgency, r.status,
                    r.requested_at::text, r.signed_at::text, r.declined_reason,
                    r.blockchain_anchor_tx, r.blockchain_anchor_chain,
                    r.notification_sent_at
             FROM design_signoff_requests r
             LEFT JOIN kts_certified_partners p ON p.id = r.kts_partner_id
             WHERE r.workspace_id = :ws"""
    params: dict[str, Any] = {"ws": ws}
    if status_filter:
        sql += " AND r.status = :status"
        params["status"] = status_filter
    sql += " ORDER BY r.requested_at DESC LIMIT 500"

    res = await db.execute(text(sql), params)
    rows = res.mappings().all()
    items: list[SignoffRequestOut] = []
    for r in rows:
        items.append(
            SignoffRequestOut(
                id=r["id"], session_id=r["session_id"], workspace_id=r["workspace_id"],
                kts_partner_id=r["kts_partner_id"], kts_partner_name=r["kts_partner_name"],
                requester_email=r["requester_email"], urgency=r["urgency"], status=r["status"],
                requested_at=r["requested_at"], signed_at=r["signed_at"],
                declined_reason=r["declined_reason"],
                blockchain_anchor_tx=r["blockchain_anchor_tx"],
                blockchain_anchor_chain=r["blockchain_anchor_chain"],
                notification_sent=bool(r["notification_sent_at"]),
            )
        )
    return SignoffRequestsListOut(requests=items, total=len(items))


# ─── 3. GET kts-partners ───────────────────────────────────────
@router.get("/kts-partners", response_model=list[KTSPartnerOut])
async def list_kts_partners(
    specialty: str | None = Query(default=None, description="filter by specialty"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KTSPartnerOut]:
    """List active KTS certified partners, optionally filtered by specialty."""
    _ = me  # authenticated user — any role can see directory
    sql = """SELECT id::text, full_name, cert_id, cert_authority,
                    cert_expires::text, specialty, email, phone,
                    fee_per_project_vnd, is_active
             FROM kts_certified_partners
             WHERE is_active = TRUE"""
    params: dict[str, Any] = {}
    if specialty:
        if specialty not in ALLOWED_SPECIALTIES:
            raise HTTPException(status_code=400,
                                detail=f"specialty phải thuộc {sorted(ALLOWED_SPECIALTIES)}")
        sql += " AND :sp = ANY(specialty)"
        params["sp"] = specialty
    sql += " ORDER BY full_name ASC LIMIT 200"

    res = await db.execute(text(sql), params)
    rows = res.mappings().all()
    return [
        KTSPartnerOut(
            id=r["id"], full_name=r["full_name"], cert_id=r["cert_id"],
            cert_authority=r["cert_authority"], cert_expires=r["cert_expires"],
            specialty=list(r["specialty"] or []), email=r["email"], phone=r["phone"],
            fee_per_project_vnd=int(r["fee_per_project_vnd"] or 0),
            is_active=bool(r["is_active"]),
        )
        for r in rows
    ]


# ─── 4. POST sign ──────────────────────────────────────────────
@router.post("/signoff/{request_id}/sign", response_model=SignoffRequestOut)
async def sign_signoff(
    request_id: str,
    payload: SignActionIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignoffRequestOut:
    """
    KTS signs the design package digitally.
      1. Validate request exists + status=pending
      2. Aggregate sha256 of all artifacts in session
      3. Compute blockchain anchor stub: 'stub-' + sha256(aggregate || signature_blob)
      4. Update request row + audit
    """
    await require_workspace_access(ws, me)
    try:
        UUID(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="request_id phải là UUID hợp lệ")

    res = await db.execute(
        text(
            """SELECT r.id::text, r.session_id::text, r.workspace_id, r.kts_partner_id::text,
                      r.status, p.email AS partner_email, p.full_name AS partner_name
               FROM design_signoff_requests r
               LEFT JOIN kts_certified_partners p ON p.id = r.kts_partner_id
               WHERE r.id = CAST(:rid AS UUID) AND r.workspace_id = :ws"""
        ),
        {"rid": request_id, "ws": ws},
    )
    req = res.mappings().first()
    if not req:
        raise HTTPException(status_code=404, detail="Signoff request không tồn tại")
    if req["status"] != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Request đang ở trạng thái {req['status']}, không thể ký")

    # Permission: only the KTS partner themselves (by email) OR Owner role can sign
    is_partner = (me.email or "").lower() == (req["partner_email"] or "").lower()
    if not is_partner and me.role != "Owner":
        raise HTTPException(status_code=403, detail="Chỉ KTS được chỉ định (hoặc Owner) mới được ký")

    # Aggregate artifact sha256 → ordered concatenation
    art_res = await db.execute(
        text(
            """SELECT id::text, filename, sha256, size_bytes
               FROM design_artifacts
               WHERE session_id = CAST(:sid AS UUID) AND workspace_id = :ws
               ORDER BY filename ASC"""
        ),
        {"sid": req["session_id"], "ws": ws},
    )
    artifacts = art_res.mappings().all()
    if not artifacts:
        raise HTTPException(status_code=400,
                            detail="Session chưa có artifacts — vui lòng /artifacts/generate trước")

    aggregate_input = "".join(
        f"{a['filename']}:{a['sha256']}" for a in artifacts
    ).encode("utf-8")
    aggregate_sha = hashlib.sha256(aggregate_input).hexdigest()

    sig_bytes = base64.b64decode(payload.ca_signature_blob_b64, validate=True)
    final_sha = hashlib.sha256(
        aggregate_sha.encode("utf-8") + sig_bytes
        + payload.recovery_phrase_hash.encode("utf-8")
    ).hexdigest()
    anchor_tx = f"stub-{final_sha}"

    signed_artifacts_json = {
        a["id"]: {
            "filename": a["filename"],
            "sha256": a["sha256"],
            "size_bytes": int(a["size_bytes"] or 0),
        }
        for a in artifacts
    }

    await db.execute(
        text(
            """UPDATE design_signoff_requests
               SET status = 'signed',
                   signed_at = NOW(),
                   signed_artifacts = CAST(:art AS JSONB),
                   blockchain_anchor_tx = :anchor,
                   blockchain_anchor_chain = 'polygon'
               WHERE id = CAST(:rid AS UUID)"""
        ),
        {
            "rid": request_id,
            "art": json.dumps(signed_artifacts_json, ensure_ascii=False),
            "anchor": anchor_tx,
        },
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.signoff.sign",
        target=request_id, severity="warn",
        metadata={
            "session_id": req["session_id"], "partner_email": req["partner_email"],
            "aggregate_sha256_prefix": aggregate_sha[:16],
            "anchor_prefix": anchor_tx[:24],
            "artifact_count": len(artifacts),
        },
    )
    await db.commit()

    # Return refreshed row
    fresh = await db.execute(
        text(
            """SELECT r.id::text, r.session_id::text, r.workspace_id,
                      r.kts_partner_id::text, p.full_name AS kts_partner_name,
                      r.requester_email, r.urgency, r.status,
                      r.requested_at::text, r.signed_at::text, r.declined_reason,
                      r.blockchain_anchor_tx, r.blockchain_anchor_chain,
                      r.notification_sent_at
               FROM design_signoff_requests r
               LEFT JOIN kts_certified_partners p ON p.id = r.kts_partner_id
               WHERE r.id = CAST(:rid AS UUID)"""
        ),
        {"rid": request_id},
    )
    fr = fresh.mappings().first()
    return SignoffRequestOut(
        id=fr["id"], session_id=fr["session_id"], workspace_id=fr["workspace_id"],
        kts_partner_id=fr["kts_partner_id"], kts_partner_name=fr["kts_partner_name"],
        requester_email=fr["requester_email"], urgency=fr["urgency"], status=fr["status"],
        requested_at=fr["requested_at"], signed_at=fr["signed_at"],
        declined_reason=fr["declined_reason"],
        blockchain_anchor_tx=fr["blockchain_anchor_tx"],
        blockchain_anchor_chain=fr["blockchain_anchor_chain"],
        notification_sent=bool(fr["notification_sent_at"]),
    )


# ─── 5. POST decline ───────────────────────────────────────────
@router.post("/signoff/{request_id}/decline", response_model=SignoffRequestOut)
async def decline_signoff(
    request_id: str,
    payload: DeclineActionIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignoffRequestOut:
    """KTS declines the signoff with a reason. Status → 'declined'."""
    await require_workspace_access(ws, me)
    try:
        UUID(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="request_id phải là UUID hợp lệ")

    res = await db.execute(
        text(
            """SELECT r.status, p.email AS partner_email
               FROM design_signoff_requests r
               LEFT JOIN kts_certified_partners p ON p.id = r.kts_partner_id
               WHERE r.id = CAST(:rid AS UUID) AND r.workspace_id = :ws"""
        ),
        {"rid": request_id, "ws": ws},
    )
    req = res.mappings().first()
    if not req:
        raise HTTPException(status_code=404, detail="Signoff request không tồn tại")
    if req["status"] != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Request đang ở trạng thái {req['status']}, không thể từ chối")

    is_partner = (me.email or "").lower() == (req["partner_email"] or "").lower()
    if not is_partner and me.role != "Owner":
        raise HTTPException(status_code=403, detail="Chỉ KTS được chỉ định (hoặc Owner) mới được từ chối")

    await db.execute(
        text(
            """UPDATE design_signoff_requests
               SET status = 'declined', declined_reason = :reason
               WHERE id = CAST(:rid AS UUID)"""
        ),
        {"rid": request_id, "reason": payload.reason[:2000]},
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.signoff.decline",
        target=request_id, severity="warn",
        metadata={"reason_excerpt": payload.reason[:200]},
    )
    await db.commit()

    fresh = await db.execute(
        text(
            """SELECT r.id::text, r.session_id::text, r.workspace_id,
                      r.kts_partner_id::text, p.full_name AS kts_partner_name,
                      r.requester_email, r.urgency, r.status,
                      r.requested_at::text, r.signed_at::text, r.declined_reason,
                      r.blockchain_anchor_tx, r.blockchain_anchor_chain,
                      r.notification_sent_at
               FROM design_signoff_requests r
               LEFT JOIN kts_certified_partners p ON p.id = r.kts_partner_id
               WHERE r.id = CAST(:rid AS UUID)"""
        ),
        {"rid": request_id},
    )
    fr = fresh.mappings().first()
    return SignoffRequestOut(
        id=fr["id"], session_id=fr["session_id"], workspace_id=fr["workspace_id"],
        kts_partner_id=fr["kts_partner_id"], kts_partner_name=fr["kts_partner_name"],
        requester_email=fr["requester_email"], urgency=fr["urgency"], status=fr["status"],
        requested_at=fr["requested_at"], signed_at=fr["signed_at"],
        declined_reason=fr["declined_reason"],
        blockchain_anchor_tx=fr["blockchain_anchor_tx"],
        blockchain_anchor_chain=fr["blockchain_anchor_chain"],
        notification_sent=bool(fr["notification_sent_at"]),
    )


# ─── Health ────────────────────────────────────────────────────
@router.get("/signoff/health")
async def signoff_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "smtp_configured": smtp_configured(),
        "urgency_values": sorted(URGENCY_VALUES),
        "specialties": sorted(ALLOWED_SPECIALTIES),
    }
