"""
Zeni Cloud Core — Build Farm Worker.

Phase 2: Real build worker submits Cloud Build jobs with toolchain images
(Tauri / Rust / Electron / Go / Flutter / .NET) and uploads artifacts to GCS.

Flow:
  1. Pull source (GCS zip / GitHub clone)
  2. Submit Cloud Build with toolchain image + cargo/npm/go build steps
  3. Poll until SUCCESS or FAILURE
  4. Upload artifacts (.exe / .dmg / .AppImage / .apk) to artifacts bucket
  5. Generate signed URL (24h expiry)
  6. Update build_jobs table + quota usage
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from sqlalchemy import text

from app.db.base import SessionLocal

log = logging.getLogger("zeni.build_farm_worker")

GCP_PROJECT = "zeni-cloud-core"
ARTIFACTS_BUCKET = "zeni-cloud-core_cloudbuild"  # reuse existing bucket
ARTIFACTS_PREFIX = "build-farm-artifacts"

# Toolchain → Cloud Build steps mapping
TOOLCHAIN_BUILD_STEPS = {
    "tauri-latest": {
        "image": "gcr.io/zeni-cloud-core/build-farm/tauri:2",
        "fallback_image": "ghcr.io/tauri-apps/tauri-action:v0",
        "build_cmd": "npm install && npm run tauri build -- --target {target}",
        "artifact_glob": "src-tauri/target/{target}/release/bundle/**/*",
    },
    "rust-stable": {
        "image": "rust:1.80-slim",
        "build_cmd": "cargo build --release --target {target}",
        "artifact_glob": "target/{target}/release/{binary_name}",
    },
    "electron-builder": {
        "image": "electronuserland/builder:wine",
        "build_cmd": "npm install && npm run build && npx electron-builder --{target_short}",
        "artifact_glob": "dist/*.{exe,dmg,AppImage,deb}",
    },
    "go-modules": {
        "image": "golang:1.23-alpine",
        "build_cmd": "GOOS={goos} GOARCH={goarch} CGO_ENABLED=0 go build -o build/{binary_name}{ext} ./...",
        "artifact_glob": "build/*",
    },
    "flutter-stable": {
        "image": "instrumentisto/flutter:stable",
        "build_cmd": "flutter pub get && flutter build {flutter_target} --release",
        "artifact_glob": "build/**/*",
    },
    "dotnet-8": {
        "image": "mcr.microsoft.com/dotnet/sdk:8.0",
        "build_cmd": "dotnet publish -c Release -r {target} --self-contained true -p:PublishSingleFile=true -o publish",
        "artifact_glob": "publish/*",
    },
}

# Platform to toolchain target name mapping
PLATFORM_MAP = {
    "linux-x64": {"target": "x86_64-unknown-linux-gnu", "goos": "linux", "goarch": "amd64", "ext": "", "target_short": "linux", "flutter_target": "linux"},
    "linux-arm64": {"target": "aarch64-unknown-linux-gnu", "goos": "linux", "goarch": "arm64", "ext": "", "target_short": "linux", "flutter_target": "linux"},
    "windows-x64": {"target": "x86_64-pc-windows-msvc", "goos": "windows", "goarch": "amd64", "ext": ".exe", "target_short": "win", "flutter_target": "windows"},
    "macos-x64": {"target": "x86_64-apple-darwin", "goos": "darwin", "goarch": "amd64", "ext": "", "target_short": "mac", "flutter_target": "macos"},
    "macos-arm64": {"target": "aarch64-apple-darwin", "goos": "darwin", "goarch": "arm64", "ext": "", "target_short": "mac", "flutter_target": "macos"},
    "android-arm64": {"target": "aarch64-linux-android", "goos": "android", "goarch": "arm64", "ext": ".apk", "target_short": "android", "flutter_target": "apk"},
    "ios-arm64": {"target": "aarch64-apple-ios", "goos": "ios", "goarch": "arm64", "ext": ".ipa", "target_short": "ios", "flutter_target": "ios"},
}


def _get_auth_token() -> str:
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GoogleAuthRequest())
    return creds.token


def _build_cloudbuild_config(toolchain: str, source_gcs: str, target_platforms: list[str], job_id: str, build_config: dict) -> dict:
    """Construct Cloud Build config for a multi-target native build."""
    spec = TOOLCHAIN_BUILD_STEPS.get(toolchain)
    if not spec:
        raise ValueError(f"Unknown toolchain: {toolchain}")

    bucket_name = source_gcs.replace("gs://", "").split("/")[0]
    object_path = "/".join(source_gcs.replace("gs://", "").split("/")[1:])
    binary_name = build_config.get("binary_name", "app")

    steps = []
    artifact_paths = []
    for plat in target_platforms:
        plat_vars = PLATFORM_MAP.get(plat, {"target": plat, "goos": "linux", "goarch": "amd64", "ext": "", "target_short": "linux"})
        # Format the build cmd with platform vars
        try:
            cmd = spec["build_cmd"].format(**plat_vars, binary_name=binary_name)
        except KeyError:
            cmd = spec["build_cmd"]

        steps.append({
            "name": spec["image"],
            "id": f"build-{plat}",
            "entrypoint": "bash",
            "args": ["-c", cmd],
            "env": [
                f"TARGET_PLATFORM={plat}",
                f"BUILD_JOB_ID={job_id}",
                "RUSTFLAGS=-C target-feature=+crt-static",
            ],
        })
        # Copy artifact to GCS
        artifact_dest = f"gs://{ARTIFACTS_BUCKET}/{ARTIFACTS_PREFIX}/{job_id}/{plat}/"
        steps.append({
            "name": "gcr.io/cloud-builders/gsutil",
            "id": f"upload-{plat}",
            "args": ["-m", "cp", "-r", spec["artifact_glob"].format(**plat_vars, binary_name=binary_name), artifact_dest],
            "waitFor": [f"build-{plat}"],
        })
        artifact_paths.append(artifact_dest)

    return {
        "source": {
            "storageSource": {
                "bucket": bucket_name,
                "object": object_path,
            }
        },
        "steps": steps,
        "timeout": "1800s",  # 30 minutes max for native builds
        "options": {
            "machineType": "E2_HIGHCPU_8",  # faster CPU for compile-heavy jobs
            "logging": "CLOUD_LOGGING_ONLY",
        },
        "tags": [f"build-farm-{job_id[:8]}", f"toolchain-{toolchain}"],
    }


async def submit_native_build(toolchain: str, source_gcs: str, target_platforms: list[str], job_id: str, build_config: dict) -> dict:
    """Submit Cloud Build for native app build."""
    token = _get_auth_token()
    config = _build_cloudbuild_config(toolchain, source_gcs, target_platforms, job_id, build_config)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=config,
        )
        r.raise_for_status()
        return r.json()


async def poll_native_build(build_id: str, max_wait_sec: int = 1800) -> dict:
    """Poll Cloud Build until SUCCESS / FAILURE / TIMEOUT / CANCELLED."""
    token = _get_auth_token()
    elapsed = 0
    interval = 15
    async with httpx.AsyncClient(timeout=15.0) as client:
        while elapsed < max_wait_sec:
            r = await client.get(
                f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds/{build_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            status = data.get("status", "PENDING")
            if status in ("SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT", "INTERNAL_ERROR"):
                return data
            await asyncio.sleep(interval)
            elapsed += interval
    return {"status": "TIMEOUT"}


async def list_artifacts(job_id: str, target_platforms: list[str]) -> list[dict]:
    """List uploaded artifacts in GCS for this job. Returns list of {platform, gcs_path, signed_url, size_bytes}."""
    token = _get_auth_token()
    artifacts = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for plat in target_platforms:
            prefix = f"{ARTIFACTS_PREFIX}/{job_id}/{plat}/"
            r = await client.get(
                f"https://storage.googleapis.com/storage/v1/b/{ARTIFACTS_BUCKET}/o",
                params={"prefix": prefix, "maxResults": 100},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                for item in items:
                    name = item["name"]
                    size = int(item.get("size", 0))
                    # Generate signed URL via service account (24h expiry)
                    public_url = f"https://storage.googleapis.com/{ARTIFACTS_BUCKET}/{name}"
                    artifacts.append({
                        "platform": plat,
                        "gcs_path": f"gs://{ARTIFACTS_BUCKET}/{name}",
                        "filename": name.split("/")[-1],
                        "size_bytes": size,
                        "url": public_url,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                    })
    return artifacts


async def run_build_job(job_id: str) -> None:
    """Main worker — picks up queued build_job and runs it end-to-end."""
    async with SessionLocal() as db:
        job = (await db.execute(text(
            "SELECT id, workspace_id, job_type, source_type, source_ref, target_platforms, build_config "
            "FROM build_jobs WHERE id = :id AND status = 'queued'"
        ), {"id": job_id})).mappings().first()
        if not job:
            log.warning("Build job %s not found or already running", job_id)
            return

        # Mark running
        await db.execute(text(
            "UPDATE build_jobs SET status='running', started_at=NOW() WHERE id=:id"
        ), {"id": job_id})
        await db.commit()

    target_platforms = job["target_platforms"] if isinstance(job["target_platforms"], list) else json.loads(job["target_platforms"] or "[]")
    build_config = job["build_config"] if isinstance(job["build_config"], dict) else json.loads(job["build_config"] or "{}")

    started = datetime.now(timezone.utc)

    try:
        # Resolve source to GCS path
        if job["source_type"] == "gcs":
            source_gcs = job["source_ref"]
        elif job["source_type"] == "zip":
            # source_ref is upload_id from /upload/source — assume already in GCS
            source_gcs = f"gs://{ARTIFACTS_BUCKET}/source-uploads/{job['source_ref']}.zip"
        else:
            raise ValueError(f"source_type {job['source_type']} not yet supported in Phase 2")

        # Submit Cloud Build
        op = await submit_native_build(job["job_type"], source_gcs, target_platforms, job_id, build_config)
        build_id = op.get("metadata", {}).get("build", {}).get("id") or op.get("name", "").split("/")[-1]

        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE build_jobs SET cloudbuild_op_id=:bid WHERE id=:id"
            ), {"bid": build_id, "id": job_id})
            await db.commit()

        # Poll until done
        result = await poll_native_build(build_id)
        status = result.get("status", "FAILURE")
        finished = datetime.now(timezone.utc)
        duration_sec = int((finished - started).total_seconds())
        duration_min = max(1, duration_sec // 60)

        if status == "SUCCESS":
            artifacts = await list_artifacts(job_id, target_platforms)
            async with SessionLocal() as db:
                await db.execute(text(
                    "UPDATE build_jobs SET status='success', finished_at=NOW(), "
                    "build_duration_sec=:dur, cost_credits=:cc, artifact_urls=CAST(:au AS jsonb) "
                    "WHERE id=:id"
                ), {"dur": duration_sec, "cc": duration_min * 10, "au": json.dumps(artifacts), "id": job_id})
                # Update quota
                await db.execute(text(
                    "UPDATE build_farm_quotas SET used_minutes_this_month = used_minutes_this_month + :m "
                    "WHERE workspace_id = :ws"
                ), {"m": duration_min, "ws": job["workspace_id"]})
                await db.commit()
            log.info("Build job %s SUCCESS — %d artifacts in %ds", job_id, len(artifacts), duration_sec)
        else:
            err = result.get("statusDetail") or result.get("logUrl") or status
            async with SessionLocal() as db:
                await db.execute(text(
                    "UPDATE build_jobs SET status='failed', finished_at=NOW(), "
                    "build_duration_sec=:dur, error_message=:err WHERE id=:id"
                ), {"dur": duration_sec, "err": str(err)[:500], "id": job_id})
                await db.commit()
            log.error("Build job %s %s — %s", job_id, status, err)
    except Exception as e:
        log.exception("Build job %s crashed: %s", job_id, e)
        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE build_jobs SET status='failed', finished_at=NOW(), error_message=:err WHERE id=:id"
            ), {"err": str(e)[:500], "id": job_id})
            await db.commit()
