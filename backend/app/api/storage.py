"""
Zeni Cloud Core — Zeni Storage API (Supabase Storage parity).

S3-compatible object storage backed by Google Cloud Storage.
Multi-tenant: bucket-per-workspace, signed URLs, lifecycle, versioning.

Endpoints (prefix /storage):
  POST   /buckets                         — Create bucket
  GET    /buckets                         — List buckets in workspace
  GET    /buckets/{name}                  — Bucket detail
  DELETE /buckets/{name}                  — Delete bucket (must be empty)

  POST   /buckets/{name}/objects          — Upload object (multipart)
  GET    /buckets/{name}/objects          — List objects in bucket
  GET    /buckets/{name}/objects/{key}    — Download object
  DELETE /buckets/{name}/objects/{key}    — Soft delete object

  POST   /buckets/{name}/signed-url       — Generate signed upload/download URL
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.storage")

router = APIRouter(prefix="/storage", tags=["storage"])

GCP_PROJECT = "zeni-cloud-core"
DEFAULT_LOCATION = "ASIA-SOUTHEAST1"  # multi-region near VN customers

BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$")


# ===== Schemas =====

class BucketCreate(BaseModel):
    name: str = Field(..., description="Lowercase alphanumeric + dash, 3-62 chars")
    visibility: str = Field("private", description="private | public-read | authenticated")
    allowed_mime_types: list[str] = Field(default_factory=list)
    max_file_size_mb: int = Field(100, ge=1, le=10240)
    storage_quota_mb: int = Field(10240, ge=100, le=1048576)
    default_expiry_days: Optional[int] = None
    versioning_enabled: bool = False


class BucketOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    gcs_bucket_name: str
    visibility: str
    allowed_mime_types: list[str]
    max_file_size_mb: int
    storage_quota_mb: int
    used_bytes: int
    default_expiry_days: Optional[int]
    versioning_enabled: bool
    created_at: str


class ObjectOut(BaseModel):
    key: str
    bucket_name: str
    content_type: Optional[str] = None
    content_length: int = 0
    etag: Optional[str] = None
    custom_metadata: dict = Field(default_factory=dict)
    uploaded_at: str
    expires_at: Optional[str] = None
    download_url: Optional[str] = None


class SignedUrlIn(BaseModel):
    key: str = Field(..., description="Object path within bucket")
    method: str = Field("GET", description="GET | PUT | DELETE")
    expires_in_seconds: int = Field(3600, ge=60, le=604800, description="URL TTL (default 1h, max 7d)")
    content_type: Optional[str] = Field(None, description="For PUT: required content-type")


# ===== Endpoints — Buckets =====

@router.post("/buckets", response_model=BucketOut, status_code=201)
async def create_bucket(
    data: BucketCreate,
    ws: str = Query(..., description="workspace_id"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Create new storage bucket. Returns bucket details + auto-creates GCS bucket."""
    await require_workspace_access(ws, me)
    if not BUCKET_NAME_RE.match(data.name):
        raise HTTPException(422, "Bucket name: lowercase alphanumeric + dash, 3-62 chars, no leading/trailing dash")

    if data.visibility not in ("private", "public-read", "authenticated"):
        raise HTTPException(422, "visibility must be private | public-read | authenticated")

    # GCS bucket name globally unique: zeni-st-{ws_slug}-{name}
    ws_slug = re.sub(r"[^a-z0-9-]", "-", ws.lower())[:20]
    gcs_name = f"zeni-st-{ws_slug}-{data.name}"[:63]

    bucket_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO storage_buckets (id, workspace_id, name, gcs_bucket_name, visibility, "
            "allowed_mime_types, max_file_size_mb, storage_quota_mb, default_expiry_days, "
            "versioning_enabled, created_by) "
            "VALUES (:id, :ws, :n, :gcs, :v, CAST(:mt AS jsonb), :mfs, :sq, :de, :ve, :cb)"
        ), {
            "id": str(bucket_id),
            "ws": ws,
            "n": data.name,
            "gcs": gcs_name,
            "v": data.visibility,
            "mt": json.dumps(data.allowed_mime_types),
            "mfs": data.max_file_size_mb,
            "sq": data.storage_quota_mb,
            "de": data.default_expiry_days,
            "ve": data.versioning_enabled,
            "cb": str(me.id) if me else None,
        })
        await db.commit()
    except Exception as e:
        if "duplicate" in str(e).lower():
            raise HTTPException(409, f"Bucket '{data.name}' already exists in this workspace")
        raise

    # Create GCS bucket (idempotent — if already exists, OK)
    try:
        await _create_gcs_bucket(gcs_name)
    except Exception as e:
        log.warning("GCS bucket creation deferred (will retry on first upload): %s", e)

    return BucketOut(
        id=str(bucket_id),
        workspace_id=ws,
        name=data.name,
        gcs_bucket_name=gcs_name,
        visibility=data.visibility,
        allowed_mime_types=data.allowed_mime_types,
        max_file_size_mb=data.max_file_size_mb,
        storage_quota_mb=data.storage_quota_mb,
        used_bytes=0,
        default_expiry_days=data.default_expiry_days,
        versioning_enabled=data.versioning_enabled,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/buckets", response_model=list[BucketOut])
async def list_buckets(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, workspace_id, name, gcs_bucket_name, visibility, allowed_mime_types, "
        "max_file_size_mb, storage_quota_mb, used_bytes, default_expiry_days, versioning_enabled, created_at "
        "FROM storage_buckets WHERE workspace_id = :ws ORDER BY name"
    ), {"ws": ws})).mappings().all()
    return [_row_to_bucket(r) for r in rows]


@router.get("/buckets/{name}", response_model=BucketOut)
async def get_bucket(
    name: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, name, gcs_bucket_name, visibility, allowed_mime_types, "
        "max_file_size_mb, storage_quota_mb, used_bytes, default_expiry_days, versioning_enabled, created_at "
        "FROM storage_buckets WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not r:
        raise HTTPException(404, "Bucket not found")
    return _row_to_bucket(r)


@router.delete("/buckets/{name}", status_code=204)
async def delete_bucket(
    name: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    bucket = (await db.execute(text(
        "SELECT id FROM storage_buckets WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    obj_count = (await db.execute(text(
        "SELECT COUNT(*) FROM storage_objects WHERE bucket_id = :bid AND deleted_at IS NULL"
    ), {"bid": str(bucket["id"])})).scalar() or 0
    if obj_count > 0:
        raise HTTPException(409, f"Bucket has {obj_count} objects. Delete objects first.")
    await db.execute(text(
        "DELETE FROM storage_buckets WHERE id = :bid"
    ), {"bid": str(bucket["id"])})
    await db.commit()


# ===== Endpoints — Objects =====

@router.post("/buckets/{bucket_name}/objects", response_model=ObjectOut, status_code=201)
async def upload_object(
    bucket_name: str,
    ws: str = Query(...),
    file: UploadFile = File(...),
    key: Optional[str] = Form(None, description="Override storage key (default: filename)"),
    content_type: Optional[str] = Form(None),
    custom_metadata: Optional[str] = Form(None, description="JSON dict"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Upload object to bucket. Multipart form-data: file=<binary>, key=<path>, ..."""
    await require_workspace_access(ws, me)
    bucket = await _get_bucket(db, ws, bucket_name)

    obj_key = key or file.filename or f"obj-{uuid.uuid4().hex[:8]}"
    obj_key = obj_key.lstrip("/")

    # Read content
    content = await file.read()
    size = len(content)
    if size > bucket["max_file_size_mb"] * 1024 * 1024:
        raise HTTPException(413, f"File > {bucket['max_file_size_mb']}MB limit")

    # Quota check
    if (bucket["used_bytes"] or 0) + size > bucket["storage_quota_mb"] * 1024 * 1024:
        raise HTTPException(413, f"Bucket quota exceeded ({bucket['storage_quota_mb']}MB)")

    # MIME check
    mt = content_type or file.content_type or "application/octet-stream"
    allowed = bucket["allowed_mime_types"]
    if isinstance(allowed, str):
        allowed = json.loads(allowed)
    if allowed:
        ok = False
        for pattern in allowed:
            if pattern.endswith("/*") and mt.startswith(pattern[:-1]):
                ok = True
                break
            if pattern == mt:
                ok = True
                break
        if not ok:
            raise HTTPException(415, f"MIME '{mt}' not in allowed list: {allowed}")

    # Upload to GCS
    etag = hashlib.md5(content).hexdigest()
    gcs_path = f"{bucket['gcs_bucket_name']}/{obj_key}"

    try:
        await _upload_to_gcs(bucket["gcs_bucket_name"], obj_key, content, mt)
    except Exception as e:
        log.exception("GCS upload failed: %s", e)
        raise HTTPException(500, f"Upload to GCS failed: {str(e)[:200]}")

    expires_at = None
    if bucket["default_expiry_days"]:
        expires_at = datetime.now(timezone.utc) + timedelta(days=bucket["default_expiry_days"])

    obj_id = uuid.uuid4()
    cm = {}
    if custom_metadata:
        try:
            cm = json.loads(custom_metadata)
        except Exception:
            cm = {}

    await db.execute(text(
        "INSERT INTO storage_objects (id, bucket_id, workspace_id, key, gcs_object_path, "
        "content_type, content_length, etag, custom_metadata, uploaded_by, expires_at) "
        "VALUES (:id, :bid, :ws, :k, :gp, :ct, :cl, :et, CAST(:cm AS jsonb), :ub, :ex)"
    ), {
        "id": str(obj_id),
        "bid": str(bucket["id"]),
        "ws": ws,
        "k": obj_key,
        "gp": f"gs://{gcs_path}",
        "ct": mt,
        "cl": size,
        "et": etag,
        "cm": json.dumps(cm),
        "ub": str(me.id) if me else None,
        "ex": expires_at,
    })
    await db.execute(text(
        "UPDATE storage_buckets SET used_bytes = used_bytes + :s, updated_at = NOW() WHERE id = :bid"
    ), {"s": size, "bid": str(bucket["id"])})
    await db.commit()

    return ObjectOut(
        key=obj_key,
        bucket_name=bucket_name,
        content_type=mt,
        content_length=size,
        etag=etag,
        custom_metadata=cm,
        uploaded_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        download_url=f"/api/v1/storage/buckets/{bucket_name}/objects/{obj_key}?ws={ws}",
    )


@router.get("/buckets/{bucket_name}/objects", response_model=list[ObjectOut])
async def list_objects(
    bucket_name: str,
    ws: str = Query(...),
    prefix: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    bucket = await _get_bucket(db, ws, bucket_name)
    sql = (
        "SELECT key, content_type, content_length, etag, custom_metadata, uploaded_at, expires_at "
        "FROM storage_objects WHERE bucket_id = :bid AND deleted_at IS NULL"
    )
    params: dict[str, Any] = {"bid": str(bucket["id"])}
    if prefix:
        sql += " AND key LIKE :pfx"
        params["pfx"] = prefix + "%"
    sql += " ORDER BY uploaded_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        ObjectOut(
            key=r["key"],
            bucket_name=bucket_name,
            content_type=r["content_type"],
            content_length=r["content_length"] or 0,
            etag=r["etag"],
            custom_metadata=r["custom_metadata"] if isinstance(r["custom_metadata"], dict) else {},
            uploaded_at=r["uploaded_at"].isoformat() if r["uploaded_at"] else "",
            expires_at=r["expires_at"].isoformat() if r["expires_at"] else None,
            download_url=f"/api/v1/storage/buckets/{bucket_name}/objects/{r['key']}?ws={ws}",
        )
        for r in rows
    ]


@router.get("/buckets/{bucket_name}/objects/{key:path}")
async def download_object(
    bucket_name: str,
    key: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Download object (proxies through Zeni; for high-traffic use signed-url instead)."""
    await require_workspace_access(ws, me)
    bucket = await _get_bucket(db, ws, bucket_name)
    obj = (await db.execute(text(
        "SELECT gcs_object_path, content_type FROM storage_objects "
        "WHERE bucket_id = :bid AND key = :k AND deleted_at IS NULL AND is_latest = TRUE"
    ), {"bid": str(bucket["id"]), "k": key})).mappings().first()
    if not obj:
        raise HTTPException(404, "Object not found")

    # Update last_accessed_at
    await db.execute(text(
        "UPDATE storage_objects SET last_accessed_at = NOW() "
        "WHERE bucket_id = :bid AND key = :k AND is_latest = TRUE"
    ), {"bid": str(bucket["id"]), "k": key})
    await db.commit()

    # Stream from GCS
    gcs_path = obj["gcs_object_path"].replace("gs://", "")
    bucket_name_gcs = gcs_path.split("/")[0]
    object_name = "/".join(gcs_path.split("/")[1:])

    async def iter_gcs():
        token = await _get_gcs_token()
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("GET",
                f"https://storage.googleapis.com/storage/v1/b/{bucket_name_gcs}/o/{object_name}?alt=media",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    return StreamingResponse(iter_gcs(), media_type=obj["content_type"] or "application/octet-stream")


@router.delete("/buckets/{bucket_name}/objects/{key:path}", status_code=204)
async def delete_object(
    bucket_name: str,
    key: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    bucket = await _get_bucket(db, ws, bucket_name)
    r = (await db.execute(text(
        "UPDATE storage_objects SET deleted_at = NOW() "
        "WHERE bucket_id = :bid AND key = :k AND deleted_at IS NULL "
        "RETURNING content_length"
    ), {"bid": str(bucket["id"]), "k": key})).first()
    if not r:
        raise HTTPException(404, "Object not found")
    if r[0]:
        await db.execute(text(
            "UPDATE storage_buckets SET used_bytes = GREATEST(0, used_bytes - :s) WHERE id = :bid"
        ), {"s": r[0], "bid": str(bucket["id"])})
    await db.commit()


@router.post("/buckets/{bucket_name}/signed-url")
async def generate_signed_url(
    bucket_name: str,
    data: SignedUrlIn,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Generate signed URL for direct GCS upload/download. TTL up to 7 days."""
    await require_workspace_access(ws, me)
    bucket = await _get_bucket(db, ws, bucket_name)
    method = data.method.upper()
    if method not in ("GET", "PUT", "DELETE"):
        raise HTTPException(422, "method must be GET | PUT | DELETE")

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=data.expires_in_seconds)

    # Use GCS signed URL via service account credentials
    try:
        signed_url = await _generate_gcs_signed_url(
            bucket["gcs_bucket_name"], data.key, method,
            expires_in_seconds=data.expires_in_seconds,
            content_type=data.content_type,
        )
    except Exception as e:
        log.exception("Signed URL generation failed: %s", e)
        raise HTTPException(500, f"Failed to generate signed URL: {str(e)[:200]}")

    # Audit log
    await db.execute(text(
        "INSERT INTO storage_signed_urls (workspace_id, bucket_id, object_key, url_method, expires_at, generated_by) "
        "VALUES (:ws, :bid, :k, :m, :ex, :gb)"
    ), {
        "ws": ws,
        "bid": str(bucket["id"]),
        "k": data.key,
        "m": method,
        "ex": expires_at,
        "gb": str(me.id) if me else None,
    })
    await db.commit()

    return {
        "url": signed_url,
        "method": method,
        "expires_at": expires_at.isoformat(),
        "bucket": bucket_name,
        "key": data.key,
    }


# ===== Helpers =====

async def _get_bucket(db: AsyncSession, ws: str, name: str) -> dict:
    r = (await db.execute(text(
        "SELECT id, workspace_id, name, gcs_bucket_name, visibility, allowed_mime_types, "
        "max_file_size_mb, storage_quota_mb, used_bytes, default_expiry_days, versioning_enabled "
        "FROM storage_buckets WHERE workspace_id = :ws AND name = :n"
    ), {"ws": ws, "n": name})).mappings().first()
    if not r:
        raise HTTPException(404, f"Bucket '{name}' not found")
    return dict(r)


def _row_to_bucket(r) -> BucketOut:
    mts = r["allowed_mime_types"] if isinstance(r["allowed_mime_types"], list) else json.loads(r["allowed_mime_types"] or "[]")
    return BucketOut(
        id=str(r["id"]),
        workspace_id=r["workspace_id"],
        name=r["name"],
        gcs_bucket_name=r["gcs_bucket_name"],
        visibility=r["visibility"],
        allowed_mime_types=mts,
        max_file_size_mb=r["max_file_size_mb"],
        storage_quota_mb=r["storage_quota_mb"],
        used_bytes=r["used_bytes"] or 0,
        default_expiry_days=r["default_expiry_days"],
        versioning_enabled=r["versioning_enabled"],
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
    )


async def _get_gcs_token() -> str:
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request as GAR
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    creds.refresh(GAR())
    return creds.token


async def _create_gcs_bucket(name: str) -> None:
    """Idempotent: creates GCS bucket. Already-exists is OK."""
    token = await _get_gcs_token()
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"https://storage.googleapis.com/storage/v1/b?project={GCP_PROJECT}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": name, "location": DEFAULT_LOCATION, "storageClass": "STANDARD"},
        )
        if r.status_code in (200, 201):
            return
        if r.status_code == 409:
            return  # already exists
        raise RuntimeError(f"GCS bucket create failed: {r.status_code} {r.text[:200]}")


async def _upload_to_gcs(bucket: str, object_name: str, content: bytes, mime: str) -> None:
    """Idempotent upload via JSON API multipart."""
    # Ensure bucket exists first (best-effort)
    try:
        await _create_gcs_bucket(bucket)
    except Exception:
        pass
    token = await _get_gcs_token()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
            params={"uploadType": "media", "name": object_name},
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
            content=content,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"GCS upload failed: {r.status_code} {r.text[:200]}")


async def _generate_gcs_signed_url(
    bucket: str, object_name: str, method: str, expires_in_seconds: int,
    content_type: Optional[str] = None,
) -> str:
    """Generate V4 signed URL using google-cloud-storage."""
    from google.cloud import storage as gcs_lib
    from datetime import timedelta as td
    client = gcs_lib.Client()
    blob = client.bucket(bucket).blob(object_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=td(seconds=expires_in_seconds),
        method=method,
        content_type=content_type,
    )
