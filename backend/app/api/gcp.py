from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.services.gcp import gcp_ready, gcs_list_buckets, gcs_list_objects, gcs_ensure_bucket, gcs_signed_url, sm_access_secret

router = APIRouter(prefix="/gcp", tags=["gcp"])


@router.get("/status")
async def status(me: CurrentUser = Depends(get_current_user)) -> dict:
    return {
        "ready": gcp_ready(),
        "project": None if not gcp_ready() else "configured",
    }


@router.get("/storage/buckets")
async def list_buckets(me: CurrentUser = Depends(get_current_user)) -> list[dict]:
    if not gcp_ready():
        raise HTTPException(status_code=503, detail="GCP chưa cấu hình")
    return await gcs_list_buckets()


@router.post("/storage/buckets/{name}")
async def ensure_bucket(
    name: str,
    location: str = "asia-southeast1",
    me: CurrentUser = Depends(get_current_user),
) -> dict:
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để tạo bucket")
    if not gcp_ready():
        raise HTTPException(status_code=503, detail="GCP chưa cấu hình")
    full = await gcs_ensure_bucket(name, location)
    return {"bucket": full}


@router.get("/storage/buckets/{bucket}/objects")
async def list_objects(
    bucket: str,
    prefix: str = "",
    limit: int = 100,
    me: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    if not gcp_ready():
        raise HTTPException(status_code=503, detail="GCP chưa cấu hình")
    return await gcs_list_objects(bucket, prefix, limit)


@router.get("/storage/signed-url")
async def signed_url(
    bucket: str,
    key: str,
    method: str = "GET",
    expires: int = 3600,
    me: CurrentUser = Depends(get_current_user),
) -> dict:
    if not gcp_ready():
        raise HTTPException(status_code=503, detail="GCP chưa cấu hình")
    url = await gcs_signed_url(bucket, key, method, expires)
    return {"url": url, "expires_in": expires}


@router.get("/secrets/{name}")
async def access_secret(
    name: str,
    version: str = "latest",
    me: CurrentUser = Depends(get_current_user),
) -> dict:
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để đọc secret")
    if not gcp_ready():
        raise HTTPException(status_code=503, detail="GCP chưa cấu hình")
    try:
        value = await sm_access_secret(name, version)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"name": name, "version": version, "masked": value[:4] + "•" * 8 + value[-4:] if len(value) > 8 else "•" * len(value)}
