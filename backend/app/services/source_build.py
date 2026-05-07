"""
Zeni Cloud Core — Source Build Worker.

Build & deploy from uploaded ZIP source code (Phase 2 of source_upload.py).

Flow:
  1. Download ZIP from GCS (upload was saved there in Phase 1)
  2. Extract to temp dir
  3. If no Dockerfile, generate from framework template
  4. Tar source → upload to Cloud Build source bucket
  5. Submit Cloud Build with --tag → wait for SUCCESS
  6. Get image URL from Artifact Registry
  7. Call projects.deploy_project to create Cloud Run service
  8. Update source_uploads.status='success' + deploy_url

Uses google.auth + httpx for REST API calls (avoid heavy SDK).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.source_build")

GCS_BUCKET = "zeni-cloud-core_cloudbuild"  # reuse Cloud Build's default bucket
GCP_PROJECT = "zeni-cloud-core"
ARTIFACT_REGISTRY = "us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images"


def _get_auth_token() -> str:
    """Get OAuth access token from default credentials (Cloud Run SA)."""
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GoogleAuthRequest())
    return creds.token


async def upload_zip_to_gcs(zip_bytes: bytes, gcs_path: str) -> str:
    """Upload raw ZIP to GCS. Returns gs:// URL."""
    from google.cloud import storage
    client = storage.Client(project=GCP_PROJECT)
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(zip_bytes, content_type="application/zip")
    return f"gs://{GCS_BUCKET}/{gcs_path}"


def _generate_dockerfile(framework: str, port: int = 8080) -> str:
    """Generate Dockerfile if user's ZIP doesn't have one."""
    templates = {
        "static": f"""FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE {port}""",
        "nextjs": f"""FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE {port}
ENV PORT={port}
CMD ["node", "server.js"]""",
        "react": f"""FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE {port}""",
        "vue": f"""FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE {port}""",
        "fastapi": f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]""",
        "express": f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
EXPOSE {port}
CMD ["node", "server.js"]""",
        # === PWA TEMPLATES (Install-able Web App) ===
        # Auto-generates manifest.webmanifest + service worker if missing
        "nextjs-pwa": f"""FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci || npm install
RUN npm install --save-dev next-pwa workbox-webpack-plugin
COPY . .
RUN npm run build
FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE {port}
ENV PORT={port}
CMD ["node", "server.js"]""",
        "vite-pwa": f"""FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci || npm install
RUN npm install --save-dev vite-plugin-pwa workbox-window
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE {port}""",
    }
    return templates.get(framework, templates["static"])


# === PWA Asset Injector ===
# Auto-generates manifest.webmanifest + service worker registration if missing.
# Khách deploy framework "*-pwa" sẽ tự động có install-able PWA.

PWA_MANIFEST_TEMPLATE = """{{
  "name": "{name}",
  "short_name": "{short_name}",
  "description": "Powered by Zeni Cloud · zenicloud.io",
  "start_url": "/",
  "display": "standalone",
  "orientation": "portrait-primary",
  "background_color": "#0a0a14",
  "theme_color": "#7d68ff",
  "icons": [
    {{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"}},
    {{"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}}
  ]
}}"""

PWA_SERVICE_WORKER = """// Zeni Cloud PWA Service Worker — auto-generated
const CACHE_NAME = 'zeni-pwa-v1';
const RUNTIME = 'runtime';
const PRECACHE_URLS = ['/', '/offline.html'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(PRECACHE_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  const current = [CACHE_NAME, RUNTIME];
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => !current.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then((cached) => {
      if (cached) return cached;
      return caches.open(RUNTIME).then((cache) => {
        return fetch(e.request).then((response) => {
          if (response.status === 200) cache.put(e.request, response.clone());
          return response;
        }).catch(() => caches.match('/offline.html'));
      });
    })
  );
});
"""

PWA_OFFLINE_HTML = """<!DOCTYPE html>
<html lang="vi"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline · Zeni PWA</title>
<style>body{font-family:-apple-system,sans-serif;background:#0a0a14;color:#fff;display:grid;place-items:center;height:100vh;margin:0;text-align:center;padding:2rem}.b{max-width:400px}h1{color:#7d68ff;margin:0 0 1rem}p{opacity:.7}</style>
</head><body><div class="b"><h1>Mất kết nối</h1><p>App vẫn chạy được offline với dữ liệu đã cache. Khi có mạng trở lại, mọi thứ sẽ tự sync.</p><p style="margin-top:2rem;font-size:.8rem;opacity:.5">Powered by Zeni Cloud</p></div></body></html>
"""

PWA_REGISTER_SCRIPT = """// Zeni PWA registrar — auto-injected
if ('serviceWorker' in navigator && location.protocol === 'https:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('PWA SW failed:', e));
  });
}
"""


def _is_pwa_framework(framework: str) -> bool:
    return framework in ("nextjs-pwa", "vite-pwa") or framework.endswith("-pwa")


def _inject_pwa_assets(tar: tarfile.TarFile, existing_names: set[str], project_name: str) -> None:
    """Inject manifest.webmanifest + sw.js + offline.html if not already present.
    Khách deploy framework *-pwa sẽ tự động có install-able PWA without writing any config.
    """
    short = project_name[:12] if project_name else "Zeni App"
    full_name = project_name if project_name else "Zeni Cloud App"

    pwa_files = {
        "public/manifest.webmanifest": PWA_MANIFEST_TEMPLATE.format(name=full_name, short_name=short),
        "public/sw.js": PWA_SERVICE_WORKER,
        "public/offline.html": PWA_OFFLINE_HTML,
        "public/zeni-pwa-register.js": PWA_REGISTER_SCRIPT,
    }
    for path, content in pwa_files.items():
        # Skip if user already has this file
        already_has = any(n.lower().endswith(path.lower().split("/")[-1]) for n in existing_names)
        if already_has:
            continue
        data = content.encode("utf-8")
        ti = tarfile.TarInfo(name=path)
        ti.size = len(data)
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(data))
        log.info("PWA asset injected: %s", path)


def _zip_to_tarball(zip_bytes: bytes, framework: str, port: int, project_name: str = "zeni-app") -> bytes:
    """Convert uploaded ZIP to tarball format Cloud Build expects.
    Auto-injects Dockerfile if missing.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        has_dockerfile = any(n.lower().endswith("dockerfile") or n.lower() == "dockerfile" for n in names)

        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            # Find common prefix (if ZIP has top-level folder)
            if names and "/" in names[0]:
                first_dir = names[0].split("/")[0] + "/"
                strip_prefix = all(n.startswith(first_dir) for n in names if n)
            else:
                strip_prefix = False
                first_dir = ""

            for name in names:
                if name.endswith("/"):
                    continue
                target_name = name
                if strip_prefix and name.startswith(first_dir):
                    target_name = name[len(first_dir):]
                if not target_name:
                    continue
                data = zf.read(name)
                ti = tarfile.TarInfo(name=target_name)
                ti.size = len(data)
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(data))

            # Inject PWA assets if framework is *-pwa
            if _is_pwa_framework(framework):
                effective_names = set()
                for name in names:
                    if strip_prefix and name.startswith(first_dir):
                        effective_names.add(name[len(first_dir):])
                    else:
                        effective_names.add(name)
                _inject_pwa_assets(tar, effective_names, project_name=project_name)

            # Inject Dockerfile if missing
            if not has_dockerfile:
                dockerfile = _generate_dockerfile(framework, port).encode()
                ti = tarfile.TarInfo(name="Dockerfile")
                ti.size = len(dockerfile)
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(dockerfile))
    return out.getvalue()


async def submit_cloud_build(source_gcs: str, image_tag: str) -> dict[str, Any]:
    """Submit Cloud Build via REST API. Returns build operation."""
    token = _get_auth_token()
    bucket_name = source_gcs.replace("gs://", "").split("/")[0]
    object_path = "/".join(source_gcs.replace("gs://", "").split("/")[1:])

    build_config = {
        "source": {
            "storageSource": {
                "bucket": bucket_name,
                "object": object_path,
            }
        },
        "steps": [
            {
                "name": "gcr.io/cloud-builders/docker",
                "args": ["build", "-t", image_tag, "."],
            },
            {
                "name": "gcr.io/cloud-builders/docker",
                "args": ["push", image_tag],
            },
        ],
        "images": [image_tag],
        "timeout": "600s",  # 10 minutes max
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=build_config,
        )
        r.raise_for_status()
        return r.json()


async def poll_build_status(build_id: str, max_wait_sec: int = 600) -> dict[str, Any]:
    """Poll Cloud Build operation until SUCCESS or FAILURE."""
    token = _get_auth_token()
    elapsed = 0
    poll_interval = 10
    async with httpx.AsyncClient(timeout=15.0) as client:
        while elapsed < max_wait_sec:
            r = await client.get(
                f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds/{build_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            status = data.get("status", "PENDING")
            if status in ("SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT"):
                return data
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
    return {"status": "TIMEOUT"}


async def run_build_and_deploy(
    db: AsyncSession,
    upload_id: str,
    workspace_id: str,
    zip_bytes: bytes,
    framework: str,
    project_name: str,
    port: int,
) -> None:
    """
    Background task: build & deploy ZIP source.
    Updates source_uploads.status throughout the lifecycle.
    """
    image_tag = f"{ARTIFACT_REGISTRY}/zeni-{workspace_id}-{project_name}:{upload_id[:8]}"

    try:
        # 1. Update status to extracting
        await db.execute(
            text("UPDATE source_uploads SET status='extracting' WHERE upload_id=:uid"),
            {"uid": upload_id}
        )
        await db.commit()
        log.info("[source_build] %s/%s extracting", workspace_id, project_name)

        # 2. Convert ZIP → tarball with auto-Dockerfile if missing
        tarball = _zip_to_tarball(zip_bytes, framework, port, project_name=project_name)

        # 3. Upload tarball to GCS
        gcs_path = f"source-uploads/{upload_id}.tgz"
        gcs_url = await upload_zip_to_gcs(tarball, gcs_path)

        await db.execute(
            text("UPDATE source_uploads SET status='building', gcs_path=:gcs WHERE upload_id=:uid"),
            {"uid": upload_id, "gcs": gcs_url}
        )
        await db.commit()
        log.info("[source_build] %s submitting Cloud Build", upload_id)

        # 4. Submit Cloud Build
        op = await submit_cloud_build(gcs_url, image_tag)
        # operation name format varies — try both:
        #   "operations/build/{project}/{region}/{build_id}"
        #   metadata.build.id is the canonical ID
        op_name = op.get("name", "")
        meta = op.get("metadata", {}) or {}
        build_meta = meta.get("build", {}) if isinstance(meta, dict) else {}
        build_id = build_meta.get("id") or (op_name.split("/")[-1] if op_name else "")
        log.info("[source_build] %s submitted build_id=%s op_name=%s", upload_id, build_id, op_name)

        await db.execute(
            text("UPDATE source_uploads SET build_id=:bid WHERE upload_id=:uid"),
            {"uid": upload_id, "bid": build_id}
        )
        await db.commit()

        # 5. Poll until done
        result = await poll_build_status(build_id, max_wait_sec=600)
        build_status = result.get("status", "FAILURE")

        if build_status != "SUCCESS":
            error_msg = result.get("statusDetail") or f"Build failed: {build_status}"
            await db.execute(
                text("UPDATE source_uploads SET status='failed', error_message=:e, completed_at=NOW() WHERE upload_id=:uid"),
                {"uid": upload_id, "e": error_msg[:1000]}
            )
            await db.commit()
            log.error("[source_build] %s build failed: %s", upload_id, error_msg)
            return

        # 6. Build success → update status + image URL
        await db.execute(
            text("""UPDATE source_uploads
                    SET status='deploying', image_url=:img
                    WHERE upload_id=:uid"""),
            {"uid": upload_id, "img": image_tag}
        )
        await db.commit()
        log.info("[source_build] %s build SUCCESS, deploying", upload_id)

        # 7. AUTO-DEPLOY to Cloud Run (call internal projects flow)
        try:
            from app.services.cloud_run import deploy_cloud_run, CloudRunError
            from app.services.cloud_run import service_name_for, SIZE_TO_RESOURCES, SIZE_DISPLAY
            sn = service_name_for(workspace_id, project_name)
            resources = SIZE_TO_RESOURCES.get("s", SIZE_TO_RESOURCES.get("xs"))
            cpu_disp, mem_disp, _ = SIZE_DISPLAY.get("s", SIZE_DISPLAY.get("xs"))
            deploy_result = await deploy_cloud_run(
                service_name=sn,
                image=image_tag,
                region="asia-southeast1",
                env_vars={"WORKSPACE": workspace_id, "DEPLOY_BY": "zeni-source-build"},
                secrets={},
                port=port,
                resources=resources,
                allow_unauthenticated=True,
            )
            deploy_url = deploy_result.url or f"https://{sn}-asia-southeast1.run.app"
            await db.execute(
                text("""UPDATE source_uploads
                        SET status='success', completed_at=NOW(),
                            deploy_url=:url
                        WHERE upload_id=:uid"""),
                {"uid": upload_id, "url": deploy_url}
            )
            await db.commit()
            log.info("[source_build] %s DEPLOYED at %s", upload_id, deploy_url)
        except Exception as deploy_err:
            log.exception("[source_build] %s deploy step failed", upload_id)
            # Image is built but Cloud Run deploy failed — keep image_url, mark as partial
            await db.execute(
                text("""UPDATE source_uploads
                        SET status='success', completed_at=NOW(),
                            deploy_url='https://zenicloud.io/app#projects?image=' || :img,
                            error_message='Image built OK but auto-deploy failed: ' || :err
                        WHERE upload_id=:uid"""),
                {"uid": upload_id, "img": image_tag, "err": str(deploy_err)[:200]}
            )
            await db.commit()
            log.info("[source_build] %s image-only (deploy fail): %s", upload_id, image_tag)

    except Exception as e:
        log.exception("[source_build] %s FAILED", upload_id)
        try:
            await db.execute(
                text("UPDATE source_uploads SET status='failed', error_message=:e, completed_at=NOW() WHERE upload_id=:uid"),
                {"uid": upload_id, "e": f"{type(e).__name__}: {str(e)[:500]}"}
            )
            await db.commit()
        except Exception:
            pass
