"""
Zeni Mail — Email Marketing API (replaces Mailchimp).

Router prefix `/mail`.

Endpoints:

  Lists
    GET    /mail/lists?ws=
    POST   /mail/lists
    PATCH  /mail/lists/{id}
    DELETE /mail/lists/{id}?ws=

  Subscribers
    POST   /mail/subscribers?ws=&list_id=
    POST   /mail/subscribers/bulk?ws=&list_id=
    POST   /mail/subscribers/import?ws=&list_id=          (CSV body)
    PATCH  /mail/subscribers/{id}
    DELETE /mail/subscribers/{id}?ws=
    POST   /mail/subscribers/{id}/unsubscribe?ws=
    GET    /mail/subscribers/{id}/confirm?token=          (public — double opt-in)
    GET    /mail/subscribers?ws=&list_id=&status=&tag=

  Templates
    GET/POST/PATCH/DELETE /mail/templates
    POST   /mail/templates/{id}/preview                    body: sample data

  Campaigns
    GET/POST/PATCH/DELETE /mail/campaigns
    POST   /mail/campaigns/{id}/send-test
    POST   /mail/campaigns/{id}/schedule
    POST   /mail/campaigns/{id}/send-now
    POST   /mail/campaigns/{id}/pause
    GET    /mail/campaigns/{id}/stats

  Tracking (PUBLIC — no auth)
    GET    /mail/track/open/{message_id}.gif
    GET    /mail/track/click/{message_id}?url=

  Automations
    GET/POST/PATCH/DELETE /mail/automations
    POST   /mail/automations/{id}/activate
    GET    /mail/automations/{id}/enrollments

  Analytics
    GET    /mail/analytics/overview?ws=&from=&to=
    GET    /mail/analytics/best-times?ws=

Security:
  - All non-tracking endpoints: get_current_user + require_workspace_access(ws)
  - PAT scope: 'email' or 'full'
  - Public confirm token uses random secret; tracking endpoints rate-limited downstream
  - audit_push for state changes
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.email import send_email
from app.services.mail_engine import (
    enqueue_campaign,
    render_template,
    trigger_subscribe_automations,
)

log = logging.getLogger("zeni.api.mail")
router = APIRouter(prefix="/mail", tags=["zeni-mail"])


# ─── Constants ──────────────────────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BULK_SUBSCRIBERS = 5000
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 1_000_000
MAX_LIST_LIMIT = 500

# 1×1 transparent GIF
_TRACK_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def _check_scope(me: CurrentUser) -> None:
    """PAT must have scope 'email' or 'full'. JWT users pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "email" not in scopes:
        raise HTTPException(
            status_code=403,
            detail="PAT cần scope 'email' hoặc 'full' để dùng /mail",
        )


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


# ─────────────────────────────────────────────────────────
# 1. LISTS
# ─────────────────────────────────────────────────────────
class ListIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    double_optin: bool = Field(default=True)
    confirmation_email_template: str | None = Field(default=None, max_length=200_000)
    welcome_email_template: str | None = Field(default=None, max_length=200_000)

    model_config = {"populate_by_name": True}


class ListPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    double_optin: bool | None = None
    confirmation_email_template: str | None = None
    welcome_email_template: str | None = None


def _row_to_list_dict(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "description": r[3],
        "double_optin": bool(r[4]),
        "subscriber_count": int(r[5]) if r[5] is not None else 0,
        "created_at": r[6].isoformat() if r[6] else None,
    }


@router.get("/lists")
async def list_lists(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, name, description, double_optin,
               subscriber_count, created_at
          FROM mail_lists
         WHERE workspace_id = :ws
         ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return {"workspace_id": ws, "count": len(rows),
            "lists": [_row_to_list_dict(r) for r in rows]}


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
        row = (await db.execute(text("""
            INSERT INTO mail_lists
              (workspace_id, name, description, double_optin,
               confirmation_email_template, welcome_email_template)
            VALUES (:ws, :n, :d, :do, :ct, :wt)
            RETURNING id, workspace_id, name, description, double_optin,
                      subscriber_count, created_at
        """), {
            "ws": ws, "n": data.name, "d": data.description,
            "do": data.double_optin,
            "ct": data.confirmation_email_template,
            "wt": data.welcome_email_template,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="list name đã tồn tại")
        log.exception("create_list failed ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"không tạo được list: {type(e).__name__}")

    out = _row_to_list_dict(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="mail.list.create", target=f"list#{out['id']}",
                         severity="ok", metadata={"name": data.name})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.patch("/lists/{list_id}")
async def patch_list(
    list_id: int,
    data: ListPatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    sets, params = [], {"id": list_id, "ws": ws}
    for f in ("name", "description", "double_optin",
              "confirmation_email_template", "welcome_email_template"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if not sets:
        raise HTTPException(status_code=400, detail="không có field nào cần update")

    sql = f"""
        UPDATE mail_lists SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING id, workspace_id, name, description, double_optin,
                   subscriber_count, created_at
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_list failed list=%s", list_id)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(status_code=404, detail="list không tồn tại")
    return _row_to_list_dict(row)


@router.delete("/lists/{list_id}")
async def delete_list(
    list_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM mail_lists WHERE id = :id AND workspace_id = :ws
    """), {"id": list_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="list không tồn tại")
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="mail.list.delete", target=f"list#{list_id}",
                         severity="warn")
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": list_id}


# ─────────────────────────────────────────────────────────
# 2. SUBSCRIBERS
# ─────────────────────────────────────────────────────────
class SubscriberIn(BaseModel):
    email: EmailStr
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    custom_fields: dict[str, Any] | None = None
    tags: list[str] | None = None
    status: str | None = Field(default=None,
                               description="default = list double_optin → 'pending', else 'active'")


class SubscriberPatchIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    custom_fields: dict[str, Any] | None = None
    tags: list[str] | None = None
    status: str | None = None


class BulkSubscribersIn(BaseModel):
    subscribers: list[SubscriberIn] = Field(..., max_length=MAX_BULK_SUBSCRIBERS)


_VALID_SUB_STATUSES = {"pending", "active", "bounced", "unsubscribed", "complained"}


def _row_to_sub_dict(r) -> dict:
    return {
        "id": int(r[0]),
        "list_id": int(r[1]),
        "workspace_id": r[2],
        "email": r[3],
        "first_name": r[4],
        "last_name": r[5],
        "custom_fields": _serialize_jsonb(r[6]),
        "tags": list(r[7]) if r[7] else [],
        "status": r[8],
        "confirmed_at": r[9].isoformat() if r[9] else None,
        "subscribed_at": r[10].isoformat() if r[10] else None,
        "unsubscribed_at": r[11].isoformat() if r[11] else None,
        "bounce_count": int(r[12]) if r[12] is not None else 0,
        "last_engagement_at": r[13].isoformat() if r[13] else None,
    }


_SUB_SELECT_COLS = ("id, list_id, workspace_id, email, first_name, last_name, "
                    "custom_fields, tags, status, confirmed_at, subscribed_at, "
                    "unsubscribed_at, bounce_count, last_engagement_at")


async def _verify_list_in_ws(db: AsyncSession, list_id: int, ws: str) -> dict:
    row = (await db.execute(text("""
        SELECT id, double_optin FROM mail_lists
         WHERE id = :id AND workspace_id = :ws
    """), {"id": list_id, "ws": ws})).first()
    if not row:
        raise HTTPException(status_code=404, detail="list không tồn tại trong workspace")
    return {"id": int(row[0]), "double_optin": bool(row[1])}


@router.post("/subscribers", status_code=201)
async def add_subscriber(
    data: SubscriberIn,
    ws: str,
    list_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    list_meta = await _verify_list_in_ws(db, list_id, ws)

    email = _validate_email(str(data.email))
    status = data.status
    if status is None:
        status = "pending" if list_meta["double_optin"] else "active"
    if status not in _VALID_SUB_STATUSES:
        raise HTTPException(status_code=400, detail=f"status không hợp lệ: {status}")

    confirm_token = secrets.token_urlsafe(24) if status == "pending" else None
    cf = json.dumps(data.custom_fields, ensure_ascii=False) if data.custom_fields else None

    try:
        row = (await db.execute(text(f"""
            INSERT INTO mail_subscribers
              (list_id, workspace_id, email, first_name, last_name,
               custom_fields, tags, status, confirmation_token,
               confirmed_at, subscribed_at)
            VALUES (:lid, :ws, :em, :fn, :ln, CAST(:cf AS JSONB), :tags,
                    :st, :ct,
                    CASE WHEN :st = 'active' THEN NOW() ELSE NULL END,
                    NOW())
            RETURNING {_SUB_SELECT_COLS}
        """), {
            "lid": list_id, "ws": ws, "em": email,
            "fn": data.first_name, "ln": data.last_name,
            "cf": cf, "tags": data.tags or [],
            "st": status, "ct": confirm_token,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="email đã tồn tại trong list")
        log.exception("add_subscriber failed ws=%s list=%s", ws, list_id)
        raise HTTPException(status_code=502, detail=f"insert failed: {type(e).__name__}")

    out = _row_to_sub_dict(row)
    sub_id = out["id"]

    # If active immediately, fire 'subscribe' automations
    if status == "active":
        try:
            await trigger_subscribe_automations(
                db, list_id=list_id, subscriber_id=sub_id, workspace_id=ws,
            )
        except Exception:
            log.exception("trigger_subscribe_automations failed (best-effort)")

    return out


@router.post("/subscribers/bulk", status_code=201)
async def bulk_subscribers(
    data: BulkSubscribersIn,
    ws: str,
    list_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    list_meta = await _verify_list_in_ws(db, list_id, ws)

    if not data.subscribers:
        return {"inserted": 0, "skipped": 0, "errors": []}
    if len(data.subscribers) > MAX_BULK_SUBSCRIBERS:
        raise HTTPException(status_code=400,
                            detail=f"vượt {MAX_BULK_SUBSCRIBERS} subscribers/request")

    inserted, skipped = 0, 0
    errors: list[dict] = []
    default_status = "pending" if list_meta["double_optin"] else "active"

    for idx, sub in enumerate(data.subscribers):
        try:
            email = _validate_email(str(sub.email))
        except HTTPException:
            skipped += 1
            errors.append({"index": idx, "error": "invalid_email"})
            continue

        status = sub.status or default_status
        if status not in _VALID_SUB_STATUSES:
            skipped += 1
            errors.append({"index": idx, "error": f"invalid_status:{status}"})
            continue

        token = secrets.token_urlsafe(24) if status == "pending" else None
        cf = json.dumps(sub.custom_fields, ensure_ascii=False) if sub.custom_fields else None
        try:
            await db.execute(text("""
                INSERT INTO mail_subscribers
                  (list_id, workspace_id, email, first_name, last_name,
                   custom_fields, tags, status, confirmation_token,
                   confirmed_at, subscribed_at)
                VALUES (:lid, :ws, :em, :fn, :ln, CAST(:cf AS JSONB), :tags,
                        :st, :ct,
                        CASE WHEN :st = 'active' THEN NOW() ELSE NULL END,
                        NOW())
                ON CONFLICT (list_id, email) DO NOTHING
            """), {
                "lid": list_id, "ws": ws, "em": email,
                "fn": sub.first_name, "ln": sub.last_name,
                "cf": cf, "tags": sub.tags or [],
                "st": status, "ct": token,
            })
            inserted += 1
        except Exception as e:
            skipped += 1
            errors.append({"index": idx, "error": type(e).__name__})

    await db.commit()
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="mail.subscribers.bulk",
                         target=f"list#{list_id}", severity="ok",
                         metadata={"inserted": inserted, "skipped": skipped})
        await db.commit()
    except Exception:
        await db.rollback()

    return {"inserted": inserted, "skipped": skipped,
            "errors": errors[:20], "list_id": list_id}


@router.post("/subscribers/import", status_code=201)
async def import_subscribers_csv(
    request: Request,
    ws: str,
    list_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Import CSV. Body = raw CSV (max 5MB).
    Required column: email. Optional: first_name, last_name, tags (semicolon-separated).
    Any other columns become custom_fields entries.
    """
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    list_meta = await _verify_list_in_ws(db, list_id, ws)

    body = await request.body()
    if len(body) > MAX_CSV_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"CSV vượt {MAX_CSV_BYTES} bytes")
    try:
        text_body = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV không phải UTF-8")

    reader = csv.DictReader(io.StringIO(text_body))
    if not reader.fieldnames or "email" not in [c.strip().lower() for c in reader.fieldnames]:
        raise HTTPException(status_code=400, detail="CSV phải có cột 'email'")

    # Build column index (case-insensitive)
    cols = {c.strip().lower(): c for c in reader.fieldnames}

    inserted, skipped = 0, 0
    errors: list[dict] = []
    default_status = "pending" if list_meta["double_optin"] else "active"

    for ridx, raw in enumerate(reader):
        if ridx >= MAX_BULK_SUBSCRIBERS:
            errors.append({"index": ridx, "error": f"max_rows_{MAX_BULK_SUBSCRIBERS}"})
            break
        email_raw = (raw.get(cols.get("email", "")) or "").strip()
        if not email_raw:
            skipped += 1
            continue
        try:
            email = _validate_email(email_raw)
        except HTTPException:
            skipped += 1
            errors.append({"row": ridx + 2, "error": "invalid_email"})
            continue

        first = (raw.get(cols.get("first_name", "")) or "").strip() or None
        last = (raw.get(cols.get("last_name", "")) or "").strip() or None
        tags_raw = (raw.get(cols.get("tags", "")) or "").strip()
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []

        # Custom fields from any other columns
        cf: dict[str, Any] = {}
        known = {"email", "first_name", "last_name", "tags"}
        for low, orig in cols.items():
            if low not in known:
                v = raw.get(orig)
                if v not in (None, ""):
                    cf[low] = v

        token = secrets.token_urlsafe(24) if default_status == "pending" else None
        cf_json = json.dumps(cf, ensure_ascii=False) if cf else None
        try:
            await db.execute(text("""
                INSERT INTO mail_subscribers
                  (list_id, workspace_id, email, first_name, last_name,
                   custom_fields, tags, status, confirmation_token,
                   confirmed_at, subscribed_at)
                VALUES (:lid, :ws, :em, :fn, :ln, CAST(:cf AS JSONB), :tags,
                        :st, :ct,
                        CASE WHEN :st = 'active' THEN NOW() ELSE NULL END,
                        NOW())
                ON CONFLICT (list_id, email) DO NOTHING
            """), {
                "lid": list_id, "ws": ws, "em": email,
                "fn": first, "ln": last,
                "cf": cf_json, "tags": tags,
                "st": default_status, "ct": token,
            })
            inserted += 1
        except Exception as e:
            skipped += 1
            errors.append({"row": ridx + 2, "error": type(e).__name__})

    await db.commit()
    return {"inserted": inserted, "skipped": skipped,
            "errors": errors[:20], "list_id": list_id}


@router.patch("/subscribers/{sub_id}")
async def patch_subscriber(
    sub_id: int,
    data: SubscriberPatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    sets, params = [], {"id": sub_id, "ws": ws}
    if data.first_name is not None:
        sets.append("first_name = :fn"); params["fn"] = data.first_name
    if data.last_name is not None:
        sets.append("last_name = :ln"); params["ln"] = data.last_name
    if data.tags is not None:
        sets.append("tags = :tags"); params["tags"] = data.tags
    if data.custom_fields is not None:
        sets.append("custom_fields = CAST(:cf AS JSONB)")
        params["cf"] = json.dumps(data.custom_fields, ensure_ascii=False)
    if data.status is not None:
        if data.status not in _VALID_SUB_STATUSES:
            raise HTTPException(status_code=400, detail=f"status không hợp lệ: {data.status}")
        sets.append("status = :st"); params["st"] = data.status
        if data.status == "active":
            sets.append("confirmed_at = COALESCE(confirmed_at, NOW())")
        elif data.status == "unsubscribed":
            sets.append("unsubscribed_at = NOW()")
    if not sets:
        raise HTTPException(status_code=400, detail="không có field cần update")

    sql = (f"UPDATE mail_subscribers SET {', '.join(sets)} "
           f"WHERE id = :id AND workspace_id = :ws RETURNING {_SUB_SELECT_COLS}")
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_subscriber failed id=%s", sub_id)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(status_code=404, detail="subscriber không tồn tại")
    return _row_to_sub_dict(row)


@router.delete("/subscribers/{sub_id}")
async def delete_subscriber(
    sub_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM mail_subscribers WHERE id = :id AND workspace_id = :ws
    """), {"id": sub_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="subscriber không tồn tại")
    return {"deleted": sub_id}


@router.post("/subscribers/{sub_id}/unsubscribe")
async def unsubscribe(
    sub_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    res = await db.execute(text("""
        UPDATE mail_subscribers
           SET status = 'unsubscribed', unsubscribed_at = NOW()
         WHERE id = :id AND workspace_id = :ws
    """), {"id": sub_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="subscriber không tồn tại")
    return {"unsubscribed": sub_id}


@router.get("/subscribers/{sub_id}/confirm")
async def confirm_subscriber(
    sub_id: int,
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """PUBLIC double-opt-in confirmation. Token is single-use."""
    if not token or len(token) > 200:
        raise HTTPException(status_code=400, detail="token không hợp lệ")
    row = (await db.execute(text("""
        SELECT id, list_id, workspace_id, status, confirmation_token
          FROM mail_subscribers WHERE id = :id
    """), {"id": sub_id})).first()
    if not row or row[4] != token:
        raise HTTPException(status_code=400, detail="token không khớp hoặc đã dùng")
    if row[3] == "active":
        return {"confirmed": True, "already_active": True}

    await db.execute(text("""
        UPDATE mail_subscribers
           SET status = 'active', confirmed_at = NOW(),
               confirmation_token = NULL
         WHERE id = :id
    """), {"id": sub_id})
    await db.commit()

    try:
        await trigger_subscribe_automations(
            db, list_id=int(row[1]), subscriber_id=sub_id,
            workspace_id=row[2],
        )
    except Exception:
        log.exception("trigger_subscribe_automations after confirm failed (best-effort)")

    return {"confirmed": True, "subscriber_id": sub_id}


@router.get("/subscribers")
async def list_subscribers(
    ws: str,
    list_id: int | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit ∈ [1,{MAX_LIST_LIMIT}]")

    sql = f"SELECT {_SUB_SELECT_COLS} FROM mail_subscribers WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if list_id is not None:
        sql += " AND list_id = :lid"; params["lid"] = list_id
    if status:
        if status not in _VALID_SUB_STATUSES:
            raise HTTPException(status_code=400, detail=f"status không hợp lệ: {status}")
        sql += " AND status = :st"; params["st"] = status
    if tag:
        sql += " AND :tag = ANY(tags)"; params["tag"] = tag
    sql += " ORDER BY subscribed_at DESC LIMIT :lim OFFSET :off"

    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "subscribers": [_row_to_sub_dict(r) for r in rows]}


# ─────────────────────────────────────────────────────────
# 3. TEMPLATES
# ─────────────────────────────────────────────────────────
class TemplateIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=120)
    subject: str = Field(..., min_length=1, max_length=500)
    body_html: str = Field(..., min_length=1, max_length=MAX_BODY_BYTES)
    body_text: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    variables: list[str] | None = None
    category: str | None = Field(default=None, max_length=40)

    model_config = {"populate_by_name": True}


class TemplatePatchIn(BaseModel):
    name: str | None = None
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    variables: list[str] | None = None
    category: str | None = None


def _row_to_tpl_dict(r) -> dict:
    return {
        "id": int(r[0]), "workspace_id": r[1], "name": r[2],
        "subject": r[3], "body_html": r[4], "body_text": r[5],
        "variables": list(r[6]) if r[6] else [],
        "category": r[7], "is_system": bool(r[8]),
        "created_at": r[9].isoformat() if r[9] else None,
    }


_TPL_COLS = ("id, workspace_id, name, subject, body_html, body_text, "
             "variables, category, is_system, created_at")


@router.get("/templates")
async def list_templates(
    ws: str,
    category: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    sql = f"SELECT {_TPL_COLS} FROM mail_templates WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws}
    if category:
        sql += " AND category = :cat"; params["cat"] = category
    sql += " ORDER BY created_at DESC"
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "templates": [_row_to_tpl_dict(r) for r in rows]}


@router.post("/templates", status_code=201)
async def create_template(
    data: TemplateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    try:
        row = (await db.execute(text(f"""
            INSERT INTO mail_templates
              (workspace_id, name, subject, body_html, body_text, variables, category)
            VALUES (:ws, :n, :s, :h, :t, :v, :c)
            RETURNING {_TPL_COLS}
        """), {
            "ws": ws, "n": data.name, "s": data.subject,
            "h": data.body_html, "t": data.body_text,
            "v": data.variables or [], "c": data.category,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="template name đã tồn tại")
        log.exception("create_template failed ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"insert failed: {type(e).__name__}")
    return _row_to_tpl_dict(row)


@router.patch("/templates/{tid}")
async def patch_template(
    tid: int,
    data: TemplatePatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets, params = [], {"id": tid, "ws": ws}
    for f in ("name", "subject", "body_html", "body_text", "category"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}"); params[f] = v
    if data.variables is not None:
        sets.append("variables = :variables"); params["variables"] = data.variables
    if not sets:
        raise HTTPException(status_code=400, detail="không có field cần update")
    sql = (f"UPDATE mail_templates SET {', '.join(sets)} "
           f"WHERE id = :id AND workspace_id = :ws RETURNING {_TPL_COLS}")
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_template failed id=%s", tid)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(status_code=404, detail="template không tồn tại")
    return _row_to_tpl_dict(row)


@router.delete("/templates/{tid}")
async def delete_template(
    tid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM mail_templates
         WHERE id = :id AND workspace_id = :ws AND is_system = FALSE
    """), {"id": tid, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="template không tồn tại hoặc là system")
    return {"deleted": tid}


@router.post("/templates/{tid}/preview")
async def preview_template(
    tid: int,
    ws: str,
    sample: dict[str, Any] = Body(default_factory=dict),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Render template with sample subscriber-like data."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (await db.execute(text("""
        SELECT subject, body_html, body_text FROM mail_templates
         WHERE id = :id AND workspace_id = :ws
    """), {"id": tid, "ws": ws})).first()
    if not row:
        raise HTTPException(status_code=404, detail="template không tồn tại")

    return {
        "subject": render_template(row[0] or "", sample),
        "body_html": render_template(row[1] or "", sample),
        "body_text": render_template(row[2] or "", sample) if row[2] else None,
    }


# ─────────────────────────────────────────────────────────
# 4. CAMPAIGNS
# ─────────────────────────────────────────────────────────
class CampaignIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=200)
    subject: str = Field(..., min_length=1, max_length=500)
    from_email: EmailStr
    from_name: str | None = Field(default=None, max_length=120)
    reply_to: EmailStr | None = None
    body_html: str = Field(..., min_length=1, max_length=MAX_BODY_BYTES)
    body_text: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    template_id: int | None = None
    list_id: int | None = None
    segment_filter: dict[str, Any] | None = None
    schedule_type: str = Field(default="immediate")
    scheduled_at: datetime | None = None

    model_config = {"populate_by_name": True}

    @field_validator("schedule_type")
    @classmethod
    def _vst(cls, v: str) -> str:
        if v not in ("immediate", "scheduled", "recurring"):
            raise ValueError("schedule_type phải là immediate|scheduled|recurring")
        return v


class CampaignPatchIn(BaseModel):
    name: str | None = None
    subject: str | None = None
    from_email: EmailStr | None = None
    from_name: str | None = None
    reply_to: EmailStr | None = None
    body_html: str | None = None
    body_text: str | None = None
    list_id: int | None = None
    segment_filter: dict[str, Any] | None = None
    schedule_type: str | None = None
    scheduled_at: datetime | None = None


def _row_to_campaign_dict(r) -> dict:
    return {
        "id": int(r[0]), "workspace_id": r[1], "name": r[2],
        "subject": r[3], "from_email": r[4], "from_name": r[5],
        "reply_to": r[6], "body_html": r[7], "body_text": r[8],
        "template_id": int(r[9]) if r[9] else None,
        "list_id": int(r[10]) if r[10] else None,
        "segment_filter": _serialize_jsonb(r[11]),
        "schedule_type": r[12], "scheduled_at": r[13].isoformat() if r[13] else None,
        "status": r[14],
        "total_recipients": int(r[15] or 0), "sent_count": int(r[16] or 0),
        "delivered_count": int(r[17] or 0), "open_count": int(r[18] or 0),
        "click_count": int(r[19] or 0), "bounce_count": int(r[20] or 0),
        "unsubscribe_count": int(r[21] or 0),
        "started_at": r[22].isoformat() if r[22] else None,
        "completed_at": r[23].isoformat() if r[23] else None,
        "created_at": r[24].isoformat() if r[24] else None,
    }


_CAMPAIGN_COLS = ("id, workspace_id, name, subject, from_email, from_name, "
                  "reply_to, body_html, body_text, template_id, list_id, "
                  "segment_filter, schedule_type, scheduled_at, status, "
                  "total_recipients, sent_count, delivered_count, open_count, "
                  "click_count, bounce_count, unsubscribe_count, "
                  "started_at, completed_at, created_at")


@router.get("/campaigns")
async def list_campaigns(
    ws: str,
    status: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    sql = f"SELECT {_CAMPAIGN_COLS} FROM mail_campaigns WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws}
    if status:
        sql += " AND status = :st"; params["st"] = status
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = (await db.execute(text(sql), params)).all()
    return {"workspace_id": ws, "count": len(rows),
            "campaigns": [_row_to_campaign_dict(r) for r in rows]}


@router.post("/campaigns", status_code=201)
async def create_campaign(
    data: CampaignIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sf = json.dumps(data.segment_filter, ensure_ascii=False) if data.segment_filter else None
    try:
        row = (await db.execute(text(f"""
            INSERT INTO mail_campaigns
              (workspace_id, name, subject, from_email, from_name, reply_to,
               body_html, body_text, template_id, list_id, segment_filter,
               schedule_type, scheduled_at, status)
            VALUES (:ws, :n, :s, :fe, :fn, :rt, :h, :t,
                    :tid, :lid, CAST(:sf AS JSONB),
                    :stype, :sat,
                    CASE WHEN :stype = 'scheduled' THEN 'scheduled' ELSE 'draft' END)
            RETURNING {_CAMPAIGN_COLS}
        """), {
            "ws": ws, "n": data.name, "s": data.subject,
            "fe": str(data.from_email), "fn": data.from_name,
            "rt": str(data.reply_to) if data.reply_to else None,
            "h": data.body_html, "t": data.body_text,
            "tid": data.template_id, "lid": data.list_id,
            "sf": sf, "stype": data.schedule_type, "sat": data.scheduled_at,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_campaign failed ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"insert failed: {type(e).__name__}")
    out = _row_to_campaign_dict(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="mail.campaign.create",
                         target=f"campaign#{out['id']}", severity="ok",
                         metadata={"name": data.name})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.patch("/campaigns/{cid}")
async def patch_campaign(
    cid: int,
    data: CampaignPatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    # Block edit when campaign already past 'draft'
    cur = (await db.execute(text("""
        SELECT status FROM mail_campaigns
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cid, "ws": ws})).first()
    if not cur:
        raise HTTPException(status_code=404, detail="campaign không tồn tại")
    if cur[0] in ("sending", "sent"):
        raise HTTPException(status_code=409, detail=f"campaign đang {cur[0]} — không sửa được")

    sets, params = [], {"id": cid, "ws": ws}
    string_fields = ("name", "subject", "from_name", "body_html", "body_text",
                     "schedule_type")
    email_fields = ("from_email", "reply_to")
    for f in string_fields:
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}"); params[f] = v
    for f in email_fields:
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}"); params[f] = str(v)
    if data.list_id is not None:
        sets.append("list_id = :lid"); params["lid"] = data.list_id
    if data.segment_filter is not None:
        sets.append("segment_filter = CAST(:sf AS JSONB)")
        params["sf"] = json.dumps(data.segment_filter, ensure_ascii=False)
    if data.scheduled_at is not None:
        sets.append("scheduled_at = :sat"); params["sat"] = data.scheduled_at
    if not sets:
        raise HTTPException(status_code=400, detail="không có field cần update")

    sql = (f"UPDATE mail_campaigns SET {', '.join(sets)} "
           f"WHERE id = :id AND workspace_id = :ws RETURNING {_CAMPAIGN_COLS}")
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_campaign failed id=%s", cid)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")
    return _row_to_campaign_dict(row)


@router.delete("/campaigns/{cid}")
async def delete_campaign(
    cid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM mail_campaigns
         WHERE id = :id AND workspace_id = :ws
           AND status IN ('draft', 'scheduled', 'paused')
    """), {"id": cid, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404,
                            detail="campaign không tồn tại hoặc đã sending/sent")
    return {"deleted": cid}


class SendTestIn(BaseModel):
    to_email: EmailStr


@router.post("/campaigns/{cid}/send-test")
async def send_test_campaign(
    cid: int,
    data: SendTestIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    row = (await db.execute(text("""
        SELECT subject, body_html, body_text FROM mail_campaigns
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cid, "ws": ws})).first()
    if not row:
        raise HTTPException(status_code=404, detail="campaign không tồn tại")

    fake_sub = {
        "email": str(data.to_email),
        "first_name": "Test", "last_name": "User",
        "tags": ["test"],
        "custom_fields": {"company": "Zeni Test"},
    }
    subject = "[TEST] " + render_template(row[0] or "", fake_sub)
    body_html = render_template(row[1] or "", fake_sub)
    body_text = render_template(row[2] or "", fake_sub) if row[2] else None

    ok = await send_email(
        to=str(data.to_email), subject=subject,
        body_html=body_html, body_text=body_text,
    )
    return {"sent": ok, "to": str(data.to_email), "campaign_id": cid}


class ScheduleIn(BaseModel):
    scheduled_at: datetime


@router.post("/campaigns/{cid}/schedule")
async def schedule_campaign(
    cid: int,
    data: ScheduleIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    if data.scheduled_at < _now() - timedelta(minutes=1):
        raise HTTPException(status_code=400, detail="scheduled_at phải ở tương lai")
    res = await db.execute(text("""
        UPDATE mail_campaigns
           SET schedule_type = 'scheduled',
               scheduled_at = :sat,
               status = 'scheduled'
         WHERE id = :id AND workspace_id = :ws
           AND status IN ('draft', 'paused', 'scheduled')
    """), {"id": cid, "ws": ws, "sat": data.scheduled_at})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404,
                            detail="campaign không tồn tại hoặc không thể schedule")
    return {"scheduled": cid, "scheduled_at": data.scheduled_at.isoformat()}


@router.post("/campaigns/{cid}/send-now")
async def send_now_campaign(
    cid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    cur = (await db.execute(text("""
        SELECT status FROM mail_campaigns
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cid, "ws": ws})).first()
    if not cur:
        raise HTTPException(status_code=404, detail="campaign không tồn tại")
    if cur[0] in ("sending", "sent"):
        raise HTTPException(status_code=409, detail=f"campaign đang {cur[0]}")

    try:
        result = await enqueue_campaign(db, cid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        log.exception("enqueue_campaign failed cid=%s", cid)
        raise HTTPException(status_code=502, detail=f"enqueue failed: {type(e).__name__}")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="mail.campaign.send",
                         target=f"campaign#{cid}", severity="ok",
                         metadata={"queued": result.get("queued", 0)})
        await db.commit()
    except Exception:
        await db.rollback()
    return result


@router.post("/campaigns/{cid}/pause")
async def pause_campaign(
    cid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        UPDATE mail_campaigns SET status = 'paused'
         WHERE id = :id AND workspace_id = :ws
           AND status IN ('scheduled', 'sending')
    """), {"id": cid, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404,
                            detail="campaign không tồn tại hoặc không thể pause")
    return {"paused": cid}


@router.get("/campaigns/{cid}/stats")
async def campaign_stats(
    cid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    row = (await db.execute(text("""
        SELECT total_recipients, sent_count, delivered_count, open_count,
               click_count, bounce_count, unsubscribe_count, status,
               started_at, completed_at
          FROM mail_campaigns
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cid, "ws": ws})).first()
    if not row:
        raise HTTPException(status_code=404, detail="campaign không tồn tại")
    total, sent, delivered, opens, clicks, bounces, unsubs, status, started, ended = row
    sent_n = int(sent or 0)
    rate = lambda x: round((int(x or 0) / sent_n) * 100, 2) if sent_n else 0.0
    return {
        "campaign_id": cid,
        "status": status,
        "total_recipients": int(total or 0),
        "sent": sent_n,
        "delivered": int(delivered or 0),
        "opens": int(opens or 0),
        "clicks": int(clicks or 0),
        "bounces": int(bounces or 0),
        "unsubscribes": int(unsubs or 0),
        "open_rate_pct": rate(opens),
        "click_rate_pct": rate(clicks),
        "bounce_rate_pct": rate(bounces),
        "unsubscribe_rate_pct": rate(unsubs),
        "started_at": started.isoformat() if started else None,
        "completed_at": ended.isoformat() if ended else None,
    }


# ─────────────────────────────────────────────────────────
# 5. TRACKING (PUBLIC)
# ─────────────────────────────────────────────────────────
@router.get("/track/open/{message_id}.gif")
async def track_open(
    message_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """1×1 GIF + record open event. Always returns the pixel even if record fails."""
    try:
        if message_id and len(message_id) <= 80:
            row = (await db.execute(text("""
                SELECT id, campaign_id, subscriber_id, opened_at, workspace_id
                  FROM mail_sends WHERE message_id = :mid
            """), {"mid": message_id})).first()
            if row:
                send_id, cid, sub_id, opened_at, ws = row
                if not opened_at:
                    await db.execute(text("""
                        UPDATE mail_sends
                           SET status = CASE WHEN status IN ('sent','delivered')
                                              THEN 'opened' ELSE status END,
                               opened_at = NOW()
                         WHERE id = :id
                    """), {"id": send_id})
                    if cid:
                        await db.execute(text("""
                            UPDATE mail_campaigns SET open_count = open_count + 1
                             WHERE id = :cid
                        """), {"cid": cid})
                    if sub_id:
                        await db.execute(text("""
                            UPDATE mail_subscribers
                               SET last_engagement_at = NOW()
                             WHERE id = :sid
                        """), {"sid": sub_id})
                    await db.commit()
    except Exception:
        log.exception("track_open record failed (best-effort)")
        try:
            await db.rollback()
        except Exception:
            pass

    return Response(
        content=_TRACK_PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/track/click/{message_id}")
async def track_click(
    message_id: str,
    request: Request,
    url: str = Query(..., max_length=2000),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Record click + 302-redirect to the original URL."""
    # Whitelist scheme to prevent open-redirect to javascript: etc.
    low = url.strip().lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise HTTPException(status_code=400, detail="url phải http/https")

    try:
        if message_id and len(message_id) <= 80:
            row = (await db.execute(text("""
                SELECT id, campaign_id, subscriber_id, workspace_id,
                       opened_at, clicked_at
                  FROM mail_sends WHERE message_id = :mid
            """), {"mid": message_id})).first()
            if row:
                send_id, cid, sub_id, ws, opened_at, clicked_at = row
                ua = request.headers.get("user-agent", "")[:500]
                ip = request.client.host if request.client else None
                await db.execute(text("""
                    INSERT INTO mail_clicks (send_id, workspace_id, url,
                                             user_agent, ip_address)
                    VALUES (:sid, :ws, :u, :ua, CAST(:ip AS INET))
                """), {"sid": send_id, "ws": ws, "u": url[:2000],
                       "ua": ua, "ip": ip})
                if not clicked_at:
                    # First click ever — update send row
                    await db.execute(text("""
                        UPDATE mail_sends
                           SET status = 'clicked', clicked_at = NOW(),
                               opened_at = COALESCE(opened_at, NOW())
                         WHERE id = :id
                    """), {"id": send_id})
                    if cid:
                        # Bump click_count always; bump open_count only if
                        # this click was also the first open event.
                        open_inc = 0 if opened_at else 1
                        await db.execute(text("""
                            UPDATE mail_campaigns
                               SET click_count = click_count + 1,
                                   open_count = open_count + :oi
                             WHERE id = :cid
                        """), {"cid": cid, "oi": open_inc})
                if sub_id:
                    await db.execute(text("""
                        UPDATE mail_subscribers SET last_engagement_at = NOW()
                         WHERE id = :sid
                    """), {"sid": sub_id})
                await db.commit()
    except Exception:
        log.exception("track_click record failed (best-effort)")
        try:
            await db.rollback()
        except Exception:
            pass

    return RedirectResponse(url=url, status_code=302)


# ─────────────────────────────────────────────────────────
# 6. AUTOMATIONS
# ─────────────────────────────────────────────────────────
class AutomationIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=200)
    trigger_type: str = Field(..., max_length=40)
    trigger_config: dict[str, Any] | None = None
    list_id: int | None = None
    steps: list[dict[str, Any]] = Field(..., min_length=1, max_length=20)
    is_active: bool = Field(default=False)

    model_config = {"populate_by_name": True}

    @field_validator("trigger_type")
    @classmethod
    def _vtt(cls, v: str) -> str:
        if v not in ("subscribe", "tag_added", "date", "event", "inactivity"):
            raise ValueError("trigger_type không hợp lệ")
        return v


class AutomationPatchIn(BaseModel):
    name: str | None = None
    trigger_type: str | None = None
    trigger_config: dict[str, Any] | None = None
    list_id: int | None = None
    steps: list[dict[str, Any]] | None = None
    is_active: bool | None = None


def _row_to_automation_dict(r) -> dict:
    return {
        "id": int(r[0]), "workspace_id": r[1], "name": r[2],
        "trigger_type": r[3], "trigger_config": _serialize_jsonb(r[4]),
        "list_id": int(r[5]) if r[5] else None,
        "steps": _serialize_jsonb(r[6]),
        "is_active": bool(r[7]),
        "created_at": r[8].isoformat() if r[8] else None,
    }


_AUTO_COLS = ("id, workspace_id, name, trigger_type, trigger_config, "
              "list_id, steps, is_active, created_at")


@router.get("/automations")
async def list_automations(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text(f"""
        SELECT {_AUTO_COLS} FROM mail_automations
         WHERE workspace_id = :ws ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return {"workspace_id": ws, "count": len(rows),
            "automations": [_row_to_automation_dict(r) for r in rows]}


@router.post("/automations", status_code=201)
async def create_automation(
    data: AutomationIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    tc = json.dumps(data.trigger_config, ensure_ascii=False) if data.trigger_config else None
    steps_json = json.dumps(data.steps, ensure_ascii=False)
    try:
        row = (await db.execute(text(f"""
            INSERT INTO mail_automations
              (workspace_id, name, trigger_type, trigger_config, list_id,
               steps, is_active)
            VALUES (:ws, :n, :tt, CAST(:tc AS JSONB), :lid,
                    CAST(:steps AS JSONB), :act)
            RETURNING {_AUTO_COLS}
        """), {
            "ws": ws, "n": data.name, "tt": data.trigger_type,
            "tc": tc, "lid": data.list_id,
            "steps": steps_json, "act": data.is_active,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_automation failed ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"insert failed: {type(e).__name__}")
    return _row_to_automation_dict(row)


@router.patch("/automations/{aid}")
async def patch_automation(
    aid: int,
    data: AutomationPatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    sets, params = [], {"id": aid, "ws": ws}
    if data.name is not None:
        sets.append("name = :n"); params["n"] = data.name
    if data.trigger_type is not None:
        if data.trigger_type not in ("subscribe", "tag_added", "date", "event", "inactivity"):
            raise HTTPException(status_code=400, detail="trigger_type không hợp lệ")
        sets.append("trigger_type = :tt"); params["tt"] = data.trigger_type
    if data.trigger_config is not None:
        sets.append("trigger_config = CAST(:tc AS JSONB)")
        params["tc"] = json.dumps(data.trigger_config, ensure_ascii=False)
    if data.list_id is not None:
        sets.append("list_id = :lid"); params["lid"] = data.list_id
    if data.steps is not None:
        sets.append("steps = CAST(:steps AS JSONB)")
        params["steps"] = json.dumps(data.steps, ensure_ascii=False)
    if data.is_active is not None:
        sets.append("is_active = :act"); params["act"] = data.is_active
    if not sets:
        raise HTTPException(status_code=400, detail="không có field cần update")
    sql = (f"UPDATE mail_automations SET {', '.join(sets)} "
           f"WHERE id = :id AND workspace_id = :ws RETURNING {_AUTO_COLS}")
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_automation failed id=%s", aid)
        raise HTTPException(status_code=502, detail=f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(status_code=404, detail="automation không tồn tại")
    return _row_to_automation_dict(row)


@router.delete("/automations/{aid}")
async def delete_automation(
    aid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        DELETE FROM mail_automations WHERE id = :id AND workspace_id = :ws
    """), {"id": aid, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="automation không tồn tại")
    return {"deleted": aid}


@router.post("/automations/{aid}/activate")
async def activate_automation(
    aid: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    res = await db.execute(text("""
        UPDATE mail_automations SET is_active = TRUE
         WHERE id = :id AND workspace_id = :ws
    """), {"id": aid, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="automation không tồn tại")
    return {"activated": aid}


@router.get("/automations/{aid}/enrollments")
async def list_enrollments(
    aid: int,
    ws: str,
    status: str | None = None,
    limit: int = 200,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    sql = """
        SELECT id, automation_id, subscriber_id, workspace_id, current_step,
               next_step_at, status, enrolled_at
          FROM mail_enrollments
         WHERE workspace_id = :ws AND automation_id = :aid
    """
    params: dict[str, Any] = {"ws": ws, "aid": aid, "lim": min(max(limit, 1), MAX_LIST_LIMIT)}
    if status:
        sql += " AND status = :st"; params["st"] = status
    sql += " ORDER BY enrolled_at DESC LIMIT :lim"
    rows = (await db.execute(text(sql), params)).all()
    return {
        "automation_id": aid,
        "count": len(rows),
        "enrollments": [
            {
                "id": int(r[0]),
                "automation_id": int(r[1]),
                "subscriber_id": int(r[2]),
                "workspace_id": r[3],
                "current_step": int(r[4] or 0),
                "next_step_at": r[5].isoformat() if r[5] else None,
                "status": r[6],
                "enrolled_at": r[7].isoformat() if r[7] else None,
            } for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────
# 7. ANALYTICS
# ─────────────────────────────────────────────────────────
@router.get("/analytics/overview")
async def analytics_overview(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    f = from_ or (_now() - timedelta(days=30))
    t = to or _now()
    if t < f:
        raise HTTPException(status_code=400, detail="to phải >= from")

    sends_row = (await db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status IN ('sent','delivered','opened','clicked'))::INT,
               COUNT(*) FILTER (WHERE opened_at IS NOT NULL)::INT,
               COUNT(*) FILTER (WHERE clicked_at IS NOT NULL)::INT,
               COUNT(*) FILTER (WHERE status = 'bounced')::INT
          FROM mail_sends
         WHERE workspace_id = :ws
           AND COALESCE(sent_at, opened_at, clicked_at) BETWEEN :f AND :t
    """), {"ws": ws, "f": f, "t": t})).first()

    sub_row = (await db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status = 'active')::INT,
               COUNT(*) FILTER (WHERE subscribed_at BETWEEN :f AND :t)::INT,
               COUNT(*) FILTER (WHERE unsubscribed_at BETWEEN :f AND :t)::INT
          FROM mail_subscribers
         WHERE workspace_id = :ws
    """), {"ws": ws, "f": f, "t": t})).first()

    sent = int(sends_row[0] or 0)
    opens = int(sends_row[1] or 0)
    clicks = int(sends_row[2] or 0)
    bounces = int(sends_row[3] or 0)
    new_subs = int(sub_row[1] or 0)
    unsubs_p = int(sub_row[2] or 0)
    growth = new_subs - unsubs_p

    return {
        "workspace_id": ws,
        "from": f.isoformat(), "to": t.isoformat(),
        "sent": sent, "opens": opens, "clicks": clicks, "bounces": bounces,
        "open_rate_pct": round((opens / sent * 100) if sent else 0, 2),
        "click_rate_pct": round((clicks / sent * 100) if sent else 0, 2),
        "bounce_rate_pct": round((bounces / sent * 100) if sent else 0, 2),
        "active_subscribers": int(sub_row[0] or 0),
        "new_subscribers": new_subs,
        "unsubscribes": unsubs_p,
        "net_growth": growth,
        "growth_rate_pct": round((growth / int(sub_row[0]) * 100), 2) if sub_row[0] else 0,
    }


@router.get("/analytics/best-times")
async def analytics_best_times(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Best send hour by engagement = (opens + 2*clicks) / sends bucketed by hour-of-day."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    rows = (await db.execute(text("""
        SELECT EXTRACT(HOUR FROM sent_at)::INT AS hr,
               COUNT(*)::INT AS sends,
               COUNT(opened_at)::INT AS opens,
               COUNT(clicked_at)::INT AS clicks
          FROM mail_sends
         WHERE workspace_id = :ws AND sent_at IS NOT NULL
         GROUP BY hr ORDER BY hr ASC
    """), {"ws": ws})).all()

    buckets = []
    for hr, sends, opens, clicks in rows:
        s = int(sends or 0)
        score = ((int(opens or 0) + 2 * int(clicks or 0)) / s) if s else 0
        buckets.append({
            "hour_utc": int(hr or 0),
            "sends": s,
            "opens": int(opens or 0),
            "clicks": int(clicks or 0),
            "engagement_score": round(score, 4),
        })
    best = max(buckets, key=lambda b: b["engagement_score"]) if buckets else None
    return {
        "workspace_id": ws,
        "buckets": buckets,
        "best_hour_utc": best["hour_utc"] if best else None,
        "best_engagement_score": best["engagement_score"] if best else None,
    }
