"""
Zeni Cloud Core — L1 Compute API (REAL Cloud Run deploy, async).

Deploys each project as a Cloud Run service. Cloud Run create/update can take
30-60s, exceeding GCP Load Balancer's 30s default timeout for serverless NEG.
We therefore use the async pattern:
  POST /projects        → return 202 immediately (DB row, status='deploying')
  background task       → call Cloud Run API, update DB row when done
  GET  /projects/{id}   → poll status (auto-syncs from Cloud Run on each call)

This avoids 30s LB cap and gives the client a clean polling UX.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.db.models import Project
from app.schemas.resources import ProjectCreateIn, ProjectOut
from app.services.audit import audit_push, billing_push
from app.services.cloud_run import (
    CloudRunError,
    SIZE_TO_RESOURCES,
    delete_service,
    deploy_service,
    get_service_status,
    service_name_for,
)

log = logging.getLogger("zeni.api.projects")
router = APIRouter(prefix="/projects", tags=["projects"])


SIZE_DISPLAY = {
    "xs": ("1 vCPU", "512MB", 0.0001),
    "s":  ("1 vCPU", "1GB",   0.0002),
    "m":  ("2 vCPU", "2GB",   0.0004),
    "l":  ("4 vCPU", "4GB",   0.0008),
}

ALLOWED_IMAGE_PREFIXES = (
    "us-central1-docker.pkg.dev/zeni-cloud-core/",
    "asia-southeast1-docker.pkg.dev/zeni-cloud-core/",
    "gcr.io/zeni-cloud-core/",
    "gcr.io/google-samples/",
    "gcr.io/google-containers/",
    "us-docker.pkg.dev/cloudrun/container/",
    "docker.io/library/",
)

import re
_VALID_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9\-\.\/:@_]{2,511}$")
MAX_PROJECTS_PER_WS = 50


def _validate_image(image: str) -> None:
    if not _VALID_IMAGE_RE.match(image):
        raise HTTPException(status_code=400, detail="Image URL không hợp lệ")
    normalized = image if "/" in image.split(":")[0] else f"docker.io/library/{image}"
    if not any(normalized.startswith(p) for p in ALLOWED_IMAGE_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=(
                "Image không nằm trong whitelist global. Allowed: Artifact Registry zeni-cloud-core, "
                "Docker Hub library, Google samples. "
                "Để dùng registry khác (vd: ghcr.io/myorg/, registry.gitlab.com/myorg/) → "
                "vào Workspace Settings → Image Registries → Add prefix."
            ),
        )


async def _validate_image_with_workspace(db, workspace_id: str, image: str) -> None:
    """Validate image: global whitelist OR per-workspace opt-in whitelist.

    Pattern Vercel/Netlify: workspace owner self-service add registry prefix
    (vd: ghcr.io/vietcontech/) qua Workspace Settings → Image Registries.
    Tránh phải add global cho mọi khách (security + scale).
    """
    if not _VALID_IMAGE_RE.match(image):
        raise HTTPException(status_code=400, detail="Image URL không hợp lệ")
    normalized = image if "/" in image.split(":")[0] else f"docker.io/library/{image}"

    # Pass 1: Global whitelist
    if any(normalized.startswith(p) for p in ALLOWED_IMAGE_PREFIXES):
        return

    # Pass 2: Per-workspace whitelist
    try:
        from sqlalchemy import text as _text
        rows = (await db.execute(_text(
            "SELECT prefix FROM workspace_image_whitelist "
            "WHERE workspace_id = :ws AND enabled = TRUE"
        ), {"ws": workspace_id})).mappings().all()
        ws_prefixes = [r["prefix"] for r in rows]
        if any(normalized.startswith(p) or image.startswith(p) for p in ws_prefixes):
            return
    except Exception as e:
        log.warning("[validate_image] workspace whitelist check failed: %s", e)

    # Reject with helpful hint
    raise HTTPException(
        status_code=400,
        detail=(
            f"Image '{image}' không trong whitelist. Cách thêm:\n"
            "  • Owner workspace vào Settings → Image Registries → Add prefix\n"
            "  • Vd: prefix = 'ghcr.io/vietcontech/' để allow ghcr.io/vietcontech/*\n"
            "  • Hoặc dùng image global allowed: docker.io/library/nginx:alpine"
        ),
    )


async def _validate_image_exists(image: str) -> None:
    """Pre-flight: verify image actually exists in registry. Raises 422 with clear hint
    if not found. Skips Docker Hub (let Cloud Run handle it).

    Why: customer submits gcr.io/.../my-app:v1 that they never built/pushed → Cloud Run
    rejects async → user sees 'deploying' forever → thinks it's a mock. Pre-flight
    catches this synchronously with a clear remediation hint.
    """
    if not (image.startswith("gcr.io/") or "-docker.pkg.dev/" in image):
        return  # Docker Hub / public — skip check

    import httpx
    try:
        from google.auth import default as _gauth_default
        from google.auth.transport.requests import Request as _GAR
    except Exception:
        return  # google-auth unavailable → soft-skip

    img_path, _, tag = image.rpartition(":")
    if not img_path:
        img_path, tag = image, "latest"
    parts = img_path.split("/", 1)
    if len(parts) < 2:
        return
    registry, repo = parts[0], parts[1]
    manifest_url = f"https://{registry}/v2/{repo}/manifests/{tag}"

    try:
        creds, _proj = _gauth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(_GAR())
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.head(
                manifest_url,
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "Accept": "application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json",
                },
            )
            if r.status_code == 404:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Image '{image}' không tồn tại trong registry. "
                        "Bạn cần build + push image trước. Cách:\n"
                        "  • Upload source code: POST /api/v1/upload/source (Zeni tự build)\n"
                        "  • Hoặc dùng Build Farm: POST /api/v1/build-farm/jobs\n"
                        "  • Hoặc dùng image public Docker Hub: docker.io/library/nginx:alpine"
                    ),
                )
    except HTTPException:
        raise
    except Exception as e:
        # Validation infra error — don't block deploy, let Cloud Run try
        log.warning("[_validate_image_exists] soft-fail for %s: %s", image, e)


# ─── Helper: resolve project by UUID OR name ────────────────────
# Pattern: customers thường pass project NAME (vd "upload-dzxtyc-z") thay vì
# UUID — endpoint phải accept cả 2 để UX tốt. Trước đây path param `UUID`
# strict reject 422 → khách bị block không add domain được.
async def _resolve_project(db, ws: str, project_id_or_name: str):
    """Return Project ORM by UUID or name. Raises 404 if not found."""
    from sqlalchemy import or_
    # Try UUID first (more specific)
    try:
        uid = UUID(project_id_or_name)
        p = (await db.execute(
            select(Project).where(Project.id == uid, Project.workspace_id == ws)
        )).scalar_one_or_none()
        if p is not None:
            return p
    except (ValueError, AttributeError):
        pass
    # Fallback to name lookup
    p = (await db.execute(
        select(Project).where(Project.name == project_id_or_name, Project.workspace_id == ws)
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail=f"project '{project_id_or_name}' not found in workspace '{ws}'")
    return p


# ─── Background deploy task ─────────────────────────────────────
async def _bg_deploy(
    project_id: UUID, ws: str, name: str, image: str, size: str, region: str,
    env_vars: dict | None, secrets: dict | None, port: int, allow_unauth: bool,
    actor_email: str, action: str, unit_cost: float, resources: dict,
    cpu_display: str, mem_display: str, git_ref: str,
) -> None:
    """Run Cloud Run deploy in background, update DB row when done."""
    async with SessionLocal() as db:
        try:
            result = await deploy_service(
                workspace=ws, project_name=name, image=image, size=size, region=region,
                env_vars=env_vars, secrets=secrets, port=port,
                allow_unauthenticated=allow_unauth, created_by=actor_email,
            )

            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if project is None:
                log.error("[bg_deploy] project %s not found in DB", project_id)
                return

            project.status = "running"
            project.region = result.region
            project.domain = result.url
            project.cloud_run_service = result.service_name
            project.current_revision = result.revision
            project.version = (f"rev-{result.revision}"[:48]) if result.revision else "rev-unknown"
            project.last_deploy = datetime.now(timezone.utc)
            project.instances = resources["max"]
            project.cpu = cpu_display
            project.memory = mem_display

            await audit_push(
                db, actor=actor_email, workspace_id=ws, action=action, target=name, severity="ok",
                metadata={"image": image, "size": size, "region": result.region,
                          "cloud_run_service": result.service_name, "url": result.url or ""},
            )
            await billing_push(db, workspace_id=ws, layer="L1", action=action, cost_usd=unit_cost)
            await db.commit()
            log.info("[bg_deploy] %s/%s OK → %s", ws, name, result.url)

        except CloudRunError as e:
            log.exception("[bg_deploy] %s/%s failed: %s", ws, name, e)
            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if project is not None:
                project.status = "failed"
                await audit_push(
                    db, actor=actor_email, workspace_id=ws, action=f"{action}.failed",
                    target=name, severity="err", metadata={"error": str(e)},
                )
                await db.commit()
        except Exception as e:
            log.exception("[bg_deploy] unexpected error for %s/%s", ws, name)
            project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if project is not None:
                project.status = "failed"
                await audit_push(
                    db, actor=actor_email, workspace_id=ws, action=f"{action}.failed",
                    target=name, severity="err", metadata={"error": f"{type(e).__name__}: {e}"},
                )
                await db.commit()


# ─── HTTP routes ────────────────────────────────────────────────
@router.get("", response_model=list[ProjectOut])
async def list_projects(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Project).where(Project.workspace_id == ws).order_by(Project.created_at.desc())
    )).scalars().all()
    return [ProjectOut.model_validate(r) for r in rows]


@router.post("", response_model=ProjectOut, status_code=202)
async def deploy_project(
    ws: str,
    data: ProjectCreateIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """Async deploy: returns 202 immediately, deploy runs in background."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không thể deploy")

    # Validate image: global whitelist OR per-workspace opt-in (Vercel pattern)
    await _validate_image_with_workspace(db, ws, data.image)
    # Pre-flight: verify image actually exists in registry — gives 422 fast feedback
    # instead of async 'deploying' status that silently fails when image not pushed yet.
    await _validate_image_exists(data.image)

    ws_count = (await db.execute(select(Project).where(Project.workspace_id == ws))).all()
    if len(ws_count) >= MAX_PROJECTS_PER_WS:
        raise HTTPException(status_code=429, detail=f"Workspace vượt giới hạn {MAX_PROJECTS_PER_WS} projects")

    existing = (await db.execute(
        select(Project).where(Project.workspace_id == ws, Project.name == data.name)
    )).scalar_one_or_none()

    cpu_display, mem_display, unit_cost = SIZE_DISPLAY[data.size]
    resources = SIZE_TO_RESOURCES[data.size]
    action = "compute.redeploy" if existing else "compute.deploy"

    if existing:
        existing.image = data.image
        existing.type = data.type
        existing.runtime = data.runtime
        existing.size = data.size
        existing.region = data.region
        existing.status = "deploying"
        existing.cpu = cpu_display
        existing.memory = mem_display
        existing.git_ref = data.git_ref or "main"
        project = existing
    else:
        project = Project(
            workspace_id=ws,
            name=data.name,
            type=data.type,
            runtime=data.runtime,
            size=data.size,
            region=data.region,
            status="deploying",
            instances=resources["max"],
            cpu=cpu_display,
            memory=mem_display,
            domain=None,
            last_deploy=None,
            version="rev-pending",
            git_ref=data.git_ref or "main",
            image=data.image,
            cloud_run_service=service_name_for(ws, data.name),
            current_revision=None,
            created_by=me.id,
        )
        db.add(project)

    await db.flush()
    project_id = project.id
    await audit_push(
        db, actor=me.email, workspace_id=ws, action=f"{action}.requested",
        target=data.name, severity="info",
        metadata={"image": data.image, "size": data.size, "region": data.region},
    )
    await db.commit()
    await db.refresh(project)

    # Schedule background deploy
    bg.add_task(
        _bg_deploy,
        project_id=project_id, ws=ws, name=data.name, image=data.image, size=data.size,
        region=data.region, env_vars=data.env_vars, secrets=data.secrets, port=data.port,
        allow_unauth=data.allow_unauthenticated, actor_email=me.email, action=action,
        unit_cost=unit_cost, resources=resources, cpu_display=cpu_display,
        mem_display=mem_display, git_ref=data.git_ref or "main",
    )

    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    ws: str,
    project_id: str,                       # accept UUID or name (resolved by helper)
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)

    # Fast-return if still deploying / failed (DB is source of truth)
    if p.status in ("deploying", "failed"):
        return ProjectOut.model_validate(p)

    # For 'running' state, sync live from Cloud Run (handles upstream deletion)
    try:
        live = await get_service_status(workspace=ws, project_name=p.name, region=p.region)
        if live.get("exists"):
            updated = False
            if live.get("state") and p.status != live["state"]:
                p.status = live["state"]; updated = True
            if live.get("url") and p.domain != live["url"]:
                p.domain = live["url"]; updated = True
            if live.get("revision") and p.current_revision != live["revision"]:
                p.current_revision = live["revision"]
                p.version = (f"rev-{live['revision']}")[:48]
                updated = True
            if updated:
                await db.commit(); await db.refresh(p)
        else:
            if p.status != "missing":
                p.status = "missing"
                await db.commit(); await db.refresh(p)
    except CloudRunError as e:
        log.warning("Cloud Run status check failed for %s/%s: %s", ws, p.name, e)

    return ProjectOut.model_validate(p)


# ─── Custom domain mapping (Stream A3) ──────────────────
class DomainMappingIn(BaseModel):
    domain: str = Field(min_length=4, max_length=253,
                        description="VD: app.nexdesign.vn")


@router.post("/{project_id}/domain")
async def add_domain_mapping(
    ws: str,
    project_id: str,                        # accept UUID or name
    data: DomainMappingIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Map custom domain (app.nexdesign.vn) → Cloud Run service of this project."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin để map domain")

    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        raise HTTPException(status_code=400, detail="Project chưa deployed lên Cloud Run")

    from app.services import domain_mapping as dm
    if not dm.is_valid_domain(data.domain):
        raise HTTPException(status_code=400, detail="Domain không hợp lệ")

    # NEW: domain_mapping service tự handle fallback (returns dict with state=MANUAL_SETUP_REQUIRED nếu API fail)
    # Không raise 502 nữa — trả 200 với instructions cho khách
    try:
        result = dm.create_domain_mapping(
            domain=data.domain,
            service_name=p.cloud_run_service,
            region=p.region or "asia-southeast1",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Fallback returns dict with manual instructions
        result = {
            "domain": data.domain,
            "service": p.cloud_run_service,
            "state": "MANUAL_SETUP_REQUIRED",
            "error": str(e)[:200],
            "dns_records_to_add": [
                {"type": "CNAME", "name": data.domain, "value": "ghs.googlehosted.com."},
            ],
            "instructions": "Em đề xuất Cloudflare proxy free: cloudflare.com → add site → đổi NS records → bật proxy orange cloud (free Universal SSL + WAF). Hoặc liên hệ support@zenicloud.io.",
        }

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="compute.domain_map",
        target=f"{p.name} → {data.domain}", severity="info",
        metadata={"project_id": str(p.id), "project_name": p.name, "domain": data.domain},
    )
    await db.commit()
    return result


@router.get("/{project_id}/domains")
async def list_project_domains(
    ws: str,
    project_id: str,                        # accept UUID or name
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        return {"domains": []}
    from app.services import domain_mapping as dm
    return {
        "project_id": str(p.id),
        "project_name": p.name,
        "service_name": p.cloud_run_service,
        "domains": dm.list_mapped_domains(p.cloud_run_service, p.region or "us-central1"),
    }


# v169 — Poll endpoint for cert provisioning status
# Customer hits this every ~30s after add DNS, checks state until "LIVE".
@router.get("/{project_id}/domain/{domain}/status")
async def get_domain_status(
    ws: str,
    project_id: str,
    domain: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get DNS + SSL cert status for a mapped domain.

    States:
      - PENDING_DNS: DNS chưa point về Zeni LB IP
      - PROVISIONING_SSL: DNS đã point đúng, Google đang cấp SSL
      - LIVE: Cert ACTIVE + DNS đúng → domain serve HTTPS 200
    """
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    from app.services import domain_mapping as dm
    if not dm.is_valid_domain(domain):
        raise HTTPException(status_code=400, detail="Domain không hợp lệ")
    status = dm.get_domain_status(domain)
    status["project_id"] = str(p.id)
    status["project_name"] = p.name
    return status


@router.delete("/{project_id}/domain/{domain}")
async def remove_domain_mapping(
    ws: str,
    project_id: str,                        # accept UUID or name
    domain: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin")
    # Resolve project (best-effort — for audit + ownership check; if not found, log warning)
    try:
        p = await _resolve_project(db, ws, project_id)
    except HTTPException:
        p = None  # legacy: domain may exist beyond our project record
    from app.services import domain_mapping as dm
    try:
        dm.delete_domain_mapping(domain)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="compute.domain_unmap",
        target=domain, severity="warn",
    )
    await db.commit()
    return {"ok": True, "domain": domain, "removed": True}


# ═══════════════════════════════════════════════════════════════
# v151 — CRITICAL endpoints cho WitsAGI deploy full stack
# ═══════════════════════════════════════════════════════════════

class EnvVarsIn(BaseModel):
    env: dict[str, str] = Field(..., description='{"KEY": "value", ...} — set env vars cho Cloud Run')


@router.post("/{project_id}/env")
async def set_project_env(
    ws: str,
    project_id: str,                            # accept UUID or name
    body: EnvVarsIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set/update environment variables cho Cloud Run service.

    Khách dùng để config production env vars (DATABASE_URL, API_KEY, ...)
    KHÔNG cần re-upload ZIP với .env.

    v151 — chairman CRITICAL item #4.
    """
    await require_workspace_access(ws, me)
    if me.role in ("Viewer",):
        raise HTTPException(status_code=403, detail="Cần Developer+ để set env")
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        raise HTTPException(status_code=400, detail="Project chưa deploy lên Cloud Run")

    # Validate env keys (no spaces, alphanumeric + underscore)
    for k in body.env.keys():
        if not k.replace("_", "").isalnum() or " " in k:
            raise HTTPException(status_code=400, detail=f"Env key '{k}' không hợp lệ — chỉ alphanumeric + underscore")
    # Limit total env size
    total_size = sum(len(k) + len(v) for k, v in body.env.items())
    if total_size > 32 * 1024:
        raise HTTPException(status_code=413, detail=f"Env vars total size {total_size} bytes > 32KB limit")

    try:
        # Use google-cloud-run SDK (gcloud CLI not available trong Cloud Run container)
        from google.cloud import run_v2
        region = p.region or "asia-southeast1"
        gcp_project = os.environ.get('GCP_PROJECT_ID', 'zeni-cloud-core')
        client = run_v2.ServicesClient()
        full_name = f"projects/{gcp_project}/locations/{region}/services/{p.cloud_run_service}"

        # 1. GET current service
        service = client.get_service(name=full_name)

        # 2. Merge new env vars with existing (UpdateMask only env)
        existing_env = {e.name: e.value for e in service.template.containers[0].env if e.value}
        existing_env.update(body.env)

        # Rebuild env list
        service.template.containers[0].env[:] = [
            run_v2.EnvVar(name=k, value=v) for k, v in existing_env.items()
        ]

        # 3. Update service
        operation = client.update_service(request={"service": service})
        # Don't wait for full operation — fire and forget (returns quickly)
    except Exception as e:
        log.exception("[set_env] SDK failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Cloud Run env update failed: {str(e)[:200]}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="compute.set_env",
        target=p.name, severity="info",
        metadata={"project_id": str(p.id), "env_keys": list(body.env.keys())},
    )
    await db.commit()
    return {
        "project_id": str(p.id),
        "project_name": p.name,
        "service_name": p.cloud_run_service,
        "env_keys_set": list(body.env.keys()),
        "count": len(body.env),
        "note": "Cloud Run sẽ create revision mới với env vars + serve traffic mới (~30s).",
    }


@router.get("/{project_id}/env")
async def get_project_env(
    ws: str,
    project_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List env var KEYS của project (KHÔNG return value — security)."""
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        return {"env_keys": []}
    try:
        from google.cloud import run_v2
        region = p.region or "asia-southeast1"
        gcp_project = os.environ.get('GCP_PROJECT_ID', 'zeni-cloud-core')
        client = run_v2.ServicesClient()
        full_name = f"projects/{gcp_project}/locations/{region}/services/{p.cloud_run_service}"
        service = client.get_service(name=full_name)
        env_keys = [e.name for e in service.template.containers[0].env]
        return {"project_id": str(p.id), "env_keys": env_keys, "count": len(env_keys)}
    except Exception as e:
        return {"project_id": str(p.id), "env_keys": [], "error": str(e)[:200]}


@router.get("/{project_id}/logs")
async def get_project_logs(
    ws: str,
    project_id: str,
    last: int = Query(200, ge=10, le=2000, description="Số dòng log cuối (max 2000)"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get Cloud Run runtime logs cho project.

    Use case: debug khi production /api/auth fail, xem error stack.
    v151 — chairman HIGH item #6.
    """
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        return {"project_id": str(p.id), "logs": "", "note": "Project chưa deploy"}
    try:
        # Use Cloud Logging REST API (no extra dep needed)
        import httpx
        from google.auth import default as google_auth_default
        from google.auth.transport.requests import Request as GoogleAuthRequest
        creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/logging.read"])
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        region = p.region or "asia-southeast1"
        gcp_project = os.environ.get('GCP_PROJECT_ID', 'zeni-cloud-core')
        filter_str = (
            f'resource.type="cloud_run_revision" AND '
            f'resource.labels.service_name="{p.cloud_run_service}" AND '
            f'resource.labels.location="{region}"'
        )
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            r = await http_client.post(
                "https://logging.googleapis.com/v2/entries:list",
                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                json={
                    "resourceNames": [f"projects/{gcp_project}"],
                    "filter": filter_str,
                    "orderBy": "timestamp desc",
                    "pageSize": last,
                },
            )
            if r.status_code != 200:
                return {"project_id": str(p.id), "logs": "", "error": f"Logging API {r.status_code}: {r.text[:200]}"}
            data = r.json()
            entries = data.get("entries", [])
            lines = []
            for e in entries:
                ts = e.get("timestamp", "")[:19]
                sev = e.get("severity", "INFO")
                text_payload = e.get("textPayload") or str(e.get("jsonPayload", ""))[:500]
                lines.append(f"{ts} {sev:8} {text_payload}")
            logs_text = "\n".join(reversed(lines))  # chronological
            return {
                "project_id": str(p.id),
                "service_name": p.cloud_run_service,
                "region": region,
                "lines_returned": len(lines),
                "logs": logs_text[:200_000],
            }
    except Exception as e:
        return {"project_id": str(p.id), "logs": "", "error": str(e)[:300]}


# Phase 2 P2.2 — List revisions + Rollback (additive)
@router.get("/{project_id}/revisions")
async def list_project_revisions(
    ws: str,
    project_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List Cloud Run revisions của project (most recent first).

    Used by Deployments UI tab — Vercel pattern. Mỗi revision có traffic_percent,
    image, created_at để khách chọn rollback.
    """
    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        return {"project_id": str(p.id), "revisions": []}
    from app.services.rollback import list_revisions
    revisions = list_revisions(p.cloud_run_service, p.region or "asia-southeast1")
    return {
        "project_id": str(p.id),
        "service_name": p.cloud_run_service,
        "region": p.region or "asia-southeast1",
        "revisions": revisions,
        "count": len(revisions),
    }


@router.post("/{project_id}/rollback")
async def rollback_project(
    ws: str,
    project_id: str,
    target_revision: str = Query(..., description="Revision name to rollback to"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rollback 1-click — flip 100% traffic về target_revision (Vercel pattern).

    Yêu cầu Admin role trở lên (Viewer/Developer không được rollback prod).
    """
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin để rollback")
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        raise HTTPException(404, "Project chưa deploy lên Cloud Run")

    from app.services.rollback import rollback_to_revision
    result = rollback_to_revision(
        service_name=p.cloud_run_service,
        region=p.region or "asia-southeast1",
        target_revision=target_revision,
        tag=f"rollback-by-{me.email.split('@')[0]}"[:50],
    )

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="compute.rollback",
        target=f"{p.name} → {target_revision}", severity="warning",
        metadata={"project_id": str(p.id), "result": result},
    )
    await db.commit()
    return result


# Phase 1 P1.3 — Realtime SSE log streaming (additive, không đụng /logs cũ)
# v170 chairman approved 2026-05-11
@router.get("/{project_id}/logs/stream")
async def stream_project_logs(
    ws: str,
    project_id: str,
    severity: str = Query("DEFAULT", description="Filter: DEFAULT, INFO, WARNING, ERROR"),
    max_duration_s: int = Query(600, ge=30, le=3600, description="Max stream duration"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Realtime SSE log stream cho Cloud Run service.

    Browser/curl mở connection → nhận log entries dạng SSE.
    Mỗi log: `event: log\ndata: {json}\n\n`
    Heartbeat: `:ping\n\n` mỗi 15s.
    Client tự reconnect khi connection drop hoặc max_duration_s.

    Use case: deploy progress UI, debug realtime, monitor live.
    """
    from fastapi.responses import StreamingResponse
    from app.services.log_streaming import stream_cloud_run_logs

    await require_workspace_access(ws, me)
    p = await _resolve_project(db, ws, project_id)
    if not p.cloud_run_service:
        raise HTTPException(404, "Project chưa deploy lên Cloud Run")

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        stream_cloud_run_logs(
            cloud_run_service=p.cloud_run_service,
            region=p.region or "asia-southeast1",
            severity_filter=severity,
            max_duration_s=max_duration_s,
        ),
        media_type="text/event-stream",
        headers=headers,
    )


@router.delete("/{project_id}", status_code=204, response_class=Response)
async def delete_project(
    ws: str,
    project_id: str,                        # accept UUID or name
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    p = await _resolve_project(db, ws, project_id)

    # Capture data needed for background delete before db row removal
    workspace_id = p.workspace_id
    project_name = p.name
    region = p.region or "us-central1"
    actor_email = me.email
    cloud_run_name = p.cloud_run_service or service_name_for(ws, p.name)

    await db.delete(p)
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="compute.delete",
        target=project_name, severity="warn",
        metadata={"cloud_run_service": cloud_run_name},
    )
    await db.commit()

    # Background: actually delete Cloud Run service
    bg.add_task(_bg_delete, workspace_id=workspace_id, project_name=project_name,
                region=region, actor_email=actor_email)

    return Response(status_code=204)


async def _bg_delete(workspace_id: str, project_name: str, region: str, actor_email: str) -> None:
    try:
        await delete_service(workspace=workspace_id, project_name=project_name, region=region)
        log.info("[bg_delete] %s/%s deleted", workspace_id, project_name)
    except CloudRunError as e:
        log.exception("[bg_delete] %s/%s failed: %s", workspace_id, project_name, e)
