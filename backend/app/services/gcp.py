"""
ZENI CLOUD CORE · Google Cloud SDK wrappers.

- Secret Manager: thay thế Vault cục bộ cho secrets prod
- Cloud Storage:  L2 object storage
- Auth: service account JSON path from GOOGLE_APPLICATION_CREDENTIALS

Mọi method đều graceful degrade nếu GCP credentials không có:
  - Secret Manager → rơi về local vault (cryptography.Fernet)
  - Cloud Storage  → ném NotImplementedError (bắt buộc config trước khi dùng prod)
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)


def gcp_ready() -> bool:
    """True nếu service account credentials sẵn sàng."""
    return bool(settings.gcp_project_id and settings.google_application_credentials)


# ─── SECRET MANAGER ──────────────────────────────────────
@lru_cache
def _secret_client():
    if not gcp_ready():
        return None
    try:
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient()
    except Exception as e:
        log.warning("Failed to init Secret Manager client: %s", e)
        return None


async def sm_create_secret(name: str, value: str) -> dict[str, Any]:
    client = _secret_client()
    if client is None:
        raise RuntimeError("GCP Secret Manager chưa cấu hình — dùng local vault")

    parent = f"projects/{settings.gcp_project_id}"
    path = f"{parent}/secrets/{name}"

    def _do():
        try:
            client.get_secret(name=path)
        except Exception:
            client.create_secret(
                parent=parent,
                secret_id=name,
                secret={"replication": {"automatic": {}}},
            )
        version = client.add_secret_version(
            parent=path,
            payload={"data": value.encode("utf-8")},
        )
        return {"name": name, "version": version.name.split("/")[-1]}

    return await asyncio.to_thread(_do)


async def sm_access_secret(name: str, version: str = "latest") -> str:
    client = _secret_client()
    if client is None:
        raise RuntimeError("GCP Secret Manager chưa cấu hình")

    path = f"projects/{settings.gcp_project_id}/secrets/{name}/versions/{version}"

    def _do():
        resp = client.access_secret_version(name=path)
        return resp.payload.data.decode("utf-8")

    return await asyncio.to_thread(_do)


async def sm_add_version(name: str, value: str) -> str:
    """Rotate: thêm version mới, version cũ tự deprecated."""
    client = _secret_client()
    if client is None:
        raise RuntimeError("GCP Secret Manager chưa cấu hình")

    path = f"projects/{settings.gcp_project_id}/secrets/{name}"

    def _do():
        v = client.add_secret_version(parent=path, payload={"data": value.encode("utf-8")})
        return v.name.split("/")[-1]

    return await asyncio.to_thread(_do)


# ─── CLOUD STORAGE ───────────────────────────────────────
@lru_cache
def _gcs_client():
    if not gcp_ready():
        return None
    try:
        from google.cloud import storage
        return storage.Client(project=settings.gcp_project_id)
    except Exception as e:
        log.warning("Failed to init GCS client: %s", e)
        return None


async def gcs_list_buckets() -> list[dict[str, Any]]:
    client = _gcs_client()
    if client is None:
        return []

    def _do():
        return [
            {"name": b.name, "location": b.location, "created": b.time_created.isoformat() if b.time_created else None}
            for b in client.list_buckets()
        ]

    return await asyncio.to_thread(_do)


async def gcs_ensure_bucket(name: str, location: str = "asia-southeast1") -> str:
    client = _gcs_client()
    if client is None:
        raise RuntimeError("GCP Cloud Storage chưa cấu hình")

    full_name = name if name.startswith(settings.gcs_bucket_prefix) else f"{settings.gcs_bucket_prefix}{name}"

    def _do():
        bucket = client.lookup_bucket(full_name)
        if bucket is None:
            bucket = client.create_bucket(full_name, location=location)
        return bucket.name

    return await asyncio.to_thread(_do)


async def gcs_upload(bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    client = _gcs_client()
    if client is None:
        raise RuntimeError("GCP Cloud Storage chưa cấu hình")

    def _do():
        b = client.bucket(bucket)
        blob = b.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{bucket}/{key}"

    return await asyncio.to_thread(_do)


async def gcs_list_objects(bucket: str, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
    client = _gcs_client()
    if client is None:
        return []

    def _do():
        b = client.bucket(bucket)
        return [
            {"key": blob.name, "size": blob.size, "updated": blob.updated.isoformat() if blob.updated else None}
            for blob in list(client.list_blobs(b, prefix=prefix, max_results=limit))
        ]

    return await asyncio.to_thread(_do)


async def gcs_signed_url(bucket: str, key: str, method: str = "GET", expires_seconds: int = 3600) -> str:
    from datetime import timedelta
    client = _gcs_client()
    if client is None:
        raise RuntimeError("GCP Cloud Storage chưa cấu hình")

    def _do():
        b = client.bucket(bucket)
        blob = b.blob(key)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_seconds),
            method=method,
        )

    return await asyncio.to_thread(_do)
