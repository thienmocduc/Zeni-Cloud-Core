"""
Zeni Cloud Core — Workspace API Tokens (Personal Access Tokens for service use).

Allows customers (NexBuild, BTHome, ANIMA, ...) to call APIs without JWT login.
Format: `zeni_pat_<base32 random>`. Stored as SHA256 hash (never plaintext after creation).

Scopes:
  - "ai"      : only /ai/complete + /ai/* — for AI training / inference clients
  - "data"    : /data/* (SQL exec)
  - "web3"    : /web3/* (read-only chain queries)
  - "full"    : everything (use sparingly)
  - comma-separated for multi: "ai,data"
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import ApiToken
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.tokens")
router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])

ALLOWED_SCOPES = {"ai", "data", "web3", "automation", "full", "deploy", "read", "deploy_only"}
MAX_TOKENS_PER_WS = 20


def _generate_token() -> tuple[str, str, str]:
    """Returns (full_token, sha256_hash, prefix_for_display)."""
    rand = _secrets.token_urlsafe(32)  # ~43 chars, URL-safe
    full = f"zeni_pat_{rand}"
    h = hashlib.sha256(full.encode()).hexdigest()
    prefix = full[:16] + "…"  # display: zeni_pat_xxxxxxx…
    return full, h, prefix


def _validate_scopes(scopes: str) -> str:
    parts = [s.strip().lower() for s in scopes.split(",") if s.strip()]
    invalid = [p for p in parts if p not in ALLOWED_SCOPES]
    if invalid:
        raise HTTPException(status_code=400,
                            detail=f"scopes không hợp lệ: {invalid}. Cho phép: {sorted(ALLOWED_SCOPES)}")
    return ",".join(sorted(set(parts)))


class TokenCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    scopes: str = Field(default="ai", description="Comma-separated: ai,data,web3,automation,full")
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class TokenOut(BaseModel):
    id: UUID
    name: str
    scopes: str
    token_prefix: str
    workspace_id: str
    expires_at: datetime | None
    last_used_at: datetime | None
    use_count: int
    revoked: bool
    created_at: datetime


class TokenCreatedOut(TokenOut):
    token: str  # full token, returned ONLY ONCE on creation


@router.get("", response_model=list[TokenOut])
async def list_tokens(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TokenOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(ApiToken).where(ApiToken.workspace_id == ws).order_by(ApiToken.created_at.desc())
    )).scalars().all()
    return [TokenOut(
        id=r.id, name=r.name, scopes=r.scopes, token_prefix=r.token_prefix,
        workspace_id=r.workspace_id, expires_at=r.expires_at, last_used_at=r.last_used_at,
        use_count=r.use_count, revoked=r.revoked, created_at=r.created_at,
    ) for r in rows]


@router.post("", response_model=TokenCreatedOut, status_code=201)
async def create_token(
    ws: str,
    data: TokenCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenCreatedOut:
    """Tạo API token mới. Full token chỉ trả 1 lần — lưu giữ kỹ!"""
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để tạo API token")

    scopes = _validate_scopes(data.scopes)

    # Cap per-ws
    existing = (await db.execute(
        select(ApiToken).where(ApiToken.workspace_id == ws, ApiToken.revoked.is_(False))
    )).scalars().all()
    if len(existing) >= MAX_TOKENS_PER_WS:
        raise HTTPException(status_code=429, detail=f"Vượt giới hạn {MAX_TOKENS_PER_WS} tokens active/ws")

    full, token_hash, prefix = _generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)) if data.expires_in_days else None

    record = ApiToken(
        workspace_id=ws, name=data.name, token_hash=token_hash, token_prefix=prefix,
        scopes=scopes, created_by=me.id, expires_at=expires_at,
    )
    db.add(record)
    await db.flush()
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="token.create",
        target=data.name, severity="ok",
        metadata={"scopes": scopes, "expires_at": expires_at.isoformat() if expires_at else None},
    )
    await db.commit()
    await db.refresh(record)
    return TokenCreatedOut(
        id=record.id, name=record.name, scopes=record.scopes, token_prefix=record.token_prefix,
        workspace_id=record.workspace_id, expires_at=record.expires_at,
        last_used_at=record.last_used_at, use_count=record.use_count,
        revoked=record.revoked, created_at=record.created_at, token=full,
    )


@router.delete("/{token_id}", status_code=204, response_class=Response)
async def revoke_token(
    ws: str,
    token_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để revoke")
    pat = (await db.execute(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.workspace_id == ws)
    )).scalar_one_or_none()
    if pat is None:
        raise HTTPException(status_code=404, detail="token not found")
    pat.revoked = True
    await audit_push(db, actor=me.email, workspace_id=ws, action="token.revoke",
                     target=pat.name, severity="warn")
    await db.commit()
    return Response(status_code=204)
