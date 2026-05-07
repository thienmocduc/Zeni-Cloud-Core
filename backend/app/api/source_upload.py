"""
Zeni Cloud Core — Source ZIP Upload API.

Cho phép khách upload ZIP source code → Zeni tự build & deploy KHÔNG CẦN GITHUB.

Workflow:
  1. Khách zip thư mục code thành .zip (max 100MB)
  2. POST /upload/source?ws=X với multipart file
  3. Zeni: upload to GCS → extract → detect framework → generate Dockerfile (nếu chưa có)
              → submit Cloud Build → push Artifact Registry → trả image URL
  4. Khách dùng image URL deploy qua /projects

Endpoints (prefix /upload):
  POST   /source?ws=X&framework=auto   — Upload ZIP, return upload_id + image URL
  GET    /source/{upload_id}?ws=X      — Poll build status
  GET    /source?ws=X                  — List recent uploads
"""
from __future__ import annotations

import asyncio
import io
import os
import secrets
import zipfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.services.source_build import run_build_and_deploy

router = APIRouter(prefix="/upload", tags=["source-upload"])

MAX_ZIP_SIZE_MB = 100
MAX_ZIP_SIZE_BYTES = MAX_ZIP_SIZE_MB * 1024 * 1024
ALLOWED_FRAMEWORKS = {
    "auto", "nextjs", "react", "vue", "static", "fastapi", "express",
    # PWA variants — auto-inject manifest + service worker
    "nextjs-pwa", "vite-pwa",
}


def _detect_framework(file_list: list[str]) -> str:
    """Auto-detect framework from file list."""
    files_lower = [f.lower() for f in file_list]
    has_pkg = any("package.json" in f for f in files_lower)
    has_reqs = any("requirements.txt" in f for f in files_lower)
    has_dockerfile = any(f.endswith("/dockerfile") or f == "dockerfile" for f in files_lower)

    if has_dockerfile:
        return "custom"  # Use repo's Dockerfile as-is
    if any("next.config" in f for f in files_lower):
        return "nextjs"
    if any("vite.config" in f for f in files_lower):
        return "react"
    if any("vue.config" in f for f in files_lower):
        return "vue"
    if has_reqs:
        return "fastapi"  # default Python = FastAPI (most common)
    if has_pkg:
        return "express"  # default Node.js = Express
    if any(f == "index.html" for f in files_lower):
        return "static"
    return "static"  # safest default


async def _bg_build_task(upload_id: str, workspace_id: str, zip_bytes: bytes,
                         framework: str, project_name: str, port: int) -> None:
    """Background wrapper that creates its own DB session."""
    async with SessionLocal() as db:
        await run_build_and_deploy(db, upload_id, workspace_id, zip_bytes,
                                    framework, project_name, port)


@router.post("/source", status_code=202)
async def upload_source(
    bg: BackgroundTasks,
    ws: str = Query(..., min_length=2, max_length=32),
    framework: str = Query(default="auto"),
    project_name: str | None = Query(default=None, max_length=48),
    file: UploadFile = File(..., description="ZIP file containing source code (max 100MB)"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upload ZIP source code → queue build & deploy."""
    await require_workspace_access(ws, me)

    if framework not in ALLOWED_FRAMEWORKS:
        raise HTTPException(status_code=400,
            detail=f"framework must be one of {sorted(ALLOWED_FRAMEWORKS)}")

    # Read file (with size limit)
    content = await file.read()
    if len(content) > MAX_ZIP_SIZE_BYTES:
        raise HTTPException(status_code=413,
            detail=f"File too large: {len(content) / 1024 / 1024:.1f} MB > {MAX_ZIP_SIZE_MB} MB limit")
    if len(content) < 100:
        raise HTTPException(status_code=400, detail="File quá nhỏ — không phải ZIP hợp lệ")

    # Validate ZIP
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            file_list = z.namelist()
            # Security: reject paths trying to escape (../, absolute paths)
            for name in file_list:
                if name.startswith("/") or ".." in name.split("/"):
                    raise HTTPException(status_code=400, detail=f"ZIP chứa đường dẫn không hợp lệ: {name}")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File không phải ZIP hợp lệ")

    # Auto-detect framework if requested
    if framework == "auto":
        framework = _detect_framework(file_list)

    upload_id = secrets.token_urlsafe(16)
    safe_project_name = project_name or f"upload-{upload_id[:8]}"
    safe_project_name = "".join(c if c.isalnum() or c == "-" else "-" for c in safe_project_name.lower())[:48]

    # Insert upload record
    await db.execute(
        text("""
            INSERT INTO source_uploads (
                upload_id, workspace_id, file_size_bytes, file_count,
                framework, detected_framework, project_name, status, uploaded_by
            ) VALUES (
                :uid, :ws, :sz, :cnt, :fw, :detect, :pn, 'queued', :u
            )
        """),
        {
            "uid": upload_id, "ws": ws, "sz": len(content), "cnt": len(file_list),
            "fw": framework, "detect": framework, "pn": safe_project_name,
            "u": me.email,
        }
    )
    await db.commit()

    # Get framework template config (for default port)
    template = (await db.execute(
        text("""SELECT install_cmd, build_cmd, output_dir, default_port, dockerfile_template
                FROM github_framework_templates WHERE framework = :fw"""),
        {"fw": framework}
    )).first()
    port = template[3] if template else 8080

    # Phase 2: Spawn background build task — clones source, builds, pushes image
    bg.add_task(_bg_build_task, upload_id, ws, content, framework, safe_project_name, port)

    return {
        "upload_id": upload_id,
        "workspace_id": ws,
        "framework": framework,
        "file_size_mb": round(len(content) / 1024 / 1024, 2),
        "file_count": len(file_list),
        "project_name": safe_project_name,
        "status": "queued",
        "next_step": "Build worker sẽ pickup và deploy. Poll /upload/source/{upload_id} để xem progress.",
        "framework_config": {
            "install": template[0] if template else None,
            "build": template[1] if template else None,
            "output_dir": template[2] if template else None,
            "port": template[3] if template else 8080,
        } if template else None,
        "estimated_deploy_time_sec": 60,
    }


@router.get("/source/{upload_id}")
async def get_upload_status(
    upload_id: str,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll upload + build + deploy status."""
    await require_workspace_access(ws, me)
    row = (await db.execute(
        text("""SELECT upload_id, framework, detected_framework, project_name, status,
                       file_size_bytes, file_count, error_message, image_url, deploy_url,
                       uploaded_at, completed_at
                FROM source_uploads WHERE upload_id = :uid AND workspace_id = :ws"""),
        {"uid": upload_id, "ws": ws}
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return {
        "upload_id": row[0], "framework": row[1], "detected_framework": row[2],
        "project_name": row[3], "status": row[4], "file_size_bytes": row[5],
        "file_count": row[6], "error_message": row[7],
        "image_url": row[8], "deploy_url": row[9],
        "uploaded_at": row[10].isoformat() if row[10] else None,
        "completed_at": row[11].isoformat() if row[11] else None,
    }


@router.get("/source")
async def list_uploads(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List recent uploads for workspace."""
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        text("""SELECT upload_id, framework, project_name, status, file_size_bytes,
                       deploy_url, uploaded_at, completed_at
                FROM source_uploads WHERE workspace_id = :ws
                ORDER BY uploaded_at DESC LIMIT 50"""),
        {"ws": ws}
    )).all()
    return [
        {
            "upload_id": r[0], "framework": r[1], "project_name": r[2],
            "status": r[3], "file_size_bytes": r[4], "deploy_url": r[5],
            "uploaded_at": r[6].isoformat() if r[6] else None,
            "completed_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]
