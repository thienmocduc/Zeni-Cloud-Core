"""
Zeni Cloud Core — Container Registry API.

Mỗi workspace = 1 Artifact Registry repo riêng. Khách push image lên đó
thay vì Docker Hub (free tier 5GB, $0.10/GB sau).

Endpoints (prefix /registry):
  GET    /info?ws=X            — registry URL + image list (paginated)
  POST   /provision?ws=X       — auto-create AR repo + SA + whitelist (idempotent)
  POST   /key?ws=X             — generate fresh SA key JSON cho docker login (1-time download)

Backend reuse pattern VCT đã có:
  - AR repo: us-central1-docker.pkg.dev/zeni-cloud-core/{workspace_slug}/
  - SA: {workspace_slug}-pusher@zeni-cloud-core.iam.gserviceaccount.com
  - Role: roles/artifactregistry.writer
  - Whitelist prefix tự động add vào workspace_image_whitelist
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.registry")
router = APIRouter(prefix="/registry", tags=["container-registry"])

GCP_PROJECT = "zeni-cloud-core"
AR_LOCATION = "us-central1"
AR_HOST = f"{AR_LOCATION}-docker.pkg.dev"


def _workspace_slug(ws: str) -> str:
    """workspace_id → slug an toàn cho AR repo + SA name."""
    s = re.sub(r"[^a-z0-9-]", "-", ws.lower()).strip("-")
    return s[:30] or "ws"


def _registry_url(ws: str) -> str:
    return f"{AR_HOST}/{GCP_PROJECT}/{_workspace_slug(ws)}"


def _pusher_sa(ws: str) -> str:
    return f"{_workspace_slug(ws)}-pusher@{GCP_PROJECT}.iam.gserviceaccount.com"


async def _get_gcp_token() -> str:
    """Cloud Run SA token cho GCP REST API calls."""
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request as GoogleAuthRequest
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GoogleAuthRequest())
    return creds.token


# ─── Schemas ─────────────────────────────────────────────────
class RegistryInfo(BaseModel):
    workspace_id: str
    registry_url: str
    pusher_sa: str
    provisioned: bool
    repo_size_bytes: int | None = None
    image_count: int | None = None
    images: list[dict] = Field(default_factory=list)


class ProvisionOut(BaseModel):
    workspace_id: str
    registry_url: str
    pusher_sa: str
    repo_created: bool
    sa_created: bool
    whitelist_added: bool


class KeyOut(BaseModel):
    workspace_id: str
    key_json: dict  # full SA key JSON — khách lưu rồi `docker login`
    docker_login_cmd: str
    expires_note: str = "Key không hết hạn nhưng có thể revoke qua GCP console nếu lộ."


# ─── Endpoints ───────────────────────────────────────────────
@router.get("/info", response_model=RegistryInfo)
async def registry_info(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RegistryInfo:
    """Show registry URL + list pushed images."""
    await require_workspace_access(ws, me)
    slug = _workspace_slug(ws)
    url = _registry_url(ws)
    sa = _pusher_sa(ws)

    token = await _get_gcp_token()
    provisioned = False
    images: list[dict] = []
    repo_size = 0
    image_count = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Check repo exists
        r = await client.get(
            f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories/{slug}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 200:
            provisioned = True
            repo_data = r.json()
            repo_size = int(repo_data.get("sizeBytes", 0))

            # List packages (image repos)
            pkg_r = await client.get(
                f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories/{slug}/packages?pageSize=50",
                headers={"Authorization": f"Bearer {token}"},
            )
            if pkg_r.status_code == 200:
                pkgs = pkg_r.json().get("packages", [])
                image_count = len(pkgs)
                for p in pkgs[:20]:
                    images.append({
                        "name": p.get("name", "").rsplit("/", 1)[-1],
                        "create_time": p.get("createTime"),
                        "update_time": p.get("updateTime"),
                    })

    return RegistryInfo(
        workspace_id=ws, registry_url=url, pusher_sa=sa,
        provisioned=provisioned, repo_size_bytes=repo_size,
        image_count=image_count, images=images,
    )


@router.post("/provision", response_model=ProvisionOut, status_code=201)
async def registry_provision(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProvisionOut:
    """Auto-create AR repo + SA + IAM binding + whitelist (idempotent)."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Cần Developer trở lên")

    slug = _workspace_slug(ws)
    url = _registry_url(ws)
    sa_email = _pusher_sa(ws)
    sa_short = f"{slug}-pusher"

    token = await _get_gcp_token()
    repo_created = sa_created = whitelist_added = False

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1) Create AR repo (idempotent — 409 = đã tồn tại OK)
        r = await client.post(
            f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories?repositoryId={slug}",
            headers=headers,
            json={
                "format": "DOCKER",
                "description": f"Zeni Container Registry for workspace {ws}",
                "labels": {"workspace": slug, "managed_by": "zeni-cloud"},
            },
        )
        if r.status_code in (200, 202):
            repo_created = True
        elif r.status_code == 409:
            log.info("[registry] repo %s already exists", slug)
        else:
            raise HTTPException(status_code=502, detail=f"AR create repo failed: {r.text[:200]}")

        # 2) Create SA (idempotent)
        r = await client.post(
            f"https://iam.googleapis.com/v1/projects/{GCP_PROJECT}/serviceAccounts",
            headers=headers,
            json={
                "accountId": sa_short,
                "serviceAccount": {
                    "displayName": f"{ws} pusher",
                    "description": f"Push images to AR repo {slug} (Zeni Cloud auto-provisioned)",
                },
            },
        )
        if r.status_code in (200, 201):
            sa_created = True
        elif r.status_code == 409:
            log.info("[registry] SA %s already exists", sa_email)
        else:
            log.warning("[registry] SA create unexpected %s: %s", r.status_code, r.text[:200])

        # 3) IAM grant artifactregistry.writer on the repo
        try:
            grant_body = {
                "policy": {
                    "bindings": [
                        {"role": "roles/artifactregistry.writer",
                         "members": [f"serviceAccount:{sa_email}"]}
                    ]
                }
            }
            await client.post(
                f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories/{slug}:setIamPolicy",
                headers=headers, json=grant_body,
            )
        except Exception as e:
            log.warning("[registry] IAM bind warn: %s", e)

    # 4) Add prefix to workspace_image_whitelist
    try:
        await db.execute(text("""
            INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, enabled)
            VALUES (:ws, :p, 'Zeni Container Registry (auto-provisioned)', TRUE)
            ON CONFLICT (workspace_id, prefix) DO NOTHING
        """), {"ws": ws, "p": url + "/"})
        await db.commit()
        whitelist_added = True
    except Exception as e:
        log.warning("[registry] whitelist insert warn: %s", e)

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="registry.provision",
        target=slug, severity="ok",
        metadata={"repo_created": repo_created, "sa_created": sa_created,
                  "whitelist_added": whitelist_added},
    )
    await db.commit()

    return ProvisionOut(
        workspace_id=ws, registry_url=url, pusher_sa=sa_email,
        repo_created=repo_created, sa_created=sa_created,
        whitelist_added=whitelist_added,
    )


@router.post("/key", response_model=KeyOut, status_code=201)
async def registry_key(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KeyOut:
    """Generate fresh SA key cho khách docker login. Trả 1 lần — khách lưu lại."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên để generate key")

    sa_email = _pusher_sa(ws)
    token = await _get_gcp_token()

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://iam.googleapis.com/v1/projects/{GCP_PROJECT}/serviceAccounts/{sa_email}/keys",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"keyAlgorithm": "KEY_ALG_RSA_2048", "privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE"},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=502,
                                 detail=f"SA key create failed: {r.text[:300]}. Đã provision registry chưa?")

        key_data = r.json()
        # privateKeyData is base64-encoded JSON
        import base64
        key_json = json.loads(base64.b64decode(key_data["privateKeyData"]))

    login_cmd = (
        f"cat key.json | docker login -u _json_key --password-stdin {AR_HOST}\n"
        f"docker tag your-image {_registry_url(ws)}/your-app:v1\n"
        f"docker push {_registry_url(ws)}/your-app:v1"
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="registry.key.generate",
        target=sa_email, severity="warn",
        metadata={"key_id": key_data.get("name", "").rsplit("/", 1)[-1]},
    )
    await db.commit()

    return KeyOut(
        workspace_id=ws, key_json=key_json, docker_login_cmd=login_cmd,
    )
