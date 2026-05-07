"""
Zeni Cloud Core — Push Notification Worker (REAL Phase 2).

Sends to APNs (HTTP/2 with .p8 token-based auth) + FCM (HTTPv1 with service account).
Looks up credentials from push_credentials → mobile_cert_secrets vault.
"""
from __future__ import annotations

import base64 as b64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import jwt as pyjwt
from sqlalchemy import text

from app.db.base import SessionLocal

log = logging.getLogger("zeni.push_worker")

APNS_PROD_HOST = "api.push.apple.com"
APNS_SANDBOX_HOST = "api.sandbox.push.apple.com"


async def _get_credentials(db, ws: str, platform: str) -> Optional[dict]:
    r = (await db.execute(text(
        "SELECT apns_team_id, apns_key_id, apns_p8_secret_id, apns_bundle_id, apns_environment, "
        "fcm_project_id, fcm_service_account_secret_id "
        "FROM push_credentials WHERE workspace_id = :ws AND platform = :pl"
    ), {"ws": ws, "pl": platform})).mappings().first()
    if not r:
        return None
    return dict(r)


async def _retrieve_secret(db, secret_id: str, ws: str) -> Optional[str]:
    """Get encrypted secret from mobile_cert_secrets vault (shared with cert manager)."""
    if not secret_id:
        return None
    r = (await db.execute(text(
        "SELECT encrypted_value FROM mobile_cert_secrets WHERE id = :id AND workspace_id = :ws"
    ), {"id": secret_id, "ws": ws})).mappings().first()
    if not r:
        return None
    enc = r["encrypted_value"]
    try:
        from app.services.identity_vault import decrypt_value
        return decrypt_value(enc)
    except Exception:
        try:
            return b64.b64decode(enc.encode()).decode()
        except Exception:
            return enc


def _build_apns_jwt(team_id: str, key_id: str, p8_pem: str) -> str:
    """JWT signed with .p8 ES256 key. APNs requires re-sign every <60min."""
    now = int(time.time())
    headers = {"alg": "ES256", "kid": key_id}
    payload = {"iss": team_id, "iat": now}
    return pyjwt.encode(payload, p8_pem, algorithm="ES256", headers=headers)


async def send_apns_push(
    device_token: str, bundle_id: str, jwt_token: str, env: str,
    title: str, body: str, payload: dict, badge: Optional[int], sound: str,
) -> tuple[bool, str]:
    """Send 1 APNs notification via HTTP/2."""
    host = APNS_SANDBOX_HOST if env == "sandbox" else APNS_PROD_HOST
    url = f"https://{host}/3/device/{device_token}"
    aps = {"alert": {"title": title, "body": body}, "sound": sound}
    if badge is not None:
        aps["badge"] = badge
    body_json = {"aps": aps, **payload}
    headers = {
        "authorization": f"bearer {jwt_token}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "content-type": "application/json",
    }
    # APNs requires HTTP/2 — httpx supports via h2 package
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        try:
            r = await client.post(url, headers=headers, json=body_json)
            return (r.status_code == 200), f"{r.status_code}:{r.text[:200]}"
        except Exception as e:
            return False, f"exception:{str(e)[:200]}"


async def send_fcm_push(
    device_token: str, project_id: str, sa_json: str,
    title: str, body: str, payload: dict, badge: Optional[int], sound: str,
) -> tuple[bool, str]:
    """Send 1 FCM notification via HTTPv1 API."""
    # Get OAuth token from service account
    sa = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
    now = int(time.time())
    jwt_payload = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    jwt_token = pyjwt.encode(jwt_payload, sa["private_key"], algorithm="RS256")
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token},
        )
        if token_resp.status_code != 200:
            return False, f"oauth_failed:{token_resp.status_code}"
        access_token = token_resp.json()["access_token"]

        message = {
            "message": {
                "token": device_token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in payload.items()},
                "android": {"notification": {"sound": sound}},
            }
        }
        r = await client.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=message,
        )
        return (r.status_code == 200), f"{r.status_code}:{r.text[:200]}"


async def run_push_notification(notif_id: str) -> None:
    """Process push notification: lookup devices → send via APNs+FCM → update result."""
    async with SessionLocal() as db:
        notif = (await db.execute(text(
            "SELECT * FROM push_notifications WHERE id = :id AND status = 'queued'"
        ), {"id": notif_id})).mappings().first()
        if not notif:
            return
        await db.execute(text(
            "UPDATE push_notifications SET status = 'processing', sent_at = NOW() WHERE id = :id"
        ), {"id": notif_id})
        await db.commit()

        ws = notif["workspace_id"]
        user_ids = notif["user_ids"] if isinstance(notif["user_ids"], list) else json.loads(notif["user_ids"] or "[]")
        device_ids = notif["device_ids"] if isinstance(notif["device_ids"], list) else json.loads(notif["device_ids"] or "[]")
        platform_filter = notif["platform_filter"]

        # Collect devices
        sql = "SELECT id, device_token, platform, app_bundle_id FROM push_devices WHERE workspace_id = :ws AND enabled = TRUE"
        params: dict[str, Any] = {"ws": ws}
        clauses = []
        if user_ids:
            clauses.append("user_id = ANY(:uids)")
            params["uids"] = user_ids
        if device_ids:
            clauses.append("id::text = ANY(:dids)")
            params["dids"] = device_ids
        if platform_filter:
            clauses.append("platform = :pl")
            params["pl"] = platform_filter
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")" if (user_ids and device_ids) else " AND " + " AND ".join(clauses)

        devices = (await db.execute(text(sql), params)).mappings().all()

        if not devices:
            await db.execute(text(
                "UPDATE push_notifications SET status = 'success', finished_at = NOW(), total_devices = 0 WHERE id = :id"
            ), {"id": notif_id})
            await db.commit()
            return

        # Get credentials
        ios_creds = await _get_credentials(db, ws, "ios")
        android_creds = await _get_credentials(db, ws, "android")

        ios_jwt = None
        if ios_creds and ios_creds["apns_p8_secret_id"]:
            p8_pem = await _retrieve_secret(db, ios_creds["apns_p8_secret_id"], ws)
            if p8_pem:
                try:
                    ios_jwt = _build_apns_jwt(ios_creds["apns_team_id"], ios_creds["apns_key_id"], p8_pem)
                except Exception as e:
                    log.warning("APNs JWT build failed: %s", e)

        fcm_sa = None
        if android_creds and android_creds["fcm_service_account_secret_id"]:
            fcm_sa = await _retrieve_secret(db, android_creds["fcm_service_account_secret_id"], ws)

        title = notif["title"] or ""
        body_t = notif["body"] or ""
        payload = notif["payload"] if isinstance(notif["payload"], dict) else json.loads(notif["payload"] or "{}")
        badge = notif["badge_count"]
        sound = notif["sound"] or "default"

        delivered = 0
        failed = 0
        errors = []

        for d in devices:
            ok, msg = False, "no_credentials"
            try:
                if d["platform"] == "ios" and ios_jwt and ios_creds:
                    ok, msg = await send_apns_push(
                        d["device_token"], d["app_bundle_id"] or ios_creds["apns_bundle_id"],
                        ios_jwt, ios_creds["apns_environment"] or "production",
                        title, body_t, payload, badge, sound,
                    )
                elif d["platform"] == "android" and fcm_sa and android_creds:
                    ok, msg = await send_fcm_push(
                        d["device_token"], android_creds["fcm_project_id"], fcm_sa,
                        title, body_t, payload, badge, sound,
                    )
            except Exception as e:
                ok, msg = False, str(e)[:200]

            if ok:
                delivered += 1
            else:
                failed += 1
                if len(errors) < 50:
                    errors.append({"device_id": str(d["id"]), "platform": d["platform"], "error": msg})

        await db.execute(text(
            "UPDATE push_notifications SET status = 'success', finished_at = NOW(), "
            "total_devices = :td, delivered_count = :dc, failed_count = :fc, errors = CAST(:e AS jsonb) "
            "WHERE id = :id"
        ), {
            "td": len(devices), "dc": delivered, "fc": failed,
            "e": json.dumps(errors), "id": notif_id,
        })
        await db.commit()
        log.info("Push %s: %d/%d delivered", notif_id, delivered, len(devices))
