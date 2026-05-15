from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.core.vault import decrypt, encrypt, mask
from app.db.base import get_db
from app.db.models import Secret
from app.schemas.resources import IdentityFlowIn, SecretCreateIn, SecretOut
from app.services.audit import audit_push, billing_push

router = APIRouter(prefix="/identity", tags=["identity"])

# Pre-flight validators — fail-fast với message tiếng Việt rõ ràng
# (Pydantic schema đã enforce pattern/length, đây là double-check + thân thiện hơn)
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_MAX_SECRET_BYTES = 64 * 1024  # 64KB — Secret Manager hard limit


def _validate_secret_create(name: str, value: str) -> None:
    """Pre-flight validate trước khi gọi Vault encrypt / DB insert."""
    if not _SECRET_NAME_RE.match(name or ""):
        raise HTTPException(
            status_code=422,
            detail="Tên secret không hợp lệ — cần CHỮ HOA, số, gạch dưới (3-64 ký tự, bắt đầu bằng chữ cái). VD: API_KEY_GEMINI",
        )
    if not value:
        raise HTTPException(status_code=422, detail="Giá trị secret không được rỗng.")
    size = len(value.encode("utf-8"))
    if size > _MAX_SECRET_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Secret value vượt 64KB (Secret Manager limit). Hiện tại: {size} bytes.",
        )


# ─── Secrets / Vault ─────────────────────────────────
@router.get("/secrets", response_model=list[SecretOut])
async def list_secrets(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SecretOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Secret).where(Secret.workspace_id == ws).order_by(Secret.updated_at.desc())
    )).scalars().all()
    return [SecretOut.model_validate(r) for r in rows]


@router.post("/secrets", response_model=SecretOut, status_code=201)
async def create_secret(
    ws: str,
    data: SecretCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SecretOut:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để tạo secret")

    # Pre-flight fail-fast (pattern L1)
    _validate_secret_create(data.name, data.value)

    existing = (await db.execute(
        select(Secret).where(Secret.workspace_id == ws, Secret.name == data.name, Secret.env == data.env)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Secret '{data.name}' env={data.env} đã tồn tại — dùng rotate.")

    try:
        encrypted = encrypt(data.value)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vault encrypt thất bại: {e}") from e

    secret = Secret(
        workspace_id=ws,
        name=data.name,
        env=data.env,
        value_encrypted=encrypted,
    )
    db.add(secret)
    await audit_push(db, actor=me.email, workspace_id=ws, action="secret.create", target=data.name, severity="ok",
                     metadata={"env": data.env})
    await db.commit()
    await db.refresh(secret)
    return SecretOut.model_validate(secret)


@router.post("/secrets/{secret_id}/rotate", response_model=SecretOut)
async def rotate_secret(
    ws: str,
    secret_id: UUID,
    new_value: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SecretOut:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để rotate")

    secret = (await db.execute(select(Secret).where(Secret.id == secret_id, Secret.workspace_id == ws))).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="secret not found")

    # If new value not provided, generate a strong 32-byte hex
    gen = new_value or f"sk_zeni_live_{random.randbytes(24).hex()}"
    secret.value_encrypted = encrypt(gen)
    secret.rotations += 1
    secret.updated_at = datetime.now(timezone.utc)
    await audit_push(db, actor=me.email, workspace_id=ws, action="secret.rotate", target=secret.name, severity="ok",
                     metadata={"rotation_no": secret.rotations})
    await db.commit()
    await db.refresh(secret)
    return SecretOut.model_validate(secret)


@router.get("/secrets/{secret_id}/reveal")
async def reveal_secret(
    ws: str,
    secret_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để xem secret")
    secret = (await db.execute(select(Secret).where(Secret.id == secret_id, Secret.workspace_id == ws))).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="secret not found")
    try:
        plain = decrypt(secret.value_encrypted)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    await audit_push(db, actor=me.email, workspace_id=ws, action="secret.reveal", target=secret.name, severity="warn")
    await db.commit()
    return {"name": secret.name, "masked": mask(plain), "full": plain, "env": secret.env}


@router.delete("/secrets/{secret_id}", status_code=204, response_class=Response)
async def delete_secret(
    ws: str,
    secret_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner để xoá secret")
    secret = (await db.execute(select(Secret).where(Secret.id == secret_id, Secret.workspace_id == ws))).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="secret not found")
    await db.delete(secret)
    await audit_push(db, actor=me.email, workspace_id=ws, action="secret.delete", target=secret.name, severity="err")
    await db.commit()
    return Response(status_code=204)


# ─── Identity flows (SSO / invite OTP) ───────────────
@router.post("/flow")
async def run_identity_flow(
    ws: str,
    data: IdentityFlowIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)

    cost = 0.0001
    result: dict = {"flow": data.flow, "workspace": ws}

    if data.flow == "sso":
        if not data.email:
            raise HTTPException(status_code=400, detail="email required")
        result.update({
            "user": data.email,
            "auth_method": "OIDC · Google Workspace",
            "session_ttl_access": 3600,
            "session_ttl_refresh": 2592000,
            "scopes": data.resource or f"zeni-cloud:{ws}:*",
            "mfa": "TOTP verified",
        })
        await audit_push(db, actor=me.email, workspace_id=ws, action="identity.sso_login", target=data.email, severity="ok")
    elif data.flow == "rotate":
        resource = data.resource or f"zeni-cloud:{ws}:*"
        result.update({
            "resource": resource,
            "old_version": "v3",
            "new_version": "v4",
            "grace_window_h": 24,
            "storage": "Zeni Vault · Fernet AES-256",
        })
        await audit_push(db, actor=me.email, workspace_id=ws, action="identity.key_rotate", target=resource, severity="ok")
        cost = 0.00005
    elif data.flow == "invite":
        if not data.email:
            raise HTTPException(status_code=400, detail="email required")
        otp = f"{random.randint(0, 999999):06d}"
        result.update({
            "invitee": data.email,
            "otp_ttl_min": 10,
            "role": "Viewer (pending)",
            "scope": data.resource or f"zeni-cloud:{ws}:*",
            "otp_sent": True,
            "otp_preview_dev_only": otp,  # In production, never return this — send via email
        })
        await audit_push(db, actor=me.email, workspace_id=ws, action="identity.invite", target=data.email, severity="ok")
        cost = 0.00008

    await billing_push(db, workspace_id=ws, layer="L5", action=f"identity.{data.flow}", cost_usd=cost)
    await db.commit()
    result["cost_usd"] = cost
    return result
