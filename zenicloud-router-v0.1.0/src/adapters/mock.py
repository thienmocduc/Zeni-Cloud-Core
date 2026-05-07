"""
ZeniCloud Router - Mock Adapter.
Used when USE_MOCK_ADAPTERS=True (default while sếp hasn't supplied real keys).
Returns deterministic fake responses but with real cost calc + telemetry.
"""
import asyncio
import hashlib
import random
import time

from src.adapters.base import BaseAdapter, CompletionRequest, CompletionResponse, ProviderError
from src.core.logging import get_logger

logger = get_logger(__name__)


class MockAdapter(BaseAdapter):
    """Returns realistic-looking responses without calling real APIs."""

    def __init__(self, provider_name: str, simulate_failure_rate: float = 0.0):
        self.provider_name = provider_name
        self.simulate_failure_rate = simulate_failure_rate

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        start = time.perf_counter()

        # Simulate provider latency (different per tier)
        tier = req.model.tier.value
        latency_map = {"fast": (0.15, 0.4), "balanced": (0.5, 1.2), "frontier": (1.5, 3.5)}
        lo, hi = latency_map.get(tier, (0.3, 0.8))
        await asyncio.sleep(random.uniform(lo, hi))

        # Simulate failures
        if self.simulate_failure_rate > 0 and random.random() < self.simulate_failure_rate:
            raise ProviderError(
                f"Mock simulated failure ({self.provider_name})",
                provider=self.provider_name,
                retriable=True,
            )

        # Build deterministic fake response based on input hash
        last_user_msg = next(
            (m.get("content", "") for m in reversed(req.messages) if m.get("role") == "user"),
            "",
        )
        input_text = (req.system or "") + last_user_msg
        digest = hashlib.sha256(input_text.encode()).hexdigest()[:8]

        fake_text = (
            f"[MOCK · {req.model.display_name}] "
            f"This is a deterministic mock response for testing ZeniRouter. "
            f"Echo digest: {digest}. "
            f"Tier: {req.model.tier.value}. "
            f"Provider: {self.provider_name}. "
            f"Replace USE_MOCK_ADAPTERS=False once real API keys are loaded."
        )

        # Realistic token estimation (rough char/4 = tokens)
        input_tokens = max(1, len(input_text) // 4)
        output_tokens = min(req.max_tokens, max(20, len(fake_text) // 4))

        cost = req.model.estimate_cost(input_tokens, output_tokens)
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return CompletionResponse(
            text=fake_text,
            model_id=req.model.model_id,
            provider=self.provider_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=elapsed_ms,
            finish_reason="stop",
            raw_response={"mock": True, "digest": digest},
        )

    async def health_check(self) -> bool:
        return True
