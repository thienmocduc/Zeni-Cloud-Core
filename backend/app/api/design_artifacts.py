"""
Zeni Cloud Core — Design Artifacts API (Phase 3 — Task #20C).

Generate + manage CAD/Excel artifacts from a completed design session.

Endpoints:
    POST /design/sessions/{session_id}/artifacts/generate
        → run CAD (7 DXF) + BOQ Excel generators, upload to GCS, return signed URLs

    GET  /design/sessions/{session_id}/artifacts
        → list artifacts for a session

    GET  /design/sessions/{session_id}/artifacts/{artifact_id}/download
        → signed-URL redirect (302) for downloading an artifact

All endpoints require workspace access. Owner+ can list/generate; Viewer = read-only.

Chairman approved 2026-05-26.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.boq_excel import generate_boq_workbook
from app.services.cad_generator import generate_full_package
from app.services.gcp import gcs_ensure_bucket, gcs_signed_url, gcs_upload, gcp_ready

log = logging.getLogger("zeni.api.design_artifacts")
router = APIRouter(prefix="/design", tags=["design-artifacts"])


# ─── GCS bucket convention ──────────────────────────────────────
def _artifacts_bucket() -> str:
    """e.g. 'zeni-design-artifacts'."""
    return f"{settings.gcs_bucket_prefix}design-artifacts"


# ─── Schemas ────────────────────────────────────────────────────
class ArtifactOut(BaseModel):
    id: str
    session_id: str
    filename: str
    content_type: str
    artifact_kind: str
    size_bytes: int
    sha256: str
    gcs_uri: str | None = None
    download_url: str | None = None  # signed URL (short-lived)
    created_at: str | None = None


class ArtifactsListOut(BaseModel):
    session_id: str
    workspace_id: str
    artifacts: list[ArtifactOut]
    total: int


class ArtifactsGenerateOut(BaseModel):
    session_id: str
    workspace_id: str
    artifacts: list[ArtifactOut]
    total_size_bytes: int
    gcs_enabled: bool


# ─── Helpers ────────────────────────────────────────────────────
def _kind_from_filename(filename: str) -> str:
    fn = filename.lower()
    if "floor" in fn:
        return "cad_floor_plan"
    if "section" in fn:
        return "cad_section"
    if "elevation" in fn:
        return "cad_elevation"
    if "structural" in fn or "foundation" in fn or "column" in fn:
        return "cad_struct"
    if "electrical" in fn or "elec" in fn:
        return "cad_elec"
    if "water" in fn or "plumb" in fn:
        return "cad_water"
    if fn.endswith(".xlsx"):
        return "boq_excel"
    return "other"


async def _load_session(db: AsyncSession, session_id: str, workspace_id: str) -> dict[str, Any]:
    res = await db.execute(
        text(
            """SELECT id::text, workspace_id, verdict, agent_outputs, num_floors,
                      location_province
               FROM design_sessions
               WHERE id = CAST(:id AS UUID) AND workspace_id = :ws"""
        ),
        {"id": session_id, "ws": workspace_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Design session not found in this workspace")
    return dict(row)


# ─── 1. POST generate artifacts ─────────────────────────────────
@router.post(
    "/sessions/{session_id}/artifacts/generate",
    response_model=ArtifactsGenerateOut,
)
async def generate_artifacts(
    session_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ArtifactsGenerateOut:
    """
    Generate full CAD + BOQ Excel package from a completed design session.

    Workflow:
      1. Load session from design_sessions (must belong to workspace `ws`)
      2. Call cad_generator.generate_full_package() → 7 DXF files
      3. Call boq_excel.generate_boq_workbook() → 1 XLSX
      4. Upload each to GCS bucket `zeni-design-artifacts/{ws}/{session_id}/...`
      5. Insert design_artifacts rows
      6. Return list with short-lived signed URLs (1 hour)
    """
    await require_workspace_access(ws, me)
    if me.role in ("Viewer",):
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên để sinh artifacts")

    sess = await _load_session(db, session_id, ws)
    agent_outputs = sess.get("agent_outputs") or {}
    if isinstance(agent_outputs, str):
        try:
            agent_outputs = json.loads(agent_outputs)
        except Exception:
            agent_outputs = {}

    log.info("[artifacts.generate] ws=%s session=%s by=%s verdict=%s",
             ws, session_id, me.email, sess.get("verdict"))

    # ─── Generate CAD package (7 DXF) ───────────────────────────
    try:
        cad_files = generate_full_package(
            session_id=session_id,
            agent_outputs={
                **agent_outputs,
                "num_floors": sess.get("num_floors") or 2,
                "project_name": f"Project {session_id[:8]}",
            },
        )
    except Exception as e:
        log.exception("[artifacts.generate] CAD generation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"CAD generation failed: {e}")

    # ─── Generate BOQ Excel ─────────────────────────────────────
    try:
        boq_result = (agent_outputs.get("boq") or {})
        xlsx_bytes = generate_boq_workbook(
            boq_result=boq_result,
            project_name=f"Project {session_id[:8]} — {ws}",
            location=str(sess.get("location_province") or "Hà Nội"),
        )
    except Exception as e:
        log.exception("[artifacts.generate] BOQ generation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"BOQ Excel generation failed: {e}")

    # ─── Upload to GCS (or local stash if GCS not configured) ───
    bucket = _artifacts_bucket()
    artifacts_out: list[ArtifactOut] = []
    total_bytes = 0
    gcs_enabled = gcp_ready()

    if gcs_enabled:
        try:
            await gcs_ensure_bucket(bucket)
        except Exception as e:
            log.warning("[artifacts.generate] gcs_ensure_bucket failed: %s — proceeding without GCS", e)
            gcs_enabled = False

    files_to_upload: list[tuple[str, bytes, str, str]] = []
    for fn, data in cad_files.items():
        files_to_upload.append((fn, data, "image/vnd.dxf", _kind_from_filename(fn)))
    files_to_upload.append(
        ("BOQ.xlsx", xlsx_bytes,
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "boq_excel"),
    )

    for filename, data, content_type, kind in files_to_upload:
        size_b = len(data)
        sha = hashlib.sha256(data).hexdigest()
        gcs_key = f"{ws}/{session_id}/{filename}"
        gcs_uri = None
        signed_url = None

        if gcs_enabled:
            try:
                gcs_uri = await gcs_upload(bucket, gcs_key, data, content_type=content_type)
                signed_url = await gcs_signed_url(bucket, gcs_key, method="GET", expires_seconds=3600)
            except Exception as e:
                log.warning("[artifacts.generate] gcs_upload %s failed: %s", filename, e)
                gcs_uri = None
                signed_url = None

        # Insert artifact row (gen_random_uuid via DEFAULT)
        ins = await db.execute(
            text(
                """INSERT INTO design_artifacts
                   (session_id, workspace_id, filename, content_type, artifact_kind,
                    gcs_bucket, gcs_key, size_bytes, sha256)
                   VALUES (CAST(:sid AS UUID), :ws, :fn, :ct, :kind, :bk, :gk, :sz, :sha)
                   RETURNING id::text, created_at::text"""
            ),
            {
                "sid": session_id,
                "ws": ws,
                "fn": filename,
                "ct": content_type,
                "kind": kind,
                "bk": bucket if gcs_enabled else None,
                "gk": gcs_key if gcs_enabled else None,
                "sz": size_b,
                "sha": sha,
            },
        )
        ins_row = ins.first()
        art_id = ins_row[0] if ins_row else ""
        created_at = ins_row[1] if ins_row else ""

        artifacts_out.append(
            ArtifactOut(
                id=art_id,
                session_id=session_id,
                filename=filename,
                content_type=content_type,
                artifact_kind=kind,
                size_bytes=size_b,
                sha256=sha,
                gcs_uri=gcs_uri,
                download_url=signed_url,
                created_at=created_at,
            )
        )
        total_bytes += size_b

    # Audit
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.artifacts.generate",
        target=session_id, severity="info",
        metadata={
            "artifacts_count": len(artifacts_out),
            "total_bytes": total_bytes,
            "gcs_enabled": gcs_enabled,
            "bucket": bucket if gcs_enabled else None,
        },
    )
    await db.commit()

    return ArtifactsGenerateOut(
        session_id=session_id,
        workspace_id=ws,
        artifacts=artifacts_out,
        total_size_bytes=total_bytes,
        gcs_enabled=gcs_enabled,
    )


# ─── 2. GET list artifacts ──────────────────────────────────────
@router.get(
    "/sessions/{session_id}/artifacts",
    response_model=ArtifactsListOut,
)
async def list_artifacts(
    session_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ArtifactsListOut:
    """List artifacts generated for a design session."""
    await require_workspace_access(ws, me)
    # Validate session belongs to workspace
    await _load_session(db, session_id, ws)

    res = await db.execute(
        text(
            """SELECT id::text, session_id::text, filename, content_type, artifact_kind,
                      size_bytes, sha256, gcs_bucket, gcs_key, created_at::text
               FROM design_artifacts
               WHERE session_id = CAST(:sid AS UUID) AND workspace_id = :ws
               ORDER BY created_at ASC"""
        ),
        {"sid": session_id, "ws": ws},
    )
    rows = res.mappings().all()
    artifacts: list[ArtifactOut] = []
    for r in rows:
        gcs_uri = None
        if r["gcs_bucket"] and r["gcs_key"]:
            gcs_uri = f"gs://{r['gcs_bucket']}/{r['gcs_key']}"
        artifacts.append(
            ArtifactOut(
                id=r["id"],
                session_id=r["session_id"],
                filename=r["filename"],
                content_type=r["content_type"],
                artifact_kind=r["artifact_kind"] or "other",
                size_bytes=r["size_bytes"] or 0,
                sha256=r["sha256"] or "",
                gcs_uri=gcs_uri,
                download_url=None,  # use /download endpoint to get fresh signed URL
                created_at=r["created_at"],
            )
        )

    return ArtifactsListOut(
        session_id=session_id, workspace_id=ws,
        artifacts=artifacts, total=len(artifacts),
    )


# ─── 3. GET download signed URL ─────────────────────────────────
@router.get("/sessions/{session_id}/artifacts/{artifact_id}/download")
async def download_artifact(
    session_id: str,
    artifact_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return 302 redirect to a fresh signed URL (10-min expiry)."""
    await require_workspace_access(ws, me)
    # Validate artifact ID format
    try:
        UUID(artifact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="artifact_id phải là UUID hợp lệ")

    res = await db.execute(
        text(
            """SELECT filename, gcs_bucket, gcs_key
               FROM design_artifacts
               WHERE id = CAST(:aid AS UUID)
                 AND session_id = CAST(:sid AS UUID)
                 AND workspace_id = :ws"""
        ),
        {"aid": artifact_id, "sid": session_id, "ws": ws},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not row["gcs_bucket"] or not row["gcs_key"]:
        raise HTTPException(status_code=503,
                            detail="Artifact chưa upload lên GCS (GCS chưa cấu hình tại thời điểm sinh)")

    try:
        url = await gcs_signed_url(row["gcs_bucket"], row["gcs_key"],
                                   method="GET", expires_seconds=600)
    except Exception as e:
        log.exception("[artifacts.download] sign failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Cannot sign URL: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.artifacts.download",
        target=artifact_id, severity="info",
        metadata={"filename": row["filename"], "session_id": session_id},
    )
    await db.commit()

    return RedirectResponse(url=url, status_code=302)


# ─── Health ─────────────────────────────────────────────────────
@router.get("/artifacts/health")
async def artifacts_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "bucket": _artifacts_bucket(),
        "gcs_ready": gcp_ready(),
    }
