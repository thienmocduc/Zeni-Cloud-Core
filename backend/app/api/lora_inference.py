"""
Zeni Cloud Core — LoRA inference (Vietcontech style render).

POST /design/render-vietcontech-style
  → Render image(s) using a deployed LoRA model.
  → Currently a STUB: logs request + returns placeholder image URLs.
  → Real impl will route to Replicate API or self-hosted SDXL inference
    endpoint (Cloud Run + Triton + Diffusers) once GPU budget approved.

GET /design/lora-models
  → List deployed LoRA models available to the workspace.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.lora_inference")
router = APIRouter(prefix="/design", tags=["design", "lora"])


# Rough cost estimate per render (SDXL 1024x1024, ~5s on A100):
# Replicate pricing ~$0.0013/sec @ A100 = ~$0.006/image. Mark up 30% for margin.
_BASE_COST_PER_IMAGE_USD = 0.008
_PLACEHOLDER_IMG_BASE = "https://storage.googleapis.com/witsagi-llm-lora/_placeholders"


class RenderIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    lora_model_id: str = Field(min_length=1, max_length=128)
    num_images: int = Field(default=1, ge=1, le=4)
    width: int = Field(default=1024, ge=512, le=2048)
    height: int = Field(default=1024, ge=512, le=2048)
    workspace_id: str = Field(min_length=1, max_length=64)
    negative_prompt: str | None = Field(default=None, max_length=2000)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class RenderOut(BaseModel):
    request_id: str
    images: list[str]
    cost_usd: float
    lora_model_id: str
    width: int
    height: int
    seed: int
    backend: str  # "stub" | "replicate" | "self-hosted"


class LoraModelOut(BaseModel):
    id: UUID
    workspace_id: str
    training_job_id: UUID | None
    name: str
    gcs_weights_uri: str
    inference_endpoint: str | None
    status: str
    use_count: int
    created_at: datetime


@router.post("/render-vietcontech-style", response_model=RenderOut)
async def render_with_lora(
    data: RenderIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RenderOut:
    """
    Render image(s) using a deployed Vietcontech-style LoRA.

    STUB IMPLEMENTATION — returns placeholder URLs + zero cost.
    Real implementation (chairman TODO):
      - If lora_model.inference_endpoint set → POST to it
      - Else → Replicate API call (replicate.com/sdxl-lora with weights_uri)
    """
    await require_workspace_access(data.workspace_id, me)

    # Verify LoRA exists and is deployed (or allow built-in named LoRAs)
    lora = (await db.execute(text("""
        SELECT id, name, gcs_weights_uri, inference_endpoint, status, use_count
        FROM lora_models
        WHERE workspace_id = :ws AND (id::text = :lid OR name = :lid)
              AND status = 'deployed'
    """), {"ws": data.workspace_id, "lid": data.lora_model_id})).first()

    if lora is None:
        # Allow well-known built-in style names without DB row (early-stage UX)
        BUILTIN = {"vietcontech-base", "indochine-classic", "japandi-soft"}
        if data.lora_model_id not in BUILTIN:
            raise HTTPException(status_code=404,
                                detail=f"LoRA '{data.lora_model_id}' không tồn tại hoặc chưa deploy")

    seed = data.seed if data.seed is not None else secrets.randbelow(2**31 - 1)
    request_id = f"render_{secrets.token_hex(8)}"

    # STUB — return placeholder images. Real impl will replace this block.
    placeholders = [
        f"{_PLACEHOLDER_IMG_BASE}/{data.lora_model_id}_{seed}_{i}.png"
        for i in range(data.num_images)
    ]

    # Compute cost based on resolution + count (scale linearly with pixels)
    px_ratio = (data.width * data.height) / (1024 * 1024)
    cost_usd = round(_BASE_COST_PER_IMAGE_USD * data.num_images * px_ratio, 6)

    # Bump use_count if real LoRA
    if lora is not None:
        await db.execute(text(
            "UPDATE lora_models SET use_count = use_count + 1 WHERE id = :id"
        ), {"id": lora[0]})

    await audit_push(db, actor=me.email, workspace_id=data.workspace_id,
                     action="design.lora.render", target=data.lora_model_id,
                     severity="info", metadata={
                         "request_id": request_id,
                         "num_images": data.num_images,
                         "width": data.width,
                         "height": data.height,
                         "cost_usd": cost_usd,
                         "stub": True,
                     })
    try:
        await billing_push(db, workspace_id=data.workspace_id,
                           layer="ai", action="lora.render", cost_usd=cost_usd)
    except Exception as e:
        log.warning("[render] billing_push failed (non-fatal): %s", e)
    await db.commit()

    log.info("[render] STUB lora=%s prompt=%r imgs=%d cost=$%.4f",
             data.lora_model_id, data.prompt[:60], data.num_images, cost_usd)

    return RenderOut(
        request_id=request_id,
        images=placeholders,
        cost_usd=cost_usd,
        lora_model_id=data.lora_model_id,
        width=data.width,
        height=data.height,
        seed=seed,
        backend="stub",
    )


@router.get("/lora-models", response_model=list[LoraModelOut])
async def list_lora_models(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LoraModelOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, training_job_id, name, gcs_weights_uri,
               inference_endpoint, status, use_count, created_at
        FROM lora_models WHERE workspace_id = :ws AND status != 'deleted'
        ORDER BY created_at DESC
    """), {"ws": ws})).all()
    return [LoraModelOut(
        id=r[0], workspace_id=r[1], training_job_id=r[2], name=r[3],
        gcs_weights_uri=r[4], inference_endpoint=r[5], status=r[6],
        use_count=r[7], created_at=r[8],
    ) for r in rows]
