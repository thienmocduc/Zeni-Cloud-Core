"""
Zeni Cloud Core — Admin Access Management API.

Admin-side endpoints quản lý Just-in-Time access vào workspace của khách:

  POST /admin-access/request           — admin (Owner) tạo request → email khách
  POST /admin-access/{id}/release      — admin chủ động giải phóng quyền sớm
  GET  /admin-access/list?status=...   — admin xem toàn bộ requests trên hệ thống
  GET  /admin-access/{id}              — admin xem chi tiết 1 request

Tất cả endpoints YÊU CẦU role Owner (admin Zeni). Customer-facing approve/deny
nằm ở /privacy/admin-access-request/{id}/approve|deny (api/privacy.py).

Khi tạo request, hệ thống:
  1. Insert row admin_access_requests (status=pending)
  2. Audit log
  3. Gửi email cho customer_email với link approve/deny (best-effort, không
     block nếu SMTP fail)

Schema match: backend/migrations/018_privacy_preferences.sql
  - admin_access_requests: customer_workspace_id VARCHAR(32),
    admin_user_id UUID, reason CHECK ('customer_support'|'legal_authority'),
    duration_seconds 21600..86400 (6h..24h)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings  # noqa: F401  (kept for app_url override)
from app.core.deps import CurrentUser, get_current_user, require_role
from app.db.base import get_db
from app.db.models import User, UserWorkspace, Workspace
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.admin_access")

router = APIRouter(prefix="/admin-access", tags=["admin-access"])


# ─────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────
class AdminAccessRequestIn(BaseModel):
    customer_workspace_id: str = Field(..., min_length=2, max_length=32)
    customer_email: EmailStr | None = Field(
        default=None,
        description="Optional override; nếu không có sẽ tự lookup Owner của workspace.",
    )
    scope: str = Field(..., min_length=2, max_length=255,
                       description="ví dụ: 'ws_acme.projects' hoặc 'ws_acme.databases.users_table'")
    reason: str = Field(..., pattern=r"^(customer_support|legal_authority)$")
    reason_detail: str = Field(..., min_length=4, max_length=2000)
    duration_hours: int = Field(default=6, ge=6, le=24)
    court_order_hash: str | None = Field(default=None, max_length=80)
    ticket_url: str | None = Field(default=None, max_length=500,
                                    description="URL helpdesk ticket nếu reason=customer_support")


class AdminAccessRequestOut(BaseModel):
    id: int
    onchain_request_id: int | None
    onchain_tx_hash: str | None
    admin_user_id: str | None
    admin_email: str | None
    customer_workspace_id: str
    scope: str | None
    reason: str
    reason_detail: str | None
    duration_seconds: int
    status: str
    requested_at: datetime | None
    approved_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    court_order_hash: str | None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
async def _resolve_customer_email(
    db: AsyncSession, workspace_id: str, override: str | None
) -> str | None:
    """Override > Owner của workspace > None."""
    if override:
        return str(override).lower()
    row = (await db.execute(text("""
        SELECT u.email
        FROM users u
        JOIN user_workspaces uw ON uw.user_id = u.id
        WHERE uw.workspace_id = :ws AND uw.role = 'Owner'
        ORDER BY u.created_at ASC NULLS LAST
        LIMIT 1
    """), {"ws": workspace_id})).scalar_one_or_none()
    return row


async def _send_approval_email(
    customer_email: str,
    request_id: int,
    workspace_id: str,
    admin_email: str,
    scope: str,
    reason: str,
    reason_detail: str,
    duration_hours: int,
    ticket_url: str | None,
) -> bool:
    """Best-effort email. Returns True if sent, False otherwise (never raises)."""
    try:
        # Import lazily — nếu services.email không tồn tại, fail mềm
        from app.services.email import send_email  # type: ignore
    except Exception as e:
        log.warning("[admin_access] email module unavailable: %s", e)
        return False

    base = "https://zenicloud.io/app#/privacy/admin-access"
    approve_url = f"{base}/{request_id}/approve"
    deny_url = f"{base}/{request_id}/deny"
    ticket_html = (
        f'<p><strong>Ticket:</strong> <a href="{ticket_url}">{ticket_url}</a></p>'
        if ticket_url else ""
    )
    reason_label = "Hỗ trợ kỹ thuật" if reason == "customer_support" else "Yêu cầu pháp lý"

    subject = f"[Zeni Cloud] Yêu cầu truy cập dữ liệu workspace {workspace_id}"
    body_html = f"""
<!DOCTYPE html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; background: #fafafa; color: #1a1a1a;">
  <div style="background: #08051F; padding: 24px; border-radius: 12px; text-align: center;">
    <h1 style="color: #FAF5FF; margin: 0; font-size: 22px;">Zeni Cloud · Yêu cầu truy cập</h1>
    <p style="color: #C4B5FD; margin: 6px 0 0; font-size: 12px; letter-spacing: 0.1em;">JUST-IN-TIME ACCESS</p>
  </div>
  <div style="background: white; padding: 28px; border-radius: 12px; margin-top: 16px;">
    <h2 style="color: #1a0938; margin: 0 0 12px; font-size: 18px;">Admin Zeni cần truy cập dữ liệu workspace của bạn</h2>
    <p style="margin: 0 0 8px;"><strong>Workspace:</strong> {workspace_id}</p>
    <p style="margin: 0 0 8px;"><strong>Admin:</strong> {admin_email}</p>
    <p style="margin: 0 0 8px;"><strong>Lý do:</strong> {reason_label}</p>
    <p style="margin: 0 0 8px;"><strong>Phạm vi:</strong> <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;">{scope}</code></p>
    <p style="margin: 0 0 8px;"><strong>Thời hạn:</strong> {duration_hours} giờ (sau đó tự động đóng)</p>
    <p style="margin: 0 0 12px;"><strong>Chi tiết:</strong> {reason_detail}</p>
    {ticket_html}
    <div style="text-align: center; margin: 28px 0 12px;">
      <a href="{approve_url}" style="display:inline-block;padding:12px 28px;background:linear-gradient(135deg,#FDE68A,#F59E0B);color:#1a0938;text-decoration:none;font-weight:700;border-radius:8px;margin-right:8px;">DUYỆT</a>
      <a href="{deny_url}" style="display:inline-block;padding:12px 28px;background:#1f2937;color:#fff;text-decoration:none;font-weight:700;border-radius:8px;">TỪ CHỐI</a>
    </div>
    <p style="color:#6b7280;font-size:12px;margin-top:20px;line-height:1.5;">
      Mọi hành động truy cập đều được log đầy đủ và (sau khi launch on-chain governance) sẽ được ghi nhận trên Polygon. Bạn có thể xem toàn bộ lịch sử tại
      <a href="https://zenicloud.io/app#/privacy">Cài đặt → Quyền riêng tư</a>.
    </p>
  </div>
  <div style="text-align:center;margin-top:20px;color:#9ca3af;font-size:11px;">
    Sản phẩm của <strong>Zeni Holdings</strong> · zenicloud.io
  </div>
</body></html>
"""
    try:
        return bool(await send_email(to=customer_email, subject=subject, body_html=body_html))
    except Exception as e:
        log.warning("[admin_access] send_email failed: %s", e)
        return False


def _row_to_out(row: dict) -> AdminAccessRequestOut:
    return AdminAccessRequestOut(
        id=row["id"],
        onchain_request_id=row.get("onchain_request_id"),
        onchain_tx_hash=row.get("onchain_tx_hash"),
        admin_user_id=str(row["admin_user_id"]) if row.get("admin_user_id") else None,
        admin_email=row.get("admin_email"),
        customer_workspace_id=row["customer_workspace_id"],
        scope=row.get("scope"),
        reason=row["reason"],
        reason_detail=row.get("reason_detail"),
        duration_seconds=int(row["duration_seconds"]),
        status=row["status"],
        requested_at=row.get("requested_at"),
        approved_at=row.get("approved_at"),
        expires_at=row.get("expires_at"),
        revoked_at=row.get("revoked_at"),
        court_order_hash=row.get("court_order_hash"),
    )


# ─────────────────────────────────────────────────────────────
# POST /admin-access/request
# ─────────────────────────────────────────────────────────────
@router.post("/request", response_model=AdminAccessRequestOut, status_code=201)
async def create_request(
    payload: AdminAccessRequestIn,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestOut:
    """
    Admin tạo yêu cầu truy cập workspace của khách (Just-in-Time).
    - Status mặc định = pending. Customer phải approve qua /privacy/admin-access-request/{id}/approve.
    - reason='legal_authority' bắt buộc có court_order_hash.
    - Email approval được gửi best-effort (không block nếu SMTP fail).
    """
    # Validate workspace tồn tại
    ws_obj = (await db.execute(
        select(Workspace).where(Workspace.id == payload.customer_workspace_id)
    )).scalar_one_or_none()
    if ws_obj is None:
        raise HTTPException(status_code=404, detail=f"workspace {payload.customer_workspace_id} not found")

    # Legal authority phải có court order hash
    if payload.reason == "legal_authority" and not payload.court_order_hash:
        raise HTTPException(status_code=400, detail="legal_authority cần court_order_hash")

    duration_seconds = payload.duration_hours * 3600
    if not (21600 <= duration_seconds <= 86400):
        raise HTTPException(status_code=400, detail="duration_hours phải trong [6, 24]")

    # Resolve customer email (override > Owner của workspace)
    customer_email = await _resolve_customer_email(db, payload.customer_workspace_id, payload.customer_email)

    # Insert request
    try:
        row = (await db.execute(text("""
            INSERT INTO admin_access_requests
                (admin_user_id, customer_workspace_id, scope, reason, reason_detail,
                 duration_seconds, court_order_hash)
            VALUES (:uid, :ws, :scope, :reason, :detail, :dur, :coh)
            RETURNING id, onchain_request_id, onchain_tx_hash, admin_user_id,
                      customer_workspace_id, scope, reason, reason_detail,
                      duration_seconds, status, requested_at, approved_at,
                      expires_at, revoked_at, court_order_hash
        """), {
            "uid": str(me.id),
            "ws": payload.customer_workspace_id,
            "scope": payload.scope,
            "reason": payload.reason,
            "detail": payload.reason_detail,
            "dur": duration_seconds,
            "coh": payload.court_order_hash,
        })).mappings().first()
    except Exception as e:
        log.exception("[admin_access] insert failed")
        raise HTTPException(status_code=500, detail=f"create failed: {e}") from e

    if row is None:
        raise HTTPException(status_code=500, detail="insert returned no row")

    request_id = int(row["id"])

    await audit_push(
        db, actor=me.email, workspace_id=payload.customer_workspace_id,
        action="admin_access.request",
        target=f"request_id={request_id}",
        severity="warn",
        metadata={
            "scope": payload.scope,
            "reason": payload.reason,
            "duration_hours": payload.duration_hours,
            "court_order_hash": payload.court_order_hash,
            "ticket_url": payload.ticket_url,
            "customer_email": customer_email,
        },
    )
    await db.commit()

    # Email best-effort cho customer_support; legal_authority sẽ qua on-chain multi-sig
    email_sent = False
    if payload.reason == "customer_support" and customer_email:
        email_sent = await _send_approval_email(
            customer_email=customer_email,
            request_id=request_id,
            workspace_id=payload.customer_workspace_id,
            admin_email=me.email,
            scope=payload.scope,
            reason=payload.reason,
            reason_detail=payload.reason_detail,
            duration_hours=payload.duration_hours,
            ticket_url=payload.ticket_url,
        )
        if email_sent:
            try:
                await audit_push(
                    db, actor=me.email, workspace_id=payload.customer_workspace_id,
                    action="admin_access.email_sent",
                    target=f"request_id={request_id}",
                    severity="info",
                    metadata={"to": customer_email},
                )
                await db.commit()
            except Exception:
                await db.rollback()

    out = _row_to_out({**dict(row), "admin_email": me.email})
    # Mượn dict trả về 1 chỗ duy nhất; không thay đổi schema
    return out


# ─────────────────────────────────────────────────────────────
# POST /admin-access/{id}/release
# ─────────────────────────────────────────────────────────────
@router.post("/{request_id}/release")
async def release_access(
    request_id: int,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Admin chủ động release quyền truy cập sớm (trước expires_at).
    Chỉ release request status='approved' về 'revoked'.
    """
    row = (await db.execute(text("""
        SELECT id, customer_workspace_id, status, admin_user_id
        FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="request not found")
    if row["status"] != "approved":
        raise HTTPException(status_code=400, detail=f"request status = {row['status']}, không thể release")

    now = datetime.now(timezone.utc)
    try:
        await db.execute(text("""
            UPDATE admin_access_requests
            SET status = 'revoked', revoked_at = :now
            WHERE id = :id AND status = 'approved'
        """), {"id": request_id, "now": now})
    except Exception as e:
        log.exception("[admin_access] release failed")
        raise HTTPException(status_code=500, detail=f"release failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=row["customer_workspace_id"],
        action="admin_access.release",
        target=f"request_id={request_id}",
        severity="warn",
        metadata={"released_by": me.email},
    )
    await db.commit()
    return {"ok": True, "request_id": request_id, "status": "revoked", "revoked_at": now.isoformat()}


# ─────────────────────────────────────────────────────────────
# GET /admin-access/list
# ─────────────────────────────────────────────────────────────
@router.get("/list", response_model=list[AdminAccessRequestOut])
async def list_requests(
    status: str = Query("all", pattern=r"^(all|pending|approved|revoked|expired)$"),
    workspace_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=200, ge=1, le=1000),
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> list[AdminAccessRequestOut]:
    """Admin xem toàn bộ requests. Filter optional theo status / workspace_id."""
    where: list[str] = []
    params: dict = {"lim": limit}
    if status != "all":
        where.append("r.status = :st")
        params["st"] = status
    if workspace_id:
        where.append("r.customer_workspace_id = :ws")
        params["ws"] = workspace_id
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = (await db.execute(text(f"""
        SELECT r.*, u.email AS admin_email
        FROM admin_access_requests r
        LEFT JOIN users u ON u.id = r.admin_user_id
        {where_sql}
        ORDER BY r.requested_at DESC
        LIMIT :lim
    """), params)).mappings().all()
    return [_row_to_out(dict(r)) for r in rows]


# ─────────────────────────────────────────────────────────────
# GET /admin-access/{id}
# ─────────────────────────────────────────────────────────────
@router.get("/{request_id}", response_model=AdminAccessRequestOut)
async def get_request(
    request_id: int,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestOut:
    """Admin xem chi tiết 1 request."""
    row = (await db.execute(text("""
        SELECT r.*, u.email AS admin_email
        FROM admin_access_requests r
        LEFT JOIN users u ON u.id = r.admin_user_id
        WHERE r.id = :id
    """), {"id": request_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="request not found")
    return _row_to_out(dict(row))


# ─────────────────────────────────────────────────────────────
# Background sweep helper (gọi từ cron) — đánh dấu expired
# ─────────────────────────────────────────────────────────────
async def sweep_expired_requests(db: AsyncSession) -> int:
    """
    Cron-callable helper: đánh dấu mọi approved request có expires_at < NOW() thành 'expired'.
    Trả về số request bị mark.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(text("""
        UPDATE admin_access_requests
        SET status = 'expired'
        WHERE status = 'approved' AND expires_at IS NOT NULL AND expires_at < :now
        RETURNING id
    """), {"now": now})
    rows = result.fetchall()
    if rows:
        log.info("[admin_access] swept %d expired requests", len(rows))
    await db.commit()
    return len(rows)


__all__ = ["router", "sweep_expired_requests"]
