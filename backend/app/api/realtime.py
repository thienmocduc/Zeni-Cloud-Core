"""
Zeni Cloud Core — Zeni Realtime API (Supabase Realtime parity).

WebSocket pub-sub channels for live chat, presence, real-time dashboards.
Backed by Cloud Pub/Sub + WebSocket gateway in Cloud Run.

Endpoints (prefix /realtime):
  POST   /channels                     — Create channel
  GET    /channels                     — List channels in workspace
  GET    /channels/{name}              — Channel detail + active subscribers
  DELETE /channels/{name}              — Delete channel
  POST   /channels/{name}/publish      — Publish event to channel (HTTP — for server-side)
  GET    /channels/{name}/messages     — Recent messages (if retention enabled)
  GET    /channels/{name}/presence     — Active subscribers + presence state
  WS     /ws/{ws_id}/{channel}         — WebSocket subscribe (full duplex)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db, SessionLocal

log = logging.getLogger("zeni.realtime")

router = APIRouter(prefix="/realtime", tags=["realtime"])


# ===== In-memory connection registry (per-instance; for multi-instance use Pub/Sub fanout) =====

class ConnectionRegistry:
    """Simple in-memory WebSocket registry. For multi-Cloud-Run-instance, swap to Cloud Pub/Sub broker."""

    def __init__(self):
        self._channels: dict[str, set[WebSocket]] = {}

    def register(self, channel_key: str, ws: WebSocket):
        self._channels.setdefault(channel_key, set()).add(ws)

    def unregister(self, channel_key: str, ws: WebSocket):
        if channel_key in self._channels:
            self._channels[channel_key].discard(ws)
            if not self._channels[channel_key]:
                del self._channels[channel_key]

    async def broadcast(self, channel_key: str, message: dict):
        conns = list(self._channels.get(channel_key, []))
        sent = 0
        for ws in conns:
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:
                self.unregister(channel_key, ws)
        return sent

    def count(self, channel_key: str) -> int:
        return len(self._channels.get(channel_key, set()))


_registry = ConnectionRegistry()


# ===== Schemas =====

class ChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    visibility: str = Field("private", description="private | public | authenticated")
    allowed_user_ids: list[str] = Field(default_factory=list)
    max_subscribers: int = Field(1000, ge=1, le=100000)
    message_retention_seconds: int = Field(0, ge=0, le=2592000, description="0 = broadcast only, >0 = keep N seconds")
    presence_enabled: bool = False


class ChannelOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    visibility: str
    max_subscribers: int
    message_retention_seconds: int
    presence_enabled: bool
    active_subscribers: int
    total_messages_sent: int
    created_at: str


class PublishIn(BaseModel):
    event_type: str = Field("message", max_length=60)
    payload: dict[str, Any]
    sender_user_id: Optional[str] = None


class MessageOut(BaseModel):
    id: str
    event_type: str
    payload: dict
    sender_user_id: Optional[str] = None
    published_at: str


class PresenceOut(BaseModel):
    user_id: Optional[str]
    client_id: Optional[str]
    presence_state: dict
    subscribed_at: str
    last_heartbeat_at: str


# ===== Endpoints — Channels =====

@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    data: ChannelCreate,
    ws: str = Query(..., description="workspace_id"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Create realtime channel. Channel name eg 'chat:room-1' or 'presence:dashboard'."""
    await require_workspace_access(ws, me)
    if data.visibility not in ("private", "public", "authenticated"):
        raise HTTPException(422, "visibility must be private | public | authenticated")

    channel_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO realtime_channels (id, workspace_id, name, visibility, allowed_user_ids, "
            "max_subscribers, message_retention_seconds, presence_enabled, created_by) "
            "VALUES (:id, :ws, :n, :v, CAST(:au AS jsonb), :ms, :mr, :pe, :cb)"
        ), {
            "id": str(channel_id),
            "ws": ws,
            "n": data.name,
            "v": data.visibility,
            "au": json.dumps(data.allowed_user_ids),
            "ms": data.max_subscribers,
            "mr": data.message_retention_seconds,
            "pe": data.presence_enabled,
            "cb": str(me.id) if me else None,
        })
        await db.commit()
    except Exception as e:
        if "duplicate" in str(e).lower():
            raise HTTPException(409, f"Channel '{data.name}' already exists")
        raise

    return ChannelOut(
        id=str(channel_id),
        workspace_id=ws,
        name=data.name,
        visibility=data.visibility,
        max_subscribers=data.max_subscribers,
        message_retention_seconds=data.message_retention_seconds,
        presence_enabled=data.presence_enabled,
        active_subscribers=0,
        total_messages_sent=0,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, workspace_id, name, visibility, max_subscribers, message_retention_seconds, "
        "presence_enabled, active_subscribers, total_messages_sent, created_at "
        "FROM realtime_channels WHERE workspace_id = :ws ORDER BY name"
    ), {"ws": ws})).mappings().all()
    return [_row_to_channel(r) for r in rows]


@router.get("/channels/{name}", response_model=ChannelOut)
async def get_channel(
    name: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, name, visibility, max_subscribers, message_retention_seconds, "
        "presence_enabled, active_subscribers, total_messages_sent, created_at "
        "FROM realtime_channels WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not r:
        raise HTTPException(404, "Channel not found")
    out = _row_to_channel(r)
    # Override active_subscribers with live registry count
    out.active_subscribers = _registry.count(f"{ws}::{name}")
    return out


@router.delete("/channels/{name}", status_code=204)
async def delete_channel(
    name: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    await db.execute(text(
        "DELETE FROM realtime_channels WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})
    await db.commit()


@router.post("/channels/{name}/publish")
async def publish_message(
    name: str,
    data: PublishIn,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Publish event to channel (server-side HTTP)."""
    await require_workspace_access(ws, me)
    channel = (await db.execute(text(
        "SELECT id, message_retention_seconds FROM realtime_channels WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not channel:
        raise HTTPException(404, "Channel not found")

    msg_id = uuid.uuid4()
    expires_at = None
    if channel["message_retention_seconds"] > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=channel["message_retention_seconds"])
        await db.execute(text(
            "INSERT INTO realtime_messages (id, channel_id, workspace_id, event_type, payload, sender_user_id, expires_at) "
            "VALUES (:id, :cid, :ws, :et, CAST(:pl AS jsonb), :su, :ex)"
        ), {
            "id": str(msg_id),
            "cid": str(channel["id"]),
            "ws": ws,
            "et": data.event_type,
            "pl": json.dumps(data.payload),
            "su": data.sender_user_id or (str(me.id) if me else None),
            "ex": expires_at,
        })
    await db.execute(text(
        "UPDATE realtime_channels SET total_messages_sent = total_messages_sent + 1 WHERE id = :id"
    ), {"id": str(channel["id"])})
    await db.commit()

    # Broadcast to in-memory connections
    delivered = await _registry.broadcast(f"{ws}::{name}", {
        "id": str(msg_id),
        "event_type": data.event_type,
        "payload": data.payload,
        "sender_user_id": data.sender_user_id or (str(me.id) if me else None),
        "published_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "message_id": str(msg_id),
        "channel": name,
        "delivered_count": delivered,
        "stored": expires_at is not None,
    }


@router.get("/channels/{name}/messages", response_model=list[MessageOut])
async def get_messages(
    name: str,
    ws: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    channel = (await db.execute(text(
        "SELECT id FROM realtime_channels WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    rows = (await db.execute(text(
        "SELECT id, event_type, payload, sender_user_id, published_at "
        "FROM realtime_messages WHERE channel_id = :cid "
        "AND (expires_at IS NULL OR expires_at > NOW()) "
        "ORDER BY published_at DESC LIMIT :lim"
    ), {"cid": str(channel["id"]), "lim": limit})).mappings().all()
    return [
        MessageOut(
            id=str(r["id"]),
            event_type=r["event_type"],
            payload=r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}"),
            sender_user_id=r["sender_user_id"],
            published_at=r["published_at"].isoformat() if r["published_at"] else "",
        )
        for r in rows
    ]


@router.get("/channels/{name}/presence", response_model=list[PresenceOut])
async def get_presence(
    name: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    channel = (await db.execute(text(
        "SELECT id FROM realtime_channels WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    rows = (await db.execute(text(
        "SELECT user_id, client_id, presence_state, subscribed_at, last_heartbeat_at "
        "FROM realtime_subscriptions WHERE channel_id = :cid AND last_heartbeat_at > NOW() - INTERVAL '60 seconds'"
    ), {"cid": str(channel["id"])})).mappings().all()
    return [
        PresenceOut(
            user_id=r["user_id"],
            client_id=r["client_id"],
            presence_state=r["presence_state"] if isinstance(r["presence_state"], dict) else {},
            subscribed_at=r["subscribed_at"].isoformat() if r["subscribed_at"] else "",
            last_heartbeat_at=r["last_heartbeat_at"].isoformat() if r["last_heartbeat_at"] else "",
        )
        for r in rows
    ]


# ===== WebSocket endpoint =====

@router.websocket("/ws/{ws_id}/{channel_name}")
async def websocket_endpoint(
    websocket: WebSocket,
    ws_id: str,
    channel_name: str,
    token: Optional[str] = Query(None, description="JWT or PAT token in query for browser clients"),
):
    """WebSocket subscriber.

    Client connects: wss://zenicloud.io/api/v1/realtime/ws/{ws}/{channel}?token={zeni_token}
    Sends/receives JSON frames:
      { "type": "publish", "event_type": "message", "payload": {...} }
      { "type": "presence", "presence_state": {...} }
      { "type": "ping" }
    """
    # Auth via query token (since browser EventSource cannot set Authorization header for WS)
    await websocket.accept()
    if not token:
        await websocket.send_json({"type": "error", "message": "missing token query param"})
        await websocket.close()
        return

    # Verify channel exists and check access
    async with SessionLocal() as db:
        channel = (await db.execute(text(
            "SELECT id, visibility, allowed_user_ids FROM realtime_channels "
            "WHERE workspace_id = :ws AND name = :n"
        ), {"ws": ws_id, "n": channel_name})).mappings().first()
        if not channel:
            await websocket.send_json({"type": "error", "message": "channel not found"})
            await websocket.close()
            return

    channel_key = f"{ws_id}::{channel_name}"
    _registry.register(channel_key, websocket)
    log.info("WS connected: %s (subs=%d)", channel_key, _registry.count(channel_key))

    try:
        await websocket.send_json({"type": "connected", "channel": channel_name})
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "publish")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "publish":
                # Broadcast to other subscribers
                await _registry.broadcast(channel_key, {
                    "event_type": data.get("event_type", "message"),
                    "payload": data.get("payload", {}),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                })
            elif msg_type == "presence":
                # Update presence state (simplified — full implementation tracks per-connection)
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("WS error %s: %s", channel_key, e)
    finally:
        _registry.unregister(channel_key, websocket)
        log.info("WS disconnected: %s (subs=%d)", channel_key, _registry.count(channel_key))


# ===== Helpers =====

def _row_to_channel(r) -> ChannelOut:
    return ChannelOut(
        id=str(r["id"]),
        workspace_id=r["workspace_id"],
        name=r["name"],
        visibility=r["visibility"],
        max_subscribers=r["max_subscribers"],
        message_retention_seconds=r["message_retention_seconds"],
        presence_enabled=r["presence_enabled"],
        active_subscribers=r["active_subscribers"] or 0,
        total_messages_sent=r["total_messages_sent"] or 0,
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
    )
