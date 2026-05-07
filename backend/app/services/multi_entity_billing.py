"""
Zeni Cloud Core — Multi-Entity Billing service (A5).

Mục đích:
  - Quản lý các pháp nhân (Zeni Holdings + ANIMA Care + Zeni Cloud + IOS Portal +
    Zeni Chain + Zeniipo).
  - Tag mọi billing transaction theo legal_entity (tách doanh thu pháp lý).
  - Map workspace → default legal_entity (1 ws có 1 entity mặc định).
  - Tạo intercompany transfer records để kế toán tổng kết cuối kỳ.

Pattern:
  - Master collected → revenue ghi nhận thuộc Zeni Holdings (cha)
  - Mỗi entity con có doanh thu → tạo transfer record from Holdings → entity con
  - Status='pending' đến khi kế toán manual confirm sang 'processed'

KHÔNG break existing wallet_balances / wallet_transactions / billing_events.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.services.multi_entity_billing")

DEFAULT_ENTITY = "zeni_cloud"  # nếu workspace chưa được gán → fallback


# ─────────────────────────────────────────────────────────────
# Entity CRUD
# ─────────────────────────────────────────────────────────────
async def list_entities(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        text("""SELECT id, name, parent_id, bank_account, tax_id, is_master,
                       notes, created_at
                FROM public.legal_entities
                ORDER BY is_master DESC, id ASC""")
    )).all()
    return [
        {
            "id": r[0],
            "name": r[1],
            "parent_id": r[2],
            "bank_account": r[3],
            "tax_id": r[4],
            "is_master": bool(r[5]),
            "notes": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


async def get_entity(db: AsyncSession, id_: str) -> dict | None:
    row = (await db.execute(
        text("""SELECT id, name, parent_id, bank_account, tax_id, is_master,
                       notes, created_at
                FROM public.legal_entities WHERE id = :id"""),
        {"id": id_},
    )).first()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "parent_id": row[2],
        "bank_account": row[3],
        "tax_id": row[4],
        "is_master": bool(row[5]),
        "notes": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
    }


async def create_entity(
    db: AsyncSession,
    id_: str,
    name: str,
    parent_id: str | None = None,
    bank_account: str | None = None,
    tax_id: str | None = None,
    is_master: bool = False,
    notes: str | None = None,
) -> dict:
    # Nếu có parent_id → check tồn tại
    if parent_id:
        parent = await get_entity(db, parent_id)
        if parent is None:
            raise ValueError(f"Pháp nhân cha '{parent_id}' không tồn tại")

    await db.execute(
        text("""INSERT INTO public.legal_entities
                  (id, name, parent_id, bank_account, tax_id, is_master, notes)
                VALUES (:id, :name, :parent_id, :bank, :tax, :master, :notes)"""),
        {
            "id": id_, "name": name, "parent_id": parent_id,
            "bank": bank_account, "tax": tax_id,
            "master": is_master, "notes": notes,
        },
    )
    return (await get_entity(db, id_)) or {}


async def update_entity(db: AsyncSession, id_: str, **fields: Any) -> dict:
    """Update fields cho entity. Cho phép: name, parent_id, bank_account, tax_id, notes."""
    allowed = {"name", "parent_id", "bank_account", "tax_id", "notes", "is_master"}
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not payload:
        cur = await get_entity(db, id_)
        if cur is None:
            raise ValueError("Pháp nhân không tồn tại")
        return cur

    cur = await get_entity(db, id_)
    if cur is None:
        raise ValueError("Pháp nhân không tồn tại")

    sets = ", ".join([f"{k} = :{k}" for k in payload.keys()])
    params = {**payload, "id": id_}
    await db.execute(
        text(f"UPDATE public.legal_entities SET {sets} WHERE id = :id"),
        params,
    )
    return (await get_entity(db, id_)) or {}


# ─────────────────────────────────────────────────────────────
# Workspace ↔ entity mapping
# ─────────────────────────────────────────────────────────────
async def assign_workspace_to_entity(
    db: AsyncSession, workspace_id: str, legal_entity_id: str
) -> None:
    """UPSERT vào workspace_legal_entity."""
    # Validate entity tồn tại
    if (await get_entity(db, legal_entity_id)) is None:
        raise ValueError("Pháp nhân không tồn tại")

    await db.execute(
        text("""INSERT INTO public.workspace_legal_entity
                  (workspace_id, legal_entity_id, assigned_at)
                VALUES (:w, :e, NOW())
                ON CONFLICT (workspace_id) DO UPDATE
                  SET legal_entity_id = EXCLUDED.legal_entity_id,
                      assigned_at = NOW()"""),
        {"w": workspace_id, "e": legal_entity_id},
    )


async def get_workspace_entity(db: AsyncSession, workspace_id: str) -> str:
    """Trả về legal_entity_id của workspace, default 'zeni_cloud' nếu chưa gán."""
    row = (await db.execute(
        text("SELECT legal_entity_id FROM public.workspace_legal_entity "
             "WHERE workspace_id = :w"),
        {"w": workspace_id},
    )).first()
    if row and row[0]:
        return row[0]
    return DEFAULT_ENTITY


# ─────────────────────────────────────────────────────────────
# Tagged charge — ghi billing_transactions với entity tag
# ─────────────────────────────────────────────────────────────
async def charge_with_tag(
    db: AsyncSession,
    workspace_id: str,
    amount_vnd: Decimal,
    action: str,
    legal_entity_id: str | None = None,
    actor: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Insert 1 row vào billing_transactions, gắn với entity.
    Nếu legal_entity_id None → resolve từ workspace mapping.
    Optional: trừ wallet_balances nếu workspace có wallet (best-effort, không fail nếu không có).
    """
    if amount_vnd <= 0:
        raise ValueError("amount_vnd phải > 0")

    # Resolve entity
    entity_id = legal_entity_id or (await get_workspace_entity(db, workspace_id))

    # Validate entity tồn tại
    if (await get_entity(db, entity_id)) is None:
        raise ValueError(f"Pháp nhân '{entity_id}' không tồn tại")

    md = json.dumps(metadata or {}, ensure_ascii=False, default=str)

    # Insert ledger row
    tx_row = (await db.execute(
        text("""INSERT INTO public.billing_transactions
                  (workspace_id, legal_entity_id, amount_vnd, action, metadata, actor)
                VALUES (:w, :e, :amt, :act, CAST(:m AS JSONB), :ac)
                RETURNING id, created_at"""),
        {
            "w": workspace_id, "e": entity_id,
            "amt": amount_vnd, "act": action, "m": md, "ac": actor,
        },
    )).first()
    tx_id, created_at = tx_row[0], tx_row[1]

    # Optional: trừ wallet_balances nếu tồn tại (không bắt buộc)
    new_balance = None
    try:
        bal_row = (await db.execute(
            text("SELECT balance_vnd FROM public.wallet_balances WHERE workspace_id = :w"),
            {"w": workspace_id},
        )).first()
        if bal_row is not None:
            await db.execute(
                text("""UPDATE public.wallet_balances SET
                          balance_vnd     = balance_vnd - :amt,
                          total_spent     = total_spent + :amt,
                          last_charged_at = NOW(),
                          updated_at      = NOW()
                        WHERE workspace_id = :w"""),
                {"w": workspace_id, "amt": amount_vnd},
            )
            new_balance = float((Decimal(str(bal_row[0])) - amount_vnd))
    except Exception:
        # Best-effort — không phá flow tag chính
        log.warning("[multi_entity] wallet adjust skipped for ws=%s", workspace_id)

    log.info(
        "[multi_entity] tagged tx ws=%s entity=%s amount=%s action=%s",
        workspace_id, entity_id, amount_vnd, action,
    )

    return {
        "tx_id": tx_id,
        "workspace_id": workspace_id,
        "legal_entity_id": entity_id,
        "amount_vnd": float(amount_vnd),
        "action": action,
        "balance_after_vnd": new_balance,
        "created_at": created_at.isoformat() if created_at else None,
    }


# ─────────────────────────────────────────────────────────────
# Reporting — revenue by entity
# ─────────────────────────────────────────────────────────────
async def revenue_by_entity(
    db: AsyncSession, period_start: date, period_end: date
) -> list[dict]:
    """
    SUM doanh thu theo legal_entity trong khoảng [period_start, period_end].
    JOIN legal_entities → trả về tên + tổng VND + count.
    """
    rows = (await db.execute(
        text("""SELECT bt.legal_entity_id,
                       COALESCE(le.name, '(unassigned)') AS entity_name,
                       COALESCE(le.is_master, FALSE) AS is_master,
                       COALESCE(SUM(bt.amount_vnd), 0) AS total_vnd,
                       COUNT(*) AS tx_count
                FROM public.billing_transactions bt
                LEFT JOIN public.legal_entities le ON le.id = bt.legal_entity_id
                WHERE bt.created_at >= :ps
                  AND bt.created_at < (:pe::date + INTERVAL '1 day')
                GROUP BY bt.legal_entity_id, le.name, le.is_master
                ORDER BY total_vnd DESC"""),
        {"ps": period_start, "pe": period_end},
    )).all()

    return [
        {
            "legal_entity_id": r[0],
            "entity_name": r[1],
            "is_master": bool(r[2]),
            "revenue_vnd": float(r[3] or 0),
            "tx_count": int(r[4] or 0),
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────
# Intercompany transfer runner
# ─────────────────────────────────────────────────────────────
async def run_intercompany_transfers(
    db: AsyncSession,
    period_start: date,
    period_end: date,
    dry_run: bool = False,
) -> list[dict]:
    """
    Tổng hợp doanh thu mỗi pháp nhân con trong period
    → tạo transfer record from='zeni_holdings' to=<entity_con> amount=<revenue>
      (vì master collected, transfer xuống entity con sau).
    Status='pending'. Idempotent: skip nếu đã có transfer cho period+to_entity này.
    """
    breakdown = await revenue_by_entity(db, period_start, period_end)

    # Tìm master
    master_row = (await db.execute(
        text("SELECT id FROM public.legal_entities WHERE is_master = TRUE LIMIT 1")
    )).first()
    master_id = master_row[0] if master_row else "zeni_holdings"

    transfers: list[dict] = []
    for item in breakdown:
        entity_id = item["legal_entity_id"]
        revenue = Decimal(str(item["revenue_vnd"]))

        # Skip master (Holdings không transfer cho chính nó), entity unassigned, revenue<=0
        if not entity_id or entity_id == master_id or revenue <= 0:
            continue

        # Idempotency check: đã có transfer cho period+to_entity?
        exists = (await db.execute(
            text("""SELECT id FROM public.intercompany_transfers
                    WHERE from_entity = :f AND to_entity = :t
                      AND period_start = :ps AND period_end = :pe
                    LIMIT 1"""),
            {"f": master_id, "t": entity_id, "ps": period_start, "pe": period_end},
        )).first()

        if exists:
            transfers.append({
                "from_entity": master_id,
                "to_entity": entity_id,
                "amount_vnd": float(revenue),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "status": "skipped_exists",
                "transfer_id": exists[0],
            })
            continue

        if dry_run:
            transfers.append({
                "from_entity": master_id,
                "to_entity": entity_id,
                "amount_vnd": float(revenue),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "status": "dry_run",
            })
            continue

        new_row = (await db.execute(
            text("""INSERT INTO public.intercompany_transfers
                      (from_entity, to_entity, amount_vnd,
                       period_start, period_end, status, notes)
                    VALUES (:f, :t, :amt, :ps, :pe, 'pending', :note)
                    RETURNING id, created_at"""),
            {
                "f": master_id, "t": entity_id, "amt": revenue,
                "ps": period_start, "pe": period_end,
                "note": f"Auto-generated from revenue_by_entity {period_start}…{period_end}",
            },
        )).first()

        transfers.append({
            "transfer_id": new_row[0],
            "from_entity": master_id,
            "to_entity": entity_id,
            "amount_vnd": float(revenue),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "status": "pending",
            "created_at": new_row[1].isoformat() if new_row[1] else None,
        })

    log.info(
        "[multi_entity] intercompany run period=%s..%s dry_run=%s transfers=%d",
        period_start, period_end, dry_run, len(transfers),
    )
    return transfers
