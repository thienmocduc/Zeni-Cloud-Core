"""
ZeniRouter MODEL REGISTRY — locked 8 models across 3 tiers.

Tiers (80/15/5 distribution target):
  FAST      — qa_simple, code_assist_fast, summarize_short
  BALANCED  — analysis, code_review, multi_step_reasoning
  FRONTIER  — research, complex_planning, scientific

Each model declares: provider, tier, prices/MTok, quality_score, capabilities,
failover_to (graceful chain), available_via (transport), and a `real_model_name`
that maps the registry id onto a model the existing llm_gateway can actually call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    SELF_HOST = "zeni_self_host"


class Tier(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    FRONTIER = "frontier"


class Capability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    TOOLS = "tools"
    STRUCTURED = "structured"
    LONG_CONTEXT = "long_context"
    CODE = "code"
    REASONING = "reasoning"


@dataclass
class ModelEntry:
    model_id: str                       # registry id (router-facing)
    display_name: str
    provider: Provider
    provider_model_name: str            # advertised vendor name
    real_model_name: str                # what we actually pass to llm_gateway
    tier: Tier
    input_price_per_mtok: float
    output_price_per_mtok: float
    context_window: int
    quality_score: float                # 0..1
    capabilities: list[Capability] = field(default_factory=list)
    failover_to: list[str] = field(default_factory=list)
    available_via: list[str] = field(default_factory=lambda: ["sdk"])

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """USD cost estimate for a single call."""
        return (
            input_tokens * self.input_price_per_mtok
            + output_tokens * self.output_price_per_mtok
        ) / 1_000_000


# ---------------------------------------------------------------------------
# Locked 8-model roster.
# Where a model isn't GA yet (gemma-4-26b, gemini-3.x, opus-4-7, gpt-5-x) we
# pin `real_model_name` to a currently-callable equivalent so the gateway works
# without touching the public router contract.
# ---------------------------------------------------------------------------
_REGISTRY: list[ModelEntry] = [
    # ---------------- FAST tier ----------------
    ModelEntry(
        model_id="gemma-4-26b",
        display_name="Gemma 4 26B",
        provider=Provider.GOOGLE,
        provider_model_name="gemma-4-26b",
        real_model_name="gemini-2.5-flash-lite",  # Gemma 4 not GA yet
        tier=Tier.FAST,
        input_price_per_mtok=0.10,
        output_price_per_mtok=0.40,
        context_window=128_000,
        quality_score=0.62,
        capabilities=[Capability.TEXT, Capability.STRUCTURED, Capability.CODE],
        failover_to=["gemini-3-1-flash", "haiku-4-5"],
        available_via=["vertex"],
    ),
    ModelEntry(
        model_id="haiku-4-5",
        display_name="Claude Haiku 4.5",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-haiku-4-5",
        real_model_name="claude-haiku-4-5-20251001",
        tier=Tier.FAST,
        input_price_per_mtok=0.80,
        output_price_per_mtok=4.0,
        context_window=200_000,
        quality_score=0.72,
        capabilities=[Capability.TEXT, Capability.TOOLS, Capability.STRUCTURED, Capability.CODE, Capability.VISION],
        failover_to=["gemini-3-1-flash", "gemma-4-26b"],
        available_via=["sdk"],
    ),
    ModelEntry(
        model_id="gemini-3-1-flash",
        display_name="Gemini 3.1 Flash",
        provider=Provider.GOOGLE,
        provider_model_name="gemini-3.1-flash",
        real_model_name="gemini-2.5-flash",  # 3.1 not GA, fall through 2.5
        tier=Tier.FAST,
        input_price_per_mtok=0.30,
        output_price_per_mtok=2.50,
        context_window=1_000_000,
        quality_score=0.74,
        capabilities=[Capability.TEXT, Capability.VISION, Capability.TOOLS,
                      Capability.STRUCTURED, Capability.LONG_CONTEXT, Capability.CODE],
        failover_to=["haiku-4-5", "gemma-4-26b"],
        available_via=["vertex"],
    ),

    # ---------------- BALANCED tier ----------------
    ModelEntry(
        model_id="sonnet-4-6",
        display_name="Claude Sonnet 4.6",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-sonnet-4-6",
        real_model_name="claude-sonnet-4-6",
        tier=Tier.BALANCED,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
        context_window=200_000,
        quality_score=0.86,
        capabilities=[Capability.TEXT, Capability.TOOLS, Capability.STRUCTURED, Capability.VISION,
                      Capability.CODE, Capability.REASONING],
        failover_to=["gemini-3-1-pro", "gpt-5-4"],
        available_via=["sdk"],
    ),
    ModelEntry(
        model_id="gemini-3-1-pro",
        display_name="Gemini 3.1 Pro",
        provider=Provider.GOOGLE,
        provider_model_name="gemini-3.1-pro",
        real_model_name="gemini-2.5-pro",
        tier=Tier.BALANCED,
        input_price_per_mtok=1.25,
        output_price_per_mtok=10.0,
        context_window=2_000_000,
        quality_score=0.85,
        capabilities=[Capability.TEXT, Capability.VISION, Capability.TOOLS,
                      Capability.STRUCTURED, Capability.LONG_CONTEXT, Capability.CODE, Capability.REASONING],
        failover_to=["sonnet-4-6", "gpt-5-4"],
        available_via=["vertex"],
    ),
    ModelEntry(
        model_id="gpt-5-4",
        display_name="GPT-5.4",
        provider=Provider.OPENAI,
        provider_model_name="gpt-5.4",
        real_model_name="gpt-4o-mini",  # placeholder until 5.4 GA
        tier=Tier.BALANCED,
        input_price_per_mtok=0.15,
        output_price_per_mtok=0.60,
        context_window=128_000,
        quality_score=0.83,
        capabilities=[Capability.TEXT, Capability.TOOLS, Capability.STRUCTURED, Capability.CODE, Capability.REASONING],
        failover_to=["sonnet-4-6", "gemini-3-1-pro"],
        available_via=["sdk"],
    ),

    # ---------------- FRONTIER tier ----------------
    ModelEntry(
        model_id="opus-4-7",
        display_name="Claude Opus 4.7",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-opus-4-7",
        real_model_name="claude-opus-4-7",
        tier=Tier.FRONTIER,
        input_price_per_mtok=15.0,
        output_price_per_mtok=75.0,
        context_window=1_000_000,
        quality_score=0.97,
        capabilities=[Capability.TEXT, Capability.TOOLS, Capability.STRUCTURED, Capability.VISION,
                      Capability.LONG_CONTEXT, Capability.CODE, Capability.REASONING],
        failover_to=["gpt-5-5", "sonnet-4-6"],
        available_via=["sdk"],
    ),
    ModelEntry(
        model_id="gpt-5-5",
        display_name="GPT-5.5",
        provider=Provider.OPENAI,
        provider_model_name="gpt-5.5",
        real_model_name="gpt-4o",  # placeholder until 5.5 GA
        tier=Tier.FRONTIER,
        input_price_per_mtok=5.0,
        output_price_per_mtok=15.0,
        context_window=128_000,
        quality_score=0.94,
        capabilities=[Capability.TEXT, Capability.TOOLS, Capability.STRUCTURED, Capability.VISION,
                      Capability.CODE, Capability.REASONING],
        failover_to=["opus-4-7", "gemini-3-1-pro"],
        available_via=["sdk"],
    ),
]

MODEL_REGISTRY: dict[str, ModelEntry] = {m.model_id: m for m in _REGISTRY}


def get_model(model_id: str) -> ModelEntry | None:
    """Lookup by router-facing id."""
    return MODEL_REGISTRY.get(model_id)


def models_in_tier(tier: Tier) -> list[ModelEntry]:
    """All entries belonging to `tier`, returned in registry order."""
    return [m for m in _REGISTRY if m.tier == tier]


def cheapest_in_tier(tier: Tier, required: list[Capability] | None = None) -> ModelEntry | None:
    """Pick the entry inside `tier` with the lowest blended (input+output) cost
    that satisfies every requested capability. Used by the routing engine."""
    candidates = models_in_tier(tier)
    if required:
        req = set(required)
        candidates = [m for m in candidates if req.issubset(set(m.capabilities))]
    if not candidates:
        return None
    return min(candidates, key=lambda m: m.input_price_per_mtok + m.output_price_per_mtok)


# ---------------------------------------------------------------------------
# Task-type → preferred tier map (the 80/15/5 heuristic).
# ---------------------------------------------------------------------------
TASK_TYPE_TIER: dict[str, Tier] = {
    # FAST (target ~80%)
    "qa_simple": Tier.FAST,
    "code_assist_fast": Tier.FAST,
    "summarize_short": Tier.FAST,
    "classify": Tier.FAST,
    "extract": Tier.FAST,
    "translate": Tier.FAST,

    # BALANCED (target ~15%)
    "analysis": Tier.BALANCED,
    "code_review": Tier.BALANCED,
    "multi_step_reasoning": Tier.BALANCED,
    "summarize_long": Tier.BALANCED,
    "rewrite": Tier.BALANCED,

    # FRONTIER (target ~5%)
    "research": Tier.FRONTIER,
    "complex_planning": Tier.FRONTIER,
    "scientific": Tier.FRONTIER,
    "creative_long_form": Tier.FRONTIER,
}


def tier_for_task(task_type: str) -> Tier:
    """Default lookup; unknown task_types fall through to FAST."""
    return TASK_TYPE_TIER.get(task_type, Tier.FAST)
