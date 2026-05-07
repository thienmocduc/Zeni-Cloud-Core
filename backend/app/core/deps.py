from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.base import get_db
from app.db.models import ApiToken, User, UserWorkspace


class CurrentUser:
    def __init__(self, user: User, workspaces: list[str], *, scope: str | None = None,
                 token_id: UUID | None = None):
        self.user = user
        self.id: UUID = user.id
        self.email: str = user.email
        self.name: str = user.name
        self.role: str = user.role
        self.workspaces: list[str] = workspaces
        self.auth_scope: str | None = scope  # null = JWT user, else PAT scope (e.g., "ai")
        self.token_id: UUID | None = token_id  # PAT id if authenticated via API token


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _try_pat_auth(token: str, db: AsyncSession) -> CurrentUser | None:
    """If token is a Workspace API Token (zeni_pat_*), authenticate via DB lookup."""
    if not token.startswith("zeni_pat_"):
        return None
    th = _hash_token(token)
    pat = (await db.execute(select(ApiToken).where(ApiToken.token_hash == th))).scalar_one_or_none()
    if pat is None or pat.revoked:
        raise HTTPException(status_code=401, detail="API token không hợp lệ hoặc đã thu hồi")
    if pat.expires_at and pat.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="API token đã hết hạn")

    # Service-account: synthesize User-like context for the workspace
    creator = (await db.execute(select(User).where(User.id == pat.created_by))).scalar_one_or_none() if pat.created_by else None
    pseudo_user = creator or User(
        id=pat.id, email=f"pat-{pat.token_prefix}@{pat.workspace_id}.zenicloud.io",
        password_hash=None, name=f"PAT:{pat.name}", role="Developer",
    )

    # Update last_used_at + use_count (best-effort, non-blocking)
    try:
        await db.execute(update(ApiToken).where(ApiToken.id == pat.id).values(
            last_used_at=datetime.now(timezone.utc), use_count=pat.use_count + 1,
        ))
        await db.commit()
    except Exception:
        await db.rollback()

    return CurrentUser(
        user=pseudo_user,
        workspaces=[pat.workspace_id],  # PAT scoped to its workspace
        scope=pat.scopes,
        token_id=pat.id,
    )


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    # Try Workspace API Token first
    pat_result = await _try_pat_auth(token, db)
    if pat_result:
        return pat_result

    # Fall back to JWT
    try:
        payload = decode_token(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid token")

    if payload.get("typ") != "access":
        raise HTTPException(status_code=401, detail="wrong token type")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="token missing sub")

    try:
        uid = UUID(sub)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid user id")

    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None or user.disabled:
        raise HTTPException(status_code=401, detail="user not found or disabled")

    ws_rows = (await db.execute(select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == uid))).scalars().all()
    workspaces = list(ws_rows)
    return CurrentUser(user=user, workspaces=workspaces)


async def require_workspace_access(
    ws: str,
    user: CurrentUser,
) -> None:
    if user.role == "Owner":
        return
    if ws not in user.workspaces:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="workspace access denied")


def require_role(*allowed: str):
    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed and "Owner" != user.role:
            raise HTTPException(status_code=403, detail=f"requires role in {allowed}")
        return user

    return _dep
