from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db
from app.db.models import Workspace, UserWorkspace
from app.schemas.resources import WorkspaceOut

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


# ─── Schemas ────────────────────────────────────────────
class WorkspaceCreateIn(BaseModel):
    id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=2, max_length=128)
    code: str | None = Field(default=None, max_length=10, pattern=r"^[A-Z0-9]{2,10}$")


class WorkspaceUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    tagline: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=64)


# ─── List ───────────────────────────────────────────────
@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceOut]:
    q = select(Workspace).order_by(Workspace.id)
    rows = (await q).scalars().all() if False else (await db.execute(q)).scalars().all()
    if me.role == "Owner":
        return [WorkspaceOut.model_validate(r) for r in rows]
    return [WorkspaceOut.model_validate(r) for r in rows if r.id in me.workspaces]


# ─── Create new workspace ───────────────────────────────
@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    data: WorkspaceCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    """
    Create a new workspace. Caller becomes Owner of it.
    Each user can create up to 10 workspaces.
    """
    # Check workspace_id not already used
    existing = (await db.execute(
        select(Workspace).where(Workspace.id == data.id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Workspace ID '{data.id}' đã được dùng. Chọn tên khác.")

    # Quota check: max 10 workspaces per user (Admin role; Owner unlimited)
    if me.role != "Owner":
        cnt = (await db.execute(
            text("SELECT COUNT(*) FROM user_workspaces WHERE user_id = :uid AND role = 'Owner'"),
            {"uid": me.id}
        )).scalar() or 0
        if cnt >= 10:
            raise HTTPException(status_code=429, detail="Tối đa 10 workspace/user. Liên hệ support để mở rộng.")

    # Generate UNIQUE code (avoid collision with existing workspaces)
    base_code = (data.code or data.id[:8]).upper()
    final_code = base_code
    for _ in range(5):
        existing_code = (await db.execute(
            select(Workspace).where(Workspace.code == final_code)
        )).scalar_one_or_none()
        if existing_code is None:
            break
        final_code = (base_code[:5] + secrets.token_hex(2).upper())[:10]

    # Create workspace
    ws = Workspace(
        id=data.id,
        code=final_code,
        name=data.name[:128],
        tagline="Self-service workspace",
        color="var(--crown)",
    )
    db.add(ws)
    await db.flush()

    # Set 14-day trial via SAVEPOINT (nested transaction — graceful if column missing).
    # Without SAVEPOINT, a failed UPDATE aborts the whole transaction.
    trial_end = datetime.now(timezone.utc) + timedelta(days=14)
    sp = await db.begin_nested()
    try:
        await db.execute(
            text("UPDATE workspaces SET trial_ends_at = :te, trial_status = 'active' WHERE id = :id"),
            {"te": trial_end, "id": data.id}
        )
        await sp.commit()
    except Exception:
        # trial_ends_at column may not exist yet (migration 044 pending) — rollback nested only
        await sp.rollback()

    # Link caller as Owner of this workspace
    db.add(UserWorkspace(user_id=me.id, workspace_id=ws.id, role="Owner"))

    # Auto-create wallet with 50K free credit
    await db.execute(
        text("INSERT INTO wallet_balances(workspace_id, balance_vnd, total_topped_up) "
             "VALUES(:w, 50000, 50000) ON CONFLICT DO NOTHING"),
        {"w": ws.id}
    )

    await db.commit()
    await db.refresh(ws)
    return WorkspaceOut.model_validate(ws)


# ─── Rename / update workspace ──────────────────────────
@router.patch("/{ws_id}", response_model=WorkspaceOut)
async def update_workspace(
    ws_id: str,
    data: WorkspaceUpdateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    """Rename workspace or change tagline/color. Owner-only."""
    # Authorization: must be Owner of this workspace OR super-admin
    if me.role != "Owner":
        link = (await db.execute(
            text("SELECT role FROM user_workspaces WHERE user_id = :u AND workspace_id = :w"),
            {"u": me.id, "w": ws_id}
        )).scalar_one_or_none()
        if link != "Owner":
            raise HTTPException(status_code=403, detail="Chỉ Owner workspace mới sửa được.")

    ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace không tồn tại.")

    if data.name is not None:
        ws.name = data.name[:128]
    if data.tagline is not None:
        ws.tagline = data.tagline[:200]
    if data.color is not None:
        ws.color = data.color[:64]

    await db.commit()
    await db.refresh(ws)
    return WorkspaceOut.model_validate(ws)
