"""
Zeni Cloud Core — L3 AI Core (full features qua Vertex AI, GCP-only).

  - generate_image()      : Imagen 3 text-to-image (cho NexBuild/BTHome design)
  - analyze_image()       : Gemini multi-modal (analyze ảnh + text prompt)
  - embed_text()          : text-embedding-004 (cho RAG / semantic search)
  - stream_complete()     : SSE-friendly streaming generation
  - similarity_search()   : pgvector cosine similarity (workspace embeddings)

All routes through Vertex AI with SA auto-attached on Cloud Run.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, AsyncIterator

from app.core.config import settings

log = logging.getLogger("zeni.ai_core")

_initialized = False


def _ensure_init() -> None:
    """Initialize Vertex AI SDK once per process."""
    global _initialized
    if _initialized:
        return
    if not settings.gcp_project_id:
        raise RuntimeError("GCP_PROJECT_ID chưa cấu hình")
    import vertexai
    vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region or "us-central1")
    _initialized = True


# ─── 1. IMAGE GENERATION (Imagen 3) ──────────────────────────────
async def generate_image(
    *, prompt: str, aspect_ratio: str = "1:1", n: int = 1,
    negative_prompt: str | None = None, seed: int | None = None,
    safety_filter: str = "block_some",
    model: str = "imagen-3.0-generate-002",
) -> dict[str, Any]:
    """
    Generate image(s) from text using Imagen 3.

    aspect_ratio: '1:1' | '9:16' | '16:9' | '3:4' | '4:3'
    model: 'imagen-3.0-generate-002' (highest quality, quota 1/min) |
           'imagen-3.0-fast-generate-001' (near-equal quality, quota 20/min, cheaper)
    Returns list of base64-encoded PNG images.
    """
    _ensure_init()
    from vertexai.preview.vision_models import ImageGenerationModel

    gen_model = ImageGenerationModel.from_pretrained(model)
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "number_of_images": min(max(1, n), 4),
        "aspect_ratio": aspect_ratio,
        "safety_filter_level": safety_filter,
        "person_generation": "allow_adult",
    }
    if negative_prompt:
        kwargs["negative_prompt"] = negative_prompt
    if seed is not None:
        kwargs["seed"] = seed
        kwargs["add_watermark"] = False  # required when seed set

    resp = await asyncio.to_thread(gen_model.generate_images, **kwargs)
    images = []
    for img in resp.images:
        b64 = base64.b64encode(img._image_bytes).decode("ascii")
        images.append({
            "data_uri": f"data:image/png;base64,{b64}",
            "size_bytes": len(img._image_bytes),
        })
    unit_price = 0.02 if "fast" in model else 0.04  # Imagen 3 fast vs standard pricing
    return {
        "model": model,
        "count": len(images),
        "aspect_ratio": aspect_ratio,
        "images": images,
        "cost_usd": unit_price * len(images),
    }


# ─── 2. MULTI-MODAL GEMINI (image+text input) ────────────────────
async def analyze_image(
    *, prompt: str, image_data_uri: str | None = None, image_url: str | None = None,
    model: str = "gemini-2.5-flash", max_tokens: int = 2048, temperature: float = 0.4,
) -> dict[str, Any]:
    """
    Analyze image with Gemini multi-modal. Pass either inline data URI or URL.

    Use cases:
      - Phân tích ảnh thiết kế nội thất → trả gợi ý cải thiện
      - OCR document
      - Tag products in catalog photo
    """
    _ensure_init()
    from vertexai.generative_models import GenerativeModel, Part

    model_obj = GenerativeModel(model)
    parts: list[Any] = [prompt]

    if image_data_uri:
        # Parse data:image/png;base64,XXXX
        if not image_data_uri.startswith("data:"):
            raise ValueError("image_data_uri phải bắt đầu 'data:image/...;base64,'")
        head, b64 = image_data_uri.split(",", 1)
        mime = head.split(";")[0].split(":", 1)[1]
        raw = base64.b64decode(b64)
        parts.insert(0, Part.from_data(data=raw, mime_type=mime))
    elif image_url:
        parts.insert(0, Part.from_uri(uri=image_url, mime_type="image/jpeg"))
    else:
        raise ValueError("Cần image_data_uri HOẶC image_url")

    resp = await asyncio.to_thread(
        model_obj.generate_content,
        parts,
        generation_config={"temperature": temperature, "max_output_tokens": max_tokens},
    )
    out_text = ""
    if resp.candidates:
        for p in resp.candidates[0].content.parts:
            out_text += getattr(p, "text", "") or ""

    usage = resp.usage_metadata
    return {
        "model": model,
        "provider": "vertex-ai",
        "output": out_text,
        "input_tokens": usage.prompt_token_count if usage else 0,
        "output_tokens": usage.candidates_token_count if usage else 0,
        "total_tokens": usage.total_token_count if usage else 0,
    }


# ─── 3. EMBEDDINGS (text-embedding-004) ──────────────────────────
async def embed_text(
    *, texts: list[str], model: str = "text-embedding-004",
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> dict[str, Any]:
    """
    Generate 768-dim embeddings for list of texts.
    task_type: 'RETRIEVAL_DOCUMENT' | 'RETRIEVAL_QUERY' | 'SEMANTIC_SIMILARITY' |
               'CLASSIFICATION' | 'CLUSTERING'
    """
    _ensure_init()
    from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput

    if len(texts) > 250:
        raise ValueError("Tối đa 250 texts/request")

    embed_model = TextEmbeddingModel.from_pretrained(model)
    inputs = [TextEmbeddingInput(text=t, task_type=task_type) for t in texts]
    embeddings = await asyncio.to_thread(embed_model.get_embeddings, inputs)
    return {
        "model": model,
        "task_type": task_type,
        "count": len(embeddings),
        "dimensions": len(embeddings[0].values) if embeddings else 0,
        "embeddings": [{"index": i, "vector": e.values, "tokens": e.statistics.token_count} for i, e in enumerate(embeddings)],
    }


# ─── 4. STREAMING completion (SSE) ───────────────────────────────
async def stream_complete(
    *, prompt: str, model: str = "gemini-2.5-flash",
    system: str | None = None, temperature: float = 0.7,
    max_tokens: int = 2048,
) -> AsyncIterator[str]:
    """
    Streaming generation. Async generator yielding text chunks as Gemini emits them.
    Backend wraps as SSE for clients.
    """
    _ensure_init()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    model_obj = GenerativeModel(model, system_instruction=system) if system else GenerativeModel(model)
    config = GenerationConfig(temperature=temperature, max_output_tokens=max_tokens)

    # Vertex SDK is sync streaming generator → wrap in async iterator
    def _gen():
        return model_obj.generate_content(prompt, generation_config=config, stream=True)

    iterator = await asyncio.to_thread(_gen)
    while True:
        try:
            chunk = await asyncio.to_thread(next, iterator)
        except StopIteration:
            break
        text = ""
        if chunk.candidates:
            for p in chunk.candidates[0].content.parts:
                text += getattr(p, "text", "") or ""
        if text:
            yield text


# ─── 5. FINE-TUNING TRIGGER ──────────────────────────────────────
async def list_finetune_jobs(workspace: str) -> list[dict[str, Any]]:
    """List Vertex AI tuning jobs filtered by workspace label."""
    _ensure_init()
    from google.cloud import aiplatform
    aiplatform.init(project=settings.gcp_project_id, location=settings.gcp_region or "us-central1")

    # Note: Real implementation would use Vertex AI Tuning API.
    # For MVP, return empty list (UI scaffold).
    return []
