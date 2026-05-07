"""
Zeni Cloud Core — Quick Deploy API.

ZERO-CONFIG deploy — designed for AI agents (Claude, ChatGPT, Cursor, Replit Agents).

Single endpoint: POST /api/v1/deploy/quick → trả live URL trong 60-90 giây.

3 cách input (chọn 1):
  1. repo_url    — GitHub public repo URL (https://github.com/owner/repo)
  2. zip_base64  — Base64-encoded ZIP source (max 50MB encoded)
  3. image       — Pre-built Docker image URL (Docker Hub/Artifact Registry/GCR samples)

Tất cả AUTO-DETECT framework + AUTO-GENERATE Dockerfile + AUTO-DEPLOY.

Example AI agent usage:
  curl -X POST https://zenicloud.io/api/v1/deploy/quick?ws=$WS \\
    -H "Authorization: Bearer $ZENI_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{"repo_url":"https://github.com/owner/repo","name":"my-app"}'

  → {"deploy_id":"...", "status":"queued", "url":"https://...run.app", "poll_url":"..."}
"""
from __future__ import annotations

import base64
import re
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.services.source_build import run_build_and_deploy

router = APIRouter(prefix="/deploy", tags=["quick-deploy"])


class QuickDeployIn(BaseModel):
    # ONE of these required
    repo_url: str | None = Field(default=None, max_length=300, pattern=r"^https://github\.com/[\w.-]+/[\w.-]+/?$")
    zip_base64: str | None = Field(default=None, max_length=70_000_000, description="Base64-encoded ZIP source")
    image: str | None = Field(default=None, max_length=512, description="Pre-built Docker image URL")
    # Common
    name: str | None = Field(default=None, max_length=48, description="App name (auto-generated if missing)")
    framework: str = Field(default="auto", pattern=r"^(auto|nextjs|react|vue|static|fastapi|express)$")
    branch: str = Field(default="main", max_length=64)
    region: str = Field(default="asia-southeast1", max_length=32)
    size: str = Field(default="s", pattern=r"^(xs|s|m|l)$")
    port: int = Field(default=8080, ge=1, le=65535)
    env_vars: dict[str, str] | None = Field(default=None)


async def _bg_zip_build(upload_id: str, ws: str, zip_bytes: bytes, framework: str, name: str, port: int):
    """Background wrapper — own DB session."""
    async with SessionLocal() as db:
        await run_build_and_deploy(db, upload_id, ws, zip_bytes, framework, name, port)


@router.post("/quick", status_code=202)
async def quick_deploy(
    data: QuickDeployIn,
    bg: BackgroundTasks,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    🚀 ONE-CALL deploy. AI-agent friendly.

    Pick ONE input method:
      - repo_url:   public GitHub repo (Phase 1: only public; Phase 2 after OAuth: private too)
      - zip_base64: base64 ZIP source code
      - image:      pre-built Docker image URL

    Returns immediately with deploy_id + poll_url. Use poll_url to track progress.
    """
    await require_workspace_access(ws, me)

    # Validate exactly ONE input method
    inputs = [bool(data.repo_url), bool(data.zip_base64), bool(data.image)]
    if sum(inputs) != 1:
        raise HTTPException(
            status_code=400,
            detail="Phải chọn ĐÚNG 1 trong 3: repo_url, zip_base64, image"
        )

    # Auto-generate project name if missing
    if not data.name:
        if data.repo_url:
            base = data.repo_url.rstrip("/").split("/")[-1].replace(".git", "")
            data.name = f"qd-{base}"[:32]
        elif data.image:
            base = data.image.split("/")[-1].split(":")[0]
            data.name = f"qd-{base}"[:32]
        else:
            data.name = f"qd-{secrets.token_hex(3)}"

    # Sanitize name: Cloud Run only allows [a-z0-9-]
    safe_name = re.sub(r"[^a-z0-9-]", "-", data.name.lower()).strip("-")[:48]
    if len(safe_name) < 3:
        safe_name = f"app-{secrets.token_hex(3)}"

    deploy_id = secrets.token_urlsafe(12)

    # ─── Path 1: Pre-built Docker image → deploy directly via /projects ───
    if data.image:
        # Direct deploy via existing project create flow
        from app.api.projects import deploy_project
        from app.schemas.resources import ProjectCreateIn
        proj_data = ProjectCreateIn(
            name=safe_name,
            type="api",
            runtime="container",
            size=data.size,
            region=data.region,
            image=data.image,
            port=data.port,
            env_vars=data.env_vars,
            allow_unauthenticated=True,
        )
        try:
            project = await deploy_project(ws=ws, data=proj_data, bg=bg, me=me, db=db)
            return {
                "deploy_id": deploy_id,
                "method": "docker_image",
                "image": data.image,
                "name": safe_name,
                "status": "deploying",
                "project_id": str(project.id),
                "poll_url": f"https://zenicloud.io/api/v1/projects/{project.id}?ws={ws}",
                "estimated_live_in_sec": 30,
                "message": "Docker image đã được submit. Poll project status để lấy URL live."
            }
        except HTTPException as e:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Deploy failed: {type(e).__name__}: {e}")

    # ─── Path 2: ZIP base64 → extract → build & deploy ───
    if data.zip_base64:
        try:
            zip_bytes = base64.b64decode(data.zip_base64)
        except Exception:
            raise HTTPException(status_code=400, detail="zip_base64 không decode được")
        if len(zip_bytes) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="ZIP quá lớn (>50MB sau decode)")
        if len(zip_bytes) < 100:
            raise HTTPException(status_code=400, detail="ZIP quá nhỏ")

        # Insert source_uploads row
        await db.execute(
            text("""INSERT INTO source_uploads
                    (upload_id, workspace_id, file_size_bytes, file_count,
                     framework, detected_framework, project_name, status, uploaded_by)
                    VALUES (:uid, :ws, :sz, 0, :fw, :fw, :pn, 'queued', :u)"""),
            {"uid": deploy_id, "ws": ws, "sz": len(zip_bytes),
             "fw": data.framework, "pn": safe_name, "u": me.email}
        )
        await db.commit()

        bg.add_task(_bg_zip_build, deploy_id, ws, zip_bytes, data.framework, safe_name, data.port)

        return {
            "deploy_id": deploy_id,
            "method": "zip_upload",
            "name": safe_name,
            "framework": data.framework,
            "size_bytes": len(zip_bytes),
            "status": "queued",
            "poll_url": f"https://zenicloud.io/api/v1/upload/source/{deploy_id}?ws={ws}",
            "estimated_live_in_sec": 90,
            "message": "ZIP đã queue. Worker sẽ build & deploy. Poll URL để track."
        }

    # ─── Path 3: GitHub public repo → clone → build & deploy ───
    if data.repo_url:
        # Phase 1 stub: For now, store as github_connection + queue manual webhook setup
        # Phase 2: Will auto-clone + build + deploy
        m = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", data.repo_url)
        if not m:
            raise HTTPException(status_code=400, detail="repo_url phải là https://github.com/owner/repo")
        owner, repo = m.group(1), m.group(2)

        # Insert as github_deploys queued + connection if needed
        webhook_secret = secrets.token_urlsafe(32)
        try:
            conn_row = (await db.execute(
                text("""INSERT INTO github_connections
                        (workspace_id, repo_url, repo_owner, repo_name, default_branch,
                         webhook_secret, framework, port, auto_deploy, status)
                        VALUES (:ws, :url, :o, :r, :br, :sec, :fw, :port, true, 'connected')
                        ON CONFLICT (workspace_id, repo_owner, repo_name) DO UPDATE SET
                          default_branch = EXCLUDED.default_branch,
                          framework = EXCLUDED.framework,
                          port = EXCLUDED.port
                        RETURNING id"""),
                {"ws": ws, "url": data.repo_url, "o": owner, "r": repo, "br": data.branch,
                 "sec": webhook_secret, "fw": data.framework, "port": data.port}
            )).first()
            conn_id = conn_row[0] if conn_row else None
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB error: {type(e).__name__}: {e}")

        # Insert deploy record
        deploy_row = (await db.execute(
            text("""INSERT INTO github_deploys
                    (connection_id, workspace_id, trigger_type, commit_sha,
                     commit_message, branch, status)
                    VALUES (:c, :ws, 'manual', 'HEAD', 'Quick deploy from API', :br, 'queued')
                    RETURNING id"""),
            {"c": conn_id, "ws": ws, "br": data.branch}
        )).first()
        await db.commit()
        deploy_db_id = deploy_row[0] if deploy_row else None

        # PHASE 2: Trigger background build & deploy worker
        from app.services.github_build import run_github_build_and_deploy
        async def _bg_gh_build():
            async with SessionLocal() as new_db:
                await run_github_build_and_deploy(
                    db=new_db, deploy_id=deploy_db_id, connection_id=conn_id,
                    workspace_id=ws, repo_owner=owner, repo_name=repo,
                    branch=data.branch, framework=data.framework,
                    port=data.port, commit_sha="HEAD",
                )
        if deploy_db_id:
            bg.add_task(_bg_gh_build)

        return {
            "deploy_id": deploy_id,
            "method": "github_repo",
            "repo": f"{owner}/{repo}",
            "branch": data.branch,
            "name": safe_name,
            "framework": data.framework,
            "connection_id": conn_id,
            "status": "queued",
            "poll_url": f"https://zenicloud.io/api/v1/github/deploys/{conn_id}?ws={ws}",
            "estimated_live_in_sec": 120,
            "message": "GitHub repo connected. Phase 2 build worker sẽ clone + deploy. Hoặc: setup webhook tại https://github.com/" + owner + "/" + repo + "/settings/hooks với secret " + webhook_secret[:16] + "...",
            "webhook_url": "https://zenicloud.io/api/v1/github/webhook",
            "note": "Phase 1: repo phải PUBLIC. Phase 2 (đang phát triển): GitHub OAuth cho private repo + auto-clone."
        }

    # Should never reach here
    raise HTTPException(status_code=500, detail="Internal: no deploy path matched")


@router.get("/quick/{deploy_id}")
async def get_quick_deploy_status(
    deploy_id: str,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll quick deploy status (works for ZIP or GitHub deploys)."""
    await require_workspace_access(ws, me)

    # Try source_uploads first (ZIP path)
    row = (await db.execute(
        text("""SELECT upload_id, status, project_name, framework, image_url, deploy_url,
                       error_message, completed_at
                FROM source_uploads WHERE upload_id = :uid AND workspace_id = :ws"""),
        {"uid": deploy_id, "ws": ws}
    )).first()
    if row:
        return {
            "deploy_id": row[0], "method": "zip_upload",
            "status": row[1], "name": row[2], "framework": row[3],
            "image_url": row[4], "deploy_url": row[5],
            "error": row[6], "completed_at": row[7].isoformat() if row[7] else None,
        }

    raise HTTPException(status_code=404, detail="Deploy not found")
