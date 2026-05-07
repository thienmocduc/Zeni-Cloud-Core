"""
Zeni Cloud Core — L3 AI Core API (full feature set).

Endpoints (all GCP-only via Vertex AI):
  POST /ai/generate-image    — Imagen 3 text-to-image
  POST /ai/analyze-image     — Gemini multi-modal (image input)
  POST /ai/embed             — text-embedding-004
  POST /ai/complete-stream   — SSE streaming
  GET  /ai/models            — list of available models + pricing

For NexBuild + BTHome interior design AI use cases.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services import ai_core
from app.services.audit import audit_push, billing_push
from app.services.llm_gateway import PRICING

log = logging.getLogger("zeni.api.ai_core")
router = APIRouter(prefix="/ai", tags=["ai", "ai-core"])


class ImageGenIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    aspect_ratio: str = Field(default="1:1", pattern=r"^(1:1|9:16|16:9|3:4|4:3)$")
    n: int = Field(default=1, ge=1, le=4)
    negative_prompt: str | None = Field(default=None, max_length=500)
    seed: int | None = Field(default=None, ge=0, le=2**31)


class AnalyzeImageIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    image_data_uri: str | None = Field(default=None, max_length=20_000_000)
    image_url: str | None = Field(default=None, pattern=r"^https?://.+", max_length=2048)
    model: str = Field(default="gemini-2.5-flash", max_length=64)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    temperature: float = Field(default=0.4, ge=0, le=2)


class EmbedIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=250)
    model: str = Field(default="text-embedding-004", max_length=64)
    task_type: str = Field(default="RETRIEVAL_DOCUMENT",
                           pattern=r"^(RETRIEVAL_DOCUMENT|RETRIEVAL_QUERY|SEMANTIC_SIMILARITY|CLASSIFICATION|CLUSTERING)$")


class StreamCompleteIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    model: str = Field(default="gemini-2.5-flash", max_length=64)
    system: str | None = Field(default=None, max_length=4000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=32768)


@router.post("/generate-image")
async def generate_image(
    ws: str,
    data: ImageGenIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Imagen 3 text-to-image. Trả về data URI base64 PNG."""
    await require_workspace_access(ws, me)
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")
    try:
        result = await ai_core.generate_image(
            prompt=data.prompt, aspect_ratio=data.aspect_ratio, n=data.n,
            negative_prompt=data.negative_prompt, seed=data.seed,
        )
    except Exception as e:
        log.exception("generate_image failed")
        raise HTTPException(status_code=502, detail=f"Imagen 3 lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="ai.generate_image",
        target=data.prompt[:80], severity="ok",
        metadata={"n": data.n, "aspect_ratio": data.aspect_ratio},
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.image", cost_usd=result.get("cost_usd", 0))
    await db.commit()
    return result


@router.post("/analyze-image")
async def analyze_image(
    ws: str,
    data: AnalyzeImageIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Phân tích ảnh + text với Gemini multi-modal."""
    await require_workspace_access(ws, me)
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")
    if not data.image_data_uri and not data.image_url:
        raise HTTPException(status_code=400, detail="Cần image_data_uri hoặc image_url")
    try:
        result = await ai_core.analyze_image(
            prompt=data.prompt, image_data_uri=data.image_data_uri,
            image_url=data.image_url, model=data.model,
            max_tokens=data.max_tokens, temperature=data.temperature,
        )
    except Exception as e:
        log.exception("analyze_image failed")
        raise HTTPException(status_code=502, detail=f"Gemini multi-modal lỗi: {e}")

    pricing = PRICING.get(data.model, (0.30, 2.50))
    cost = (result["input_tokens"] * pricing[0] + result["output_tokens"] * pricing[1]) / 1_000_000

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="ai.analyze_image",
        target=data.prompt[:80], severity="ok",
        metadata={"model": data.model, "tokens": result.get("total_tokens")},
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.vision", cost_usd=cost)
    await db.commit()
    result["cost_usd"] = cost
    return result


@router.post("/embed")
async def embed_text(
    ws: str,
    data: EmbedIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate 768-dim embeddings cho RAG / semantic search."""
    await require_workspace_access(ws, me)
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")
    try:
        result = await ai_core.embed_text(
            texts=data.texts, model=data.model, task_type=data.task_type,
        )
    except Exception as e:
        log.exception("embed_text failed")
        raise HTTPException(status_code=502, detail=f"Embeddings lỗi: {e}")

    total_tokens = sum(e["tokens"] for e in result.get("embeddings", []))
    cost = total_tokens * 0.025 / 1_000_000  # text-embedding-004 pricing

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="ai.embed",
        target=f"{len(data.texts)} texts", severity="ok",
        metadata={"model": data.model, "task_type": data.task_type, "tokens": total_tokens},
    )
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.embed", cost_usd=cost)
    await db.commit()
    result["cost_usd"] = cost
    result["total_tokens"] = total_tokens
    return result


@router.post("/complete-stream")
async def complete_stream(
    ws: str,
    data: StreamCompleteIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Streaming completion via SSE. Mỗi chunk text emit ngay khi Vertex generate."""
    await require_workspace_access(ws, me)
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")

    actor_email = me.email

    async def event_stream():
        try:
            full = ""
            async for chunk in ai_core.stream_complete(
                prompt=data.prompt, model=data.model, system=data.system,
                temperature=data.temperature, max_tokens=data.max_tokens,
            ):
                full += chunk
                payload = json.dumps({"chunk": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            # Final event with full text + audit
            yield f"data: {json.dumps({'done': True, 'total_chars': len(full)})}\n\n"

            # Audit (use new session since the request session may be closed)
            from app.db.base import SessionLocal
            async with SessionLocal() as bg_db:
                await audit_push(
                    bg_db, actor=actor_email, workspace_id=ws, action="ai.stream",
                    target=data.prompt[:80], severity="ok",
                    metadata={"model": data.model, "chars": len(full)},
                )
                await billing_push(bg_db, workspace_id=ws, layer="L3", action="ai.stream", cost_usd=0.0001)
                await bg_db.commit()
        except Exception as e:
            log.exception("stream_complete failed")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/models")
async def list_models() -> dict:
    """List all AI models available + pricing."""
    return {
        "text": [
            {"id": "gemini-2.5-pro",        "provider": "vertex-ai", "input_per_1m": 1.25, "output_per_1m": 10.0,
             "use_cases": ["reasoning", "long-context", "complex-task"]},
            {"id": "gemini-2.5-flash",      "provider": "vertex-ai", "input_per_1m": 0.30, "output_per_1m": 2.50,
             "use_cases": ["chat", "default", "high-throughput"]},
            {"id": "gemini-2.5-flash-lite", "provider": "vertex-ai", "input_per_1m": 0.10, "output_per_1m": 0.40,
             "use_cases": ["bulk", "simple-task", "no-thinking"]},
        ],
        "image": [
            {"id": "imagen-3.0",             "provider": "vertex-ai", "cost_per_image": 0.04,
             "aspect_ratios": ["1:1","9:16","16:9","3:4","4:3"]},
        ],
        "embedding": [
            {"id": "text-embedding-004",     "provider": "vertex-ai",
             "dimensions": 768, "input_per_1m": 0.025},
        ],
        "multimodal": [
            {"id": "gemini-2.5-flash",       "input_modes": ["text","image","video","pdf"]},
            {"id": "gemini-2.5-pro",         "input_modes": ["text","image","video","pdf","audio"]},
        ],
    }
