"""
Zeni Cloud Core — Multi-Entity Billing API (A5).

Endpoints:
  GET    /legal-entities                            — list (any auth user)
  POST   /legal-entities                            — create (admin/Owner)
  PATCH  /legal-entities/{id}                       — update (admin/Owner)
  POST   /legal-entities/assign?ws=                 — map ws → entity (admin/Owner)
  POST   /billing/charge-tagged?ws=                 — charge + tag (admin/Owner)
  GET    /billing/revenue-by-entity?period_start&period_end  — breakdown (admin/Owner)
  POST   /billing/intercompany/run                  — auto-generate transfers (admin/Owner)
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import multi_entity_billing as meb
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.legal_entities")

router = APIRouter(tags=["legal-entities", "multi-entity-billing"])

ADMIN_ROLES = {"Owner", "Admin"}


# ─── Helpers ──────────────────────────────────────────────────
def _ensure_admin(me: CurrentUser) -> None:
    if me.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Bạn không có quyền")


# ─── Schemas ──────────────────────────────────────────────────
class EntityCreateIn(BaseModel):
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=128)
    parent_id: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=128)
    tax_id: str | None = Field(default=None, max_length=64)
    is_master: bool = False
    notes: str | None = Field(default=None, max_length=500)


class EntityUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    parent_id: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=128)
    tax_id: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=500)
    is_master: bool | None = None


class AssignIn(BaseModel):
    legal_entity_id: str = Field(min_length=2, max_length=64)


class ChargeTaggedIn(BaseModel):
    amount_vnd: Decimal = Field(gt=0, le=Decimal("10000000000"))
    legal_entity_id: str | None = Field(default=None, max_length=64)
    action: str = Field(min_length=2, max_length=64)
    metadata: dict | None = None


class IntercompanyRunIn(BaseModel):
    period_start: date
    period_end: date
    dry_run: bool = False


# ═════════════════════════════════════════════════════════════
# 1. Legal Entities CRUD
# ═════════════════════════════════════════════════════════════
@router.get("/legal-entities")
async def list_legal_entities(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List tất cả pháp nhân — mọi auth user đều xem được."""
    entities = await meb.list_entities(db)
    return {"ok": True, "count": len(entities), "entities": entities}


@router.post("/legal-entities")
async def create_legal_entity(
    data: EntityCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo pháp nhân mới — admin/Owner only."""
    _ensure_admin(me)

    # Check id chưa tồn tại
    existing = await meb.get_entity(db, data.id)
    if existing is not None:
        raise HTTPException(status_code=400, detail=f"Pháp nhân '{data.id}' đã tồn tại")

    try:
        entity = await meb.create_entity(
            db,
            id_=data.id, name=data.name,
            parent_id=data.parent_id,
            bank_account=data.bank_account, tax_id=data.tax_id,
            is_master=data.is_master, notes=data.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="billing.legal_entity.create", target=data.id,
        severity="ok",
        metadata={"name": data.name, "parent_id": data.parent_id, "is_master": data.is_master},
    )
    await db.commit()
    return {"ok": True, "entity": entity}


@router.patch("/legal-entities/{entity_id}")
async def update_legal_entity(
    entity_id: str,
    data: EntityUpdateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update pháp nhân — admin/Owner only."""
    _ensure_admin(me)

    try:
        updated = await meb.update_entity(db, entity_id, **data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="billing.legal_entity.update", target=entity_id,
        severity="info", metadata=data.model_dump(exclude_none=True),
    )
    await db.commit()
    return {"ok": True, "entity": updated}


# ═════════════════════════════════════════════════════════════
# 2. Workspace ↔ Entity assignment
# ═════════════════════════════════════════════════════════════
@router.post("/legal-entities/assign")
async def assign_workspace(
    data: AssignIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Gán workspace → pháp nhân mặc định — admin/Owner only."""
    _ensure_admin(me)
    await require_workspace_access(ws, me)

    try:
        await meb.assign_workspace_to_entity(db, ws, data.legal_entity_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="billing.workspace_entity.assign",
        target=f"{ws}→{data.legal_entity_id}", severity="ok",
        metadata={"legal_entity_id": data.legal_entity_id},
    )
    await db.commit()
    return {"ok": True, "workspace_id": ws, "legal_entity_id": data.legal_entity_id}


# ═════════════════════════════════════════════════════════════
# 3. Charge with entity tag
# ═════════════════════════════════════════════════════════════
@router.post("/billing/charge-tagged")
async def charge_tagged(
    data: ChargeTaggedIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Charge có entity tag — admin/Owner only (sẽ mở rộng cho PAT scope sau).
    Resolve entity:
      - body.legal_entity_id ưu tiên
      - nếu None → workspace_legal_entity mapping
      - nếu chưa map → 'zeni_cloud' (default)
    """
    _ensure_admin(me)
    await require_workspace_access(ws, me)

    try:
        result = await meb.charge_with_tag(
            db,
            workspace_id=ws,
            amount_vnd=data.amount_vnd,
            action=data.action,
            legal_entity_id=data.legal_entity_id,
            actor=me.email,
            metadata=data.metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="billing.charge_tagged",
        target=f"{result['legal_entity_id']}:{data.action}",
        severity="ok",
        metadata={
            "amount_vnd": float(data.amount_vnd),
            "tx_id": result["tx_id"],
            "legal_entity_id": result["legal_entity_id"],
        },
    )
    await db.commit()
    return {"ok": True, **result}


# ═════════════════════════════════════════════════════════════
# 4. Revenue by entity
# ═════════════════════════════════════════════════════════════
@router.get("/billing/revenue-by-entity")
async def get_revenue_by_entity(
    period_start: date = Query(..., description="YYYY-MM-DD"),
    period_end: date = Query(..., description="YYYY-MM-DD (inclusive)"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Doanh thu chia theo pháp nhân trong khoảng period — admin/Owner only."""
    _ensure_admin(me)

    if period_start > period_end:
        raise HTTPException(status_code=400, detail="period_start phải <= period_end")

    breakdown = await meb.revenue_by_entity(db, period_start, period_end)
    total_vnd = sum(item["revenue_vnd"] for item in breakdown)
    total_tx = sum(item["tx_count"] for item in breakdown)

    return {
        "ok": True,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "currency": "VND",
        "total_vnd": float(total_vnd),
        "total_tx_count": total_tx,
        "breakdown": breakdown,
    }


# ═════════════════════════════════════════════════════════════
# 5. Intercompany transfer runner
# ═════════════════════════════════════════════════════════════
@router.post("/billing/intercompany/run")
async def run_intercompany(
    data: IntercompanyRunIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Tự động tạo transfer records từ Zeni Holdings → các pháp nhân con.
    Idempotent: nếu đã có transfer cho period+to_entity → skip.
    """
    _ensure_admin(me)

    if data.period_start > data.period_end:
        raise HTTPException(status_code=400, detail="period_start phải <= period_end")

    transfers = await meb.run_intercompany_transfers(
        db, data.period_start, data.period_end, dry_run=data.dry_run
    )

    total_vnd = sum(
        t["amount_vnd"] for t in transfers
        if t.get("status") in ("pending", "dry_run")
    )

    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="billing.intercompany.run",
        target=f"{data.period_start}..{data.period_end}",
        severity="info",
        metadata={
            "dry_run": data.dry_run,
            "transfer_count": len(transfers),
            "total_vnd": float(total_vnd),
        },
    )
    await db.commit()

    return {
        "ok": True,
        "period_start": data.period_start.isoformat(),
        "period_end": data.period_end.isoformat(),
        "dry_run": data.dry_run,
        "transfer_count": len(transfers),
        "total_vnd": float(total_vnd),
        "transfers": transfers,
    }


# ═════════════════════════════════════════════════════════════
# 6. List intercompany transfers (read-only)
# ═════════════════════════════════════════════════════════════
@router.get("/billing/intercompany")
async def list_intercompany(
    status: str | None = Query(default=None, pattern=r"^(pending|processed|failed|cancelled)$"),
    limit: int = Query(default=50, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List transfer records — admin/Owner only."""
    _ensure_admin(me)

    from sqlalchemy import text  # local import to avoid top-level pollution
    if status:
        rows = (await db.execute(
            text("""SELECT id, from_entity, to_entity, amount_vnd, period_start, period_end,
                           status, external_ref, notes, created_at, processed_at
                    FROM public.intercompany_transfers
                    WHERE status = :s
                    ORDER BY id DESC LIMIT :lim"""),
            {"s": status, "lim": limit},
        )).all()
    else:
        rows = (await db.execute(
            text("""SELECT id, from_entity, to_entity, amount_vnd, period_start, period_end,
                           status, external_ref, notes, created_at, processed_at
                    FROM public.intercompany_transfers
                    ORDER BY id DESC LIMIT :lim"""),
            {"lim": limit},
        )).all()

    return {
        "ok": True,
        "count": len(rows),
        "transfers": [
            {
                "id": r[0],
                "from_entity": r[1],
                "to_entity": r[2],
                "amount_vnd": float(r[3] or 0),
                "period_start": r[4].isoformat() if r[4] else None,
                "period_end": r[5].isoformat() if r[5] else None,
                "status": r[6],
                "external_ref": r[7],
                "notes": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
                "processed_at": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ],
    }
