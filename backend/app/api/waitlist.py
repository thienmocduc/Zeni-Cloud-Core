"""
Zeni Cloud Core — Waitlist API.

Public POST /api/v1/waitlist/signup endpoint for the landing page.
Stores leads in `waitlist` table. Idempotent: re-submitting same email
returns 200 with existing record (no duplicate).
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db
from app.db.models import Waitlist
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.waitlist")
router = APIRouter(prefix="/waitlist", tags=["waitlist"])


class WaitlistSignupIn(BaseModel):
    email: EmailStr
    source: str = Field(default="landing", max_length=32)
    lang: str | None = Field(default="vi", max_length=8)
    referrer: str | None = Field(default=None, max_length=512)
    user_agent: str | None = Field(default=None, max_length=512)


class WaitlistSignupOut(BaseModel):
    ok: bool
    position: int
    message: str


def _ip_hint(request: Request) -> str:
    """Return coarse, hashed IP hint (privacy-respecting)."""
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = fwd or (request.client.host if request.client else "unknown")
    # Hash so we don't store raw IP — for de-dup signal only
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


@router.post("/signup", response_model=WaitlistSignupOut)
async def signup(
    data: WaitlistSignupIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WaitlistSignupOut:
    """Public endpoint. Captures email + minimal metadata."""
    email = str(data.email).lower().strip()

    # Idempotent: if email already exists, just return current position
    existing = (await db.execute(select(Waitlist).where(Waitlist.email == email))).scalar_one_or_none()
    if existing:
        # Position = how many waitlisted before me + 1
        before = (await db.execute(
            select(func.count()).select_from(Waitlist).where(Waitlist.created_at < existing.created_at)
        )).scalar() or 0
        return WaitlistSignupOut(
            ok=True,
            position=before + 1,
            message=f"Bạn đã có trong waitlist từ {existing.created_at.date()}. Vị trí #{before + 1}.",
        )

    record = Waitlist(
        email=email,
        source=data.source[:32],
        lang=(data.lang or "vi")[:8],
        referrer=(data.referrer or "")[:512] or None,
        user_agent=(data.user_agent or "")[:512] or None,
        ip_hint=_ip_hint(request),
    )
    db.add(record)
    await db.flush()

    # Position
    total = (await db.execute(select(func.count()).select_from(Waitlist))).scalar() or 0

    await audit_push(
        db, actor=email, workspace_id=None, action="waitlist.signup", target=email, severity="ok",
        metadata={"source": record.source, "lang": record.lang, "position": total},
    )
    await db.commit()

    log.info("[waitlist] new signup #%d: %s (source=%s, lang=%s)", total, email, record.source, record.lang)

    return WaitlistSignupOut(
        ok=True,
        position=total,
        message=f"✓ Đã ghi nhận {email}. Bạn là #{total} trên waitlist. Sẽ liên hệ khi alpha mở Q3/2026.",
    )


@router.get("", response_model=list[dict])
async def list_waitlist(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
) -> list[dict]:
    """Owner-only: list all waitlist signups for review."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner để xem waitlist")
    rows = (await db.execute(
        select(Waitlist).order_by(Waitlist.created_at.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": r.id, "email": r.email, "source": r.source, "lang": r.lang,
            "invited": r.invited, "created_at": r.created_at.isoformat(),
            "contacted_at": r.contacted_at.isoformat() if r.contacted_at else None,
            "referrer": r.referrer, "notes": r.notes,
        }
        for r in rows
    ]
