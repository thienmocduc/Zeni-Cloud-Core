"""
Zeni Cloud Core — Messaging API (Pub/Sub + scheduled tasks + DLQ).

Endpoints:
  Topics
    POST   /messaging/topics                       — create topic
    GET    /messaging/topics?ws=                   — list workspace topics
    DELETE /messaging/topics/{id}?ws=

  Subscriptions
    POST   /messaging/topics/{id}/subscriptions    — create subscription
    GET    /messaging/topics/{id}/subscriptions?ws=

  Publish / read
    POST   /messaging/topics/{id}/publish?ws=      — publish message → fan-out
    GET    /messaging/messages?ws=&topic=&limit=   — list recent messages

  Scheduled tasks (Cloud Tasks-like)
    POST   /messaging/tasks                        — schedule task
    GET    /messaging/tasks?ws=&status=            — list
    DELETE /messaging/tasks/{id}?ws=               — cancel pending

  DLQ
    GET    /messaging/dlq?ws=                      — list DLQ entries
    POST   /messaging/dlq/{id}/requeue?ws=         — requeue from DLQ

Quy tắc:
  - Mọi endpoint require_user + require_workspace_access(ws)
  - PAT scope check: cần "automation" hoặc "full"
  - audit_push cho mọi state-changing op
  - Try/except Exception → 502
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.messaging")
router = APIRouter(prefix="/messaging", tags=["messaging"])


# ─── Constants & validators ──────────────────────────
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-\.]{0,118}$")
MAX_PAYLOAD_BYTES = 1_000_000
MAX_TOPICS_PER_WS = 200
MAX_SUBS_PER_TOPIC = 50
MAX_LIST_LIMIT = 500
DEFAULT_RETENTION_SECONDS = 604_800       # 7 days
MAX_RETENTION_SECONDS = 86_400 * 30       # 30 days
MIN_RETENTION_SECONDS = 60                # 1 minute


def _validate_name(name: str, label: str = "name") -> None:
    if not name or not isinstance(name, str) or not NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"{label} không hợp lệ. Định dạng: bắt đầu chữ, [a-zA-Z0-9_-.], max 119 ký tự",
        )


def _validate_payload(payload: Any) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"payload không JSON-serializable: {e}")
    if len(s.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"payload vượt {MAX_PAYLOAD_BYTES} bytes")
    return s


def _check_scope(me: CurrentUser) -> None:
    """PAT phải có scope 'automation' hoặc 'full'. JWT user thì pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "automation" not in scopes:
        raise HTTPException(
            status_code=403,
            detail="PAT cần scope 'automation' hoặc 'full' để dùng /messaging",
        )


# ─── Schemas ─────────────────────────────────────────
class TopicCreateIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    schema_: dict | None = Field(default=None, alias="schema",
                                 description="JSON schema validate payload (optional)")
    retention_seconds: int = Field(default=DEFAULT_RETENTION_SECONDS,
                                    ge=MIN_RETENTION_SECONDS, le=MAX_RETENTION_SECONDS)

    model_config = {"populate_by_name": True}


class SubscriptionCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    delivery_type: str = Field(default="webhook")
    webhook_url: str | None = Field(default=None, max_length=2000)
    webhook_secret: str | None = Field(default=None, min_length=8, max_length=200,
                                       description="HMAC signing key. None = auto-generate.")
    filter_expression: str | None = Field(default=None, max_length=500)
    max_retry_count: int = Field(default=5, ge=1, le=20)
    ack_deadline_seconds: int = Field(default=60, ge=10, le=3600)
    enabled: bool = Field(default=True)

    @field_validator("delivery_type")
    @classmethod
    def _validate_dt(cls, v: str) -> str:
        if v not in ("webhook", "pull", "queue"):
            raise ValueError("delivery_type phải là 'webhook', 'pull' hoặc 'queue'")
        return v


class PublishIn(BaseModel):
    payload: Any = Field(..., description="JSON-serializable payload")
    attributes: dict[str, Any] | None = Field(default=None,
                                              description="Attributes for filter routing")


class ScheduledTaskIn(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=32, alias="ws")
    task_name: str = Field(..., min_length=1, max_length=120)
    target_url: str = Field(..., min_length=8, max_length=2000)
    method: str = Field(default="POST")
    headers: dict[str, str] | None = Field(default=None)
    body: Any | None = Field(default=None)
    scheduled_at: datetime = Field(..., description="UTC ISO timestamp")
    max_retries: int = Field(default=3, ge=0, le=10)

    model_config = {"populate_by_name": True}

    @field_validator("method")
    @classmethod
    def _validate_method(cls, v: str) -> str:
        m = v.upper()
        if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("method không hợp lệ")
        return m


# ─── Helpers ─────────────────────────────────────────
def _row_to_topic_dict(r) -> dict:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "name": r[2],
        "description": r[3],
        "schema": r[4],
        "retention_seconds": int(r[5]) if r[5] is not None else None,
        "created_at": r[6].isoformat() if r[6] else None,
    }


def _row_to_sub_dict(r) -> dict:
    return {
        "id": int(r[0]),
        "topic_id": int(r[1]),
        "workspace_id": r[2],
        "name": r[3],
        "delivery_type": r[4],
        "webhook_url": r[5],
        "webhook_secret": "***" if r[6] else None,   # never expose secret
        "filter_expression": r[7],
        "max_retry_count": int(r[8]) if r[8] is not None else None,
        "ack_deadline_seconds": int(r[9]) if r[9] is not None else None,
        "enabled": bool(r[10]),
        "created_at": r[11].isoformat() if r[11] else None,
    }


# ─── Topic endpoints ─────────────────────────────────
@router.post("/topics", status_code=201)
async def create_topic(
    data: TopicCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo topic mới trong workspace."""
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được tạo topic")
    _validate_name(data.name, "topic name")

    # Check quota
    cnt = (await db.execute(text(
        "SELECT COUNT(*)::INT FROM pubsub_topics WHERE workspace_id = :ws"
    ), {"ws": ws})).scalar() or 0
    if cnt >= MAX_TOPICS_PER_WS:
        raise HTTPException(status_code=429,
                            detail=f"workspace đã đạt giới hạn {MAX_TOPICS_PER_WS} topics")

    schema_json = json.dumps(data.schema_, ensure_ascii=False) if data.schema_ else None
    try:
        row = (await db.execute(text("""
            INSERT INTO pubsub_topics
              (workspace_id, name, description, schema, retention_seconds)
            VALUES
              (:ws, :n, :d, CAST(:s AS JSONB), :r)
            RETURNING id, workspace_id, name, description, schema,
                      retention_seconds, created_at
        """), {
            "ws": ws, "n": data.name, "d": data.description,
            "s": schema_json, "r": data.retention_seconds,
        })).first()
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        # Likely UNIQUE violation
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="topic name đã tồn tại trong workspace")
        log.exception("create_topic failed for ws=%s name=%s", ws, data.name)
        raise HTTPException(status_code=502, detail=f"không tạo được topic: {type(e).__name__}")

    if row is None:
        raise HTTPException(status_code=502, detail="không tạo được topic")

    out = _row_to_topic_dict(row)

    # Audit
    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.topic.create", target=f"topic#{out['id']}",
            severity="ok",
            metadata={"name": data.name, "retention_seconds": data.retention_seconds},
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.topic.create (best-effort)")
        await db.rollback()

    return out


@router.get("/topics")
async def list_topics(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List topics của workspace."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    try:
        rows = (await db.execute(text("""
            SELECT id, workspace_id, name, description, schema,
                   retention_seconds, created_at
            FROM pubsub_topics
            WHERE workspace_id = :ws
            ORDER BY created_at DESC
        """), {"ws": ws})).all()
    except Exception as e:
        log.exception("list_topics failed for ws=%s", ws)
        raise HTTPException(status_code=502, detail=f"không list được topics: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "count": len(rows),
        "topics": [_row_to_topic_dict(r) for r in rows],
    }


@router.delete("/topics/{topic_id}")
async def delete_topic(
    topic_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Xoá topic + cascade subscriptions + messages."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được xoá topic")

    try:
        res = await db.execute(text("""
            DELETE FROM pubsub_topics
            WHERE id = :id AND workspace_id = :ws
        """), {"id": topic_id, "ws": ws})
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("delete_topic failed for ws=%s topic=%s", ws, topic_id)
        raise HTTPException(status_code=502, detail=f"không xoá được topic: {type(e).__name__}")

    deleted = bool(res.rowcount)
    if not deleted:
        raise HTTPException(status_code=404, detail="topic không tồn tại trong workspace")

    # Audit
    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.topic.delete", target=f"topic#{topic_id}",
            severity="warn",
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.topic.delete (best-effort)")
        await db.rollback()

    return {"ok": True, "deleted": True, "topic_id": topic_id}


# ─── Subscription endpoints ──────────────────────────
@router.post("/topics/{topic_id}/subscriptions", status_code=201)
async def create_subscription(
    topic_id: int,
    ws: str,
    data: SubscriptionCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo subscription cho topic."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được tạo subscription")
    _validate_name(data.name, "subscription name")

    if data.delivery_type == "webhook" and not data.webhook_url:
        raise HTTPException(status_code=400,
                            detail="delivery_type=webhook bắt buộc có webhook_url")

    # Verify topic ownership
    t = (await db.execute(text("""
        SELECT id FROM pubsub_topics WHERE id = :id AND workspace_id = :ws
    """), {"id": topic_id, "ws": ws})).first()
    if t is None:
        raise HTTPException(status_code=404, detail="topic không tồn tại trong workspace")

    # Check quota
    cnt = (await db.execute(text(
        "SELECT COUNT(*)::INT FROM pubsub_subscriptions WHERE topic_id = :tid"
    ), {"tid": topic_id})).scalar() or 0
    if cnt >= MAX_SUBS_PER_TOPIC:
        raise HTTPException(
            status_code=429,
            detail=f"topic đã đạt giới hạn {MAX_SUBS_PER_TOPIC} subscriptions",
        )

    # Auto-generate webhook secret nếu không cung cấp (32 bytes hex)
    secret = data.webhook_secret or secrets.token_hex(32)

    try:
        row = (await db.execute(text("""
            INSERT INTO pubsub_subscriptions
              (topic_id, workspace_id, name, delivery_type, webhook_url,
               webhook_secret, filter_expression, max_retry_count,
               ack_deadline_seconds, enabled)
            VALUES
              (:tid, :ws, :n, :dt, :url, :sec, :flt, :mr, :ack, :en)
            RETURNING id, topic_id, workspace_id, name, delivery_type,
                      webhook_url, webhook_secret, filter_expression,
                      max_retry_count, ack_deadline_seconds, enabled, created_at
        """), {
            "tid": topic_id, "ws": ws, "n": data.name,
            "dt": data.delivery_type, "url": data.webhook_url,
            "sec": secret, "flt": data.filter_expression,
            "mr": data.max_retry_count, "ack": data.ack_deadline_seconds,
            "en": data.enabled,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_subscription failed for ws=%s topic=%s", ws, topic_id)
        raise HTTPException(status_code=502,
                            detail=f"không tạo được subscription: {type(e).__name__}")

    if row is None:
        raise HTTPException(status_code=502, detail="không tạo được subscription")

    out = _row_to_sub_dict(row)
    # Trả webhook_secret 1 lần duy nhất khi tạo (sau này sẽ masked)
    out["webhook_secret_one_time"] = secret if not data.webhook_secret else None

    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.subscription.create",
            target=f"sub#{out['id']}",
            severity="ok",
            metadata={"topic_id": topic_id, "delivery_type": data.delivery_type},
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.subscription.create")
        await db.rollback()

    return out


@router.get("/topics/{topic_id}/subscriptions")
async def list_subscriptions(
    topic_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List subscriptions của 1 topic."""
    await require_workspace_access(ws, me)
    _check_scope(me)

    try:
        rows = (await db.execute(text("""
            SELECT id, topic_id, workspace_id, name, delivery_type,
                   webhook_url, webhook_secret, filter_expression,
                   max_retry_count, ack_deadline_seconds, enabled, created_at
            FROM pubsub_subscriptions
            WHERE topic_id = :tid AND workspace_id = :ws
            ORDER BY created_at DESC
        """), {"tid": topic_id, "ws": ws})).all()
    except Exception as e:
        log.exception("list_subscriptions failed for ws=%s topic=%s", ws, topic_id)
        raise HTTPException(status_code=502,
                            detail=f"không list được subs: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "topic_id": topic_id,
        "count": len(rows),
        "subscriptions": [_row_to_sub_dict(r) for r in rows],
    }


# ─── Publish (fan-out) ───────────────────────────────
def _matches_filter(payload: Any, attributes: dict | None, expr: str | None) -> bool:
    """
    Đánh giá filter rất hạn chế (không eval Python tuỳ ý):
    - 'true' / None / empty → match all
    - "attributes.<key> == 'value'" / "payload.<key> == 'value'"
    - "payload.<key> > N"  /  "payload.<key> >= N"  /  ... ( <, >=, <=, ==, != )

    Cấu trúc tối thiểu, đủ dùng cho phần lớn use-case routing.
    """
    if not expr:
        return True
    e = expr.strip()
    if e.lower() in ("true", "1", "*", "all"):
        return True

    # tách operator
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if op in e:
            left, right = e.split(op, 1)
            left = left.strip()
            right = right.strip().strip("'\"")
            # Lấy giá trị từ payload / attributes
            obj_path = left.split(".", 1)
            if len(obj_path) != 2:
                return False
            root, key = obj_path[0], obj_path[1]
            src = None
            if root == "payload" and isinstance(payload, dict):
                src = payload.get(key)
            elif root == "attributes" and isinstance(attributes, dict):
                src = attributes.get(key)
            if src is None:
                return False
            # numeric cmp nếu cả 2 đều numeric
            try:
                lf = float(src); rf = float(right)
                if op == ">":  return lf > rf
                if op == ">=": return lf >= rf
                if op == "<":  return lf < rf
                if op == "<=": return lf <= rf
                if op == "==": return lf == rf
                if op == "!=": return lf != rf
            except (ValueError, TypeError):
                # string cmp
                ls = str(src)
                if op == "==": return ls == right
                if op == "!=": return ls != right
                return False
    return False


@router.post("/topics/{topic_id}/publish", status_code=201)
async def publish_message(
    topic_id: int,
    ws: str,
    data: PublishIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Publish 1 message → fan-out tới mọi subscription enabled+match filter."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được publish")

    payload_json = _validate_payload(data.payload)
    attrs_json = json.dumps(data.attributes or {}, ensure_ascii=False)

    # Verify topic ownership + lấy retention
    t = (await db.execute(text("""
        SELECT id, retention_seconds FROM pubsub_topics
        WHERE id = :id AND workspace_id = :ws
    """), {"id": topic_id, "ws": ws})).first()
    if t is None:
        raise HTTPException(status_code=404, detail="topic không tồn tại trong workspace")
    retention = int(t[1]) if t[1] is not None else DEFAULT_RETENTION_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=retention)

    msg_id = str(uuid.uuid4())

    try:
        await db.execute(text("""
            INSERT INTO pubsub_messages
              (topic_id, workspace_id, message_id, payload, attributes,
               expires_at)
            VALUES
              (:tid, :ws, :mid, CAST(:p AS JSONB), CAST(:a AS JSONB), :exp)
        """), {
            "tid": topic_id, "ws": ws, "mid": msg_id,
            "p": payload_json, "a": attrs_json, "exp": expires_at,
        })

        # Fetch subscriptions enabled
        subs = (await db.execute(text("""
            SELECT id, filter_expression, delivery_type
            FROM pubsub_subscriptions
            WHERE topic_id = :tid AND enabled = TRUE
        """), {"tid": topic_id})).all()

        delivered_subs: list[int] = []
        for s in subs:
            sub_id = int(s[0]); flt = s[1]; dt = s[2]
            if not _matches_filter(data.payload, data.attributes, flt):
                continue
            # 'pull' delivery doesn't get pre-staged delivery rows; consumers pull on demand.
            if dt == "pull":
                delivered_subs.append(sub_id)
                continue
            await db.execute(text("""
                INSERT INTO pubsub_deliveries
                  (message_id, subscription_id, workspace_id,
                   status, next_attempt_at)
                VALUES (:mid, :sid, :ws, 'pending', NOW())
            """), {"mid": msg_id, "sid": sub_id, "ws": ws})
            delivered_subs.append(sub_id)

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        log.exception("publish_message failed for ws=%s topic=%s", ws, topic_id)
        raise HTTPException(status_code=502,
                            detail=f"không publish được message: {type(e).__name__}")

    # Audit (light — chỉ topic + message_id, không log payload)
    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.publish",
            target=f"topic#{topic_id}/{msg_id}",
            severity="ok",
            metadata={
                "subscription_count": len(delivered_subs),
                "payload_size": len(payload_json),
            },
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.publish")
        await db.rollback()

    return {
        "ok": True,
        "message_id": msg_id,
        "topic_id": topic_id,
        "fanout": len(delivered_subs),
        "subscription_ids": delivered_subs,
        "expires_at": expires_at.isoformat(),
    }


# ─── List recent messages ────────────────────────────
@router.get("/messages")
async def list_messages(
    ws: str,
    topic: str | None = None,
    topic_id: int | None = None,
    limit: int = 50,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List recent messages của workspace, filter theo topic name hoặc topic_id."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit phải 1..{MAX_LIST_LIMIT}")

    where = ["m.workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit}
    if topic_id:
        where.append("m.topic_id = :tid"); params["tid"] = topic_id
    elif topic:
        where.append("t.name = :tname"); params["tname"] = topic

    sql = f"""
        SELECT m.id, m.topic_id, m.workspace_id, m.message_id,
               m.payload, m.attributes, m.published_at, m.expires_at,
               t.name AS topic_name
        FROM pubsub_messages m
        JOIN pubsub_topics t ON t.id = m.topic_id
        WHERE {" AND ".join(where)}
        ORDER BY m.published_at DESC
        LIMIT :lim
    """
    try:
        rows = (await db.execute(text(sql), params)).all()
    except Exception as e:
        log.exception("list_messages failed for ws=%s", ws)
        raise HTTPException(status_code=502,
                            detail=f"không list được messages: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "count": len(rows),
        "messages": [
            {
                "id": int(r[0]),
                "topic_id": int(r[1]),
                "workspace_id": r[2],
                "message_id": r[3],
                "payload": r[4],
                "attributes": r[5],
                "published_at": r[6].isoformat() if r[6] else None,
                "expires_at": r[7].isoformat() if r[7] else None,
                "topic_name": r[8],
            } for r in rows
        ],
    }


# ─── Scheduled tasks ─────────────────────────────────
@router.post("/tasks", status_code=201)
async def schedule_task(
    data: ScheduledTaskIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Schedule một HTTP task chạy sau một lúc (tối đa 30 ngày)."""
    ws = data.workspace_id
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được schedule task")
    _validate_name(data.task_name, "task name")

    # scheduled_at must be in future, max 30 days
    now = datetime.now(timezone.utc)
    sched = data.scheduled_at
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)
    if sched < now - timedelta(seconds=5):
        raise HTTPException(status_code=400, detail="scheduled_at phải ở tương lai")
    if sched > now + timedelta(days=30):
        raise HTTPException(status_code=400, detail="scheduled_at không quá 30 ngày")

    headers_json = json.dumps(data.headers or {}, ensure_ascii=False)
    body_json: str | None = None
    if data.body is not None:
        body_json = _validate_payload(data.body)

    try:
        row = (await db.execute(text("""
            INSERT INTO scheduled_tasks
              (workspace_id, task_name, target_url, method, headers, body,
               scheduled_at, max_retries)
            VALUES
              (:ws, :n, :url, :m, CAST(:h AS JSONB), CAST(:b AS JSONB),
               :s, :mr)
            RETURNING id, workspace_id, task_name, target_url, method,
                      scheduled_at, status, max_retries, created_at
        """), {
            "ws": ws, "n": data.task_name, "url": data.target_url,
            "m": data.method, "h": headers_json, "b": body_json,
            "s": sched, "mr": data.max_retries,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("schedule_task failed for ws=%s name=%s", ws, data.task_name)
        raise HTTPException(status_code=502,
                            detail=f"không schedule được task: {type(e).__name__}")

    if row is None:
        raise HTTPException(status_code=502, detail="không schedule được task")

    out = {
        "id": int(row[0]),
        "workspace_id": row[1],
        "task_name": row[2],
        "target_url": row[3],
        "method": row[4],
        "scheduled_at": row[5].isoformat() if row[5] else None,
        "status": row[6],
        "max_retries": int(row[7]) if row[7] is not None else None,
        "created_at": row[8].isoformat() if row[8] else None,
    }

    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.task.schedule",
            target=f"task#{out['id']}",
            severity="ok",
            metadata={"task_name": data.task_name, "scheduled_at": out["scheduled_at"]},
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.task.schedule")
        await db.rollback()

    return out


@router.get("/tasks")
async def list_tasks(
    ws: str,
    status: str | None = None,
    limit: int = 50,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List scheduled tasks của workspace, optionally filter status."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit phải 1..{MAX_LIST_LIMIT}")
    if status and status not in ("pending", "succeeded", "failed", "cancelled", "dlq"):
        raise HTTPException(status_code=400, detail="status không hợp lệ")

    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit}
    if status:
        where.append("status = :st"); params["st"] = status

    try:
        rows = (await db.execute(text(f"""
            SELECT id, workspace_id, task_name, target_url, method,
                   scheduled_at, status, retry_count, max_retries,
                   executed_at, response_code, last_error, created_at
            FROM scheduled_tasks
            WHERE {" AND ".join(where)}
            ORDER BY scheduled_at DESC
            LIMIT :lim
        """), params)).all()
    except Exception as e:
        log.exception("list_tasks failed for ws=%s", ws)
        raise HTTPException(status_code=502,
                            detail=f"không list được tasks: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "count": len(rows),
        "tasks": [
            {
                "id": int(r[0]),
                "workspace_id": r[1],
                "task_name": r[2],
                "target_url": r[3],
                "method": r[4],
                "scheduled_at": r[5].isoformat() if r[5] else None,
                "status": r[6],
                "retry_count": int(r[7]) if r[7] is not None else 0,
                "max_retries": int(r[8]) if r[8] is not None else 0,
                "executed_at": r[9].isoformat() if r[9] else None,
                "response_code": int(r[10]) if r[10] is not None else None,
                "last_error": r[11],
                "created_at": r[12].isoformat() if r[12] else None,
            } for r in rows
        ],
    }


@router.delete("/tasks/{task_id}")
async def cancel_task(
    task_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel pending task. Chỉ huỷ được task chưa execute."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được cancel task")

    try:
        res = await db.execute(text("""
            UPDATE scheduled_tasks
            SET status = 'cancelled'
            WHERE id = :id AND workspace_id = :ws AND status = 'pending'
        """), {"id": task_id, "ws": ws})
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("cancel_task failed for ws=%s task=%s", ws, task_id)
        raise HTTPException(status_code=502,
                            detail=f"không cancel được task: {type(e).__name__}")

    if not res.rowcount:
        # Check if task exists at all
        exists = (await db.execute(text("""
            SELECT status FROM scheduled_tasks WHERE id = :id AND workspace_id = :ws
        """), {"id": task_id, "ws": ws})).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="task không tồn tại trong workspace")
        raise HTTPException(status_code=409,
                            detail=f"task đã ở trạng thái '{exists[0]}', không cancel được")

    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.task.cancel",
            target=f"task#{task_id}",
            severity="warn",
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.task.cancel")
        await db.rollback()

    return {"ok": True, "task_id": task_id, "status": "cancelled"}


# ─── DLQ ─────────────────────────────────────────────
@router.get("/dlq")
async def list_dlq(
    ws: str,
    source_type: str | None = None,
    limit: int = 100,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List DLQ entries của workspace."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit phải 1..{MAX_LIST_LIMIT}")
    if source_type and source_type not in ("pubsub", "task", "webhook"):
        raise HTTPException(status_code=400, detail="source_type không hợp lệ")

    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit}
    if source_type:
        where.append("source_type = :src"); params["src"] = source_type

    try:
        rows = (await db.execute(text(f"""
            SELECT id, workspace_id, source_type, source_id, payload,
                   failure_reason, attempts, moved_to_dlq_at, requeued_at
            FROM dlq_messages
            WHERE {" AND ".join(where)}
            ORDER BY moved_to_dlq_at DESC
            LIMIT :lim
        """), params)).all()
    except Exception as e:
        log.exception("list_dlq failed for ws=%s", ws)
        raise HTTPException(status_code=502,
                            detail=f"không list được dlq: {type(e).__name__}")

    return {
        "workspace_id": ws,
        "count": len(rows),
        "items": [
            {
                "id": int(r[0]),
                "workspace_id": r[1],
                "source_type": r[2],
                "source_id": int(r[3]) if r[3] is not None else None,
                "payload": r[4],
                "failure_reason": r[5],
                "attempts": int(r[6]) if r[6] is not None else 0,
                "moved_to_dlq_at": r[7].isoformat() if r[7] else None,
                "requeued_at": r[8].isoformat() if r[8] else None,
            } for r in rows
        ],
    }


@router.post("/dlq/{dlq_id}/requeue")
async def requeue_from_dlq(
    dlq_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Re-enqueue 1 DLQ entry trở lại nguồn (pubsub_deliveries / scheduled_tasks)."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được requeue")

    # Fetch entry
    row = (await db.execute(text("""
        SELECT id, source_type, source_id, payload, requeued_at
        FROM dlq_messages
        WHERE id = :id AND workspace_id = :ws
        FOR UPDATE
    """), {"id": dlq_id, "ws": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="dlq entry không tồn tại")
    if row[4] is not None:
        raise HTTPException(status_code=409, detail="dlq entry đã được requeue rồi")

    src_type = row[1]; src_id = row[2]; payload = row[3]
    requeued_target: dict[str, Any] = {}

    try:
        if src_type == "pubsub" and src_id is not None:
            # Reset pubsub_deliveries row
            r = await db.execute(text("""
                UPDATE pubsub_deliveries
                SET status = 'pending',
                    attempt_count = 0,
                    next_attempt_at = NOW(),
                    last_error = NULL,
                    response_code = NULL,
                    response_body = NULL
                WHERE id = :id AND workspace_id = :ws
            """), {"id": src_id, "ws": ws})
            requeued_target = {"target": "pubsub_deliveries", "id": src_id, "rows": r.rowcount}

        elif src_type == "task" and src_id is not None:
            r = await db.execute(text("""
                UPDATE scheduled_tasks
                SET status = 'pending',
                    retry_count = 0,
                    last_error = NULL,
                    response_code = NULL,
                    response_body = NULL,
                    scheduled_at = GREATEST(scheduled_at, NOW())
                WHERE id = :id AND workspace_id = :ws
            """), {"id": src_id, "ws": ws})
            requeued_target = {"target": "scheduled_tasks", "id": src_id, "rows": r.rowcount}

        elif src_type == "webhook" and src_id is not None:
            r = await db.execute(text("""
                UPDATE webhook_attempts
                SET status = 'pending',
                    attempt_count = 0,
                    next_attempt_at = NOW(),
                    last_error = NULL,
                    last_status_code = NULL,
                    last_response = NULL
                WHERE id = :id AND workspace_id = :ws
            """), {"id": src_id, "ws": ws})
            requeued_target = {"target": "webhook_attempts", "id": src_id, "rows": r.rowcount}
        else:
            raise HTTPException(
                status_code=400,
                detail=f"source_type '{src_type}' không hỗ trợ requeue tự động",
            )

        # Mark DLQ entry as requeued
        await db.execute(text("""
            UPDATE dlq_messages SET requeued_at = NOW() WHERE id = :id
        """), {"id": dlq_id})
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        log.exception("requeue_from_dlq failed for ws=%s dlq=%s", ws, dlq_id)
        raise HTTPException(status_code=502,
                            detail=f"không requeue được: {type(e).__name__}")

    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="messaging.dlq.requeue",
            target=f"dlq#{dlq_id}",
            severity="warn",
            metadata={"source_type": src_type, "source_id": src_id},
        )
        await db.commit()
    except Exception:
        log.exception("audit_push failed for messaging.dlq.requeue")
        await db.rollback()

    return {
        "ok": True,
        "dlq_id": dlq_id,
        "source_type": src_type,
        "requeued": requeued_target,
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else None,
    }
