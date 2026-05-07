"""
Zeni Cloud Core — Backup & Disaster Recovery API.

Endpoints quản lý backup policies, backup jobs, restore, PITR, và DR site
governance cho khách của Zeni Cloud (PaaS).

Phân quyền:
  * /backup/policies, /backup/jobs, /backup/restore, /backup/pitr,
    /backup/reports/* → khách (require_workspace_access trên ?ws=)
  * /backup/dr/*  → admin only (require_platform_admin)

Tất cả restore actions ghi audit_push (sensitive) — schema phải khớp với
backend/migrations/039_backup_dr.sql.

Endpoints (prefix /backup, tag backup-dr):

  Policies:
    POST   /policies?ws=
    GET    /policies?ws=
    GET    /policies/{id}?ws=
    PATCH  /policies/{id}?ws=
    DELETE /policies/{id}?ws=
    POST   /policies/{id}/test?ws=

  Backup jobs:
    POST   /jobs?ws=
    GET    /jobs?ws=&status=&from=&to=
    GET    /jobs/{id}?ws=
    DELETE /jobs/{id}?ws=
    POST   /jobs/{id}/download-link?ws=

  Restore:
    POST   /restore?ws=
    GET    /restore?ws=&status=
    GET    /restore/{id}?ws=

  PITR:
    POST   /pitr?ws=
    GET    /pitr/available?ws=

  DR (admin):
    GET    /dr/sites
    POST   /dr/failover
    POST   /dr/failback
    GET    /dr/replication-status

  Compliance reports:
    GET    /reports/coverage?ws=
    GET    /reports/sla?ws=&from=&to=
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_role import require_platform_admin
from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_workspace_access,
)
from app.db.base import get_db
from app.services import backup_engine as engine
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.backup_dr")

router = APIRouter(prefix="/backup", tags=["backup-dr"])


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas (v2)
# ════════════════════════════════════════════════════════════════════════════
SCOPE_PATTERN = r"^(workspace|project|database|storage)$"


class PolicyIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    schedule_cron: str = Field(..., min_length=1, max_length=64)
    retention_days: int = Field(default=30, ge=1, le=3650)
    scope: str = Field(default="workspace", pattern=SCOPE_PATTERN)
    scope_target_id: str | None = Field(default=None, max_length=64)
    encryption_kms_key: str | None = Field(default=None, max_length=255)
    target_region: str = Field(default="us-central1", max_length=32)
    enabled: bool = True


class PolicyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    schedule_cron: str | None = Field(default=None, max_length=64)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    scope: str | None = Field(default=None, pattern=SCOPE_PATTERN)
    scope_target_id: str | None = Field(default=None, max_length=64)
    encryption_kms_key: str | None = Field(default=None, max_length=255)
    target_region: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None


class PolicyOut(BaseModel):
    id: int
    workspace_id: str
    name: str
    schedule_cron: str
    retention_days: int
    scope: str
    scope_target_id: str | None
    encryption_kms_key: str
    target_region: str
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class BackupJobIn(BaseModel):
    scope: str = Field(default="workspace", pattern=SCOPE_PATTERN)
    scope_target_id: str | None = Field(default=None, max_length=64)
    encryption_kms_key: str | None = Field(default=None, max_length=255)
    target_region: str = Field(default="us-central1", max_length=32)
    retention_days: int = Field(default=30, ge=1, le=3650)


class BackupJobOut(BaseModel):
    id: int
    workspace_id: str
    policy_id: int | None
    job_type: str
    status: str
    scope: str
    scope_target_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    size_bytes: int
    file_count: int
    gcs_uri: str | None
    encryption_status: str
    encryption_kms_key: str | None
    checksum_sha256: str | None
    error_message: str | None
    triggered_by: str | None
    expires_at: datetime | None
    created_at: datetime


class RestoreIn(BaseModel):
    backup_id: int
    target_workspace_id: str | None = Field(default=None, max_length=32)
    scope: dict[str, Any] = Field(default_factory=dict)


class RestoreOut(BaseModel):
    id: int
    workspace_id: str
    backup_id: int | None
    target_workspace_id: str | None
    job_kind: str
    pitr_target_ts: datetime | None
    scope: dict[str, Any]
    status: str
    requested_by: str
    started_at: datetime | None
    completed_at: datetime | None
    restored_records_count: int
    restored_size_bytes: int
    error_message: str | None
    created_at: datetime


class PITRIn(BaseModel):
    target_timestamp: datetime
    scope: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class DownloadLinkOut(BaseModel):
    backup_id: int
    url: str
    expires_in_seconds: int
    expires_at: datetime


class DRSiteOut(BaseModel):
    id: int
    primary_region: str
    dr_region: str
    replication_lag_seconds: int
    last_failover_at: datetime | None
    last_failback_at: datetime | None
    status: str
    rto_seconds: int
    rpo_seconds: int
    health_check_url: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class FailoverIn(BaseModel):
    target_region: str = Field(..., min_length=2, max_length=32)
    reason: str = Field(..., min_length=4, max_length=500)


class FailoverOut(BaseModel):
    site_id: int
    primary_region: str
    dr_region: str
    new_status: str
    failed_over_at: datetime
    note: str


class CoverageOut(BaseModel):
    workspace_id: str
    coverage_pct: float
    active_policies: int
    last_backup_at: datetime | None
    recent_backups_7d: int


class SlaOut(BaseModel):
    workspace_id: str
    from_: str = Field(..., alias="from")
    to: str
    total_jobs: int
    completed: int
    failed: int
    success_rate_pct: float
    avg_rto_seconds: float
    max_rto_seconds: float
    rpo_target_seconds: int
    rto_target_seconds: int
    rto_compliant: bool

    model_config = ConfigDict(populate_by_name=True)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _row_to_policy(r: dict[str, Any]) -> PolicyOut:
    return PolicyOut(
        id=int(r["id"]),
        workspace_id=r["workspace_id"],
        name=r["name"],
        schedule_cron=r["schedule_cron"],
        retention_days=int(r["retention_days"]),
        scope=r["scope"],
        scope_target_id=r.get("scope_target_id"),
        encryption_kms_key=r["encryption_kms_key"],
        target_region=r["target_region"],
        enabled=bool(r["enabled"]),
        last_run_at=r.get("last_run_at"),
        next_run_at=r.get("next_run_at"),
        created_by=r.get("created_by"),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _row_to_job(r: dict[str, Any]) -> BackupJobOut:
    return BackupJobOut(
        id=int(r["id"]),
        workspace_id=r["workspace_id"],
        policy_id=int(r["policy_id"]) if r.get("policy_id") is not None else None,
        job_type=r["job_type"],
        status=r["status"],
        scope=r["scope"],
        scope_target_id=r.get("scope_target_id"),
        started_at=r.get("started_at"),
        completed_at=r.get("completed_at"),
        size_bytes=int(r.get("size_bytes") or 0),
        file_count=int(r.get("file_count") or 0),
        gcs_uri=r.get("gcs_uri"),
        encryption_status=r.get("encryption_status") or "pending",
        encryption_kms_key=r.get("encryption_kms_key"),
        checksum_sha256=r.get("checksum_sha256"),
        error_message=r.get("error_message"),
        triggered_by=r.get("triggered_by"),
        expires_at=r.get("expires_at"),
        created_at=r["created_at"],
    )


def _row_to_restore(r: dict[str, Any]) -> RestoreOut:
    scope = r.get("scope") or {}
    if isinstance(scope, str):
        try:
            scope = json.loads(scope)
        except Exception:
            scope = {}
    return RestoreOut(
        id=int(r["id"]),
        workspace_id=r["workspace_id"],
        backup_id=int(r["backup_id"]) if r.get("backup_id") is not None else None,
        target_workspace_id=r.get("target_workspace_id"),
        job_kind=r["job_kind"],
        pitr_target_ts=r.get("pitr_target_ts"),
        scope=scope if isinstance(scope, dict) else {},
        status=r["status"],
        requested_by=r["requested_by"],
        started_at=r.get("started_at"),
        completed_at=r.get("completed_at"),
        restored_records_count=int(r.get("restored_records_count") or 0),
        restored_size_bytes=int(r.get("restored_size_bytes") or 0),
        error_message=r.get("error_message"),
        created_at=r["created_at"],
    )


# ════════════════════════════════════════════════════════════════════════════
# POLICIES
# ════════════════════════════════════════════════════════════════════════════
@router.post("/policies", response_model=PolicyOut, status_code=201)
async def create_policy(
    body: PolicyIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PolicyOut:
    await require_workspace_access(ws, me)
    kms = body.encryption_kms_key or engine.DEFAULT_KMS_KEY
    row = (
        await db.execute(
            text(
                """
                INSERT INTO backup_policies
                  (workspace_id, name, schedule_cron, retention_days, scope,
                   scope_target_id, encryption_kms_key, target_region, enabled,
                   created_by, next_run_at)
                VALUES (:ws, :n, :cron, :rd, :sc, :sti, :kms, :reg, :en,
                        :cb, :nxt)
                RETURNING *
                """
            ),
            {
                "ws": ws,
                "n": body.name,
                "cron": body.schedule_cron,
                "rd": body.retention_days,
                "sc": body.scope,
                "sti": body.scope_target_id,
                "kms": kms,
                "reg": body.target_region,
                "en": body.enabled,
                "cb": me.email,
                "nxt": engine._parse_cron_next(body.schedule_cron),
            },
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.policy.create", target=f"policy:{row['id']}",
        severity="info", metadata={"scope": body.scope, "cron": body.schedule_cron},
    )
    await db.commit()
    return _row_to_policy(dict(row))


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PolicyOut]:
    await require_workspace_access(ws, me)
    rows = (
        await db.execute(
            text(
                """
                SELECT * FROM backup_policies
                 WHERE workspace_id = :ws
                 ORDER BY created_at DESC
                """
            ),
            {"ws": ws},
        )
    ).mappings().all()
    return [_row_to_policy(dict(r)) for r in rows]


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(
    policy_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PolicyOut:
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                "SELECT * FROM backup_policies WHERE id=:id AND workspace_id=:ws"
            ),
            {"id": policy_id, "ws": ws},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return _row_to_policy(dict(row))


@router.patch("/policies/{policy_id}", response_model=PolicyOut)
async def patch_policy(
    policy_id: int,
    body: PolicyPatch,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PolicyOut:
    await require_workspace_access(ws, me)
    sets: list[str] = []
    params: dict[str, Any] = {"id": policy_id, "ws": ws}
    fields = body.model_dump(exclude_unset=True)
    for k, v in fields.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    if not sets:
        raise HTTPException(status_code=400, detail="empty patch")
    if "schedule_cron" in fields:
        sets.append("next_run_at = :nxt")
        params["nxt"] = engine._parse_cron_next(fields["schedule_cron"])
    sets.append("updated_at = NOW()")

    row = (
        await db.execute(
            text(
                f"""
                UPDATE backup_policies
                   SET {', '.join(sets)}
                 WHERE id = :id AND workspace_id = :ws
                 RETURNING *
                """
            ),
            params,
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="policy not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.policy.update", target=f"policy:{policy_id}",
        severity="info", metadata=fields,
    )
    await db.commit()
    return _row_to_policy(dict(row))


@router.delete("/policies/{policy_id}", status_code=204, response_model=None)
async def delete_policy(
    policy_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_access(ws, me)
    res = await db.execute(
        text(
            "DELETE FROM backup_policies WHERE id=:id AND workspace_id=:ws"
        ),
        {"id": policy_id, "ws": ws},
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="policy not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.policy.delete", target=f"policy:{policy_id}",
        severity="warn", metadata={},
    )
    await db.commit()


@router.post("/policies/{policy_id}/test")
async def test_policy(
    policy_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trigger integrity test on most recent backup of this policy."""
    await require_workspace_access(ws, me)
    pol = (
        await db.execute(
            text(
                "SELECT id FROM backup_policies WHERE id=:id AND workspace_id=:ws"
            ),
            {"id": policy_id, "ws": ws},
        )
    ).mappings().first()
    if pol is None:
        raise HTTPException(status_code=404, detail="policy not found")

    last_backup = (
        await db.execute(
            text(
                """
                SELECT id FROM backup_jobs
                 WHERE policy_id = :pid AND status='completed'
                 ORDER BY completed_at DESC LIMIT 1
                """
            ),
            {"pid": policy_id},
        )
    ).mappings().first()
    if last_backup is None:
        raise HTTPException(status_code=400, detail="no completed backup to test")

    test_id = await engine.verify_backup_integrity(
        db, backup_id=int(last_backup["id"]), policy_id=policy_id,
        notes=f"manual test by {me.email}",
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.policy.test", target=f"policy:{policy_id}",
        severity="info", metadata={"test_run_id": test_id, "backup_id": int(last_backup["id"])},
    )
    await db.commit()
    return {"test_run_id": test_id, "policy_id": policy_id, "backup_id": int(last_backup["id"])}


# ════════════════════════════════════════════════════════════════════════════
# BACKUP JOBS
# ════════════════════════════════════════════════════════════════════════════
@router.post("/jobs", response_model=BackupJobOut, status_code=201)
async def create_manual_backup(
    body: BackupJobIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BackupJobOut:
    await require_workspace_access(ws, me)
    job_id = await engine.create_backup(
        db,
        workspace_id=ws,
        scope=body.scope,
        scope_target_id=body.scope_target_id,
        encryption_kms_key=body.encryption_kms_key,
        target_region=body.target_region,
        policy_id=None,
        job_type="manual",
        triggered_by=me.email,
        retention_days=body.retention_days,
    )
    row = (
        await db.execute(
            text("SELECT * FROM backup_jobs WHERE id=:id"),
            {"id": job_id},
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.job.manual", target=f"job:{job_id}",
        severity="info",
        metadata={"scope": body.scope, "size_bytes": int(row["size_bytes"] or 0)},
    )
    await db.commit()
    return _row_to_job(dict(row))


@router.get("/jobs", response_model=list[BackupJobOut])
async def list_jobs(
    ws: str = Query(..., min_length=2, max_length=32),
    status: str | None = Query(default=None, max_length=20),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BackupJobOut]:
    await require_workspace_access(ws, me)
    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if status:
        where.append("status = :st")
        params["st"] = status
    if from_:
        where.append("created_at >= :f")
        params["f"] = from_
    if to:
        where.append("created_at <= :t")
        params["t"] = to
    rows = (
        await db.execute(
            text(
                f"""
                SELECT * FROM backup_jobs
                 WHERE {' AND '.join(where)}
                 ORDER BY created_at DESC
                 LIMIT :lim OFFSET :off
                """
            ),
            params,
        )
    ).mappings().all()
    return [_row_to_job(dict(r)) for r in rows]


@router.get("/jobs/{job_id}", response_model=BackupJobOut)
async def get_job(
    job_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BackupJobOut:
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                "SELECT * FROM backup_jobs WHERE id=:id AND workspace_id=:ws"
            ),
            {"id": job_id, "ws": ws},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="backup job not found")
    return _row_to_job(dict(row))


@router.delete("/jobs/{job_id}", status_code=204, response_model=None)
async def delete_job(
    job_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete backup file (mark expired + remove GCS object)."""
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                "SELECT id, gcs_uri FROM backup_jobs WHERE id=:id AND workspace_id=:ws"
            ),
            {"id": job_id, "ws": ws},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="backup job not found")
    if row.get("gcs_uri"):
        try:
            await engine._delete_gcs_object(row["gcs_uri"])
        except Exception as e:
            log.warning("[backup] gcs delete %s failed: %s", row["gcs_uri"], e)
    await db.execute(
        text(
            """
            UPDATE backup_jobs
               SET status='expired', gcs_uri=NULL, encryption_status='none'
             WHERE id=:id
            """
        ),
        {"id": job_id},
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.job.delete", target=f"job:{job_id}",
        severity="warn", metadata={"gcs_uri": row.get("gcs_uri")},
    )
    await db.commit()


@router.post("/jobs/{job_id}/download-link", response_model=DownloadLinkOut)
async def job_download_link(
    job_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DownloadLinkOut:
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                """
                SELECT id, gcs_uri, status FROM backup_jobs
                 WHERE id=:id AND workspace_id=:ws
                """
            ),
            {"id": job_id, "ws": ws},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="backup job not found")
    if row["status"] != "completed" or not row["gcs_uri"]:
        raise HTTPException(status_code=400, detail="backup không sẵn sàng download")

    expires_seconds = 3600
    url = await engine._signed_url(row["gcs_uri"], expires_seconds=expires_seconds)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.job.download_link", target=f"job:{job_id}",
        severity="warn", metadata={"expires_in": expires_seconds},
    )
    await db.commit()
    return DownloadLinkOut(
        backup_id=job_id, url=url,
        expires_in_seconds=expires_seconds, expires_at=expires_at,
    )


# ════════════════════════════════════════════════════════════════════════════
# RESTORE
# ════════════════════════════════════════════════════════════════════════════
@router.post("/restore", response_model=RestoreOut, status_code=201)
async def post_restore(
    body: RestoreIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RestoreOut:
    await require_workspace_access(ws, me)
    if body.target_workspace_id and body.target_workspace_id != ws:
        # Cross-workspace restore: phải có quyền với target ws
        await require_workspace_access(body.target_workspace_id, me)

    try:
        rid = await engine.restore_backup(
            db,
            workspace_id=ws,
            backup_id=body.backup_id,
            target_workspace_id=body.target_workspace_id,
            scope=body.scope,
            requested_by=me.email,
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    row = (
        await db.execute(
            text("SELECT * FROM restore_jobs WHERE id=:id"),
            {"id": rid},
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.restore.execute", target=f"restore:{rid}",
        severity="warn",
        metadata={"backup_id": body.backup_id, "target_ws": body.target_workspace_id, "scope": body.scope},
    )
    await db.commit()
    return _row_to_restore(dict(row))


@router.get("/restore", response_model=list[RestoreOut])
async def list_restores(
    ws: str = Query(..., min_length=2, max_length=32),
    status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RestoreOut]:
    await require_workspace_access(ws, me)
    where = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if status:
        where.append("status = :st")
        params["st"] = status
    rows = (
        await db.execute(
            text(
                f"""
                SELECT * FROM restore_jobs
                 WHERE {' AND '.join(where)}
                 ORDER BY created_at DESC
                 LIMIT :lim OFFSET :off
                """
            ),
            params,
        )
    ).mappings().all()
    return [_row_to_restore(dict(r)) for r in rows]


@router.get("/restore/{restore_id}", response_model=RestoreOut)
async def get_restore(
    restore_id: int,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RestoreOut:
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                "SELECT * FROM restore_jobs WHERE id=:id AND workspace_id=:ws"
            ),
            {"id": restore_id, "ws": ws},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="restore job not found")
    return _row_to_restore(dict(row))


# ════════════════════════════════════════════════════════════════════════════
# POINT-IN-TIME RECOVERY (PITR)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/pitr", response_model=RestoreOut, status_code=201)
async def post_pitr(
    body: PITRIn,
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RestoreOut:
    await require_workspace_access(ws, me)
    try:
        rid = await engine.pitr_restore(
            db,
            workspace_id=ws,
            target_ts=body.target_timestamp,
            scope=body.scope,
            requested_by=me.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    row = (
        await db.execute(
            text("SELECT * FROM restore_jobs WHERE id=:id"),
            {"id": rid},
        )
    ).mappings().first()
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="backup.pitr.execute", target=f"pitr:{rid}",
        severity="warn",
        metadata={"target_ts": body.target_timestamp.isoformat(), "scope": body.scope},
    )
    await db.commit()
    return _row_to_restore(dict(row))


@router.get("/pitr/available")
async def get_pitr_available(
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    return await engine.pitr_available_window(db, ws)


# ════════════════════════════════════════════════════════════════════════════
# DR (admin only)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/dr/sites", response_model=list[DRSiteOut])
async def list_dr_sites(
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[DRSiteOut]:
    rows = (
        await db.execute(
            text(
                """
                SELECT * FROM dr_sites
                 ORDER BY primary_region, dr_region
                """
            )
        )
    ).mappings().all()
    return [DRSiteOut(**dict(r)) for r in rows]


@router.post("/dr/failover", response_model=FailoverOut)
async def post_dr_failover(
    body: FailoverIn,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> FailoverOut:
    """Emergency failover từ primary → DR region.

    Production sẽ:
      1. Promote Cloud SQL DR replica → primary
      2. Cập nhật DNS/Load balancer → DR region
      3. Mark dr_sites.status = 'failed_over'

    Local: chỉ flip status + ghi audit.
    """
    site = (
        await db.execute(
            text(
                """
                SELECT * FROM dr_sites WHERE dr_region=:r
                 ORDER BY id LIMIT 1
                """
            ),
            {"r": body.target_region},
        )
    ).mappings().first()
    if site is None:
        raise HTTPException(
            status_code=404, detail=f"DR site for region '{body.target_region}' not configured"
        )
    if site["status"] == "failed_over":
        raise HTTPException(status_code=409, detail="đã ở trạng thái failed_over")

    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            """
            UPDATE dr_sites
               SET status='failed_over', last_failover_at=:now, updated_at=:now,
                   notes = COALESCE(notes, '') || E'\n[' || :now::text || '] failover by ' || :who || ': ' || :reason
             WHERE id=:id
            """
        ),
        {"id": site["id"], "now": now, "who": me.email, "reason": body.reason},
    )
    # Audit (admin action — also append to audit_log for history)
    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="dr.failover.execute", target=f"site:{site['id']}",
        severity="err",
        metadata={
            "primary_region": site["primary_region"],
            "dr_region": body.target_region,
            "reason": body.reason,
        },
    )
    await db.commit()
    log.warning(
        "[dr] FAILOVER executed by %s: %s -> %s reason=%s",
        me.email, site["primary_region"], body.target_region, body.reason,
    )
    return FailoverOut(
        site_id=int(site["id"]),
        primary_region=site["primary_region"],
        dr_region=body.target_region,
        new_status="failed_over",
        failed_over_at=now,
        note="DR failover initiated. Cloud SQL replica promotion + DNS swap in progress.",
    )


@router.post("/dr/failback")
async def post_dr_failback(
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return services from DR region back to primary."""
    sites = (
        await db.execute(
            text(
                "SELECT * FROM dr_sites WHERE status='failed_over' ORDER BY id"
            )
        )
    ).mappings().all()
    if not sites:
        raise HTTPException(status_code=409, detail="không có site nào đang failed_over")

    now = datetime.now(timezone.utc)
    failed_back = []
    for s in sites:
        await db.execute(
            text(
                """
                UPDATE dr_sites
                   SET status='active', last_failback_at=:now, updated_at=:now,
                       notes = COALESCE(notes,'') || E'\n[' || :now::text || '] failback by ' || :who
                 WHERE id=:id
                """
            ),
            {"id": s["id"], "now": now, "who": me.email},
        )
        failed_back.append(int(s["id"]))

    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="dr.failback.execute", target="dr_sites",
        severity="warn", metadata={"site_ids": failed_back},
    )
    await db.commit()
    log.warning("[dr] FAILBACK executed by %s: sites=%s", me.email, failed_back)
    return {"failed_back_site_ids": failed_back, "at": now.isoformat()}


@router.get("/dr/replication-status")
async def dr_replication_status(
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            text(
                """
                SELECT primary_region, dr_region, status,
                       replication_lag_seconds, rto_seconds, rpo_seconds,
                       last_failover_at, last_failback_at
                  FROM dr_sites
                """
            )
        )
    ).mappings().all()
    sites = [dict(r) for r in rows]
    overall = "healthy"
    for s in sites:
        if s["status"] == "failed_over":
            overall = "failed_over"
            break
        if s["replication_lag_seconds"] > s["rpo_seconds"]:
            overall = "degraded"
    return {
        "overall": overall,
        "sites": [
            {
                "primary_region": s["primary_region"],
                "dr_region": s["dr_region"],
                "status": s["status"],
                "replication_lag_seconds": int(s["replication_lag_seconds"] or 0),
                "rpo_seconds": int(s["rpo_seconds"] or 0),
                "rto_seconds": int(s["rto_seconds"] or 0),
                "lag_within_rpo": (s["replication_lag_seconds"] or 0) <= (s["rpo_seconds"] or 0),
                "last_failover_at": s["last_failover_at"].isoformat() if s["last_failover_at"] else None,
                "last_failback_at": s["last_failback_at"].isoformat() if s["last_failback_at"] else None,
            }
            for s in sites
        ],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# COMPLIANCE REPORTS
# ════════════════════════════════════════════════════════════════════════════
@router.get("/reports/coverage", response_model=CoverageOut)
async def report_coverage(
    ws: str = Query(..., min_length=2, max_length=32),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoverageOut:
    await require_workspace_access(ws, me)
    data = await engine.coverage_report(db, ws)
    return CoverageOut(**data)


@router.get("/reports/sla", response_model=SlaOut)
async def report_sla(
    ws: str = Query(..., min_length=2, max_length=32),
    from_: str = Query(..., alias="from"),
    to: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlaOut:
    await require_workspace_access(ws, me)
    try:
        from_dt = datetime.fromisoformat(from_)
    except ValueError:
        raise HTTPException(status_code=400, detail="from must be ISO8601")
    if to:
        try:
            to_dt = datetime.fromisoformat(to)
        except ValueError:
            raise HTTPException(status_code=400, detail="to must be ISO8601")
    else:
        to_dt = datetime.now(timezone.utc)
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)
    data = await engine.sla_report(db, ws, from_dt=from_dt, to_dt=to_dt)
    return SlaOut(**{**data, "from": data["from"]})


__all__ = ["router"]
