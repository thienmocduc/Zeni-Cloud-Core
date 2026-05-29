"""
ZENI LLM GATEWAY — unified interface across Claude / OpenAI / Gemini.

If a real API key is configured, route to the provider.
If not, fall back to a deterministic mock (safe for dev and CI).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings


# Per-1M token pricing (USD) — rough public reference, update as needed
PRICING: dict[str, tuple[float, float]] = {
    # input, output
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    # Gemini 2.5 — via Vertex AI (service account auth)
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    # Legacy aliases — fall back to Vertex names if called
    "gemini-2-pro": (1.25, 10.0),
    "gemini-2-flash": (0.30, 2.50),
    "llama-3-70b": (1.0, 4.0),
    "zeni-anima-7b": (0.50, 2.0),
}

# Map alias → real Vertex AI model ID
MODEL_ALIASES: dict[str, str] = {
    "gemini-2-pro": "gemini-2.5-pro",
    "gemini-2-flash": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-2.5-pro",
    "gemini-1.5-flash": "gemini-2.5-flash",
    "gemini-pro-latest": "gemini-2.5-pro",
    "gemini-flash-latest": "gemini-2.5-flash",
}


@dataclass
class InferenceResult:
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    output: str
    cost_usd: float
    latency_ms: int


def _provider_for(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    return "zeni_self_host"


def _estimate_tokens(text: str) -> int:
    # Quick heuristic — 1 token ≈ 4 chars
    return max(1, len(text) // 4)


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_price, out_price = PRICING.get(model, (1.0, 3.0))
    return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000


# ──────────────────────────────────────────────────────────
# Provider implementations
# ──────────────────────────────────────────────────────────

async def _call_anthropic(model: str, prompt: str, system: str | None, temperature: float, max_tokens: int) -> str:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    import anthropic  # lazy import

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system or "You are Zeni Cloud AI assistant.",
        messages=[{"role": "user", "content": prompt}],
    )
    # Collect text blocks
    return "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")


async def _call_openai(model: str, prompt: str, system: str | None, temperature: float, max_tokens: int) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


async def _call_gemini(model: str, prompt: str, system: str | None, temperature: float, max_tokens: int) -> str:
    """
    Call Gemini through Vertex AI (preferred — uses service account auth)
    or fall back to Generative Language API key.
    """
    model_name = MODEL_ALIASES.get(model, model)

    # Preferred: Vertex AI with service account.
    # On Cloud Run / GCE the SA is auto-attached via metadata server — no need
    # for GOOGLE_APPLICATION_CREDENTIALS file. Only require GCP_PROJECT_ID.
    if settings.gcp_project_id:
        try:
            return await _call_vertex_ai(model_name, prompt, system, temperature, max_tokens)
        except Exception as e:
            # Bubble down to Gemini API key fallback if Vertex fails entirely
            import logging
            logging.getLogger("zeni.llm").warning("Vertex AI call failed: %s — fallback to Gemini API key", e)

    # Fallback: Generative Language API with API key
    if settings.gemini_api_key:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        gemini_model = genai.GenerativeModel(model_name=model_name, system_instruction=system)
        resp = await asyncio.to_thread(
            gemini_model.generate_content,
            prompt,
            generation_config={"temperature": temperature, "max_output_tokens": max_tokens},
        )
        return resp.text or ""

    raise RuntimeError("Neither Vertex AI (GCP_PROJECT_ID+GOOGLE_APPLICATION_CREDENTIALS) nor GEMINI_API_KEY configured")


_vertex_initialized = False


async def _call_vertex_ai(model_name: str, prompt: str, system: str | None, temperature: float, max_tokens: int) -> str:
    global _vertex_initialized
    import vertexai
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    if not _vertex_initialized:
        vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region)
        _vertex_initialized = True

    model_obj = GenerativeModel(model_name=model_name, system_instruction=system)
    config = GenerationConfig(temperature=temperature, max_output_tokens=max_tokens)

    # Phase B fires 3 agents in parallel → can burst Vertex quota. Retry transient
    # 429s with backoff before letting the caller degrade to the mock fallback.
    for attempt in range(4):
        try:
            resp = await asyncio.to_thread(model_obj.generate_content, prompt, generation_config=config)
            # .text shortcut throws if multi-part; join parts manually
            if not resp.candidates:
                return ""
            parts = resp.candidates[0].content.parts
            return "".join(getattr(p, "text", "") for p in parts)
        except Exception as e:
            msg = str(e).lower()
            if attempt < 3 and ("429" in msg or "resource exhausted" in msg or "rate limit" in msg):
                await asyncio.sleep(1.5 * (2 ** attempt))
                continue
            raise


def _mock_response(prompt: str, model: str) -> str:
    return (
        f"[Zeni LLM Gateway · mock response from {model}]\n\n"
        "Đây là phản hồi demo vì provider API key chưa được cấu hình.\n"
        f"Prompt đã nhận: {prompt[:200]}{'...' if len(prompt) > 200 else ''}\n\n"
        "Để kích hoạt kết nối thật, anh thêm ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY vào .env và restart backend."
    )


async def run_inference(
    *,
    model: str,
    prompt: str,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 512,
) -> InferenceResult:
    start = time.perf_counter()
    provider = _provider_for(model)
    input_tokens = _estimate_tokens((system or "") + prompt)

    try:
        if provider == "anthropic":
            output = await _call_anthropic(model, prompt, system, temperature, max_tokens)
        elif provider == "openai":
            output = await _call_openai(model, prompt, system, temperature, max_tokens)
        elif provider == "google":
            output = await _call_gemini(model, prompt, system, temperature, max_tokens)
        else:
            output = _mock_response(prompt, model)
    except Exception as e:
        # Graceful degrade to mock on provider error
        output = _mock_response(f"(provider error: {e}) {prompt}", model)

    latency_ms = int((time.perf_counter() - start) * 1000)
    output_tokens = _estimate_tokens(output)
    cost = _compute_cost(model, input_tokens, output_tokens)

    return InferenceResult(
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        output=output,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


def list_available_models() -> list[dict[str, Any]]:
    configured = {
        "anthropic": bool(settings.anthropic_api_key),
        "openai": bool(settings.openai_api_key),
        "google": bool(settings.gemini_api_key),
    }
    return [
        {"id": m, "provider": _provider_for(m), "configured": configured.get(_provider_for(m), False),
         "input_price_per_1m": p[0], "output_price_per_1m": p[1]}
        for m, p in PRICING.items()
    ]
