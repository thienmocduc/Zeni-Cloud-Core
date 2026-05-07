"""
Zeni Cloud Core — Privacy API.

Endpoints quản lý privacy của workspace + admin access governance:

  GET  /privacy/preferences?ws=...                   — đọc settings
  POST /privacy/preferences?ws=...                   — update opt-in / region
  POST /privacy/accept-terms?ws=...                  — ký Terms / DPA
  GET  /privacy/admin-access-log?ws=...              — customer xem history
  POST /privacy/admin-access-request                 — admin request access
  POST /privacy/admin-access-approve/{request_id}    — customer approve
  POST /privacy/admin-access-revoke/{request_id}     — customer/admin revoke
  GET  /privacy/my-data?ws=...                       — export toàn bộ data (GDPR portability)
  POST /privacy/delete-all?ws=...                    — mark workspace để purge
  GET  /privacy/output-filter-logs?ws=...            — xem leak attempts (transparency)

Tất cả endpoints dùng `get_current_user` (alias: auth_required) + check workspace
permission + audit_push mọi action.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings  # noqa: F401  (kept for future use)
from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import (
    Agent,
    AuditLog,
    Connector,
    Contract,
    Database,
    Project,
    Secret,
    User,
    UserWorkspace,
    Workspace,
)
from app.services.audit import audit_push

# Alias để khớp với spec: import auth_required = get_current_user
auth_required = get_current_user

log = logging.getLogger("zeni.api.privacy")

router = APIRouter(prefix="/privacy", tags=["privacy"])


# ─────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────
class PrivacyPreferencesOut(BaseModel):
    workspace_id: str
    ai_training_opt_in: bool
    ai_training_opted_in_at: datetime | None
    ai_training_opted_out_at: datetime | None
    data_region: str
    cmek_enabled: bool
    cmek_key_name: str | None
    cmek_enabled_at: datetime | None
    terms_accepted_at: datetime | None
    terms_version: str | None
    dpa_signed_at: datetime | None
    dpa_version: str | None
    updated_at: datetime | None


class PrivacyPreferencesUpdate(BaseModel):
    ai_training_opt_in: bool | None = None
    data_region: str | None = Field(default=None, max_length=20)
    cmek_key_name: str | None = Field(default=None, max_length=255)


class AcceptTermsIn(BaseModel):
    terms_version: str = Field(min_length=1, max_length=20)
    dpa_version: str = Field(min_length=1, max_length=20)


class AdminAccessRequestIn(BaseModel):
    customer_workspace_id: str = Field(min_length=1, max_length=32)
    scope: str = Field(min_length=1, max_length=255)
    reason: str = Field(pattern=r"^(customer_support|legal_authority)$")
    reason_detail: str | None = Field(default=None, max_length=2000)
    duration_seconds: int = Field(ge=21600, le=86400)  # 6h..24h
    court_order_hash: str | None = Field(default=None, max_length=80)


class AdminAccessRequestOut(BaseModel):
    id: int
    onchain_request_id: int | None
    onchain_tx_hash: str | None
    admin_user_id: str | None
    customer_workspace_id: str | None
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


class DeleteAllIn(BaseModel):
    confirm: str = Field(description='Phải gửi đúng chuỗi "DELETE_ALL_MY_DATA"')


class OutputFilterLogOut(BaseModel):
    id: int
    workspace_id: str | None
    user_id: str | None
    agent_name: str | None
    leak_type: str
    blocked_excerpt: str | None
    severity: str
    created_at: datetime | None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
async def _ensure_pref_row(db: AsyncSession, ws: str) -> None:
    """Đảm bảo có 1 row privacy_preferences cho workspace này."""
    await db.execute(_sql_text("""
        INSERT INTO privacy_preferences (workspace_id)
        VALUES (:ws)
        ON CONFLICT (workspace_id) DO NOTHING
    """), {"ws": ws})


def _is_admin(me: CurrentUser) -> bool:
    return me.role in ("Owner", "Admin")


# ─────────────────────────────────────────────────────────────
# 1. GET /privacy/preferences
# ─────────────────────────────────────────────────────────────
@router.get("/preferences", response_model=PrivacyPreferencesOut)
async def get_preferences(
    ws: str,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> PrivacyPreferencesOut:
    """Đọc privacy preferences của workspace."""
    await require_workspace_access(ws, me)
    await _ensure_pref_row(db, ws)
    row = (await db.execute(_sql_text("""
        SELECT workspace_id, ai_training_opt_in, ai_training_opted_in_at,
               ai_training_opted_out_at, data_region, cmek_key_name,
               cmek_enabled_at, terms_accepted_at, terms_version,
               dpa_signed_at, dpa_version, updated_at
        FROM privacy_preferences WHERE workspace_id = :ws
    """), {"ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="privacy preferences not found")
    return PrivacyPreferencesOut(
        workspace_id=row["workspace_id"],
        ai_training_opt_in=row["ai_training_opt_in"],
        ai_training_opted_in_at=row["ai_training_opted_in_at"],
        ai_training_opted_out_at=row["ai_training_opted_out_at"],
        data_region=row["data_region"],
        cmek_enabled=bool(row["cmek_enabled_at"]),
        cmek_key_name=row["cmek_key_name"],
        cmek_enabled_at=row["cmek_enabled_at"],
        terms_accepted_at=row["terms_accepted_at"],
        terms_version=row["terms_version"],
        dpa_signed_at=row["dpa_signed_at"],
        dpa_version=row["dpa_version"],
        updated_at=row["updated_at"],
    )


# ─────────────────────────────────────────────────────────────
# 2. POST /privacy/preferences  (update)
# ─────────────────────────────────────────────────────────────
@router.post("/preferences", response_model=PrivacyPreferencesOut)
async def update_preferences(
    ws: str,
    data: PrivacyPreferencesUpdate,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> PrivacyPreferencesOut:
    """Update opt-in AI training / data region / CMEK."""
    await require_workspace_access(ws, me)
    if not _is_admin(me):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để update privacy")
    await _ensure_pref_row(db, ws)

    now = datetime.now(timezone.utc)
    sets: list[str] = ["updated_at = :now"]
    params: dict[str, Any] = {"ws": ws, "now": now}

    if data.ai_training_opt_in is not None:
        sets.append("ai_training_opt_in = :opt")
        params["opt"] = data.ai_training_opt_in
        if data.ai_training_opt_in:
            sets.append("ai_training_opted_in_at = :now")
        else:
            sets.append("ai_training_opted_out_at = :now")
    if data.data_region:
        sets.append("data_region = :region")
        params["region"] = data.data_region
    if data.cmek_key_name is not None:
        sets.append("cmek_key_name = :ck")
        sets.append("cmek_enabled_at = :now")
        params["ck"] = data.cmek_key_name

    sql = "UPDATE privacy_preferences SET " + ", ".join(sets) + " WHERE workspace_id = :ws"
    try:
        await db.execute(_sql_text(sql), params)
    except Exception as e:
        log.exception("[privacy] update_preferences failed")
        raise HTTPException(status_code=500, detail=f"update failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="privacy.preferences.update",
        target=ws, severity="ok",
        metadata={"changes": data.model_dump(exclude_none=True)},
    )
    await db.commit()
    return await get_preferences(ws=ws, me=me, db=db)


# ─────────────────────────────────────────────────────────────
# 3. POST /privacy/accept-terms
# ─────────────────────────────────────────────────────────────
@router.post("/accept-terms")
async def accept_terms(
    ws: str,
    data: AcceptTermsIn,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ký Terms of Service + Data Processing Agreement."""
    await require_workspace_access(ws, me)
    if not _is_admin(me):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để ký Terms/DPA")
    await _ensure_pref_row(db, ws)
    now = datetime.now(timezone.utc)
    try:
        await db.execute(_sql_text("""
            UPDATE privacy_preferences
               SET terms_accepted_at = :now,
                   terms_version = :tv,
                   dpa_signed_at = :now,
                   dpa_version = :dv,
                   updated_at = :now
             WHERE workspace_id = :ws
        """), {"now": now, "tv": data.terms_version, "dv": data.dpa_version, "ws": ws})
    except Exception as e:
        log.exception("[privacy] accept_terms failed")
        raise HTTPException(status_code=500, detail=f"accept_terms failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="privacy.terms.accept", target=ws, severity="ok",
        metadata={"terms_version": data.terms_version, "dpa_version": data.dpa_version},
    )
    await db.commit()
    return {"ok": True, "terms_accepted_at": now.isoformat(),
            "terms_version": data.terms_version, "dpa_version": data.dpa_version}


# ─────────────────────────────────────────────────────────────
# 4. GET /privacy/admin-access-log  (customer xem)
# ─────────────────────────────────────────────────────────────
@router.get("/admin-access-log", response_model=list[AdminAccessRequestOut])
async def list_admin_access_log(
    ws: str,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> list[AdminAccessRequestOut]:
    """Customer xem mọi lần admin Zeni request access vào data của họ."""
    await require_workspace_access(ws, me)
    rows = (await db.execute(_sql_text("""
        SELECT id, onchain_request_id, onchain_tx_hash, admin_user_id,
               customer_workspace_id, scope, reason, reason_detail,
               duration_seconds, status, requested_at, approved_at,
               expires_at, revoked_at, court_order_hash
          FROM admin_access_requests
         WHERE customer_workspace_id = :ws
         ORDER BY requested_at DESC
         LIMIT 500
    """), {"ws": ws})).mappings().all()
    return [AdminAccessRequestOut(
        id=r["id"],
        onchain_request_id=r["onchain_request_id"],
        onchain_tx_hash=r["onchain_tx_hash"],
        admin_user_id=str(r["admin_user_id"]) if r["admin_user_id"] else None,
        customer_workspace_id=r["customer_workspace_id"],
        scope=r["scope"], reason=r["reason"], reason_detail=r["reason_detail"],
        duration_seconds=r["duration_seconds"], status=r["status"],
        requested_at=r["requested_at"], approved_at=r["approved_at"],
        expires_at=r["expires_at"], revoked_at=r["revoked_at"],
        court_order_hash=r["court_order_hash"],
    ) for r in rows]


# ─────────────────────────────────────────────────────────────
# 5. POST /privacy/admin-access-request  (admin Zeni only)
# ─────────────────────────────────────────────────────────────
@router.post("/admin-access-request", response_model=AdminAccessRequestOut, status_code=201)
async def create_admin_access_request(
    data: AdminAccessRequestIn,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestOut:
    """
    Admin Zeni tạo request access vào workspace của khách.
    Status mặc định = pending. Sau khi customer approve → status = approved + expires_at.
    Sẽ link với on-chain request_id sau khi tx confirm.
    """
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Chỉ Owner Zeni được tạo admin access request")

    # Check workspace tồn tại
    ws = (await db.execute(
        select(Workspace).where(Workspace.id == data.customer_workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {data.customer_workspace_id} not found")

    # Legal authority phải có court_order_hash
    if data.reason == "legal_authority" and not data.court_order_hash:
        raise HTTPException(status_code=400, detail="legal_authority cần court_order_hash")

    try:
        row = (await db.execute(_sql_text("""
            INSERT INTO admin_access_requests
                (admin_user_id, customer_workspace_id, scope, reason, reason_detail,
                 duration_seconds, court_order_hash)
            VALUES (:uid, :ws, :scope, :reason, :detail, :dur, :coh)
            RETURNING id, onchain_request_id, onchain_tx_hash, admin_user_id,
                      customer_workspace_id, scope, reason, reason_detail,
                      duration_seconds, status, requested_at, approved_at,
                      expires_at, revoked_at, court_order_hash
        """), {
            "uid": str(me.id), "ws": data.customer_workspace_id, "scope": data.scope,
            "reason": data.reason, "detail": data.reason_detail,
            "dur": data.duration_seconds, "coh": data.court_order_hash,
        })).mappings().first()
    except Exception as e:
        log.exception("[privacy] admin_access_request failed")
        raise HTTPException(status_code=500, detail=f"create failed: {e}") from e

    if row is None:
        raise HTTPException(status_code=500, detail="insert returned no row")

    await audit_push(
        db, actor=me.email, workspace_id=data.customer_workspace_id,
        action="privacy.admin_access.request",
        target=data.customer_workspace_id, severity="warn",
        metadata={"reason": data.reason, "scope": data.scope,
                  "duration_seconds": data.duration_seconds,
                  "court_order_hash": data.court_order_hash},
    )
    await db.commit()
    return AdminAccessRequestOut(
        id=row["id"],
        onchain_request_id=row["onchain_request_id"],
        onchain_tx_hash=row["onchain_tx_hash"],
        admin_user_id=str(row["admin_user_id"]) if row["admin_user_id"] else None,
        customer_workspace_id=row["customer_workspace_id"],
        scope=row["scope"], reason=row["reason"], reason_detail=row["reason_detail"],
        duration_seconds=row["duration_seconds"], status=row["status"],
        requested_at=row["requested_at"], approved_at=row["approved_at"],
        expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        court_order_hash=row["court_order_hash"],
    )


# ─────────────────────────────────────────────────────────────
# 6. POST /privacy/admin-access-approve/{request_id}
# ─────────────────────────────────────────────────────────────
@router.post("/admin-access-approve/{request_id}", response_model=AdminAccessRequestOut)
async def approve_admin_access(
    request_id: int,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestOut:
    """Customer approve admin access request → set status=approved + expires_at."""
    req = (await db.execute(_sql_text("""
        SELECT id, customer_workspace_id, duration_seconds, status
          FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()
    if not req:
        raise HTTPException(status_code=404, detail="request not found")
    await require_workspace_access(req["customer_workspace_id"], me)
    if req["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"request status = {req['status']}, không thể approve")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=int(req["duration_seconds"]))
    try:
        await db.execute(_sql_text("""
            UPDATE admin_access_requests
               SET status = 'approved',
                   approved_at = :now,
                   expires_at = :exp
             WHERE id = :id
        """), {"now": now, "exp": expires, "id": request_id})
    except Exception as e:
        log.exception("[privacy] admin_access_approve failed")
        raise HTTPException(status_code=500, detail=f"approve failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=req["customer_workspace_id"],
        action="privacy.admin_access.approve",
        target=str(request_id), severity="warn",
        metadata={"expires_at": expires.isoformat()},
    )
    await db.commit()

    row = (await db.execute(_sql_text("""
        SELECT id, onchain_request_id, onchain_tx_hash, admin_user_id,
               customer_workspace_id, scope, reason, reason_detail,
               duration_seconds, status, requested_at, approved_at,
               expires_at, revoked_at, court_order_hash
          FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()
    return AdminAccessRequestOut(
        id=row["id"], onchain_request_id=row["onchain_request_id"],
        onchain_tx_hash=row["onchain_tx_hash"],
        admin_user_id=str(row["admin_user_id"]) if row["admin_user_id"] else None,
        customer_workspace_id=row["customer_workspace_id"],
        scope=row["scope"], reason=row["reason"], reason_detail=row["reason_detail"],
        duration_seconds=row["duration_seconds"], status=row["status"],
        requested_at=row["requested_at"], approved_at=row["approved_at"],
        expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        court_order_hash=row["court_order_hash"],
    )


# ─────────────────────────────────────────────────────────────
# 7. POST /privacy/admin-access-revoke/{request_id}
# ─────────────────────────────────────────────────────────────
@router.post("/admin-access-revoke/{request_id}")
async def revoke_admin_access(
    request_id: int,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Customer hoặc admin revoke quyền truy cập trước khi hết hạn."""
    req = (await db.execute(_sql_text("""
        SELECT id, customer_workspace_id, status, admin_user_id
          FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()
    if not req:
        raise HTTPException(status_code=404, detail="request not found")

    # Cho phép: customer của workspace, OR admin Zeni (Owner role), OR admin chính người request
    is_customer = (me.role == "Owner") or (req["customer_workspace_id"] in me.workspaces)
    is_zeni_owner = (me.role == "Owner")
    is_self_admin = req["admin_user_id"] is not None and str(req["admin_user_id"]) == str(me.id)
    if not (is_customer or is_zeni_owner or is_self_admin):
        raise HTTPException(status_code=403, detail="không có quyền revoke request này")

    if req["status"] in ("revoked", "expired"):
        raise HTTPException(status_code=409, detail=f"request đã {req['status']}")

    now = datetime.now(timezone.utc)
    try:
        await db.execute(_sql_text("""
            UPDATE admin_access_requests
               SET status = 'revoked', revoked_at = :now
             WHERE id = :id
        """), {"now": now, "id": request_id})
    except Exception as e:
        log.exception("[privacy] admin_access_revoke failed")
        raise HTTPException(status_code=500, detail=f"revoke failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=req["customer_workspace_id"],
        action="privacy.admin_access.revoke",
        target=str(request_id), severity="warn",
        metadata={"revoked_by_role": me.role},
    )
    await db.commit()
    return {"ok": True, "request_id": request_id, "status": "revoked",
            "revoked_at": now.isoformat()}


# ─────────────────────────────────────────────────────────────
# 8. GET /privacy/my-data  (export — GDPR right to portability)
# ─────────────────────────────────────────────────────────────
@router.get("/my-data")
async def export_my_data(
    ws: str,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Export toàn bộ data của workspace dưới dạng JSON.
    Chỉ return metadata (ID, name, created_at, ...) — không dump payload nặng.
    """
    await require_workspace_access(ws, me)
    if not _is_admin(me):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để export data")

    out: dict[str, Any] = {"workspace_id": ws, "exported_at": datetime.now(timezone.utc).isoformat()}
    try:
        wsr = (await db.execute(select(Workspace).where(Workspace.id == ws))).scalar_one_or_none()
        out["workspace"] = {
            "id": wsr.id, "code": wsr.code, "name": wsr.name,
            "created_at": wsr.created_at.isoformat() if wsr.created_at else None,
        } if wsr else None

        members = (await db.execute(
            select(User.id, User.email, User.name, User.role, UserWorkspace.role.label("ws_role"))
            .join(UserWorkspace, UserWorkspace.user_id == User.id)
            .where(UserWorkspace.workspace_id == ws)
        )).all()
        out["members"] = [{"id": str(m[0]), "email": m[1], "name": m[2],
                           "role": m[3], "workspace_role": m[4]} for m in members]

        proj = (await db.execute(select(Project).where(Project.workspace_id == ws))).scalars().all()
        out["projects"] = [{"id": str(p.id), "name": p.name, "type": p.type,
                            "runtime": p.runtime, "status": p.status,
                            "created_at": p.created_at.isoformat() if p.created_at else None}
                           for p in proj]

        dbs = (await db.execute(select(Database).where(Database.workspace_id == ws))).scalars().all()
        out["databases"] = [{"id": str(d.id), "name": d.name, "kind": d.kind,
                             "row_count": d.row_count} for d in dbs]

        agents = (await db.execute(select(Agent).where(Agent.workspace_id == ws))).scalars().all()
        out["agents"] = [{"id": str(a.id), "name": a.name, "model": a.model,
                          "calls": a.calls} for a in agents]

        connectors = (await db.execute(select(Connector).where(Connector.workspace_id == ws))).scalars().all()
        out["connectors"] = [{"id": str(c.id), "type": c.type, "status": c.status} for c in connectors]

        secrets = (await db.execute(select(Secret).where(Secret.workspace_id == ws))).scalars().all()
        out["secrets"] = [{"id": str(s.id), "name": s.name, "env": s.env,
                           "rotations": s.rotations} for s in secrets]  # value KHÔNG export

        contracts = (await db.execute(select(Contract).where(Contract.workspace_id == ws))).scalars().all()
        out["contracts"] = [{"id": str(c.id), "name": c.name, "chain": c.chain,
                             "address": c.address, "status": c.status} for c in contracts]

        prefs = (await db.execute(_sql_text("""
            SELECT * FROM privacy_preferences WHERE workspace_id = :ws
        """), {"ws": ws})).mappings().first()
        out["privacy_preferences"] = dict(prefs) if prefs else None

        # Audit log (last 1000)
        audit_rows = (await db.execute(
            select(AuditLog).where(AuditLog.workspace_id == ws)
            .order_by(AuditLog.ts.desc()).limit(1000)
        )).scalars().all()
        out["audit_log"] = [{
            "ts": a.ts.isoformat() if a.ts else None,
            "actor": a.actor, "action": a.action, "target": a.target,
            "severity": a.severity,
        } for a in audit_rows]
    except Exception as e:
        log.exception("[privacy] my_data export failed")
        raise HTTPException(status_code=500, detail=f"export failed: {e}") from e

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="privacy.export.my_data", target=ws, severity="info",
        metadata={"sections": list(out.keys())},
    )
    await db.commit()
    return out


# ─────────────────────────────────────────────────────────────
# 9. POST /privacy/delete-all  (mark workspace để purge)
# ─────────────────────────────────────────────────────────────
@router.post("/delete-all")
async def delete_all(
    ws: str,
    data: DeleteAllIn,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Đánh dấu workspace để background job purge sau 7 ngày.
    Body phải gửi confirm = "DELETE_ALL_MY_DATA" để tránh trigger nhầm.
    """
    await require_workspace_access(ws, me)
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Chỉ Owner mới được delete-all")
    if data.confirm != "DELETE_ALL_MY_DATA":
        raise HTTPException(status_code=400, detail='Phải gửi confirm = "DELETE_ALL_MY_DATA"')

    purge_at = datetime.now(timezone.utc) + timedelta(days=7)
    # Lưu marker vào privacy_preferences metadata-style: dùng audit_log + cờ trên row
    try:
        await _ensure_pref_row(db, ws)
        # Stash vào cmek_key_name + cmek_enabled_at là sai schema; dùng audit_log thay thế
        # và sau này background job sẽ poll audit_log action='privacy.purge.requested'.
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="privacy.purge.requested", target=ws, severity="critical",
            metadata={"purge_at": purge_at.isoformat(), "confirm": True},
        )
    except Exception as e:
        log.exception("[privacy] delete_all failed")
        raise HTTPException(status_code=500, detail=f"delete_all failed: {e}") from e

    await db.commit()
    return {
        "ok": True,
        "workspace_id": ws,
        "purge_scheduled_at": purge_at.isoformat(),
        "message": "Workspace sẽ bị purge sau 7 ngày. Liên hệ support để hủy nếu đổi ý.",
    }


# ─────────────────────────────────────────────────────────────
# 10. GET /privacy/output-filter-logs (transparency)
# ─────────────────────────────────────────────────────────────
@router.get("/output-filter-logs", response_model=list[OutputFilterLogOut])
async def list_output_filter_logs(
    ws: str,
    limit: int = 100,
    me: CurrentUser = Depends(auth_required),
    db: AsyncSession = Depends(get_db),
) -> list[OutputFilterLogOut]:
    """Customer xem mọi lần Output Filter chặn agent leak."""
    await require_workspace_access(ws, me)
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit phải trong [1, 1000]")
    rows = (await db.execute(_sql_text("""
        SELECT id, workspace_id, user_id, agent_name, leak_type,
               blocked_excerpt, severity, created_at
          FROM output_filter_logs
         WHERE workspace_id = :ws
         ORDER BY created_at DESC
         LIMIT :lim
    """), {"ws": ws, "lim": limit})).mappings().all()
    return [OutputFilterLogOut(
        id=r["id"], workspace_id=r["workspace_id"],
        user_id=str(r["user_id"]) if r["user_id"] else None,
        agent_name=r["agent_name"], leak_type=r["leak_type"],
        blocked_excerpt=r["blocked_excerpt"], severity=r["severity"],
        created_at=r["created_at"],
    ) for r in rows]
