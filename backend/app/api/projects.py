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
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
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
            detail="Image phải từ Artifact Registry zeni-cloud-core, Docker Hub library, hoặc Google samples.",
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

    _validate_image(data.image)
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
    project_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    await require_workspace_access(ws, me)
    p = (await db.execute(select(Project).where(Project.id == project_id, Project.workspace_id == ws))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")

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
    project_id: UUID,
    data: DomainMappingIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Map custom domain (app.nexdesign.vn) → Cloud Run service of this project."""
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin để map domain")

    p = (await db.execute(select(Project).where(Project.id == project_id, Project.workspace_id == ws))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
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
        metadata={"project_id": str(project_id), "domain": data.domain},
    )
    await db.commit()
    return result


@router.get("/{project_id}/domains")
async def list_project_domains(
    ws: str,
    project_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    p = (await db.execute(select(Project).where(Project.id == project_id, Project.workspace_id == ws))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not p.cloud_run_service:
        return {"domains": []}
    from app.services import domain_mapping as dm
    return {
        "project_id": str(project_id),
        "service_name": p.cloud_run_service,
        "domains": dm.list_mapped_domains(p.cloud_run_service, p.region or "us-central1"),
    }


@router.delete("/{project_id}/domain/{domain}")
async def remove_domain_mapping(
    ws: str,
    project_id: UUID,
    domain: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin")
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


@router.delete("/{project_id}", status_code=204, response_class=Response)
async def delete_project(
    ws: str,
    project_id: UUID,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên")
    p = (await db.execute(select(Project).where(Project.id == project_id, Project.workspace_id == ws))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")

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
