"""
ZeniCloud Router - Model Registry.
Source of truth for: pricing, capabilities, quality scores, tier mapping.
80/15/5 distribution lives here.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Provider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    AWS_BEDROCK = "aws_bedrock"
    GCP_VERTEX = "gcp_vertex"
    SELF_HOSTED = "self_hosted"


class Tier(str, Enum):
    FAST = "fast"          # 80% traffic - cheap, good enough
    BALANCED = "balanced"  # 15% traffic - workhorse
    FRONTIER = "frontier"  # 5% traffic - hardest tasks


class Capability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    CODE = "code"
    FUNCTION_CALLING = "function_calling"
    LONG_CONTEXT = "long_context"
    REASONING = "reasoning"
    AUDIO = "audio"
    IMAGE_GEN = "image_gen"


@dataclass
class ModelEntry:
    """One model in the registry."""
    model_id: str                    # canonical id used by Zeni
    provider: Provider
    provider_model_name: str         # actual name in provider API
    display_name: str
    tier: Tier
    input_price_per_mtok: float      # USD
    output_price_per_mtok: float
    context_window: int
    quality_score: float             # 0.0 - 1.0 (internal benchmark)
    capabilities: list[Capability] = field(default_factory=list)
    failover_to: str | None = None   # fallback model_id
    available_via: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def avg_price_per_mtok(self) -> float:
        # Weighted: assume 1:3 input:output ratio typical
        return (self.input_price_per_mtok + 3 * self.output_price_per_mtok) / 4

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            (input_tokens / 1_000_000) * self.input_price_per_mtok
            + (output_tokens / 1_000_000) * self.output_price_per_mtok
        )


# ════════════════════════════════════════════════════════════════
# REGISTRY - locked from strategic report v1.0 (2026-04-30)
# ════════════════════════════════════════════════════════════════
MODEL_REGISTRY: dict[str, ModelEntry] = {
    # ─── TIER FAST (80% traffic target) ───
    "gemma-4-26b": ModelEntry(
        model_id="gemma-4-26b",
        provider=Provider.GCP_VERTEX,
        provider_model_name="gemma-4-26b-it",
        display_name="Gemma 4 26B MoE",
        tier=Tier.FAST,
        input_price_per_mtok=0.13,
        output_price_per_mtok=0.40,
        context_window=128_000,
        quality_score=0.68,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.FUNCTION_CALLING],
        failover_to="haiku-4-5",
        available_via=["gcp_vertex", "self_hosted"],
        notes="Cheapest managed LLM. Fine-tune candidate for VN-native.",
    ),
    "haiku-4-5": ModelEntry(
        model_id="haiku-4-5",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-haiku-4-5-20251001",
        display_name="Claude Haiku 4.5",
        tier=Tier.FAST,
        input_price_per_mtok=1.00,
        output_price_per_mtok=5.00,
        context_window=200_000,
        quality_score=0.78,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.FUNCTION_CALLING, Capability.VISION],
        failover_to="gemini-3-1-flash",
        available_via=["anthropic", "aws_bedrock", "gcp_vertex"],
        notes="Fast, agentic loops. Default for sub-agent calls.",
    ),
    "gemini-3-1-flash": ModelEntry(
        model_id="gemini-3-1-flash",
        provider=Provider.GOOGLE,
        provider_model_name="gemini-3.1-flash",
        display_name="Gemini 3.1 Flash",
        tier=Tier.FAST,
        input_price_per_mtok=0.30,
        output_price_per_mtok=2.50,
        context_window=1_000_000,
        quality_score=0.80,
        capabilities=[Capability.TEXT, Capability.VISION, Capability.LONG_CONTEXT, Capability.FUNCTION_CALLING],
        failover_to="haiku-4-5",
        available_via=["google", "gcp_vertex"],
        notes="Multimodal + 1M context. Best price/quality for fast tier.",
    ),

    # ─── TIER BALANCED (15% traffic target) ───
    "sonnet-4-6": ModelEntry(
        model_id="sonnet-4-6",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6",
        tier=Tier.BALANCED,
        input_price_per_mtok=3.00,
        output_price_per_mtok=15.00,
        context_window=1_000_000,
        quality_score=0.91,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.VISION, Capability.REASONING, Capability.LONG_CONTEXT],
        failover_to="gemini-3-1-pro",
        available_via=["anthropic", "aws_bedrock", "gcp_vertex"],
        notes="Production workhorse. Default for ZeniMake builders.",
    ),
    "gemini-3-1-pro": ModelEntry(
        model_id="gemini-3-1-pro",
        provider=Provider.GOOGLE,
        provider_model_name="gemini-3.1-pro",
        display_name="Gemini 3.1 Pro",
        tier=Tier.BALANCED,
        input_price_per_mtok=3.50,
        output_price_per_mtok=15.00,
        context_window=2_000_000,
        quality_score=0.92,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.VISION, Capability.REASONING, Capability.LONG_CONTEXT],
        failover_to="sonnet-4-6",
        available_via=["google", "gcp_vertex"],
        notes="2M context. Best for long-doc analysis.",
    ),
    "gpt-5-4": ModelEntry(
        model_id="gpt-5-4",
        provider=Provider.OPENAI,
        provider_model_name="gpt-5.4",
        display_name="GPT-5.4",
        tier=Tier.BALANCED,
        input_price_per_mtok=2.50,
        output_price_per_mtok=15.00,
        context_window=400_000,
        quality_score=0.90,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.VISION, Capability.FUNCTION_CALLING],
        failover_to="sonnet-4-6",
        available_via=["openai", "aws_bedrock"],
        notes="Available on Bedrock from 28/04/2026.",
    ),

    # ─── TIER FRONTIER (5% traffic target) ───
    "opus-4-7": ModelEntry(
        model_id="opus-4-7",
        provider=Provider.ANTHROPIC,
        provider_model_name="claude-opus-4-7",
        display_name="Claude Opus 4.7",
        tier=Tier.FRONTIER,
        input_price_per_mtok=5.00,
        output_price_per_mtok=25.00,
        context_window=1_000_000,
        quality_score=0.97,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.VISION, Capability.REASONING, Capability.LONG_CONTEXT, Capability.FUNCTION_CALLING],
        failover_to="gpt-5-5",
        available_via=["anthropic", "aws_bedrock", "gcp_vertex"],
        notes="#1 SWE-Bench. Tokenizer +35% vs 4.6 - real cost can be higher.",
    ),
    "gpt-5-5": ModelEntry(
        model_id="gpt-5-5",
        provider=Provider.OPENAI,
        provider_model_name="gpt-5.5",
        display_name="GPT-5.5",
        tier=Tier.FRONTIER,
        input_price_per_mtok=5.00,
        output_price_per_mtok=30.00,
        context_window=500_000,
        quality_score=0.96,
        capabilities=[Capability.TEXT, Capability.CODE, Capability.VISION, Capability.REASONING, Capability.FUNCTION_CALLING],
        failover_to="opus-4-7",
        available_via=["openai", "aws_bedrock"],
        notes="On AWS Bedrock limited preview from 28/04/2026.",
    ),
}


def get_model(model_id: str) -> ModelEntry | None:
    return MODEL_REGISTRY.get(model_id)


def models_by_tier(tier: Tier) -> list[ModelEntry]:
    return [m for m in MODEL_REGISTRY.values() if m.tier == tier]


def cheapest_in_tier(tier: Tier, required_caps: list[Capability] | None = None) -> ModelEntry | None:
    candidates = models_by_tier(tier)
    if required_caps:
        candidates = [m for m in candidates if all(c in m.capabilities for c in required_caps)]
    if not candidates:
        return None
    return min(candidates, key=lambda m: m.avg_price_per_mtok)


def all_models() -> list[ModelEntry]:
    return list(MODEL_REGISTRY.values())
