"""
ZeniCloud Router - Anthropic Adapter.
Real implementation. Used when USE_MOCK_ADAPTERS=False and ANTHROPIC_API_KEY is set.
"""
import time

from src.adapters.base import (
    AuthError,
    BaseAdapter,
    CompletionRequest,
    CompletionResponse,
    ProviderError,
    RateLimitError,
)
from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class AnthropicAdapter(BaseAdapter):
    provider_name = "anthropic"

    def __init__(self) -> None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        # Lazy import - SDK only needed when real adapter used
        from anthropic import AsyncAnthropic  # type: ignore

        self.client = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
            timeout=settings.FAILOVER_TIMEOUT_SECONDS * 4,  # generous for first attempt
        )

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        start = time.perf_counter()
        try:
            from anthropic import APIError, APIStatusError, RateLimitError as ARateLimit  # type: ignore

            kwargs = {
                "model": req.model.provider_model_name,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "messages": [m for m in req.messages if m.get("role") != "system"],
            }
            if req.system:
                kwargs["system"] = req.system
            elif system_msg := next(
                (m["content"] for m in req.messages if m.get("role") == "system"), None
            ):
                kwargs["system"] = system_msg

            if req.tools:
                kwargs["tools"] = req.tools

            resp = await self.client.messages.create(**kwargs)

            text_parts = [b.text for b in resp.content if hasattr(b, "text")]
            text = "".join(text_parts)

            input_tokens = resp.usage.input_tokens
            output_tokens = resp.usage.output_tokens
            cost = req.model.estimate_cost(input_tokens, output_tokens)
            elapsed = int((time.perf_counter() - start) * 1000)

            return CompletionResponse(
                text=text,
                model_id=req.model.model_id,
                provider=self.provider_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=elapsed,
                finish_reason=resp.stop_reason or "stop",
                raw_response=None,  # never persist raw to avoid PII leak
            )

        except ARateLimit as e:
            raise RateLimitError(str(e), provider=self.provider_name) from e
        except APIStatusError as e:
            if e.status_code in (401, 403):
                raise AuthError(str(e), provider=self.provider_name) from e
            raise ProviderError(str(e), provider=self.provider_name, retriable=True) from e
        except APIError as e:
            raise ProviderError(str(e), provider=self.provider_name, retriable=True) from e

    async def health_check(self) -> bool:
        try:
            # Cheap call to verify auth
            await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception as e:
            logger.warning("anthropic_health_check_failed", error=str(e))
            return False
