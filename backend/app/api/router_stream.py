"""
ZeniRouter STREAMING API — SSE streaming cho AI responses.

Endpoints:
  POST /api/v1/router/stream   — streaming version of /router/complete (SSE)

Tenant integration:
  - Tương thích EventSource pattern (text/event-stream)
  - Mỗi chunk là JSON: {"text": "...", "done": false}
  - Chunk cuối: {"text": "", "done": true, "usage": {...}, "routing": {...}}
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.router.cache import cache_set, make_cache_key
from app.services.router.quota import check_quota, increment_usage
from app.services.router.registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
    Tier,
)
from app.services.router.routing_engine import RoutingEngine, RoutingRequest

router = APIRouter(prefix="/router", tags=["router"])
_engine = RoutingEngine()


class StreamIn(BaseModel):
    messages: list[dict]
    task_type: str = Field(default="qa_simple")
    product: str = Field(default="zenicloud")
    max_tokens: int = Field(default=2048, ge=1, le=32_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    explicit_model: str | None = None
    explicit_tier: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)


def _coerce_caps(values: list[str]) -> list[Capability]:
    valid = {e.value for e in Capability}
    return [Capability(v) for v in values if v in valid]


def _coerce_tier(value: str | None) -> Tier | None:
    if not value:
        return None
    try:
        return Tier(value)
    except ValueError:
        return None


async def _get_workspace_id(db: AsyncSession, ws: str) -> str:
    row = (await db.execute(text(
        "SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1"
    ), {"ws": ws})).mappings().first()
    if not row:
        raise HTTPException(404, "workspace not found")
    return row["id"]


def _join_messages(messages: list[dict]) -> tuple[str, str | None]:
    system_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    system = "\n".join(p for p in system_parts if p) or None
    convo: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = str(m.get("content", ""))
        if not content:
            continue
        if role == "assistant":
            convo.append(f"[assistant]: {content}")
        else:
            convo.append(content)
    return "\n\n".join(convo) if convo else "", system


async def _stream_anthropic(model: str, prompt: str, system: str | None,
                            temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from Anthropic Claude API."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system or "You are Zeni Cloud AI assistant.",
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text_chunk in stream.text_stream:
            yield text_chunk


async def _stream_openai(model: str, prompt: str, system: str | None,
                         temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from OpenAI API."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content


async def _stream_deepseek(model: str, prompt: str, system: str | None,
                           temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from DeepSeek API (OpenAI-compatible)."""
    import os
    from openai import AsyncOpenAI

    api_key = getattr(settings, "deepseek_api_key", None) or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not configured")

    model_map = {
        "deepseek-v4-pro": "deepseek-chat",
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek-chat": "deepseek-chat",
        "deepseek-flash": "deepseek-flash",
        "deepseek-reasoner": "deepseek-reasoner",
    }
    api_model = model_map.get(model, model)

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    stream = await client.chat.completions.create(
        model=api_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content


async def _stream_gemini(model: str, prompt: str, system: str | None,
                         temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Stream from Gemini via Vertex AI. Falls back to non-streaming if needed."""
    import asyncio
    import vertexai
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region)

    model_aliases = {
        "gemini-2-pro": "gemini-2.5-pro",
        "gemini-2-flash": "gemini-2.5-flash",
        "gemini-3.1-pro": "gemini-2.5-pro",
        "gemini-3.1-flash": "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash",
    }
    model_name = model_aliases.get(model, model)
    gen_model = GenerativeModel(model_name=model_name, system_instruction=system)
    config = GenerationConfig(temperature=temperature, max_output_tokens=max_tokens)

    # Vertex AI Python SDK doesn't have native async streaming yet
    # Use generate_content in thread, then yield the result
    resp = await asyncio.to_thread(gen_model.generate_content, prompt, generation_config=config)
    if resp.candidates:
        parts = resp.candidates[0].content.parts
        full_text = "".join(getattr(p, "text", "") for p in parts)
        # Simulate streaming by chunking the response
        chunk_size = 50
        for i in range(0, len(full_text), chunk_size):
            yield full_text[i:i + chunk_size]


def _provider_for_model(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("gemini") or model.startswith("gemma"):
        return "google"
    if model.startswith("deepseek"):
        return "deepseek"
    return "unknown"


async def _stream_model(real_model: str, prompt: str, system: str | None,
                        temperature: float, max_tokens: int) -> AsyncIterator[str]:
    """Route to the correct streaming provider."""
    provider = _provider_for_model(real_model)

    if provider == "anthropic" and settings.anthropic_api_key:
        async for chunk in _stream_anthropic(real_model, prompt, system, temperature, max_tokens):
            yield chunk
    elif provider == "openai" and settings.openai_api_key:
        async for chunk in _stream_openai(real_model, prompt, system, temperature, max_tokens):
            yield chunk
    elif provider == "deepseek" and getattr(settings, "deepseek_api_key", ""):
        async for chunk in _stream_deepseek(real_model, prompt, system, temperature, max_tokens):
            yield chunk
    elif provider == "google" and settings.gcp_project_id:
        async for chunk in _stream_gemini(real_model, prompt, system, temperature, max_tokens):
            yield chunk
    else:
        # Mock fallback
        yield f"[Zeni AI · mock stream · {real_model}] API key chưa cấu hình."


@router.post("/stream")
async def stream_complete(
    payload: StreamIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Streaming AI completion via SSE. Returns text/event-stream."""
    # 1) Auth + workspace
    workspace_id = await _get_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    # 2) Quota check
    within, current, limit = await check_quota(db, workspace_id)
    if not within:
        raise HTTPException(429, f"Monthly quota exceeded: ${current:.4f} / ${limit:.2f}")

    # 3) Routing decision
    estimated_input = sum(len(str(m.get("content", ""))) // 4 for m in payload.messages)
    routing_req = RoutingRequest(
        tenant_id=workspace_id,
        product=payload.product,
        task_type=payload.task_type,
        estimated_input_tokens=estimated_input,
        expected_output_tokens=payload.max_tokens,
        required_capabilities=_coerce_caps(payload.required_capabilities),
        explicit_model_id=payload.explicit_model,
        explicit_tier=_coerce_tier(payload.explicit_tier),
    )
    decision = _engine.decide(routing_req)

    # 4) Build failover chain
    chain: list[ModelEntry] = [decision.primary_model] + decision.failover_chain
    prompt, system = _join_messages(payload.messages)

    async def event_generator() -> AsyncIterator[str]:
        full_text = ""
        served_by = decision.primary_model
        start = time.perf_counter()
        success = False

        for idx, model in enumerate(chain):
            real = model.real_model_name or model.provider_model_name
            try:
                async for chunk in _stream_model(real, prompt, system,
                                                 payload.temperature, payload.max_tokens):
                    full_text += chunk
                    yield f"data: {json.dumps({'text': chunk, 'done': False})}\n\n"
                served_by = model
                success = True
                break
            except Exception as e:
                # Try next model in chain
                if idx < len(chain) - 1:
                    yield f"data: {json.dumps({'text': '', 'done': False, 'failover': model.model_id, 'error': str(e)[:100]})}\n\n"
                    continue
                else:
                    yield f"data: {json.dumps({'text': '', 'done': True, 'error': f'All models failed: {e}'})}\n\n"
                    return

        latency_ms = int((time.perf_counter() - start) * 1000)
        output_tokens = max(1, len(full_text) // 4)
        cost = served_by.estimate_cost(estimated_input, output_tokens)

        # Usage tracking (best-effort, non-blocking)
        try:
            await increment_usage(db, workspace_id, cost)
            await audit_push(
                db, actor=me.email, workspace_id=workspace_id,
                action="router.stream", target=served_by.model_id, severity="ok",
                metadata={
                    "served_by": served_by.model_id,
                    "tier": decision.tier.value,
                    "input_tokens": estimated_input,
                    "output_tokens": output_tokens,
                },
            )
            await billing_push(db, workspace_id=workspace_id, layer="L3",
                               action="router.stream", cost_usd=cost)
        except Exception:
            pass  # Don't break stream on billing error

        # Final event with metadata
        yield f"data: {json.dumps({'text': '', 'done': True, 'usage': {'input_tokens': estimated_input, 'output_tokens': output_tokens, 'total_tokens': estimated_input + output_tokens, 'cost_usd': round(cost, 6)}, 'routing': {'primary_model': decision.primary_model.model_id, 'served_by': served_by.model_id, 'tier': decision.tier.value, 'latency_ms': latency_ms}})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
