"""
Zeni Cloud Core — Training Pipeline API (Vietcontech LoRA fine-tuning).

Datasets:
  POST   /training/datasets         — register a GCS dataset
  GET    /training/datasets         — list per workspace
  GET    /training/datasets/{id}    — one dataset
  DELETE /training/datasets/{id}    — soft delete

Jobs:
  POST   /training/jobs             — submit a training job (records only — NOT launched)
  GET    /training/jobs             — list per workspace
  GET    /training/jobs/{id}        — status + progress
  POST   /training/jobs/{id}/cancel — mark cancelled

Note: actual Vertex AI Custom Training submission is the responsibility of a
separate endpoint POST /training/jobs/{id}/start (TODO — needs Vertex AI SDK + GPU
budget approval from chairman). The current handler simply records the request.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.training")
router = APIRouter(prefix="/training", tags=["training"])


ALLOWED_BASE_MODELS = {"sdxl", "flux1-dev"}
ALLOWED_LORA_RANK = {8, 16, 32, 64}
GCS_URI_RE = re.compile(r"^gs://[a-z0-9][-a-z0-9._]{1,221}/.+$")


# ─── Schemas ────────────────────────────────────────────────
class DatasetCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    gcs_uri: str = Field(min_length=8, max_length=1024)
    format: str = Field(default="webdataset", max_length=32)
    image_count_estimate: int = Field(default=0, ge=0, le=100_000_000)
    workspace_id: str = Field(min_length=1, max_length=64)

    @field_validator("gcs_uri")
    @classmethod
    def _validate_uri(cls, v: str) -> str:
        if not GCS_URI_RE.match(v):
            raise ValueError("gcs_uri phải đúng format gs://bucket/path")
        return v


class DatasetOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    gcs_uri: str
    format: str
    image_count: int
    total_size_bytes: int
    status: str
    created_at: datetime
    updated_at: datetime


class TrainingJobCreateIn(BaseModel):
    dataset_id: UUID
    base_model: str = Field(default="sdxl")
    lora_rank: int = Field(default=16)
    training_steps: int = Field(default=4000, ge=1000, le=10000)
    learning_rate: float = Field(default=1e-4, ge=1e-5, le=1e-3)
    workspace_id: str = Field(min_length=1, max_length=64)

    @field_validator("base_model")
    @classmethod
    def _validate_base(cls, v: str) -> str:
        if v not in ALLOWED_BASE_MODELS:
            raise ValueError(f"base_model phải thuộc {sorted(ALLOWED_BASE_MODELS)}")
        return v

    @field_validator("lora_rank")
    @classmethod
    def _validate_rank(cls, v: int) -> int:
        if v not in ALLOWED_LORA_RANK:
            raise ValueError(f"lora_rank phải thuộc {sorted(ALLOWED_LORA_RANK)}")
        return v


class TrainingJobOut(BaseModel):
    id: UUID
    workspace_id: str
    dataset_id: UUID
    base_model: str
    lora_rank: int
    training_steps: int
    learning_rate: float
    status: str
    vertex_job_id: str | None
    gcs_output_uri: str | None
    started_at: datetime | None
    completed_at: datetime | None
    cost_usd: float
    error_message: str | None
    created_at: datetime
    updated_at: datetime


# ─── Dataset endpoints ──────────────────────────────────────
@router.post("/datasets", response_model=DatasetOut, status_code=201)
async def create_dataset(
    data: DatasetCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DatasetOut:
    await require_workspace_access(data.workspace_id, me)
    if me.role not in ("Owner", "Admin", "Developer"):
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên")

    row = (await db.execute(text("""
        INSERT INTO training_datasets
            (workspace_id, name, gcs_uri, format, image_count, total_size_bytes, status)
        VALUES (:ws, :name, :uri, :fmt, :n, 0, 'ready')
        RETURNING id, workspace_id, name, gcs_uri, format, image_count,
                  total_size_bytes, status, created_at, updated_at
    """), {
        "ws": data.workspace_id, "name": data.name, "uri": data.gcs_uri,
        "fmt": data.format, "n": data.image_count_estimate,
    })).first()

    await audit_push(db, actor=me.email, workspace_id=data.workspace_id,
                     action="training.dataset.create", target=str(row[0]),
                     severity="ok", metadata={"gcs_uri": data.gcs_uri,
                                               "image_count": data.image_count_estimate})
    await db.commit()

    return DatasetOut(
        id=row[0], workspace_id=row[1], name=row[2], gcs_uri=row[3], format=row[4],
        image_count=row[5], total_size_bytes=row[6], status=row[7],
        created_at=row[8], updated_at=row[9],
    )


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DatasetOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, name, gcs_uri, format, image_count,
               total_size_bytes, status, created_at, updated_at
        FROM training_datasets
        WHERE workspace_id = :ws AND status != 'deleted'
        ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return [DatasetOut(
        id=r[0], workspace_id=r[1], name=r[2], gcs_uri=r[3], format=r[4],
        image_count=r[5], total_size_bytes=r[6], status=r[7],
        created_at=r[8], updated_at=r[9],
    ) for r in rows]


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
async def get_dataset(
    dataset_id: UUID,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DatasetOut:
    await require_workspace_access(ws, me)
    row = (await db.execute(text("""
        SELECT id, workspace_id, name, gcs_uri, format, image_count,
               total_size_bytes, status, created_at, updated_at
        FROM training_datasets WHERE id = :id AND workspace_id = :ws
    """), {"id": dataset_id, "ws": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return DatasetOut(
        id=row[0], workspace_id=row[1], name=row[2], gcs_uri=row[3], format=row[4],
        image_count=row[5], total_size_bytes=row[6], status=row[7],
        created_at=row[8], updated_at=row[9],
    )


@router.delete("/datasets/{dataset_id}", status_code=204, response_class=Response)
async def delete_dataset(
    dataset_id: UUID,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Admin để xoá dataset")

    row = (await db.execute(text(
        "SELECT id FROM training_datasets WHERE id = :id AND workspace_id = :ws"
    ), {"id": dataset_id, "ws": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="dataset not found")

    # Soft delete — preserves audit trail + any linked jobs still reference it.
    await db.execute(text("""
        UPDATE training_datasets SET status = 'deleted', updated_at = NOW()
        WHERE id = :id
    """), {"id": dataset_id})

    await audit_push(db, actor=me.email, workspace_id=ws,
                     action="training.dataset.delete", target=str(dataset_id),
                     severity="warn")
    await db.commit()
    return Response(status_code=204)


# ─── Training job endpoints ────────────────────────────────
@router.post("/jobs", response_model=TrainingJobOut, status_code=201)
async def create_training_job(
    data: TrainingJobCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingJobOut:
    """
    Record a training job request. Status = 'queued'.

    TODO: A follow-up endpoint POST /training/jobs/{id}/start will:
      1. Build args for backend/services/lora_train.py
      2. Submit Vertex AI Custom Training job (a2-highgpu-1g, 1× A100 40GB)
      3. Update vertex_job_id, status='running', started_at
      4. Stream stdout to GCS for progress polling
    """
    await require_workspace_access(data.workspace_id, me)
    if me.role not in ("Owner", "Admin", "Developer"):
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên")

    # Verify dataset belongs to workspace and is ready
    ds = (await db.execute(text("""
        SELECT id, status FROM training_datasets
        WHERE id = :id AND workspace_id = :ws
    """), {"id": data.dataset_id, "ws": data.workspace_id})).first()
    if ds is None:
        raise HTTPException(status_code=404, detail="dataset không tồn tại trong workspace")
    if ds[1] != "ready":
        raise HTTPException(status_code=400,
                            detail=f"dataset trạng thái '{ds[1]}' — phải là 'ready'")

    row = (await db.execute(text("""
        INSERT INTO training_jobs
            (workspace_id, dataset_id, base_model, lora_rank, training_steps,
             learning_rate, status)
        VALUES (:ws, :ds, :bm, :r, :s, :lr, 'queued')
        RETURNING id, workspace_id, dataset_id, base_model, lora_rank,
                  training_steps, learning_rate, status, vertex_job_id,
                  gcs_output_uri, started_at, completed_at, cost_usd,
                  error_message, created_at, updated_at
    """), {
        "ws": data.workspace_id, "ds": data.dataset_id, "bm": data.base_model,
        "r": data.lora_rank, "s": data.training_steps, "lr": data.learning_rate,
    })).first()

    await audit_push(db, actor=me.email, workspace_id=data.workspace_id,
                     action="training.job.create", target=str(row[0]),
                     severity="ok", metadata={
                         "base_model": data.base_model,
                         "steps": data.training_steps,
                         "lora_rank": data.lora_rank,
                     })
    await db.commit()
    log.info("[training] queued job=%s ds=%s base=%s steps=%d",
             row[0], data.dataset_id, data.base_model, data.training_steps)
    return _row_to_job(row)


@router.get("/jobs", response_model=list[TrainingJobOut])
async def list_jobs(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TrainingJobOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, dataset_id, base_model, lora_rank,
               training_steps, learning_rate, status, vertex_job_id,
               gcs_output_uri, started_at, completed_at, cost_usd,
               error_message, created_at, updated_at
        FROM training_jobs WHERE workspace_id = :ws
        ORDER BY created_at DESC LIMIT 200
    """), {"ws": ws})).all()
    return [_row_to_job(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=TrainingJobOut)
async def get_job(
    job_id: UUID,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrainingJobOut:
    await require_workspace_access(ws, me)
    row = (await db.execute(text("""
        SELECT id, workspace_id, dataset_id, base_model, lora_rank,
               training_steps, learning_rate, status, vertex_job_id,
               gcs_output_uri, started_at, completed_at, cost_usd,
               error_message, created_at, updated_at
        FROM training_jobs WHERE id = :id AND workspace_id = :ws
    """), {"id": job_id, "ws": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _row_to_job(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: UUID,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin", "Developer"):
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên")
    row = (await db.execute(text("""
        SELECT id, status FROM training_jobs
        WHERE id = :id AND workspace_id = :ws
    """), {"id": job_id, "ws": ws})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row[1] in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"job đã ở trạng thái terminal '{row[1]}'")

    # TODO: if vertex_job_id set, call aiplatform.CustomJob.cancel() here.
    await db.execute(text("""
        UPDATE training_jobs SET status = 'cancelled', completed_at = NOW(),
                                 updated_at = NOW()
        WHERE id = :id
    """), {"id": job_id})
    await audit_push(db, actor=me.email, workspace_id=ws,
                     action="training.job.cancel", target=str(job_id),
                     severity="warn")
    await db.commit()
    return {"ok": True, "job_id": str(job_id), "status": "cancelled"}


# ─── Helpers ─────────────────────────────────────────────────
def _row_to_job(row: Any) -> TrainingJobOut:
    return TrainingJobOut(
        id=row[0], workspace_id=row[1], dataset_id=row[2],
        base_model=row[3], lora_rank=row[4], training_steps=row[5],
        learning_rate=float(row[6]), status=row[7], vertex_job_id=row[8],
        gcs_output_uri=row[9], started_at=row[10], completed_at=row[11],
        cost_usd=float(row[12] or 0), error_message=row[13],
        created_at=row[14], updated_at=row[15],
    )
