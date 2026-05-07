"""
Zeni Cloud Core — Push Notification Gateway (P0#6 ClawWits).

Wraps APNs (iOS) + FCM (Android) so apps gửi 1 endpoint Zeni, Zeni route đúng platform.
Replaces OneSignal / Pusher / Firebase Cloud Messaging direct integration.

Endpoints (prefix /push):
  POST   /devices                    — Register device (iOS/Android/Web)
  GET    /devices                    — List devices in workspace
  DELETE /devices/{device_id}        — Unregister
  POST   /send                       — Send push notification (broadcast or targeted)
  GET    /notifications              — Recent send history
  GET    /notifications/{id}         — Delivery details
  POST   /credentials                — Set APNs/FCM credentials (refs vault secrets)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.push")

router = APIRouter(prefix="/push", tags=["push-notifications"])


# ===== Schemas =====

class DeviceRegister(BaseModel):
    device_token: str = Field(..., min_length=10, max_length=4096)
    platform: str = Field(..., description="ios | android | web")
    user_id: Optional[str] = Field(None, description="Customer's user_id")
    app_bundle_id: Optional[str] = None
    device_locale: Optional[str] = "vi-VN"
    device_model: Optional[str] = None
    app_version: Optional[str] = None


class DeviceOut(BaseModel):
    id: str
    workspace_id: str
    user_id: Optional[str]
    platform: str
    device_token_preview: str  # only first 16 chars for privacy
    enabled: bool
    last_seen_at: Optional[str]
    created_at: str


class SendPushIn(BaseModel):
    user_ids: list[str] = Field(default_factory=list, description="Target user_ids; empty = use device_ids or platform_filter")
    device_ids: list[str] = Field(default_factory=list)
    platform_filter: Optional[str] = Field(None, description="ios | android | web")
    title: str = Field(..., max_length=200)
    body: str = Field(..., max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict, description="Custom data, e.g. {deep_link, type}")
    badge_count: Optional[int] = None
    sound: str = Field("default")


class PushOut(BaseModel):
    id: str
    workspace_id: str
    status: str
    title: Optional[str]
    body: Optional[str]
    total_devices: int
    delivered_count: int
    failed_count: int
    created_at: str
    sent_at: Optional[str]
    finished_at: Optional[str]


class CredentialsIn(BaseModel):
    platform: str = Field(..., description="ios | android")
    # iOS APNs
    apns_team_id: Optional[str] = None
    apns_key_id: Optional[str] = None
    apns_p8_secret_id: Optional[str] = Field(None, description="Vault secret_id holding .p8 cert content")
    apns_bundle_id: Optional[str] = None
    apns_environment: Optional[str] = Field("production", description="production | sandbox")
    # Android FCM
    fcm_project_id: Optional[str] = None
    fcm_service_account_secret_id: Optional[str] = Field(None, description="Vault secret_id holding service-account JSON")


# ===== Endpoints =====

@router.post("/devices", response_model=DeviceOut, status_code=201)
async def register_device(
    data: DeviceRegister,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Register device for push notifications.

    Idempotent — same (workspace_id, device_token) updates last_seen_at.
    """
    await require_workspace_access(ws, me)
    if data.platform not in ("ios", "android", "web"):
        raise HTTPException(422, "Platform must be ios | android | web")

    device_id = uuid.uuid4()
    row = (await db.execute(text(
        "INSERT INTO push_devices (id, workspace_id, user_id, device_token, platform, app_bundle_id, "
        "device_locale, device_model, app_version, last_seen_at) "
        "VALUES (:id, :ws, :uid, :tok, :pl, :bid, :loc, :mdl, :ver, NOW()) "
        "ON CONFLICT (workspace_id, device_token) DO UPDATE SET "
        "user_id = COALESCE(EXCLUDED.user_id, push_devices.user_id), "
        "last_seen_at = NOW(), enabled = TRUE "
        "RETURNING id, workspace_id, user_id, platform, device_token, enabled, created_at, last_seen_at"
    ), {
        "id": str(device_id),
        "ws": ws,
        "uid": data.user_id,
        "tok": data.device_token,
        "pl": data.platform,
        "bid": data.app_bundle_id,
        "loc": data.device_locale,
        "mdl": data.device_model,
        "ver": data.app_version,
    })).mappings().first()
    await db.commit()

    return DeviceOut(
        id=str(row["id"]),
        workspace_id=ws,
        user_id=row["user_id"],
        platform=row["platform"],
        device_token_preview=(row["device_token"][:16] + "…") if row["device_token"] else "",
        enabled=row["enabled"],
        last_seen_at=row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    ws: str = Query(...),
    user_id: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    sql = "SELECT id, workspace_id, user_id, platform, device_token, enabled, created_at, last_seen_at FROM push_devices WHERE workspace_id = :ws"
    params: dict[str, Any] = {"ws": ws}
    if user_id:
        sql += " AND user_id = :uid"
        params["uid"] = user_id
    if platform:
        sql += " AND platform = :pl"
        params["pl"] = platform
    sql += " ORDER BY last_seen_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        DeviceOut(
            id=str(r["id"]),
            workspace_id=ws,
            user_id=r["user_id"],
            platform=r["platform"],
            device_token_preview=(r["device_token"][:16] + "…") if r["device_token"] else "",
            enabled=r["enabled"],
            last_seen_at=r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]


@router.delete("/devices/{device_id}", status_code=204)
async def unregister_device(
    device_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    await db.execute(text(
        "UPDATE push_devices SET enabled = FALSE WHERE id = :id AND workspace_id = :ws"
    ), {"id": device_id, "ws": ws})
    await db.commit()


@router.post("/send", response_model=PushOut, status_code=202)
async def send_push(
    data: SendPushIn,
    bg: BackgroundTasks,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Send push to user_ids OR device_ids OR all devices in workspace (with optional platform_filter).

    AI agent:
      curl -X POST 'https://zenicloud.io/api/v1/push/send?ws=clawwits_flatform' \\
        -H "Authorization: Bearer $ZENI_TOKEN" \\
        -d '{"user_ids":["uid1","uid2"],"title":"New message","body":"Test","payload":{"deep_link":"app://chat/1"}}'
    """
    await require_workspace_access(ws, me)

    # Compute targets
    target_clauses = ["workspace_id = :ws", "enabled = TRUE"]
    params: dict[str, Any] = {"ws": ws}
    if data.user_ids:
        target_clauses.append("user_id = ANY(:uids)")
        params["uids"] = data.user_ids
    if data.device_ids:
        target_clauses.append("id::text = ANY(:dids)")
        params["dids"] = data.device_ids
    if data.platform_filter:
        target_clauses.append("platform = :pl")
        params["pl"] = data.platform_filter

    where_sql = " AND ".join(target_clauses)
    count = (await db.execute(text(
        f"SELECT COUNT(*) FROM push_devices WHERE {where_sql}"
    ), params)).scalar() or 0

    notif_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO push_notifications (id, workspace_id, user_ids, device_ids, platform_filter, "
        "title, body, payload, badge_count, sound, status, total_devices) "
        "VALUES (:id, :ws, CAST(:uids AS jsonb), CAST(:dids AS jsonb), :pl, "
        ":t, :b, CAST(:pl_data AS jsonb), :bc, :sn, 'queued', :td)"
    ), {
        "id": str(notif_id),
        "ws": ws,
        "uids": json.dumps(data.user_ids),
        "dids": json.dumps(data.device_ids),
        "pl": data.platform_filter,
        "t": data.title,
        "b": data.body,
        "pl_data": json.dumps(data.payload),
        "bc": data.badge_count,
        "sn": data.sound,
        "td": count,
    })
    await db.commit()

    bg.add_task(_stub_push_worker, str(notif_id))

    return PushOut(
        id=str(notif_id),
        workspace_id=ws,
        status="queued",
        title=data.title,
        body=data.body,
        total_devices=count,
        delivered_count=0,
        failed_count=0,
        created_at=datetime.now(timezone.utc).isoformat(),
        sent_at=None,
        finished_at=None,
    )


@router.get("/notifications", response_model=list[PushOut])
async def list_notifications(
    ws: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, workspace_id, status, title, body, total_devices, delivered_count, failed_count, "
        "created_at, sent_at, finished_at FROM push_notifications WHERE workspace_id = :ws "
        "ORDER BY created_at DESC LIMIT :lim"
    ), {"ws": ws, "lim": limit})).mappings().all()
    return [
        PushOut(
            id=str(r["id"]),
            workspace_id=ws,
            status=r["status"],
            title=r["title"],
            body=r["body"],
            total_devices=r["total_devices"] or 0,
            delivered_count=r["delivered_count"] or 0,
            failed_count=r["failed_count"] or 0,
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
            sent_at=r["sent_at"].isoformat() if r["sent_at"] else None,
            finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        )
        for r in rows
    ]


@router.post("/credentials", status_code=201)
async def set_credentials(
    data: CredentialsIn,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Set APNs/FCM credentials for workspace. Cert content goes through Identity Vault."""
    await require_workspace_access(ws, me)
    if data.platform not in ("ios", "android"):
        raise HTTPException(422, "Platform must be ios or android")
    await db.execute(text(
        "INSERT INTO push_credentials (workspace_id, platform, apns_team_id, apns_key_id, apns_p8_secret_id, "
        "apns_bundle_id, apns_environment, fcm_project_id, fcm_service_account_secret_id, updated_at) "
        "VALUES (:ws, :pl, :tid, :kid, :sid, :bid, :env, :fpid, :fssid, NOW()) "
        "ON CONFLICT (workspace_id, platform) DO UPDATE SET "
        "apns_team_id = EXCLUDED.apns_team_id, apns_key_id = EXCLUDED.apns_key_id, "
        "apns_p8_secret_id = EXCLUDED.apns_p8_secret_id, apns_bundle_id = EXCLUDED.apns_bundle_id, "
        "apns_environment = EXCLUDED.apns_environment, fcm_project_id = EXCLUDED.fcm_project_id, "
        "fcm_service_account_secret_id = EXCLUDED.fcm_service_account_secret_id, updated_at = NOW()"
    ), {
        "ws": ws,
        "pl": data.platform,
        "tid": data.apns_team_id,
        "kid": data.apns_key_id,
        "sid": data.apns_p8_secret_id,
        "bid": data.apns_bundle_id,
        "env": data.apns_environment,
        "fpid": data.fcm_project_id,
        "fssid": data.fcm_service_account_secret_id,
    })
    await db.commit()
    return {"workspace_id": ws, "platform": data.platform, "status": "credentials_set"}


async def _stub_push_worker(notif_id: str) -> None:
    """REAL Phase 2 worker — wires APNs HTTP/2 + FCM HTTPv1 via push_worker.run_push_notification."""
    try:
        from app.services.push_worker import run_push_notification
        await run_push_notification(notif_id)
    except Exception as e:
        log.exception("Push worker dispatch failed for %s: %s", notif_id, e)
