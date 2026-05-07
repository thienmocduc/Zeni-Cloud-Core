"""
Zeni CRM — HubSpot-like CRM cho Zeni Cloud (Sprint A6).

Router prefix `/crm`.

Endpoints:

  Contacts
    GET    /crm/contacts?ws=&q=&stage=&owner=&tag=&limit=&offset=
    POST   /crm/contacts
    GET    /crm/contacts/{id}?ws=
    PATCH  /crm/contacts/{id}
    DELETE /crm/contacts/{id}?ws=
    POST   /crm/contacts/import?ws=                       (CSV body)

  Companies
    GET    /crm/companies?ws=&q=&owner=&limit=&offset=
    POST   /crm/companies
    GET    /crm/companies/{id}?ws=
    PATCH  /crm/companies/{id}
    DELETE /crm/companies/{id}?ws=

  Deals + Pipelines
    GET    /crm/deals?ws=&pipeline_id=&stage=&owner=&status=&limit=&offset=
    POST   /crm/deals
    GET    /crm/deals/{id}?ws=
    PATCH  /crm/deals/{id}
    POST   /crm/deals/{id}/move-stage                     {stage_id, status?}
    DELETE /crm/deals/{id}?ws=
    GET    /crm/pipelines?ws=
    POST   /crm/pipelines
    PATCH  /crm/pipelines/{id}
    DELETE /crm/pipelines/{id}?ws=

  Activities
    GET    /crm/activities?ws=&contact_id=&deal_id=&owner=&type=&completed=&limit=&offset=
    POST   /crm/activities
    PATCH  /crm/activities/{id}
    DELETE /crm/activities/{id}?ws=

  Tickets
    GET    /crm/tickets?ws=&status=&priority=&assignee=&limit=&offset=
    POST   /crm/tickets
    PATCH  /crm/tickets/{id}
    DELETE /crm/tickets/{id}?ws=

  Sequences (email drip)
    GET    /crm/sequences?ws=
    POST   /crm/sequences
    PATCH  /crm/sequences/{id}
    DELETE /crm/sequences/{id}?ws=
    POST   /crm/sequences/{id}/enroll                     {contact_ids: [...]}
    POST   /crm/sequences/{id}/unenroll                   {contact_ids: [...]}
    GET    /crm/sequences/{id}/enrollments?ws=&status=

  Lists (segments)
    GET    /crm/lists?ws=
    POST   /crm/lists
    PATCH  /crm/lists/{id}
    DELETE /crm/lists/{id}?ws=
    POST   /crm/lists/{id}/members                        {contact_ids: [...]}
    DELETE /crm/lists/{id}/members                        {contact_ids: [...]}
    GET    /crm/lists/{id}/members?ws=
    POST   /crm/lists/{id}/refresh                        (dynamic list only)

  Reports
    GET    /crm/reports/funnel?ws=&pipeline_id=
    GET    /crm/reports/forecast?ws=&from=&to=
    GET    /crm/reports/activities?ws=&owner=&from=&to=

Security:
  - All endpoints: get_current_user + require_workspace_access(ws)
  - PAT scope: 'crm' or 'full'
  - audit_push for all state changes
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.crm_engine import (
    auto_create_contact_from_lead,
    compute_deal_score,
    evaluate_dynamic_list,
    merge_duplicates,
)

log = logging.getLogger("zeni.api.crm")
router = APIRouter(prefix="/crm", tags=["zeni-crm"])


# ─── Constants ──────────────────────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LIMIT = 500
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_BULK = 5000
LIFECYCLE_STAGES = {"lead", "mql", "sql", "customer", "evangelist"}
SOURCES = {"website", "import", "manual", "api", "referral"}
DEAL_STATUS = {"open", "won", "lost"}
ACTIVITY_TYPES = {"call", "email", "meeting", "note", "task"}
TICKET_STATUS = {"open", "pending", "resolved", "closed"}
TICKET_PRIORITY = {"low", "normal", "high", "urgent"}
LIST_TYPES = {"static", "dynamic"}


def _check_scope(me: CurrentUser) -> None:
    """PAT must have scope 'crm' or 'full'. JWT users pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "crm" not in scopes:
        raise HTTPException(status_code=403, detail="PAT cần scope 'crm' hoặc 'full' để dùng /crm")


def _require_writer(me: CurrentUser) -> None:
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không có quyền ghi")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not e or not EMAIL_RE.match(e) or len(e) > 255:
        raise HTTPException(status_code=400, detail=f"email không hợp lệ: {email!r}")
    return e


def _serialize_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _coerce_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()][:50]
    return []


# ═════════════════════════════════════════════════════════
# 1. CONTACTS
# ═════════════════════════════════════════════════════════
class ContactIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=240)
    phone: str | None = Field(default=None, max_length=40)
    company_id: int | None = None
    job_title: str | None = Field(default=None, max_length=160)
    lifecycle_stage: str = Field(default="lead")
    source: str = Field(default="manual")
    owner_email: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("lifecycle_stage")
    @classmethod
    def _v_stage(cls, v: str) -> str:
        if v not in LIFECYCLE_STAGES:
            raise ValueError(f"lifecycle_stage phải thuộc {sorted(LIFECYCLE_STAGES)}")
        return v

    @field_validator("source")
    @classmethod
    def _v_source(cls, v: str) -> str:
        if v not in SOURCES:
            raise ValueError(f"source phải thuộc {sorted(SOURCES)}")
        return v


class ContactPatchIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=240)
    phone: str | None = Field(default=None, max_length=40)
    company_id: int | None = None
    job_title: str | None = Field(default=None, max_length=160)
    lifecycle_stage: str | None = None
    source: str | None = None
    owner_email: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None
    properties: dict[str, Any] | None = None


def _row_contact(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "email": r[2],
        "full_name": r[3],
        "phone": r[4],
        "company_id": int(r[5]) if r[5] is not None else None,
        "job_title": r[6],
        "lifecycle_stage": r[7],
        "source": r[8],
        "owner_email": r[9],
        "tags": list(r[10] or []),
        "properties": _serialize_jsonb(r[11]),
        "last_activity_at": r[12].isoformat() if r[12] else None,
        "created_at": r[13].isoformat() if r[13] else None,
    }


_CONTACT_COLS = (
    "id, workspace_id, email, full_name, phone, company_id, job_title, "
    "lifecycle_stage, source, owner_email, tags, properties, "
    "last_activity_at, created_at"
)


@router.get("/contacts")
async def list_contacts(
    ws: str,
    q: str | None = None,
    stage: str | None = None,
    owner: str | None = None,
    tag: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if q:
        clauses.append("(email ILIKE :q OR full_name ILIKE :q OR phone ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if stage:
        if stage not in LIFECYCLE_STAGES:
            raise HTTPException(400, f"stage phải thuộc {sorted(LIFECYCLE_STAGES)}")
        clauses.append("lifecycle_stage = :stage")
        params["stage"] = stage
    if owner:
        clauses.append("owner_email = :owner")
        params["owner"] = owner.lower()
    if tag:
        clauses.append(":tag = ANY(tags)")
        params["tag"] = tag

    sql = f"""
        SELECT {_CONTACT_COLS}
          FROM crm_contacts
         WHERE {' AND '.join(clauses)}
         ORDER BY created_at DESC
         LIMIT :lim OFFSET :off
    """
    rows = (await db.execute(text(sql), params)).all()
    total = (await db.execute(
        text(f"SELECT COUNT(*) FROM crm_contacts WHERE {' AND '.join(clauses)}"),
        {k: v for k, v in params.items() if k not in ("lim", "off")}
    )).scalar_one()
    return {
        "workspace_id": ws,
        "count": len(rows),
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "contacts": [_row_contact(r) for r in rows],
    }


@router.post("/contacts", status_code=201)
async def create_contact(
    data: ContactIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    email = _validate_email(str(data.email))

    try:
        row = (await db.execute(text(f"""
            INSERT INTO crm_contacts
              (workspace_id, email, full_name, phone, company_id, job_title,
               lifecycle_stage, source, owner_email, tags, properties)
            VALUES
              (:ws, :email, :name, :phone, :cid, :title, :stage, :src, :owner,
               :tags, CAST(:props AS JSONB))
            RETURNING {_CONTACT_COLS}
        """), {
            "ws": ws, "email": email, "name": data.full_name,
            "phone": data.phone, "cid": data.company_id, "title": data.job_title,
            "stage": data.lifecycle_stage, "src": data.source,
            "owner": (data.owner_email or "").lower() or None,
            "tags": _coerce_tags(data.tags),
            "props": json.dumps(data.properties or {}),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"contact với email {email} đã tồn tại")
        log.exception("create_contact failed ws=%s", ws)
        raise HTTPException(502, f"không tạo được contact: {type(e).__name__}")

    out = _row_contact(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.contact.create", target=f"contact#{out['id']}",
                         severity="ok", metadata={"email": email})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (await db.execute(text(f"""
        SELECT {_CONTACT_COLS} FROM crm_contacts
         WHERE id = :id AND workspace_id = :ws
    """), {"id": contact_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "contact không tồn tại")
    return _row_contact(row)


@router.patch("/contacts/{contact_id}")
async def patch_contact(
    contact_id: int,
    data: ContactPatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    if data.lifecycle_stage and data.lifecycle_stage not in LIFECYCLE_STAGES:
        raise HTTPException(400, f"lifecycle_stage phải thuộc {sorted(LIFECYCLE_STAGES)}")
    if data.source and data.source not in SOURCES:
        raise HTTPException(400, f"source phải thuộc {sorted(SOURCES)}")

    sets: list[str] = []
    params: dict[str, Any] = {"id": contact_id, "ws": ws}
    for f in ("full_name", "phone", "company_id", "job_title",
              "lifecycle_stage", "source", "owner_email"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v.lower() if f == "owner_email" else v
    if data.tags is not None:
        sets.append("tags = :tags")
        params["tags"] = _coerce_tags(data.tags)
    if data.properties is not None:
        sets.append("properties = CAST(:props AS JSONB)")
        params["props"] = json.dumps(data.properties)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")

    sql = f"""
        UPDATE crm_contacts SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_CONTACT_COLS}
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_contact failed id=%s", contact_id)
        raise HTTPException(502, f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(404, "contact không tồn tại")
    return _row_contact(row)


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM crm_contacts WHERE id = :id AND workspace_id = :ws
    """), {"id": contact_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "contact không tồn tại")
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.contact.delete", target=f"contact#{contact_id}",
                         severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": contact_id}


@router.post("/contacts/import")
async def import_contacts_csv(
    ws: str,
    body: bytes = Body(..., media_type="text/csv"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bulk import contacts từ CSV.
    CSV header: email, full_name, phone, job_title, lifecycle_stage, source, owner_email, tags
    `tags` field: comma-separated trong cell, nên CSV nên dùng dấu ; làm delimiter HOẶC quote tags.
    """
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if len(body) > MAX_CSV_BYTES:
        raise HTTPException(413, f"CSV vượt quá {MAX_CSV_BYTES} bytes")

    try:
        text_data = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_data = body.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_data))
    if not reader.fieldnames or "email" not in [f.lower().strip() for f in reader.fieldnames]:
        raise HTTPException(400, "CSV phải có cột 'email'")

    inserted = 0
    skipped = 0
    errors: list[str] = []
    rows_processed = 0
    for raw in reader:
        rows_processed += 1
        if rows_processed > MAX_BULK:
            errors.append(f"đã đạt giới hạn {MAX_BULK} dòng — dừng")
            break
        rec = {k.lower().strip(): (v or "").strip() for k, v in raw.items()}
        email = rec.get("email", "")
        try:
            email = _validate_email(email)
        except HTTPException:
            skipped += 1
            errors.append(f"row {rows_processed}: email không hợp lệ {email!r}")
            continue
        stage = rec.get("lifecycle_stage") or "lead"
        if stage not in LIFECYCLE_STAGES:
            stage = "lead"
        src = rec.get("source") or "import"
        if src not in SOURCES:
            src = "import"
        tags_str = rec.get("tags") or ""
        tags = [t.strip() for t in re.split(r"[,;|]", tags_str) if t.strip()][:50]

        try:
            await db.execute(text("""
                INSERT INTO crm_contacts
                  (workspace_id, email, full_name, phone, job_title,
                   lifecycle_stage, source, owner_email, tags)
                VALUES
                  (:ws, :email, :name, :phone, :title, :stage, :src, :owner, :tags)
                ON CONFLICT (workspace_id, email) DO UPDATE
                  SET full_name = COALESCE(EXCLUDED.full_name, crm_contacts.full_name),
                      phone = COALESCE(EXCLUDED.phone, crm_contacts.phone),
                      updated_at = NOW()
            """), {
                "ws": ws, "email": email,
                "name": rec.get("full_name") or None,
                "phone": rec.get("phone") or None,
                "title": rec.get("job_title") or None,
                "stage": stage, "src": src,
                "owner": (rec.get("owner_email") or "").lower() or None,
                "tags": tags,
            })
            inserted += 1
        except Exception as e:
            skipped += 1
            errors.append(f"row {rows_processed}: {type(e).__name__}: {str(e)[:80]}")

    await db.commit()
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.contact.import",
                         severity="ok",
                         metadata={"inserted": inserted, "skipped": skipped})
        await db.commit()
    except Exception:
        await db.rollback()
    return {
        "workspace_id": ws,
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors[:50],
    }


# ═════════════════════════════════════════════════════════
# 2. COMPANIES
# ═════════════════════════════════════════════════════════
class CompanyIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=240)
    domain: str | None = Field(default=None, max_length=240)
    industry: str | None = Field(default=None, max_length=120)
    employees: int | None = None
    revenue_vnd: float | None = None
    address: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    owner_email: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}


class CompanyPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=240)
    domain: str | None = Field(default=None, max_length=240)
    industry: str | None = Field(default=None, max_length=120)
    employees: int | None = None
    revenue_vnd: float | None = None
    address: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    owner_email: str | None = None
    tags: list[str] | None = None
    properties: dict[str, Any] | None = None


_COMPANY_COLS = (
    "id, workspace_id, name, domain, industry, employees, revenue_vnd, "
    "address, phone, owner_email, tags, properties, created_at"
)


def _row_company(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "domain": r[3],
        "industry": r[4],
        "employees": int(r[5]) if r[5] is not None else None,
        "revenue_vnd": float(r[6]) if r[6] is not None else None,
        "address": r[7],
        "phone": r[8],
        "owner_email": r[9],
        "tags": list(r[10] or []),
        "properties": _serialize_jsonb(r[11]),
        "created_at": r[12].isoformat() if r[12] else None,
    }


@router.get("/companies")
async def list_companies(
    ws: str,
    q: str | None = None,
    owner: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if q:
        clauses.append("(name ILIKE :q OR domain ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if owner:
        clauses.append("owner_email = :owner")
        params["owner"] = owner.lower()
    sql = f"""
        SELECT {_COMPANY_COLS} FROM crm_companies
         WHERE {' AND '.join(clauses)}
         ORDER BY created_at DESC LIMIT :lim OFFSET :off
    """
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "companies": [_row_company(r) for r in rows]}


@router.post("/companies", status_code=201)
async def create_company(
    data: CompanyIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    try:
        row = (await db.execute(text(f"""
            INSERT INTO crm_companies
              (workspace_id, name, domain, industry, employees, revenue_vnd,
               address, phone, owner_email, tags, properties)
            VALUES (:ws, :n, :dom, :ind, :emp, :rev, :addr, :ph, :owner, :tags,
                    CAST(:props AS JSONB))
            RETURNING {_COMPANY_COLS}
        """), {
            "ws": ws, "n": data.name, "dom": data.domain, "ind": data.industry,
            "emp": data.employees, "rev": data.revenue_vnd, "addr": data.address,
            "ph": data.phone,
            "owner": (data.owner_email or "").lower() or None,
            "tags": _coerce_tags(data.tags),
            "props": json.dumps(data.properties or {}),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_company failed ws=%s", ws)
        raise HTTPException(502, f"không tạo được company: {type(e).__name__}")
    out = _row_company(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.company.create", target=f"company#{out['id']}",
                         severity="ok", metadata={"name": data.name})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/companies/{company_id}")
async def get_company(
    company_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (await db.execute(text(f"""
        SELECT {_COMPANY_COLS} FROM crm_companies
         WHERE id = :id AND workspace_id = :ws
    """), {"id": company_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "company không tồn tại")
    return _row_company(row)


@router.patch("/companies/{company_id}")
async def patch_company(
    company_id: int, data: CompanyPatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    sets: list[str] = []
    params: dict[str, Any] = {"id": company_id, "ws": ws}
    for f in ("name", "domain", "industry", "employees", "revenue_vnd",
              "address", "phone", "owner_email"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v.lower() if f == "owner_email" else v
    if data.tags is not None:
        sets.append("tags = :tags")
        params["tags"] = _coerce_tags(data.tags)
    if data.properties is not None:
        sets.append("properties = CAST(:props AS JSONB)")
        params["props"] = json.dumps(data.properties)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")

    sql = f"""
        UPDATE crm_companies SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_COMPANY_COLS}
    """
    row = (await db.execute(text(sql), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "company không tồn tại")
    return _row_company(row)


@router.delete("/companies/{company_id}")
async def delete_company(
    company_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM crm_companies WHERE id = :id AND workspace_id = :ws
    """), {"id": company_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "company không tồn tại")
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.company.delete",
                         target=f"company#{company_id}", severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": company_id}


# ═════════════════════════════════════════════════════════
# 3. PIPELINES
# ═════════════════════════════════════════════════════════
class PipelineIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=160)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False
    model_config = {"populate_by_name": True}


class PipelinePatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    stages: list[dict[str, Any]] | None = None
    is_default: bool | None = None


def _validate_stages(stages: list[dict]) -> list[dict]:
    if not isinstance(stages, list) or not stages:
        raise HTTPException(400, "stages phải là list không rỗng")
    if len(stages) > 30:
        raise HTTPException(400, "tối đa 30 stages")
    seen_ids = set()
    out = []
    for i, s in enumerate(stages):
        sid = str(s.get("id", "")).strip()
        if not sid or len(sid) > 40:
            raise HTTPException(400, f"stage[{i}].id không hợp lệ")
        if sid in seen_ids:
            raise HTTPException(400, f"stage id trùng: {sid}")
        seen_ids.add(sid)
        prob = int(s.get("probability") or 0)
        if not 0 <= prob <= 100:
            raise HTTPException(400, f"stage[{i}].probability phải 0-100")
        out.append({
            "id": sid,
            "name": str(s.get("name") or sid)[:120],
            "probability": prob,
            "position": int(s.get("position") or (i + 1)),
        })
    return out


def _row_pipeline(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "stages": _serialize_jsonb(r[3]) or [],
        "is_default": bool(r[4]),
        "created_at": r[5].isoformat() if r[5] else None,
    }


@router.get("/pipelines")
async def list_pipelines(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, name, stages, is_default, created_at
          FROM crm_pipelines WHERE workspace_id = :ws
         ORDER BY is_default DESC, created_at ASC
    """), {"ws": ws})).all()
    return {"workspace_id": ws, "count": len(rows),
            "pipelines": [_row_pipeline(r) for r in rows]}


@router.post("/pipelines", status_code=201)
async def create_pipeline(
    data: PipelineIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    stages = _validate_stages(data.stages)
    try:
        if data.is_default:
            await db.execute(text("UPDATE crm_pipelines SET is_default = FALSE WHERE workspace_id = :ws"),
                             {"ws": ws})
        row = (await db.execute(text("""
            INSERT INTO crm_pipelines (workspace_id, name, stages, is_default)
            VALUES (:ws, :n, CAST(:stages AS JSONB), :def)
            RETURNING id, workspace_id, name, stages, is_default, created_at
        """), {"ws": ws, "n": data.name,
               "stages": json.dumps(stages), "def": data.is_default})).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, "pipeline name đã tồn tại")
        raise HTTPException(502, f"create pipeline failed: {type(e).__name__}")
    return _row_pipeline(row)


@router.patch("/pipelines/{pipeline_id}")
async def patch_pipeline(
    pipeline_id: int, data: PipelinePatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets: list[str] = []
    params: dict[str, Any] = {"id": pipeline_id, "ws": ws}
    if data.name is not None:
        sets.append("name = :name")
        params["name"] = data.name
    if data.stages is not None:
        sets.append("stages = CAST(:stages AS JSONB)")
        params["stages"] = json.dumps(_validate_stages(data.stages))
    if data.is_default is not None:
        if data.is_default:
            await db.execute(text("UPDATE crm_pipelines SET is_default = FALSE WHERE workspace_id = :ws"),
                             {"ws": ws})
        sets.append("is_default = :def")
        params["def"] = data.is_default
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")
    row = (await db.execute(text(f"""
        UPDATE crm_pipelines SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING id, workspace_id, name, stages, is_default, created_at
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "pipeline không tồn tại")
    return _row_pipeline(row)


@router.delete("/pipelines/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    deal_count = (await db.execute(text(
        "SELECT COUNT(*) FROM crm_deals WHERE pipeline_id = :id AND workspace_id = :ws"
    ), {"id": pipeline_id, "ws": ws})).scalar_one()
    if int(deal_count or 0) > 0:
        raise HTTPException(409, f"pipeline còn {deal_count} deals — không thể xoá")
    res = await db.execute(text("""
        DELETE FROM crm_pipelines WHERE id = :id AND workspace_id = :ws
    """), {"id": pipeline_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "pipeline không tồn tại")
    return {"deleted": True, "id": pipeline_id}


# ═════════════════════════════════════════════════════════
# 4. DEALS
# ═════════════════════════════════════════════════════════
class DealIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=240)
    contact_id: int | None = None
    company_id: int | None = None
    pipeline_id: int
    stage_id: str = Field(..., max_length=40)
    amount_vnd: float | None = 0
    probability: int | None = None
    expected_close_date: date | None = None
    owner_email: str | None = None
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}


class DealPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=240)
    contact_id: int | None = None
    company_id: int | None = None
    pipeline_id: int | None = None
    stage_id: str | None = Field(default=None, max_length=40)
    amount_vnd: float | None = None
    probability: int | None = None
    expected_close_date: date | None = None
    actual_close_date: date | None = None
    status: str | None = None
    owner_email: str | None = None
    tags: list[str] | None = None
    properties: dict[str, Any] | None = None
    lost_reason: str | None = Field(default=None, max_length=240)


class DealMoveIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    stage_id: str = Field(..., max_length=40)
    status: str | None = None
    lost_reason: str | None = None
    model_config = {"populate_by_name": True}


_DEAL_COLS = (
    "id, workspace_id, name, contact_id, company_id, pipeline_id, stage_id, "
    "amount_vnd, probability, expected_close_date, actual_close_date, status, "
    "owner_email, tags, properties, score, lost_reason, created_at"
)


def _row_deal(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "contact_id": int(r[3]) if r[3] is not None else None,
        "company_id": int(r[4]) if r[4] is not None else None,
        "pipeline_id": int(r[5]),
        "stage_id": r[6],
        "amount_vnd": float(r[7]) if r[7] is not None else 0.0,
        "probability": int(r[8]) if r[8] is not None else 0,
        "expected_close_date": r[9].isoformat() if r[9] else None,
        "actual_close_date": r[10].isoformat() if r[10] else None,
        "status": r[11],
        "owner_email": r[12],
        "tags": list(r[13] or []),
        "properties": _serialize_jsonb(r[14]),
        "score": int(r[15] or 0),
        "lost_reason": r[16],
        "created_at": r[17].isoformat() if r[17] else None,
    }


async def _resolve_pipeline_stage(db: AsyncSession, ws: str, pipeline_id: int,
                                  stage_id: str) -> dict[str, Any]:
    row = (await db.execute(text(
        "SELECT stages FROM crm_pipelines WHERE id = :id AND workspace_id = :ws"
    ), {"id": pipeline_id, "ws": ws})).first()
    if not row:
        raise HTTPException(400, "pipeline không tồn tại")
    stages = _serialize_jsonb(row[0]) or []
    for s in stages:
        if s.get("id") == stage_id:
            return s
    raise HTTPException(400, f"stage_id {stage_id!r} không có trong pipeline")


@router.get("/deals")
async def list_deals(
    ws: str,
    pipeline_id: int | None = None,
    stage: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if pipeline_id is not None:
        clauses.append("pipeline_id = :pid")
        params["pid"] = pipeline_id
    if stage:
        clauses.append("stage_id = :stage")
        params["stage"] = stage
    if owner:
        clauses.append("owner_email = :owner")
        params["owner"] = owner.lower()
    if status:
        if status not in DEAL_STATUS:
            raise HTTPException(400, f"status phải thuộc {sorted(DEAL_STATUS)}")
        clauses.append("status = :st")
        params["st"] = status

    sql = f"""
        SELECT {_DEAL_COLS} FROM crm_deals
         WHERE {' AND '.join(clauses)}
         ORDER BY created_at DESC LIMIT :lim OFFSET :off
    """
    rows = (await db.execute(text(sql), params)).all()
    total_amount = (await db.execute(text(
        f"SELECT COALESCE(SUM(amount_vnd),0) FROM crm_deals WHERE {' AND '.join(clauses)}"
    ), {k: v for k, v in params.items() if k not in ("lim", "off")})).scalar_one()
    return {
        "workspace_id": ws,
        "count": len(rows),
        "total_amount_vnd": float(total_amount or 0),
        "deals": [_row_deal(r) for r in rows],
    }


@router.post("/deals", status_code=201)
async def create_deal(
    data: DealIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    stage = await _resolve_pipeline_stage(db, ws, data.pipeline_id, data.stage_id)
    prob = data.probability if data.probability is not None else int(stage.get("probability") or 0)

    try:
        row = (await db.execute(text(f"""
            INSERT INTO crm_deals
              (workspace_id, name, contact_id, company_id, pipeline_id, stage_id,
               amount_vnd, probability, expected_close_date, owner_email,
               tags, properties)
            VALUES (:ws, :n, :cid, :coid, :pid, :sid, :amt, :prob, :ecd, :owner,
                    :tags, CAST(:props AS JSONB))
            RETURNING {_DEAL_COLS}
        """), {
            "ws": ws, "n": data.name, "cid": data.contact_id,
            "coid": data.company_id, "pid": data.pipeline_id,
            "sid": data.stage_id, "amt": data.amount_vnd or 0,
            "prob": prob, "ecd": data.expected_close_date,
            "owner": (data.owner_email or "").lower() or None,
            "tags": _coerce_tags(data.tags),
            "props": json.dumps(data.properties or {}),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_deal failed ws=%s", ws)
        raise HTTPException(502, f"không tạo được deal: {type(e).__name__}")

    out = _row_deal(row)
    # Compute initial score
    try:
        await compute_deal_score(db, deal_id=out["id"])
        await db.commit()
    except Exception:
        await db.rollback()
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.deal.create", target=f"deal#{out['id']}",
                         severity="ok", metadata={"name": data.name,
                                                  "amount_vnd": float(data.amount_vnd or 0)})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/deals/{deal_id}")
async def get_deal(
    deal_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (await db.execute(text(f"""
        SELECT {_DEAL_COLS} FROM crm_deals
         WHERE id = :id AND workspace_id = :ws
    """), {"id": deal_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "deal không tồn tại")
    return _row_deal(row)


@router.patch("/deals/{deal_id}")
async def patch_deal(
    deal_id: int, data: DealPatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    if data.status and data.status not in DEAL_STATUS:
        raise HTTPException(400, f"status phải thuộc {sorted(DEAL_STATUS)}")
    sets: list[str] = []
    params: dict[str, Any] = {"id": deal_id, "ws": ws}
    for f in ("name", "contact_id", "company_id", "pipeline_id", "stage_id",
              "amount_vnd", "probability", "expected_close_date",
              "actual_close_date", "status", "owner_email", "lost_reason"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v.lower() if f == "owner_email" else v
    if data.tags is not None:
        sets.append("tags = :tags")
        params["tags"] = _coerce_tags(data.tags)
    if data.properties is not None:
        sets.append("properties = CAST(:props AS JSONB)")
        params["props"] = json.dumps(data.properties)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")

    sql = f"""
        UPDATE crm_deals SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_DEAL_COLS}
    """
    row = (await db.execute(text(sql), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "deal không tồn tại")
    try:
        await compute_deal_score(db, deal_id=deal_id)
        await db.commit()
    except Exception:
        await db.rollback()
    return _row_deal(row)


@router.post("/deals/{deal_id}/move-stage")
async def move_deal_stage(
    deal_id: int, data: DealMoveIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    cur = (await db.execute(text(
        "SELECT pipeline_id, stage_id FROM crm_deals WHERE id = :id AND workspace_id = :ws"
    ), {"id": deal_id, "ws": ws})).first()
    if not cur:
        raise HTTPException(404, "deal không tồn tại")
    stage = await _resolve_pipeline_stage(db, ws, int(cur[0]), data.stage_id)
    new_status = data.status or "open"
    if new_status not in DEAL_STATUS:
        raise HTTPException(400, f"status phải thuộc {sorted(DEAL_STATUS)}")
    actual_close = None
    if new_status in ("won", "lost"):
        actual_close = date.today()

    row = (await db.execute(text(f"""
        UPDATE crm_deals
           SET stage_id = :sid,
               probability = :prob,
               status = :st,
               actual_close_date = COALESCE(:acd, actual_close_date),
               lost_reason = CASE WHEN :st = 'lost' THEN :lr ELSE lost_reason END,
               updated_at = NOW()
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_DEAL_COLS}
    """), {
        "id": deal_id, "ws": ws, "sid": data.stage_id,
        "prob": int(stage.get("probability") or 0),
        "st": new_status, "acd": actual_close, "lr": data.lost_reason,
    })).first()
    await db.commit()

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.deal.move_stage",
                         target=f"deal#{deal_id}", severity="ok",
                         metadata={"from": cur[1], "to": data.stage_id, "status": new_status})
        await db.commit()
    except Exception:
        await db.rollback()
    return _row_deal(row)


@router.delete("/deals/{deal_id}")
async def delete_deal(
    deal_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM crm_deals WHERE id = :id AND workspace_id = :ws
    """), {"id": deal_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "deal không tồn tại")
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.deal.delete", target=f"deal#{deal_id}",
                         severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": deal_id}


# ═════════════════════════════════════════════════════════
# 5. ACTIVITIES
# ═════════════════════════════════════════════════════════
class ActivityIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    contact_id: int | None = None
    deal_id: int | None = None
    company_id: int | None = None
    type: str = Field(..., description="call/email/meeting/note/task")
    subject: str | None = Field(default=None, max_length=240)
    description: str | None = None
    completed: bool = False
    due_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in ACTIVITY_TYPES:
            raise ValueError(f"type phải thuộc {sorted(ACTIVITY_TYPES)}")
        return v


class ActivityPatchIn(BaseModel):
    subject: str | None = Field(default=None, max_length=240)
    description: str | None = None
    completed: bool | None = None
    due_at: datetime | None = None
    metadata: dict[str, Any] | None = None


_ACTIVITY_COLS = (
    "id, workspace_id, contact_id, deal_id, company_id, type, subject, "
    "description, completed, due_at, completed_at, created_by, metadata, created_at"
)


def _row_activity(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "contact_id": int(r[2]) if r[2] is not None else None,
        "deal_id": int(r[3]) if r[3] is not None else None,
        "company_id": int(r[4]) if r[4] is not None else None,
        "type": r[5],
        "subject": r[6],
        "description": r[7],
        "completed": bool(r[8]),
        "due_at": r[9].isoformat() if r[9] else None,
        "completed_at": r[10].isoformat() if r[10] else None,
        "created_by": r[11],
        "metadata": _serialize_jsonb(r[12]),
        "created_at": r[13].isoformat() if r[13] else None,
    }


@router.get("/activities")
async def list_activities(
    ws: str,
    contact_id: int | None = None,
    deal_id: int | None = None,
    owner: str | None = None,
    type: str | None = None,
    completed: bool | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if contact_id is not None:
        clauses.append("contact_id = :cid")
        params["cid"] = contact_id
    if deal_id is not None:
        clauses.append("deal_id = :did")
        params["did"] = deal_id
    if owner:
        clauses.append("created_by = :owner")
        params["owner"] = owner.lower()
    if type:
        if type not in ACTIVITY_TYPES:
            raise HTTPException(400, f"type phải thuộc {sorted(ACTIVITY_TYPES)}")
        clauses.append("type = :tp")
        params["tp"] = type
    if completed is not None:
        clauses.append("completed = :comp")
        params["comp"] = completed

    sql = f"""
        SELECT {_ACTIVITY_COLS} FROM crm_activities
         WHERE {' AND '.join(clauses)}
         ORDER BY created_at DESC LIMIT :lim OFFSET :off
    """
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "activities": [_row_activity(r) for r in rows]}


@router.post("/activities", status_code=201)
async def create_activity(
    data: ActivityIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    completed_at = _now() if data.completed else None
    row = (await db.execute(text(f"""
        INSERT INTO crm_activities
          (workspace_id, contact_id, deal_id, company_id, type, subject,
           description, completed, due_at, completed_at, created_by, metadata)
        VALUES (:ws, :cid, :did, :coid, :tp, :sub, :desc, :comp, :due, :compat,
                :cb, CAST(:meta AS JSONB))
        RETURNING {_ACTIVITY_COLS}
    """), {
        "ws": ws, "cid": data.contact_id, "did": data.deal_id,
        "coid": data.company_id, "tp": data.type, "sub": data.subject,
        "desc": data.description, "comp": data.completed,
        "due": data.due_at, "compat": completed_at, "cb": me.email,
        "meta": json.dumps(data.metadata or {}),
    })).first()
    # Update last_activity_at on contact
    if data.contact_id:
        await db.execute(text(
            "UPDATE crm_contacts SET last_activity_at = NOW() WHERE id = :id AND workspace_id = :ws"
        ), {"id": data.contact_id, "ws": ws})
    await db.commit()
    return _row_activity(row)


@router.patch("/activities/{activity_id}")
async def patch_activity(
    activity_id: int, data: ActivityPatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets: list[str] = []
    params: dict[str, Any] = {"id": activity_id, "ws": ws}
    if data.subject is not None:
        sets.append("subject = :sub"); params["sub"] = data.subject
    if data.description is not None:
        sets.append("description = :desc"); params["desc"] = data.description
    if data.due_at is not None:
        sets.append("due_at = :due"); params["due"] = data.due_at
    if data.completed is not None:
        sets.append("completed = :comp"); params["comp"] = data.completed
        sets.append("completed_at = :compat")
        params["compat"] = _now() if data.completed else None
    if data.metadata is not None:
        sets.append("metadata = CAST(:meta AS JSONB)")
        params["meta"] = json.dumps(data.metadata)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    row = (await db.execute(text(f"""
        UPDATE crm_activities SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_ACTIVITY_COLS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "activity không tồn tại")
    return _row_activity(row)


@router.delete("/activities/{activity_id}")
async def delete_activity(
    activity_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text(
        "DELETE FROM crm_activities WHERE id = :id AND workspace_id = :ws"
    ), {"id": activity_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "activity không tồn tại")
    return {"deleted": True, "id": activity_id}


# ═════════════════════════════════════════════════════════
# 6. TICKETS
# ═════════════════════════════════════════════════════════
class TicketIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    contact_id: int | None = None
    company_id: int | None = None
    subject: str = Field(..., min_length=1, max_length=240)
    description: str | None = None
    status: str = Field(default="open")
    priority: str = Field(default="normal")
    assignee_email: str | None = None
    source: str = Field(default="manual")
    properties: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}

    @field_validator("status")
    @classmethod
    def _v_status(cls, v: str) -> str:
        if v not in TICKET_STATUS:
            raise ValueError(f"status phải thuộc {sorted(TICKET_STATUS)}")
        return v

    @field_validator("priority")
    @classmethod
    def _v_priority(cls, v: str) -> str:
        if v not in TICKET_PRIORITY:
            raise ValueError(f"priority phải thuộc {sorted(TICKET_PRIORITY)}")
        return v


class TicketPatchIn(BaseModel):
    subject: str | None = Field(default=None, max_length=240)
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee_email: str | None = None
    properties: dict[str, Any] | None = None


_TICKET_COLS = (
    "id, workspace_id, contact_id, company_id, subject, description, status, "
    "priority, assignee_email, source, properties, created_at, resolved_at"
)


def _row_ticket(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "contact_id": int(r[2]) if r[2] is not None else None,
        "company_id": int(r[3]) if r[3] is not None else None,
        "subject": r[4],
        "description": r[5],
        "status": r[6],
        "priority": r[7],
        "assignee_email": r[8],
        "source": r[9],
        "properties": _serialize_jsonb(r[10]),
        "created_at": r[11].isoformat() if r[11] else None,
        "resolved_at": r[12].isoformat() if r[12] else None,
    }


@router.get("/tickets")
async def list_tickets(
    ws: str,
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if status:
        if status not in TICKET_STATUS:
            raise HTTPException(400, f"status phải thuộc {sorted(TICKET_STATUS)}")
        clauses.append("status = :st"); params["st"] = status
    if priority:
        if priority not in TICKET_PRIORITY:
            raise HTTPException(400, f"priority phải thuộc {sorted(TICKET_PRIORITY)}")
        clauses.append("priority = :pr"); params["pr"] = priority
    if assignee:
        clauses.append("assignee_email = :assignee"); params["assignee"] = assignee.lower()

    sql = f"""
        SELECT {_TICKET_COLS} FROM crm_tickets
         WHERE {' AND '.join(clauses)}
         ORDER BY (priority='urgent') DESC, (priority='high') DESC, created_at DESC
         LIMIT :lim OFFSET :off
    """
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "tickets": [_row_ticket(r) for r in rows]}


@router.post("/tickets", status_code=201)
async def create_ticket(
    data: TicketIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    row = (await db.execute(text(f"""
        INSERT INTO crm_tickets
          (workspace_id, contact_id, company_id, subject, description, status,
           priority, assignee_email, source, properties)
        VALUES (:ws, :cid, :coid, :sub, :desc, :st, :pr, :assignee, :src,
                CAST(:props AS JSONB))
        RETURNING {_TICKET_COLS}
    """), {
        "ws": ws, "cid": data.contact_id, "coid": data.company_id,
        "sub": data.subject, "desc": data.description, "st": data.status,
        "pr": data.priority,
        "assignee": (data.assignee_email or "").lower() or None,
        "src": data.source, "props": json.dumps(data.properties or {}),
    })).first()
    await db.commit()
    out = _row_ticket(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.ticket.create", target=f"ticket#{out['id']}",
                         severity="ok", metadata={"priority": data.priority})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.patch("/tickets/{ticket_id}")
async def patch_ticket(
    ticket_id: int, data: TicketPatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if data.status and data.status not in TICKET_STATUS:
        raise HTTPException(400, f"status phải thuộc {sorted(TICKET_STATUS)}")
    if data.priority and data.priority not in TICKET_PRIORITY:
        raise HTTPException(400, f"priority phải thuộc {sorted(TICKET_PRIORITY)}")
    sets: list[str] = []
    params: dict[str, Any] = {"id": ticket_id, "ws": ws}
    for f in ("subject", "description", "status", "priority", "assignee_email"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v.lower() if f == "assignee_email" else v
    if data.properties is not None:
        sets.append("properties = CAST(:props AS JSONB)")
        params["props"] = json.dumps(data.properties)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")
    if data.status in ("resolved", "closed"):
        sets.append("resolved_at = COALESCE(resolved_at, NOW())")

    row = (await db.execute(text(f"""
        UPDATE crm_tickets SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_TICKET_COLS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "ticket không tồn tại")
    return _row_ticket(row)


@router.delete("/tickets/{ticket_id}")
async def delete_ticket(
    ticket_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text(
        "DELETE FROM crm_tickets WHERE id = :id AND workspace_id = :ws"
    ), {"id": ticket_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "ticket không tồn tại")
    return {"deleted": True, "id": ticket_id}


# ═════════════════════════════════════════════════════════
# 7. SEQUENCES (email drip)
# ═════════════════════════════════════════════════════════
class SequenceIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=160)
    description: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    active: bool = True
    sender_email: str | None = None
    model_config = {"populate_by_name": True}


class SequencePatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = None
    steps: list[dict[str, Any]] | None = None
    active: bool | None = None
    sender_email: str | None = None


def _validate_steps(steps: list[dict]) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "steps phải là list không rỗng")
    if len(steps) > 30:
        raise HTTPException(400, "tối đa 30 steps")
    out = []
    for i, s in enumerate(steps):
        sub = str(s.get("subject") or "").strip()
        body = str(s.get("body_html") or s.get("body") or "").strip()
        if not sub or not body:
            raise HTTPException(400, f"step[{i}] thiếu subject hoặc body_html")
        if len(body) > 200_000:
            raise HTTPException(400, f"step[{i}].body_html quá dài (>200KB)")
        wait = int(s.get("wait_days") or 0)
        if not 0 <= wait <= 365:
            raise HTTPException(400, f"step[{i}].wait_days phải 0-365")
        out.append({
            "order": int(s.get("order") or (i + 1)),
            "wait_days": wait,
            "subject": sub[:240],
            "body_html": body,
            "body_text": str(s.get("body_text") or "")[:200_000] or None,
        })
    return out


_SEQ_COLS = (
    "id, workspace_id, name, description, steps, active, sender_email, created_at"
)


def _row_sequence(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "description": r[3],
        "steps": _serialize_jsonb(r[4]) or [],
        "active": bool(r[5]),
        "sender_email": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
    }


@router.get("/sequences")
async def list_sequences(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text(f"""
        SELECT {_SEQ_COLS} FROM crm_sequences WHERE workspace_id = :ws
         ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return {"workspace_id": ws, "count": len(rows),
            "sequences": [_row_sequence(r) for r in rows]}


@router.post("/sequences", status_code=201)
async def create_sequence(
    data: SequenceIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    steps = _validate_steps(data.steps)
    try:
        row = (await db.execute(text(f"""
            INSERT INTO crm_sequences
              (workspace_id, name, description, steps, active, sender_email, created_by)
            VALUES (:ws, :n, :d, CAST(:s AS JSONB), :a, :se, :cb)
            RETURNING {_SEQ_COLS}
        """), {
            "ws": ws, "n": data.name, "d": data.description,
            "s": json.dumps(steps), "a": data.active,
            "se": (data.sender_email or "").lower() or None, "cb": me.email,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, "sequence name đã tồn tại")
        raise HTTPException(502, f"không tạo được sequence: {type(e).__name__}")
    return _row_sequence(row)


@router.patch("/sequences/{seq_id}")
async def patch_sequence(
    seq_id: int, data: SequencePatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets: list[str] = []
    params: dict[str, Any] = {"id": seq_id, "ws": ws}
    if data.name is not None:
        sets.append("name = :n"); params["n"] = data.name
    if data.description is not None:
        sets.append("description = :d"); params["d"] = data.description
    if data.steps is not None:
        sets.append("steps = CAST(:s AS JSONB)")
        params["s"] = json.dumps(_validate_steps(data.steps))
    if data.active is not None:
        sets.append("active = :a"); params["a"] = data.active
    if data.sender_email is not None:
        sets.append("sender_email = :se"); params["se"] = data.sender_email.lower() or None
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")
    row = (await db.execute(text(f"""
        UPDATE crm_sequences SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_SEQ_COLS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "sequence không tồn tại")
    return _row_sequence(row)


@router.delete("/sequences/{seq_id}")
async def delete_sequence(
    seq_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text(
        "DELETE FROM crm_sequences WHERE id = :id AND workspace_id = :ws"
    ), {"id": seq_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "sequence không tồn tại")
    return {"deleted": True, "id": seq_id}


class EnrollIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    contact_ids: list[int] = Field(..., min_length=1, max_length=MAX_BULK)
    model_config = {"populate_by_name": True}


@router.post("/sequences/{seq_id}/enroll")
async def enroll_contacts(
    seq_id: int, data: EnrollIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    seq = (await db.execute(text(
        "SELECT id, active FROM crm_sequences WHERE id = :id AND workspace_id = :ws"
    ), {"id": seq_id, "ws": ws})).first()
    if not seq:
        raise HTTPException(404, "sequence không tồn tại")

    enrolled = 0
    skipped = 0
    for cid in data.contact_ids:
        try:
            res = await db.execute(text("""
                INSERT INTO crm_sequence_enrollments
                  (sequence_id, contact_id, workspace_id, current_step,
                   status, next_run_at)
                VALUES (:sid, :cid, :ws, 0, 'active', NOW())
                ON CONFLICT (sequence_id, contact_id) DO NOTHING
            """), {"sid": seq_id, "cid": cid, "ws": ws})
            if res.rowcount > 0:
                enrolled += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    await db.commit()
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.sequence.enroll",
                         target=f"sequence#{seq_id}", severity="ok",
                         metadata={"enrolled": enrolled, "skipped": skipped})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"enrolled": enrolled, "skipped": skipped}


@router.post("/sequences/{seq_id}/unenroll")
async def unenroll_contacts(
    seq_id: int, data: EnrollIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        UPDATE crm_sequence_enrollments
           SET status = 'unsubscribed', completed_at = NOW()
         WHERE sequence_id = :sid AND workspace_id = :ws
           AND contact_id = ANY(:cids) AND status = 'active'
    """), {"sid": seq_id, "ws": ws, "cids": data.contact_ids})
    await db.commit()
    return {"unenrolled": res.rowcount}


@router.get("/sequences/{seq_id}/enrollments")
async def list_enrollments(
    seq_id: int, ws: str,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    clauses = ["e.sequence_id = :sid", "e.workspace_id = :ws"]
    params: dict[str, Any] = {"sid": seq_id, "ws": ws, "lim": limit, "off": offset}
    if status:
        clauses.append("e.status = :st"); params["st"] = status
    rows = (await db.execute(text(f"""
        SELECT e.id, e.sequence_id, e.contact_id, c.email, e.current_step,
               e.status, e.next_run_at, e.enrolled_at, e.completed_at
          FROM crm_sequence_enrollments e
          LEFT JOIN crm_contacts c ON c.id = e.contact_id
         WHERE {' AND '.join(clauses)}
         ORDER BY e.enrolled_at DESC LIMIT :lim OFFSET :off
    """), params)).all()
    return {
        "sequence_id": seq_id,
        "count": len(rows),
        "enrollments": [{
            "id": int(r[0]),
            "sequence_id": int(r[1]),
            "contact_id": int(r[2]),
            "email": r[3],
            "current_step": int(r[4] or 0),
            "status": r[5],
            "next_run_at": r[6].isoformat() if r[6] else None,
            "enrolled_at": r[7].isoformat() if r[7] else None,
            "completed_at": r[8].isoformat() if r[8] else None,
        } for r in rows],
    }


# ═════════════════════════════════════════════════════════
# 8. LISTS (segments)
# ═════════════════════════════════════════════════════════
class ListIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=160)
    description: str | None = None
    type: str = Field(default="static")
    filter: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in LIST_TYPES:
            raise ValueError(f"type phải là static hoặc dynamic")
        return v


class ListPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = None
    filter: dict[str, Any] | None = None


_LIST_COLS = (
    "id, workspace_id, name, description, type, filter, member_count, "
    "last_refreshed_at, created_at"
)


def _row_list(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "description": r[3],
        "type": r[4],
        "filter": _serialize_jsonb(r[5]),
        "member_count": int(r[6] or 0),
        "last_refreshed_at": r[7].isoformat() if r[7] else None,
        "created_at": r[8].isoformat() if r[8] else None,
    }


@router.get("/lists")
async def list_segments(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text(f"""
        SELECT {_LIST_COLS} FROM crm_lists WHERE workspace_id = :ws
         ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return {"workspace_id": ws, "count": len(rows),
            "lists": [_row_list(r) for r in rows]}


@router.post("/lists", status_code=201)
async def create_list(
    data: ListIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    try:
        row = (await db.execute(text(f"""
            INSERT INTO crm_lists (workspace_id, name, description, type, filter)
            VALUES (:ws, :n, :d, :t, CAST(:f AS JSONB))
            RETURNING {_LIST_COLS}
        """), {
            "ws": ws, "n": data.name, "d": data.description,
            "t": data.type, "f": json.dumps(data.filter or {}),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate" in str(e).lower():
            raise HTTPException(409, "list name đã tồn tại")
        raise HTTPException(502, f"create list failed: {type(e).__name__}")
    return _row_list(row)


@router.patch("/lists/{list_id}")
async def patch_list(
    list_id: int, data: ListPatchIn, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets: list[str] = []
    params: dict[str, Any] = {"id": list_id, "ws": ws}
    if data.name is not None:
        sets.append("name = :n"); params["n"] = data.name
    if data.description is not None:
        sets.append("description = :d"); params["d"] = data.description
    if data.filter is not None:
        sets.append("filter = CAST(:f AS JSONB)")
        params["f"] = json.dumps(data.filter)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")
    sets.append("updated_at = NOW()")
    row = (await db.execute(text(f"""
        UPDATE crm_lists SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING {_LIST_COLS}
    """), params)).first()
    await db.commit()
    if not row:
        raise HTTPException(404, "list không tồn tại")
    return _row_list(row)


@router.delete("/lists/{list_id}")
async def delete_list(
    list_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text(
        "DELETE FROM crm_lists WHERE id = :id AND workspace_id = :ws"
    ), {"id": list_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "list không tồn tại")
    return {"deleted": True, "id": list_id}


class ListMembersIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    contact_ids: list[int] = Field(..., min_length=1, max_length=MAX_BULK)
    model_config = {"populate_by_name": True}


@router.post("/lists/{list_id}/members")
async def add_list_members(
    list_id: int, data: ListMembersIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    lst = (await db.execute(text(
        "SELECT type FROM crm_lists WHERE id = :id AND workspace_id = :ws"
    ), {"id": list_id, "ws": ws})).first()
    if not lst:
        raise HTTPException(404, "list không tồn tại")
    if lst[0] != "static":
        raise HTTPException(400, "chỉ thêm member cho static list")
    added = 0
    for cid in data.contact_ids:
        res = await db.execute(text("""
            INSERT INTO crm_list_members (list_id, contact_id)
            VALUES (:lid, :cid)
            ON CONFLICT DO NOTHING
        """), {"lid": list_id, "cid": cid})
        if res.rowcount > 0:
            added += 1
    await db.execute(text("""
        UPDATE crm_lists SET member_count = (
          SELECT COUNT(*) FROM crm_list_members WHERE list_id = :id
        ), updated_at = NOW() WHERE id = :id
    """), {"id": list_id})
    await db.commit()
    return {"added": added}


@router.delete("/lists/{list_id}/members")
async def remove_list_members(
    list_id: int, data: ListMembersIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM crm_list_members
         WHERE list_id = :lid AND contact_id = ANY(:cids)
    """), {"lid": list_id, "cids": data.contact_ids})
    await db.execute(text("""
        UPDATE crm_lists SET member_count = (
          SELECT COUNT(*) FROM crm_list_members WHERE list_id = :id
        ), updated_at = NOW() WHERE id = :id
    """), {"id": list_id})
    await db.commit()
    return {"removed": res.rowcount}


@router.get("/lists/{list_id}/members")
async def get_list_members(
    list_id: int, ws: str,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    lst = (await db.execute(text(
        "SELECT type FROM crm_lists WHERE id = :id AND workspace_id = :ws"
    ), {"id": list_id, "ws": ws})).first()
    if not lst:
        raise HTTPException(404, "list không tồn tại")
    rows = (await db.execute(text(f"""
        SELECT c.id, c.email, c.full_name, c.lifecycle_stage, c.tags
          FROM crm_list_members m
          JOIN crm_contacts c ON c.id = m.contact_id
         WHERE m.list_id = :lid AND c.workspace_id = :ws
         ORDER BY m.added_at DESC LIMIT :lim OFFSET :off
    """), {"lid": list_id, "ws": ws, "lim": limit, "off": offset})).all()
    return {
        "list_id": list_id,
        "count": len(rows),
        "members": [{
            "contact_id": int(r[0]),
            "email": r[1],
            "full_name": r[2],
            "lifecycle_stage": r[3],
            "tags": list(r[4] or []),
        } for r in rows],
    }


@router.post("/lists/{list_id}/refresh")
async def refresh_list(
    list_id: int, ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    result = await evaluate_dynamic_list(db, list_id=list_id, workspace_id=ws)
    await db.commit()
    return result


# ═════════════════════════════════════════════════════════
# 9. REPORTS
# ═════════════════════════════════════════════════════════
@router.get("/reports/funnel")
async def report_funnel(
    ws: str,
    pipeline_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Conversion funnel: số deal + tổng tiền tại mỗi stage."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    pipe = (await db.execute(text(
        "SELECT name, stages FROM crm_pipelines WHERE id = :id AND workspace_id = :ws"
    ), {"id": pipeline_id, "ws": ws})).first()
    if not pipe:
        raise HTTPException(404, "pipeline không tồn tại")
    stages = _serialize_jsonb(pipe[1]) or []

    rows = (await db.execute(text("""
        SELECT stage_id, COUNT(*) AS cnt, COALESCE(SUM(amount_vnd),0) AS total
          FROM crm_deals
         WHERE workspace_id = :ws AND pipeline_id = :pid
         GROUP BY stage_id
    """), {"ws": ws, "pid": pipeline_id})).all()
    by_stage = {r[0]: {"count": int(r[1]), "amount_vnd": float(r[2])} for r in rows}

    funnel: list[dict[str, Any]] = []
    prev_count = None
    for s in sorted(stages, key=lambda x: int(x.get("position") or 0)):
        sid = s.get("id")
        d = by_stage.get(sid, {"count": 0, "amount_vnd": 0.0})
        conversion = None
        if prev_count is not None and prev_count > 0:
            conversion = round(100.0 * d["count"] / prev_count, 2)
        funnel.append({
            "stage_id": sid,
            "stage_name": s.get("name"),
            "probability": s.get("probability"),
            "count": d["count"],
            "amount_vnd": d["amount_vnd"],
            "conversion_pct_from_prev": conversion,
        })
        prev_count = d["count"]
    won = next((f for f in funnel if f["stage_id"] == "won"), None)
    total_open = sum(f["count"] for f in funnel if f["stage_id"] not in ("won", "lost"))
    return {
        "workspace_id": ws,
        "pipeline_id": pipeline_id,
        "pipeline_name": pipe[0],
        "funnel": funnel,
        "total_open": total_open,
        "won_count": won["count"] if won else 0,
    }


@router.get("/reports/forecast")
async def report_forecast(
    ws: str,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Projected revenue = SUM(amount * probability/100) for open deals
    với expected_close_date trong khoảng."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    from_d = from_date or date.today()
    to_d = to_date or (from_d + timedelta(days=90))
    rows = (await db.execute(text("""
        SELECT
          DATE_TRUNC('month', expected_close_date)::date AS month,
          COUNT(*) AS deal_count,
          COALESCE(SUM(amount_vnd), 0) AS total_pipeline,
          COALESCE(SUM(amount_vnd * probability / 100.0), 0) AS weighted
          FROM crm_deals
         WHERE workspace_id = :ws AND status = 'open'
           AND expected_close_date BETWEEN :fd AND :td
         GROUP BY 1 ORDER BY 1
    """), {"ws": ws, "fd": from_d, "td": to_d})).all()
    months = [{
        "month": r[0].isoformat() if r[0] else None,
        "deal_count": int(r[1]),
        "total_pipeline_vnd": float(r[2]),
        "weighted_forecast_vnd": float(r[3]),
    } for r in rows]

    won_rows = (await db.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(amount_vnd),0) FROM crm_deals
         WHERE workspace_id = :ws AND status = 'won'
           AND actual_close_date BETWEEN :fd AND :td
    """), {"ws": ws, "fd": from_d, "td": to_d})).first()

    return {
        "workspace_id": ws,
        "from": from_d.isoformat(),
        "to": to_d.isoformat(),
        "months": months,
        "summary": {
            "open_pipeline_vnd": sum(m["total_pipeline_vnd"] for m in months),
            "weighted_forecast_vnd": sum(m["weighted_forecast_vnd"] for m in months),
            "closed_won_count": int(won_rows[0]) if won_rows else 0,
            "closed_won_vnd": float(won_rows[1]) if won_rows else 0.0,
        },
    }


@router.get("/reports/activities")
async def report_activities(
    ws: str,
    owner: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Activity by sales rep — đếm calls/emails/meetings/tasks per owner."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    from_d = from_date or (date.today() - timedelta(days=30))
    to_d = to_date or date.today()
    clauses = ["workspace_id = :ws", "created_at >= :fd", "created_at < :td + INTERVAL '1 day'"]
    params: dict[str, Any] = {"ws": ws, "fd": from_d, "td": to_d}
    if owner:
        clauses.append("created_by = :owner"); params["owner"] = owner.lower()

    rows = (await db.execute(text(f"""
        SELECT created_by, type, COUNT(*) AS cnt,
               SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS done
          FROM crm_activities
         WHERE {' AND '.join(clauses)}
         GROUP BY created_by, type
         ORDER BY created_by, type
    """), params)).all()
    by_owner: dict[str, dict[str, Any]] = {}
    for r in rows:
        ow = r[0] or "(unknown)"
        d = by_owner.setdefault(ow, {"owner": ow, "total": 0, "completed": 0,
                                     "by_type": {}})
        d["by_type"][r[1]] = {"count": int(r[2]), "completed": int(r[3] or 0)}
        d["total"] += int(r[2])
        d["completed"] += int(r[3] or 0)
    return {
        "workspace_id": ws,
        "from": from_d.isoformat(),
        "to": to_d.isoformat(),
        "owners": list(by_owner.values()),
    }


# ═════════════════════════════════════════════════════════
# 10. PUBLIC LANDING-PAGE LEAD CAPTURE (auto-create contact)
# ═════════════════════════════════════════════════════════
class LandingLeadIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    email: EmailStr
    full_name: str | None = None
    phone: str | None = None
    source: str = "website"
    properties: dict[str, Any] = Field(default_factory=dict)
    model_config = {"populate_by_name": True}


@router.post("/leads", status_code=201)
async def capture_lead(
    data: LandingLeadIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Endpoint cho landing page — tạo contact tự động (lifecycle = lead)."""
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await auto_create_contact_from_lead(
        db, workspace_id=ws, email=str(data.email),
        full_name=data.full_name, phone=data.phone,
        source=data.source if data.source in SOURCES else "website",
        properties=data.properties,
    )
    await db.commit()
    return res


# ═════════════════════════════════════════════════════════
# 11. MERGE DUPLICATES (admin tool)
# ═════════════════════════════════════════════════════════
class MergeIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    primary_id: int
    duplicate_ids: list[int] = Field(..., min_length=1, max_length=20)
    model_config = {"populate_by_name": True}


@router.post("/contacts/merge")
async def merge_contacts(
    data: MergeIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if data.primary_id in data.duplicate_ids:
        raise HTTPException(400, "primary_id không được có trong duplicate_ids")
    res = await merge_duplicates(
        db, workspace_id=ws,
        primary_id=data.primary_id, duplicate_ids=data.duplicate_ids,
    )
    await db.commit()
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="crm.contact.merge",
                         target=f"contact#{data.primary_id}", severity="warn",
                         metadata={"merged": data.duplicate_ids})
        await db.commit()
    except Exception:
        await db.rollback()
    return res
