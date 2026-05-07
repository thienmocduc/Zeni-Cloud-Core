"""
ZeniCloud Router - HTTP routes.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from src.adapters.base import CompletionRequest as AdapterReq
from src.core.config import settings
from src.core.logging import get_logger
from src.core.registry import Capability, MODEL_REGISTRY, Tier, all_models
from src.middleware.auth import verify_api_key
from src.schemas.api import (
    CompletionRequestSchema,
    CompletionResponseSchema,
    HealthResponse,
    ModelListItem,
    RoutingMetadata,
)
from src.services.failover import failover_executor
from src.services.routing_engine import RoutingRequest, routing_engine

logger = get_logger(__name__)

router = APIRouter()


# ─────────────── HEALTH ───────────────
@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Public health check."""
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        mock_mode=settings.USE_MOCK_ADAPTERS,
        providers={
            "anthropic": settings.ANTHROPIC_API_KEY is not None or settings.USE_MOCK_ADAPTERS,
            "openai": settings.OPENAI_API_KEY is not None or settings.USE_MOCK_ADAPTERS,
            "google": settings.GOOGLE_API_KEY is not None or settings.USE_MOCK_ADAPTERS,
            "aws_bedrock": settings.AWS_ACCESS_KEY_ID is not None or settings.USE_MOCK_ADAPTERS,
        },
    )


# ─────────────── MODELS ───────────────
@router.get("/v1/models", response_model=list[ModelListItem], tags=["models"])
async def list_models(
    tier: str | None = None,
    _ctx: dict = Depends(verify_api_key),
) -> list[ModelListItem]:
    """List available models. Optional tier filter."""
    models = all_models()
    if tier:
        try:
            tier_enum = Tier(tier)
            models = [m for m in models if m.tier == tier_enum]
        except ValueError as e:
            raise HTTPException(400, f"Invalid tier: {tier}") from e

    return [
        ModelListItem(
            model_id=m.model_id,
            display_name=m.display_name,
            provider=m.provider.value,
            tier=m.tier.value,
            input_price_per_mtok=m.input_price_per_mtok,
            output_price_per_mtok=m.output_price_per_mtok,
            context_window=m.context_window,
            quality_score=m.quality_score,
            capabilities=[c.value for c in m.capabilities],
        )
        for m in models
    ]


# ─────────────── ROUTING (preview only, no LLM call) ───────────────
@router.post("/v1/route", tags=["routing"])
async def preview_route(
    req: CompletionRequestSchema,
    ctx: dict = Depends(verify_api_key),
) -> dict:
    """Preview which model would be selected without invoking it. Useful for cost estimation."""
    routing_req = _build_routing_request(req, ctx)
    decision = routing_engine.decide(routing_req)
    return {
        "primary_model": decision.primary_model.model_id,
        "tier": decision.tier.value,
        "estimated_cost_usd": round(decision.estimated_cost_usd, 6),
        "decision_reason": decision.decision_reason,
        "failover_chain": [m.model_id for m in decision.failover_chain],
    }


# ─────────────── COMPLETE (route + execute) ───────────────
@router.post("/v1/complete", response_model=CompletionResponseSchema, tags=["completion"])
async def complete(
    req: CompletionRequestSchema,
    ctx: dict = Depends(verify_api_key),
) -> CompletionResponseSchema:
    """Main endpoint. Route + execute + return."""
    routing_req = _build_routing_request(req, ctx)
    decision = routing_engine.decide(routing_req)

    # Build adapter-level request
    adapter_req = AdapterReq(
        model=decision.primary_model,
        messages=[m.model_dump() for m in req.messages],
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        system=req.system,
        stream=False,
    )

    try:
        resp = await failover_executor.execute(decision, adapter_req)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e

    failover_count = len(resp.raw_response.get("failover_attempts", [])) - 1 if resp.raw_response else 0

    return CompletionResponseSchema(
        text=resp.text,
        routing=RoutingMetadata(
            primary_model=decision.primary_model.model_id,
            served_by=resp.model_id,
            tier=decision.tier.value,
            estimated_cost_usd=round(decision.estimated_cost_usd, 6),
            actual_cost_usd=round(resp.cost_usd, 6),
            decision_reason=decision.decision_reason,
            failover_count=max(0, failover_count),
        ),
        usage={
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "total_tokens": resp.input_tokens + resp.output_tokens,
        },
        latency_ms=resp.latency_ms,
    )


# ─────────────── helpers ───────────────
def _build_routing_request(req: CompletionRequestSchema, ctx: dict) -> RoutingRequest:
    # Estimate tokens if not provided
    est_in = req.estimated_input_tokens or sum(len(m.content) // 4 for m in req.messages)
    est_out = req.expected_output_tokens or req.max_tokens

    # Parse capabilities
    caps: list[Capability] = []
    for c in req.required_capabilities:
        try:
            caps.append(Capability(c))
        except ValueError:
            logger.warning("unknown_capability", cap=c)

    # Tier override parsing
    tier_override = None
    if req.tier:
        tier_override = Tier(req.tier)

    # Validate explicit model_id if provided
    if req.model_id and req.model_id not in MODEL_REGISTRY:
        raise HTTPException(400, f"Unknown model_id: {req.model_id}")

    return RoutingRequest(
        tenant_id=ctx["tenant_id"],
        product=req.product,
        task_type=req.task_type,
        estimated_input_tokens=est_in,
        expected_output_tokens=est_out,
        required_capabilities=caps,
        explicit_model_id=req.model_id,
        explicit_tier=tier_override,
        max_cost_usd=req.max_cost_usd,
        quality_threshold=req.quality_threshold,
    )
