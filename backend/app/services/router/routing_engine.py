"""
ZeniRouter ROUTING ENGINE — task_type → complexity → tier → cheapest_in_tier.

Decision flow (80/15/5):
  1. If `explicit_model_id` provided and registered → use it.
  2. Else pick tier:
       a. `explicit_tier` wins if provided
       b. else complexity-bumped `tier_for_task(task_type)`
  3. Pick cheapest model inside tier that satisfies `required_capabilities`.
  4. Build failover chain from the model's `failover_to` list.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
    Tier,
    cheapest_in_tier,
    get_model,
    tier_for_task,
)


# Tokens-input thresholds that bump tier upward (heuristic).
COMPLEXITY_LONG_CONTEXT_BUMP = 80_000
COMPLEXITY_FRONTIER_BUMP = 250_000


@dataclass
class RoutingRequest:
    tenant_id: str
    product: str = "zenicloud"
    task_type: str = "qa_simple"
    estimated_input_tokens: int = 0
    expected_output_tokens: int = 512
    required_capabilities: list[Capability] = field(default_factory=list)
    explicit_model_id: str | None = None
    explicit_tier: Tier | None = None


@dataclass
class RoutingDecision:
    primary_model: ModelEntry
    failover_chain: list[ModelEntry]
    tier: Tier
    decision_reason: str
    estimated_cost_usd: float


class RoutingEngine:
    """Stateless decision-maker. Safe to instantiate once at module scope."""

    def decide(self, req: RoutingRequest) -> RoutingDecision:
        # 1) Explicit model wins
        if req.explicit_model_id:
            m = get_model(req.explicit_model_id)
            if m is not None:
                return self._build(
                    m,
                    reason=f"explicit_model:{req.explicit_model_id}",
                    expected_output=req.expected_output_tokens,
                    estimated_input=req.estimated_input_tokens,
                )

        # 2) Tier selection
        if req.explicit_tier is not None:
            tier = req.explicit_tier
            reason = f"explicit_tier:{tier.value}"
        else:
            tier = self._tier_with_complexity_bump(req)
            reason = f"task_type:{req.task_type}->tier:{tier.value}"

        # 3) Cheapest in tier honouring capabilities; fall back gracefully
        chosen = cheapest_in_tier(tier, req.required_capabilities)
        if chosen is None and req.required_capabilities:
            # Drop capability filter rather than fail outright
            chosen = cheapest_in_tier(tier, None)
            reason += "|caps_relaxed"
        if chosen is None:
            # Tier completely empty (shouldn't happen) — fall back to first model
            chosen = next(iter(MODEL_REGISTRY.values()))
            reason += "|fallback_first"

        return self._build(
            chosen,
            reason=reason,
            expected_output=req.expected_output_tokens,
            estimated_input=req.estimated_input_tokens,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _tier_with_complexity_bump(self, req: RoutingRequest) -> Tier:
        base = tier_for_task(req.task_type)
        if req.estimated_input_tokens >= COMPLEXITY_FRONTIER_BUMP:
            return Tier.FRONTIER
        if req.estimated_input_tokens >= COMPLEXITY_LONG_CONTEXT_BUMP and base == Tier.FAST:
            return Tier.BALANCED
        return base

    def _build(
        self,
        primary: ModelEntry,
        *,
        reason: str,
        expected_output: int,
        estimated_input: int,
    ) -> RoutingDecision:
        chain: list[ModelEntry] = []
        for fid in primary.failover_to:
            fm = get_model(fid)
            if fm is not None:
                chain.append(fm)
        cost = primary.estimate_cost(estimated_input, expected_output)
        return RoutingDecision(
            primary_model=primary,
            failover_chain=chain,
            tier=primary.tier,
            decision_reason=reason,
            estimated_cost_usd=cost,
        )
