"""
OpenAI-Compatible API — drop-in replacement cho tenants.

Tenants chỉ cần đổi:
  base_url = "https://zenicloud.io/api/v1/openai"
  api_key  = "zeni_pat_..."

Endpoints:
  POST /api/v1/openai/v1/chat/completions  — OpenAI ChatCompletion format
  GET  /api/v1/openai/v1/models            — list available models

Protocol:
  - Input: OpenAI ChatCompletion request format
  - Output: OpenAI ChatCompletion response format (non-streaming + streaming SSE)
  - Streaming: SSE format `data: {...}` compatible with openai-python SDK

Integration example (Python):
  from openai import OpenAI
  client = OpenAI(
      base_url="https://zenicloud.io/api/v1/openai/v1",
      api_key="zeni_pat_abc123...",
  )
  resp = client.chat.completions.create(
      model="sonnet-4-6",        # ZeniRouter model ID
      messages=[{"role": "user", "content": "Xin chào!"}],
      stream=True,
  )
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.llm_gateway import run_inference
from app.services.router.quota import check_quota, increment_usage
from app.services.router.registry import MODEL_REGISTRY, ModelEntry
from app.services.router.routing_engine import RoutingEngine, RoutingRequest
from app.services.router.registry import Tier

router = APIRouter(prefix="/openai", tags=["openai-compat"])
_engine = RoutingEngine()


# ---------------------------------------------------------------------------
# OpenAI-format Pydantic schemas
# ---------------------------------------------------------------------------
class OAIMessage(BaseModel):
    role: str
    content: str | None = None


class OAIChatRequest(BaseModel):
    model: str
    messages: list[OAIMessage]
    temperature: float | None = 0.7
    max_tokens: int | None = 2048
    stream: bool | None = False
    top_p: float | None = None
    n: int | None = 1
    stop: list[str] | str | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None  # workspace_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_model(model_id: str) -> ModelEntry | None:
    """Resolve model: try router registry first, then direct LLM gateway names."""
    if model_id in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_id]
    # Try matching by provider_model_name or real_model_name
    for entry in MODEL_REGISTRY.values():
        if entry.provider_model_name == model_id or entry.real_model_name == model_id:
            return entry
    return None


async def _get_workspace(db: AsyncSession, me: CurrentUser) -> str:
    """Get user's primary workspace."""
    if me.workspaces:
        return me.workspaces[0]
    raise HTTPException(403, "No workspace access")


def _make_completion_id() -> str:
    return f"chatcmpl-zeni-{uuid.uuid4().hex[:24]}"


def _unix_now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------
@router.post("/v1/chat/completions")
async def chat_completions(
    payload: OAIChatRequest,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """OpenAI-compatible chat completions endpoint."""
    workspace_id = await _get_workspace(db, me)

    # Quota check
    within, current, limit = await check_quota(db, workspace_id)
    if not within:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"Monthly quota exceeded: ${current:.4f} / ${limit:.2f}. Upgrade tier.",
                    "type": "rate_limit_exceeded",
                    "code": "quota_exceeded",
                }
            },
        )

    # Resolve model
    entry = _resolve_model(payload.model)
    if entry is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": f"Model '{payload.model}' not found. Use GET /v1/models to list available models.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )

    real_model = entry.real_model_name
    messages_dicts = [{"role": m.role, "content": m.content or ""} for m in payload.messages]

    # Extract prompt/system for gateway
    system_parts = [m.content for m in payload.messages if m.role == "system" and m.content]
    system = "\n".join(system_parts) or None
    convo_parts = []
    for m in payload.messages:
        if m.role == "system":
            continue
        if m.role == "assistant":
            convo_parts.append(f"[assistant]: {m.content or ''}")
        else:
            convo_parts.append(m.content or "")
    prompt = "\n\n".join(convo_parts)

    temperature = payload.temperature or 0.7
    max_tokens = payload.max_tokens or 2048
    completion_id = _make_completion_id()
    created = _unix_now()

    if payload.stream:
        return await _handle_streaming(
            db=db, me=me, workspace_id=workspace_id,
            entry=entry, real_model=real_model,
            prompt=prompt, system=system,
            temperature=temperature, max_tokens=max_tokens,
            completion_id=completion_id, created=created,
        )

    # Non-streaming
    start = time.perf_counter()
    try:
        result = await run_inference(
            model=real_model,
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"Model execution failed: {str(e)[:200]}",
                    "type": "server_error",
                    "code": "model_error",
                }
            },
        )

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = entry.estimate_cost(result.input_tokens, result.output_tokens)

    # Track usage
    try:
        await increment_usage(db, workspace_id, cost)
        await billing_push(db, workspace_id=workspace_id, layer="L3",
                           action="openai.chat.completions", cost_usd=cost)
    except Exception:
        pass

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": entry.model_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.output,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.input_tokens,
            "completion_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
        "system_fingerprint": f"zeni-{entry.model_id}",
        "_zeni": {
            "served_by": entry.model_id,
            "real_model": real_model,
            "tier": entry.tier.value,
            "cost_usd": round(cost, 6),
            "latency_ms": latency_ms,
        },
    }


async def _handle_streaming(
    *,
    db: AsyncSession,
    me: CurrentUser,
    workspace_id: str,
    entry: ModelEntry,
    real_model: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    completion_id: str,
    created: int,
) -> StreamingResponse:
    """Handle streaming response in OpenAI SSE format."""

    async def _anthropic_stream():
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        async with client.messages.stream(
            model=real_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are Zeni Cloud AI assistant.",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk

    async def _openai_stream():
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        stream = await client.chat.completions.create(
            model=real_model, messages=msgs,
            temperature=temperature, max_tokens=max_tokens, stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    async def _deepseek_stream():
        import os
        from openai import AsyncOpenAI
        api_key = getattr(settings, "deepseek_api_key", None) or os.environ.get("DEEPSEEK_API_KEY")
        model_map = {"deepseek-chat": "deepseek-chat", "deepseek-flash": "deepseek-flash",
                      "deepseek-reasoner": "deepseek-reasoner"}
        api_model = model_map.get(real_model, real_model)
        client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        stream = await client.chat.completions.create(
            model=api_model, messages=msgs,
            temperature=temperature, max_tokens=max_tokens, stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def _provider():
        if real_model.startswith("claude"):
            return "anthropic"
        if real_model.startswith("gpt"):
            return "openai"
        if real_model.startswith("deepseek"):
            return "deepseek"
        return "google"

    async def event_generator() -> AsyncIterator[str]:
        full_text = ""
        estimated_input = max(1, len(prompt) // 4)
        start = time.perf_counter()

        provider = _provider()
        try:
            if provider == "anthropic" and settings.anthropic_api_key:
                source = _anthropic_stream()
            elif provider == "openai" and settings.openai_api_key:
                source = _openai_stream()
            elif provider == "deepseek" and getattr(settings, "deepseek_api_key", ""):
                source = _deepseek_stream()
            else:
                # Fallback non-streaming
                result = await run_inference(
                    model=real_model, prompt=prompt, system=system,
                    temperature=temperature, max_tokens=max_tokens,
                )
                chunk_data = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": entry.model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": result.output}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"
                full_text = result.output
                source = None

            if source is not None:
                # First chunk with role
                first_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": entry.model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(first_chunk)}\n\n"

                async for text_chunk in source:
                    full_text += text_chunk
                    chunk_data = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": entry.model_id,
                        "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk_data)}\n\n"

        except Exception as e:
            error_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": entry.model_id,
                "choices": [{"index": 0, "delta": {"content": f"[Error: {str(e)[:100]}]"}, "finish_reason": "error"}],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"

        # Final chunk with finish_reason
        final_chunk = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": entry.model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

        # Track usage (best-effort)
        output_tokens = max(1, len(full_text) // 4)
        cost = entry.estimate_cost(estimated_input, output_tokens)
        try:
            await increment_usage(db, workspace_id, cost)
            await billing_push(db, workspace_id=workspace_id, layer="L3",
                               action="openai.chat.completions.stream", cost_usd=cost)
        except Exception:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------
@router.get("/v1/models")
async def list_models() -> dict:
    """OpenAI-compatible model listing."""
    models = []
    for entry in MODEL_REGISTRY.values():
        models.append({
            "id": entry.model_id,
            "object": "model",
            "created": 1716422400,  # 2024-05-23 epoch
            "owned_by": f"zeni-{entry.provider.value}",
            "permission": [],
            "root": entry.model_id,
            "parent": None,
        })
    return {
        "object": "list",
        "data": models,
    }
