from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import MemberInvite, User, UserWorkspace, Workspace
from app.schemas.resources import MemberInviteIn, MemberOut
from app.services.audit import audit_push
from app.services.email import is_configured as smtp_configured, render_invite_email, send_email

log = logging.getLogger("zeni.api.members")

router = APIRouter(prefix="/members", tags=["members"])


@router.get("", response_model=list[MemberOut])
async def list_members(
    ws: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    q = select(User, UserWorkspace.workspace_id).join(UserWorkspace, UserWorkspace.user_id == User.id)
    if ws:
        await require_workspace_access(ws, me)
        q = q.where(UserWorkspace.workspace_id == ws)
    elif me.role != "Owner":
        q = q.where(UserWorkspace.workspace_id.in_(me.workspaces or ["__none__"]))

    rows = (await db.execute(q)).all()
    return [
        MemberOut(
            id=u.id, email=u.email, name=u.name, role=u.role,
            workspace_id=ws_id, last_active=u.last_login,
        )
        for (u, ws_id) in rows
    ]


@router.post("/invite", status_code=201)
async def invite_member(
    data: MemberInviteIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(data.workspace_id, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để mời")

    token = secrets.token_urlsafe(32)
    invite = MemberInvite(
        workspace_id=data.workspace_id,
        email=data.email.lower(),
        role=data.role,
        token=token,
        invited_by=me.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invite)

    # Build accept URL — full URL pointing to /app/invite/accept?token=...
    base_url = settings.app_base_url.rstrip("/") if hasattr(settings, "app_base_url") else "https://zenicloud.io"
    accept_url = f"{base_url}/app/invite/accept?token={token}"

    # Get workspace display name
    ws_row = (await db.execute(select(Workspace).where(Workspace.id == data.workspace_id))).scalar_one_or_none()
    ws_name = ws_row.name if ws_row else data.workspace_id

    await audit_push(db, actor=me.email, workspace_id=data.workspace_id,
                     action="member.invite", target=data.email, severity="ok",
                     metadata={"role": data.role, "smtp_configured": smtp_configured()})
    await db.commit()

    # Send email in background (non-blocking)
    email_sent_status = "queued" if smtp_configured() else "skipped (SMTP chưa cấu hình)"
    if smtp_configured():
        subject, html = render_invite_email(
            inviter_name=me.user.name or me.email,
            workspace_name=ws_name,
            accept_url=accept_url,
        )
        bg.add_task(send_email, to=data.email, subject=subject, body_html=html)

    return {
        "invite_token": token,
        "accept_url": accept_url,
        "email": data.email,
        "role": data.role,
        "workspace_id": data.workspace_id,
        "workspace_name": ws_name,
        "expires_in_days": 7,
        "email_status": email_sent_status,
    }


@router.post("/invite/accept")
async def accept_invite(
    token: str,
    password: str,
    name: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.core.security import hash_password

    invite = (await db.execute(select(MemberInvite).where(MemberInvite.token == token))).scalar_one_or_none()
    if invite is None or invite.status != "pending":
        raise HTTPException(status_code=404, detail="invite không tồn tại hoặc đã dùng")
    if invite.expires_at and invite.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="invite đã hết hạn")

    user = (await db.execute(select(User).where(User.email == invite.email))).scalar_one_or_none()
    if user is None:
        user = User(email=invite.email, password_hash=hash_password(password), name=name, role=invite.role)
        db.add(user)
        await db.flush()

    # Link to workspace if not already
    existing = (await db.execute(
        select(UserWorkspace).where(UserWorkspace.user_id == user.id, UserWorkspace.workspace_id == invite.workspace_id)
    )).scalar_one_or_none()
    if not existing:
        db.add(UserWorkspace(user_id=user.id, workspace_id=invite.workspace_id, role=invite.role))

    invite.status = "accepted"
    await audit_push(db, actor=user.email, workspace_id=invite.workspace_id,
                     action="member.accept", target=user.email, severity="ok")
    await db.commit()
    return {"user_id": str(user.id), "email": user.email, "workspace_id": invite.workspace_id}
