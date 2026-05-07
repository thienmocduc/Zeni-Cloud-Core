"""
ZeniCloud Router - Routing Engine.
The 80/15/5 decision brain.

Strategy:
1. If client specifies model_id explicitly → use it (after policy check).
2. Else: classify task → infer tier → pick cheapest model in tier matching required capabilities.
3. Failover chain on provider errors.
"""
from dataclasses import dataclass
from enum import Enum

from src.core.config import settings
from src.core.logging import get_logger
from src.core.registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
    Tier,
    cheapest_in_tier,
    get_model,
    models_by_tier,
)

logger = get_logger(__name__)


class TaskComplexity(str, Enum):
    TRIVIAL = "trivial"        # caption, classification, short answer
    SIMPLE = "simple"          # summary, basic Q&A
    MODERATE = "moderate"      # multi-step reasoning, code review
    COMPLEX = "complex"        # full code generation, deep analysis
    FRONTIER = "frontier"      # IPO doc, legal contract, long agent chain


@dataclass
class RoutingRequest:
    """Input to router."""
    tenant_id: str
    product: str  # e.g. "zenimake", "zenilaw", "zeniipo"
    task_type: str  # caller-supplied hint
    estimated_input_tokens: int
    expected_output_tokens: int
    required_capabilities: list[Capability]
    explicit_model_id: str | None = None
    explicit_tier: Tier | None = None
    max_cost_usd: float | None = None
    quality_threshold: float | None = None


@dataclass
class RoutingDecision:
    """Output of routing logic."""
    primary_model: ModelEntry
    failover_chain: list[ModelEntry]
    estimated_cost_usd: float
    decision_reason: str
    tier: Tier


# ────────────────────────────────────────────────────────────
# Heuristic: task_type → complexity
# ────────────────────────────────────────────────────────────
TASK_COMPLEXITY_MAP: dict[str, TaskComplexity] = {
    # Trivial
    "caption": TaskComplexity.TRIVIAL,
    "classify": TaskComplexity.TRIVIAL,
    "extract_field": TaskComplexity.TRIVIAL,
    "translate_short": TaskComplexity.TRIVIAL,
    "summary_short": TaskComplexity.TRIVIAL,
    # Simple
    "qa_simple": TaskComplexity.SIMPLE,
    "summary_long": TaskComplexity.SIMPLE,
    "translate_long": TaskComplexity.SIMPLE,
    "rewrite": TaskComplexity.SIMPLE,
    "embed_query": TaskComplexity.SIMPLE,
    # Moderate
    "code_review": TaskComplexity.MODERATE,
    "code_explain": TaskComplexity.MODERATE,
    "agent_step": TaskComplexity.MODERATE,
    "rag_answer": TaskComplexity.MODERATE,
    "tool_call": TaskComplexity.MODERATE,
    # Complex
    "code_generate": TaskComplexity.COMPLEX,
    "multi_step_reasoning": TaskComplexity.COMPLEX,
    "long_doc_analysis": TaskComplexity.COMPLEX,
    "agent_orchestration": TaskComplexity.COMPLEX,
    # Frontier
    "ipo_document": TaskComplexity.FRONTIER,
    "legal_contract": TaskComplexity.FRONTIER,
    "litigation_brief": TaskComplexity.FRONTIER,
    "supreme_coordinator": TaskComplexity.FRONTIER,
    "deep_research": TaskComplexity.FRONTIER,
}


COMPLEXITY_TO_TIER: dict[TaskComplexity, Tier] = {
    TaskComplexity.TRIVIAL: Tier.FAST,
    TaskComplexity.SIMPLE: Tier.FAST,
    TaskComplexity.MODERATE: Tier.FAST,         # 80% bucket — Haiku/Gemini Flash handle these
    TaskComplexity.COMPLEX: Tier.BALANCED,      # 15% bucket — Sonnet/Gemini Pro
    TaskComplexity.FRONTIER: Tier.FRONTIER,     # 5% bucket — Opus/GPT-5.5
}


class RoutingEngine:
    """The brain of ZeniRouter."""

    def __init__(self) -> None:
        self.log = logger.bind(component="routing_engine")

    def decide(self, req: RoutingRequest) -> RoutingDecision:
        """Main routing decision."""
        # Path 1: explicit model override (used when caller knows what they want)
        if req.explicit_model_id:
            m = get_model(req.explicit_model_id)
            if m is None:
                raise ValueError(f"Unknown model_id: {req.explicit_model_id}")
            return self._build_decision(m, req, reason="explicit_model_id")

        # Path 2: explicit tier override
        if req.explicit_tier:
            picked = cheapest_in_tier(req.explicit_tier, req.required_capabilities)
            if picked is None:
                raise ValueError(f"No model in tier {req.explicit_tier} matches caps {req.required_capabilities}")
            return self._build_decision(picked, req, reason=f"explicit_tier={req.explicit_tier.value}")

        # Path 3: auto-route from task_type → complexity → tier
        complexity = TASK_COMPLEXITY_MAP.get(req.task_type, TaskComplexity.MODERATE)
        target_tier = COMPLEXITY_TO_TIER[complexity]

        # Adjust tier based on quality_threshold if provided
        if req.quality_threshold is not None:
            if req.quality_threshold >= 0.95 and target_tier != Tier.FRONTIER:
                target_tier = Tier.FRONTIER
            elif req.quality_threshold >= 0.88 and target_tier == Tier.FAST:
                target_tier = Tier.BALANCED

        picked = cheapest_in_tier(target_tier, req.required_capabilities)

        # Fallback: if no model in target tier matches caps, escalate
        if picked is None:
            for fallback_tier in [Tier.BALANCED, Tier.FRONTIER]:
                picked = cheapest_in_tier(fallback_tier, req.required_capabilities)
                if picked:
                    target_tier = fallback_tier
                    break

        if picked is None:
            raise RuntimeError(f"No model matches required capabilities: {req.required_capabilities}")

        # Cost gate
        est_cost = picked.estimate_cost(req.estimated_input_tokens, req.expected_output_tokens)
        if req.max_cost_usd is not None and est_cost > req.max_cost_usd:
            # Try to downgrade to cheaper tier
            for cheaper_tier in [Tier.FAST]:
                cheaper = cheapest_in_tier(cheaper_tier, req.required_capabilities)
                if cheaper and cheaper.estimate_cost(
                    req.estimated_input_tokens, req.expected_output_tokens
                ) <= req.max_cost_usd:
                    return self._build_decision(
                        cheaper, req, reason=f"cost_gate_downgrade(was={picked.model_id})"
                    )
            self.log.warning(
                "cost_gate_exceeded",
                estimated=est_cost,
                budget=req.max_cost_usd,
                model=picked.model_id,
            )

        return self._build_decision(
            picked, req, reason=f"auto_tier={target_tier.value}_complexity={complexity.value}"
        )

    def _build_decision(
        self, primary: ModelEntry, req: RoutingRequest, reason: str
    ) -> RoutingDecision:
        # Build failover chain (primary → failover_to → models in same tier)
        chain: list[ModelEntry] = []
        if primary.failover_to:
            fb = get_model(primary.failover_to)
            if fb:
                chain.append(fb)
        # Add other models in same tier as last resort
        for m in models_by_tier(primary.tier):
            if m.model_id != primary.model_id and m not in chain:
                if all(c in m.capabilities for c in req.required_capabilities):
                    chain.append(m)

        cost = primary.estimate_cost(req.estimated_input_tokens, req.expected_output_tokens)

        decision = RoutingDecision(
            primary_model=primary,
            failover_chain=chain[:3],  # cap at 3 fallbacks
            estimated_cost_usd=cost,
            decision_reason=reason,
            tier=primary.tier,
        )

        self.log.info(
            "routing_decision",
            tenant=req.tenant_id,
            product=req.product,
            task=req.task_type,
            primary=primary.model_id,
            failover=[m.model_id for m in decision.failover_chain],
            est_cost_usd=round(cost, 6),
            reason=reason,
        )
        return decision


# Singleton
routing_engine = RoutingEngine()
