"""
Zeni Cloud Core — L4 Automation API (REAL webhook dispatch).

Real connector dispatch:
- Connector types: webhook (generic POST), slack (Slack incoming webhook),
  discord (Discord webhook), email (SMTP — handled in L5)
- POST /events/fire actually does HTTP POST to configured connector URL
- Event log persisted to ws_<workspace>.events table
- Updates connector.events_7d counter
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.db.models import Connector
from app.schemas.resources import ConnectorCreateIn, ConnectorOut, EventFireIn
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.automation")
router = APIRouter(prefix="/automation", tags=["automation"])


# Built-in connector type allowlist (basic + native VN partners)
NATIVE_CONNECTORS = [
    "Zalo OA", "Shopee", "TikTok Shop", "Meta Ads", "Google Ads", "Mailchimp",
    "VNPay", "MoMo", "ZaloPay", "Stripe", "Slack", "Discord", "Twilio", "SendGrid",
    "OpenAI", "Anthropic", "Notion", "Airtable", "Google Sheets", "HubSpot",
    "Salesforce", "Pancake", "Haravan", "KiotViet", "Sapo", "Nhanh.vn",
]

# Generic dispatchable kinds (case-sensitive lower variants)
DISPATCHABLE = {"webhook", "slack", "discord", "generic_http"}

DISPATCH_TIMEOUT = 15.0  # seconds


@router.get("/catalog")
async def catalog() -> list[dict]:
    return [
        {"name": n, "slug": n.lower().replace(" ", "_").replace(".", ""), "type": "native"}
        for n in NATIVE_CONNECTORS
    ] + [
        {"name": "Webhook (custom)", "slug": "webhook",  "type": "generic"},
        {"name": "HTTP POST",        "slug": "generic_http", "type": "generic"},
    ]


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_connectors(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Connector).where(Connector.workspace_id == ws).order_by(Connector.events_7d.desc())
    )).scalars().all()
    out = []
    for r in rows:
        # Hide sensitive config keys (token, secret) from response
        cfg = dict(r.config or {})
        for k in list(cfg.keys()):
            if any(s in k.lower() for s in ("token", "secret", "password", "key")):
                cfg[k] = "***"
        out.append(ConnectorOut(
            id=r.id, workspace_id=r.workspace_id, type=r.type, status=r.status,
            events_7d=r.events_7d, config=cfg,
        ))
    return out


@router.post("/connectors", response_model=ConnectorOut, status_code=201)
async def add_connector(
    ws: str,
    data: ConnectorCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được thêm connector")

    # Validate connector type
    if data.type not in NATIVE_CONNECTORS and data.type.lower() not in DISPATCHABLE:
        raise HTTPException(status_code=400, detail="connector type không hợp lệ")

    # For dispatchable connectors, require URL in config
    if data.type.lower() in DISPATCHABLE and not data.config.get("url"):
        raise HTTPException(status_code=400, detail="webhook/slack/discord cần config.url")

    c = Connector(
        workspace_id=ws, type=data.type, status="connected", events_7d=0,
        config=data.config or {},
    )
    db.add(c)
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="automation.connect",
        target=data.type, severity="ok",
        metadata={"has_url": bool(data.config.get("url"))},
    )
    await db.commit()
    await db.refresh(c)
    cfg_safe = dict(c.config or {})
    for k in list(cfg_safe.keys()):
        if any(s in k.lower() for s in ("token", "secret", "password", "key")):
            cfg_safe[k] = "***"
    return ConnectorOut(
        id=c.id, workspace_id=c.workspace_id, type=c.type, status=c.status,
        events_7d=c.events_7d, config=cfg_safe,
    )


@router.delete("/connectors/{connector_id}", status_code=204, response_class=Response)
async def delete_connector(
    ws: str,
    connector_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    c = (await db.execute(
        select(Connector).where(Connector.id == connector_id, Connector.workspace_id == ws)
    )).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="connector not found")
    await db.delete(c)
    await audit_push(db, actor=me.email, workspace_id=ws, action="automation.disconnect",
                     target=c.type, severity="warn")
    await db.commit()
    return Response(status_code=204)


# ─── Event dispatch (REAL) ──────────────────────────
async def _dispatch_to_connector(connector: Connector, source: str, action: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
    """Make actual HTTP request to connector. Returns delivery result."""
    cfg = connector.config or {}
    url = cfg.get("url")
    if not url:
        return {"ok": False, "error": "no_url", "connector": connector.type}

    body: Any
    headers = {"Content-Type": "application/json", "User-Agent": "ZeniCloud/1.0"}
    headers.update(cfg.get("headers") or {})

    ctype = connector.type.lower()
    if ctype == "slack":
        # Slack incoming webhook expects {"text": "..."}
        text_msg = (
            f"*[Zeni Cloud · {source}]* `{action}`\n"
            + f"```\n{json.dumps(payload, ensure_ascii=False, indent=2)[:1500]}\n```"
        )
        body = {"text": text_msg}
    elif ctype == "discord":
        # Discord webhook expects {"content": "..."}
        body = {
            "content": f"**[Zeni · {source}] `{action}`**\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)[:1500]}\n```"
        }
    else:
        # Generic webhook: pass through full event envelope
        body = {
            "source": source, "action": action, "payload": payload,
            "ts": int(time.time()), "platform": "zenicloud",
        }
        if cfg.get("secret_token"):
            headers["X-Zeni-Token"] = cfg["secret_token"]

    try:
        async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
            t0 = time.perf_counter()
            r = await client.post(url, json=body, headers=headers)
            latency_ms = int((time.perf_counter() - t0) * 1000)
        ok = 200 <= r.status_code < 300
        return {
            "ok": ok, "status_code": r.status_code, "latency_ms": latency_ms,
            "response": r.text[:300], "connector": connector.type,
            "connector_id": str(connector.id),
        }
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout", "connector": connector.type, "connector_id": str(connector.id)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "connector": connector.type, "connector_id": str(connector.id)}


async def _persist_event(workspace_id: str, kind: str, payload: dict[str, Any],
                         actor: str, deliveries: list[dict]) -> None:
    """Insert event into per-workspace events table."""
    schema = f"ws_{workspace_id}"
    full_payload = {**payload, "_deliveries": deliveries}
    try:
        async with SessionLocal() as conn:
            await conn.execute(
                text(f'INSERT INTO {schema}.events (kind, payload, actor) VALUES (:k, :p::jsonb, :a)'),
                {"k": kind, "p": json.dumps(full_payload, ensure_ascii=False), "a": actor},
            )
            await conn.commit()
    except Exception as e:
        log.warning("[automation] failed to persist event for ws=%s: %s", workspace_id, e)


@router.post("/events/fire")
async def fire_event(
    ws: str,
    data: EventFireIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)

    t0 = time.perf_counter()

    # Find connectors to dispatch to
    if data.connector_id:
        target = (await db.execute(
            select(Connector).where(Connector.id == data.connector_id, Connector.workspace_id == ws)
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="connector_id không tồn tại trong workspace")
        connectors = [target] if target.type.lower() in DISPATCHABLE or target.config.get("url") else []
    else:
        # All dispatchable connectors with URL configured
        all_c = (await db.execute(
            select(Connector).where(Connector.workspace_id == ws, Connector.status == "connected")
        )).scalars().all()
        connectors = [c for c in all_c if c.config and c.config.get("url")]

    # Dispatch to each connector (parallel)
    deliveries: list[dict] = []
    if connectors:
        import asyncio
        deliveries = await asyncio.gather(
            *[_dispatch_to_connector(c, data.source, data.action, data.payload) for c in connectors]
        )
        # Increment events_7d counter
        for c in connectors:
            c.events_7d = (c.events_7d or 0) + 1

    e2e_ms = int((time.perf_counter() - t0) * 1000)
    success_count = sum(1 for d in deliveries if d.get("ok"))
    cost = 0.00003 * max(1, len(connectors))

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="automation.fire",
        target=f"{data.source}->{data.action}",
        severity="ok" if success_count == len(deliveries) else "warn",
        metadata={"connectors": len(connectors), "success": success_count,
                  "payload_keys": list(data.payload.keys())},
    )
    await billing_push(db, workspace_id=ws, layer="L4", action="automation.fire", cost_usd=cost)
    await db.commit()

    # Auto-enqueue retry for failed deliveries
    from app.services import webhook_retry
    retry_ids = []
    for c, d in zip(connectors, deliveries):
        if not d.get("ok"):
            try:
                aid = await webhook_retry.enqueue_dispatch(
                    db, workspace_id=ws, connector_id=str(c.id),
                    source=data.source, action=data.action,
                    target_url=c.config.get("url", ""),
                    payload=data.payload,
                    headers=c.config.get("headers"),
                    actor=me.email, max_attempts=5,
                )
                retry_ids.append(aid)
            except Exception as e:
                log.warning("Failed to enqueue retry for connector %s: %s", c.id, e)

    # Persist event log in background
    bg.add_task(_persist_event, ws, f"{data.source}.{data.action}", data.payload, me.email, deliveries)

    return {
        "source": data.source,
        "action": data.action,
        "e2e_latency_ms": e2e_ms,
        "connectors_attempted": len(connectors),
        "successful": success_count,
        "cost_usd": cost,
        "deliveries": deliveries,
        "retry_queue_ids": retry_ids,
    }


# ─── Webhook retry queue inspection (Stream A4) ───────────────
@router.get("/webhook-attempts")
async def list_webhook_attempts(
    ws: str,
    status: str | None = None,
    limit: int = 50,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Liệt kê webhook attempts. status=pending|succeeded|failed|dlq."""
    await require_workspace_access(ws, me)
    limit = min(max(1, limit), 500)
    where_clauses = ["workspace_id = :w"]
    params = {"w": ws, "lim": limit}
    if status:
        where_clauses.append("status = :st")
        params["st"] = status
    sql = f"""
        SELECT id, source, action, target_url, status, attempt_count, max_attempts,
               last_status_code, last_error, next_attempt_at, succeeded_at, dlq_at, created_at
        FROM webhook_attempts
        WHERE {' AND '.join(where_clauses)}
        ORDER BY id DESC
        LIMIT :lim
    """
    rows = (await db.execute(text(sql), params)).all()
    return {
        "workspace_id": ws,
        "count": len(rows),
        "attempts": [
            {"id": r[0], "source": r[1], "action": r[2], "target_url": r[3],
             "status": r[4], "attempts": f"{r[5]}/{r[6]}",
             "last_status_code": r[7], "last_error": r[8],
             "next_attempt_at": r[9].isoformat() if r[9] else None,
             "succeeded_at": r[10].isoformat() if r[10] else None,
             "dlq_at": r[11].isoformat() if r[11] else None,
             "created_at": r[12].isoformat() if r[12] else None}
            for r in rows
        ],
    }


@router.post("/webhook-attempts/{attempt_id}/retry")
async def retry_dlq_webhook(
    ws: str,
    attempt_id: int,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manual retry a DLQ webhook attempt — reset status to pending."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin để retry DLQ")
    row = (await db.execute(text("""
        UPDATE webhook_attempts SET
          status = 'pending', attempt_count = 0,
          next_attempt_at = NOW(), dlq_at = NULL, updated_at = NOW()
        WHERE id = :id AND workspace_id = :w AND status = 'dlq'
        RETURNING id
    """), {"id": attempt_id, "w": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="DLQ attempt không tồn tại")
    await audit_push(db, actor=me.email, workspace_id=ws,
                     action="webhook.dlq_retry", target=str(attempt_id), severity="info")
    await db.commit()
    return {"ok": True, "attempt_id": attempt_id, "requeued": True}


@router.get("/events")
async def list_events(
    ws: str,
    limit: int = 50,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recent events for workspace (read from ws_<workspace>.events)."""
    await require_workspace_access(ws, me)
    schema = f"ws_{ws}"
    limit = min(max(1, limit), 500)
    try:
        rows = (await db.execute(
            text(f'SELECT id, kind, payload, actor, ts FROM {schema}.events ORDER BY id DESC LIMIT :lim'),
            {"lim": limit},
        )).all()
        return {
            "schema": schema,
            "count": len(rows),
            "events": [
                {"id": r[0], "kind": r[1], "payload": r[2], "actor": r[3],
                 "ts": r[4].isoformat() if r[4] else None}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("list_events failed for ws=%s", ws)
        raise HTTPException(status_code=500, detail=f"không đọc được events: {e}")
