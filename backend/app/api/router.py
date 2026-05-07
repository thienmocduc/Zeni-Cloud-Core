"""
ZeniRouter API — smart multi-model AI router.

Endpoints:
  POST /api/v1/router/complete  — main routing + execution + cache + quota
  POST /api/v1/router/route     — preview routing decision (no execution)
  GET  /api/v1/router/models    — list available registered models
  GET  /api/v1/router/quota     — current quota usage for workspace

Wires the registry + routing engine + cache + quota services together with the
existing `llm_gateway.run_inference()` adapter so we never duplicate provider
SDK logic. Registry ids that aren't yet GA fall onto a real_model_name.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.llm_gateway import run_inference
from app.services.router.cache import cache_get, cache_set, make_cache_key
from app.services.router.quota import check_quota, increment_usage
from app.services.router.registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
    Tier,
)
from app.services.router.routing_engine import RoutingDecision, RoutingEngine, RoutingRequest

router = APIRouter(prefix="/router", tags=["router"])
engine = RoutingEngine()


# ---------------------------------------------------------------------------
# Pydantic v2 schemas
# ---------------------------------------------------------------------------
class CompleteIn(BaseModel):
    messages: list[dict]                            # [{role, content}]
    task_type: str = Field(default="qa_simple")
    product: str = Field(default="zenicloud")
    max_tokens: int = Field(default=2048, ge=1, le=32_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    explicit_model: str | None = None
    explicit_tier: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    cache_enabled: bool = Field(default=True)
    cache_ttl_seconds: int = Field(default=300, ge=10, le=86_400)


class CompleteOut(BaseModel):
    text: str
    routing: dict
    usage: dict
    cache_hit: bool
    latency_ms: int


class RouteOut(BaseModel):
    primary_model: str
    served_by: str
    tier: str
    decision_reason: str
    failover_chain: list[str]
    estimated_cost_usd: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_workspace_id(workspace_code_or_id: str) -> str:
    """The router exposes workspace IDs directly (matches existing apis like
    /ai/complete)."""
    return workspace_code_or_id


async def _get_workspace_id(db: AsyncSession, ws: str) -> str:
    """Accept either a workspace code or a workspace id; return the canonical id."""
    row = (await db.execute(text("""
        SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1
    """), {"ws": ws})).mappings().first()
    if not row:
        raise HTTPException(404, "workspace not found")
    return row["id"]


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


def _build_routing_request(
    workspace_id: str, payload: CompleteIn
) -> RoutingRequest:
    estimated_input = sum(len(str(m.get("content", ""))) // 4 for m in payload.messages)
    return RoutingRequest(
        tenant_id=workspace_id,
        product=payload.product,
        task_type=payload.task_type,
        estimated_input_tokens=estimated_input,
        expected_output_tokens=payload.max_tokens,
        required_capabilities=_coerce_caps(payload.required_capabilities),
        explicit_model_id=payload.explicit_model,
        explicit_tier=_coerce_tier(payload.explicit_tier),
    )


def _routing_dict(decision: RoutingDecision, served_by: ModelEntry,
                  failover_count: int, actual_cost: float, reason_override: str | None = None) -> dict[str, Any]:
    return {
        "primary_model": decision.primary_model.model_id,
        "served_by": served_by.model_id,
        "tier": decision.tier.value,
        "decision_reason": reason_override or decision.decision_reason,
        "failover_count": failover_count,
        "estimated_cost_usd": round(decision.estimated_cost_usd, 6),
        "actual_cost_usd": round(actual_cost, 6),
    }


def _join_messages_for_prompt(messages: list[dict]) -> tuple[str, str | None]:
    """Adapter glue — flatten chat-style messages into the prompt/system pair
    that `run_inference()` expects. Last user message becomes the prompt; all
    `system` messages become the system instruction."""
    system_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    system = "\n".join(p for p in system_parts if p) or None

    # Last non-system message is the prompt; concatenate prior turns ahead of it
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
    prompt = "\n\n".join(convo) if convo else ""
    return prompt, system


async def _log_usage(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_email: str | None,
    payload: CompleteIn,
    decision: RoutingDecision,
    served_by_model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    cache_hit: bool,
    failover_count: int,
) -> None:
    await db.execute(text("""
        INSERT INTO router_usage_log
            (workspace_id, user_email, product, task_type,
             primary_model, served_by_model, tier,
             input_tokens, output_tokens, cost_usd,
             latency_ms, cache_hit, failover_count, decision_reason)
        VALUES
            (:ws, :email, :prod, :tt,
             :pm, :sb, :tier,
             :it, :ot, :cost,
             :lat, :ch, :fc, :reason)
    """), {
        "ws": workspace_id, "email": user_email, "prod": payload.product, "tt": payload.task_type,
        "pm": decision.primary_model.model_id, "sb": served_by_model, "tier": decision.tier.value,
        "it": input_tokens, "ot": output_tokens, "cost": cost_usd,
        "lat": latency_ms, "ch": cache_hit, "fc": failover_count, "reason": decision.decision_reason,
    })
    await db.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/complete", response_model=CompleteOut)
async def complete(
    payload: CompleteIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompleteOut:
    """Main routing endpoint. Routes -> caches -> executes -> tracks usage."""
    # 1) Workspace + permission
    workspace_id = await _get_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    # 2) Quota check
    within, current, limit = await check_quota(db, workspace_id)
    if not within:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly quota exceeded: ${current:.4f} / ${limit:.2f}. Upgrade tier.",
        )

    # 3) Routing decision
    routing_req = _build_routing_request(workspace_id, payload)
    decision = engine.decide(routing_req)
    estimated_input = routing_req.estimated_input_tokens

    # 4) Cache lookup (keyed off the *primary* model so explicit overrides cache predictably)
    cache_key = make_cache_key(
        workspace_id, payload.messages, decision.primary_model.model_id, payload.temperature
    )
    if payload.cache_enabled:
        cached = await cache_get(db, cache_key)
        if cached:
            served_id = cached.get("model_id") or decision.primary_model.model_id
            served_entry = MODEL_REGISTRY.get(served_id, decision.primary_model)
            await _log_usage(
                db,
                workspace_id=workspace_id,
                user_email=me.email,
                payload=payload,
                decision=decision,
                served_by_model=served_id,
                input_tokens=int(cached.get("input_tokens") or 0),
                output_tokens=int(cached.get("output_tokens") or 0),
                cost_usd=0.0,
                latency_ms=0,
                cache_hit=True,
                failover_count=0,
            )
            return CompleteOut(
                text=cached["response_text"],
                routing=_routing_dict(
                    decision, served_entry, failover_count=0, actual_cost=0.0,
                    reason_override="cache_hit",
                ),
                usage={
                    "input_tokens": int(cached.get("input_tokens") or 0),
                    "output_tokens": int(cached.get("output_tokens") or 0),
                    "total_tokens": int((cached.get("input_tokens") or 0) + (cached.get("output_tokens") or 0)),
                },
                cache_hit=True,
                latency_ms=0,
            )

    # 5) Execute via existing llm_gateway, walking the failover chain
    prompt, system = _join_messages_for_prompt(payload.messages)
    chain: list[ModelEntry] = [decision.primary_model] + decision.failover_chain

    served_by: ModelEntry = decision.primary_model
    response_text: str = ""
    failover_count = 0
    actual_input_tokens = estimated_input
    actual_output_tokens = 0
    actual_cost = 0.0
    actual_latency_ms = 0
    last_error: Exception | None = None

    overall_start = time.perf_counter()
    for idx, model in enumerate(chain):
        real = getattr(model, "real_model_name", None) or model.provider_model_name
        try:
            result = await run_inference(
                model=real,
                prompt=prompt,
                system=system,
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
            )
            response_text = result.output
            served_by = model
            failover_count = idx
            actual_input_tokens = result.input_tokens
            actual_output_tokens = result.output_tokens
            actual_cost = float(result.cost_usd)
            actual_latency_ms = result.latency_ms
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue

    if not response_text:
        raise HTTPException(503, f"All models failed: {last_error}")

    if actual_latency_ms == 0:
        actual_latency_ms = int((time.perf_counter() - overall_start) * 1000)

    # 6) Cache write (under primary model id so subsequent identical calls hit)
    if payload.cache_enabled:
        await cache_set(
            db,
            cache_key,
            workspace_id,
            response_text,
            served_by.model_id,
            actual_input_tokens,
            actual_output_tokens,
            payload.cache_ttl_seconds,
        )

    # 7) Quota + audit + billing + usage log
    await increment_usage(db, workspace_id, actual_cost)
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="router.complete", target=served_by.model_id, severity="ok",
        metadata={
            "primary_model": decision.primary_model.model_id,
            "served_by": served_by.model_id,
            "tier": decision.tier.value,
            "failover_count": failover_count,
            "input_tokens": actual_input_tokens,
            "output_tokens": actual_output_tokens,
        },
    )
    await billing_push(
        db, workspace_id=workspace_id, layer="L3",
        action="router.complete", cost_usd=actual_cost,
    )
    await _log_usage(
        db,
        workspace_id=workspace_id,
        user_email=me.email,
        payload=payload,
        decision=decision,
        served_by_model=served_by.model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_usd=actual_cost,
        latency_ms=actual_latency_ms,
        cache_hit=False,
        failover_count=failover_count,
    )

    return CompleteOut(
        text=response_text,
        routing=_routing_dict(
            decision, served_by, failover_count=failover_count, actual_cost=actual_cost,
        ),
        usage={
            "input_tokens": actual_input_tokens,
            "output_tokens": actual_output_tokens,
            "total_tokens": actual_input_tokens + actual_output_tokens,
        },
        cache_hit=False,
        latency_ms=actual_latency_ms,
    )


@router.post("/route", response_model=RouteOut)
async def preview_route(
    payload: CompleteIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RouteOut:
    """Preview the routing decision without executing the model."""
    workspace_id = await _get_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    routing_req = _build_routing_request(workspace_id, payload)
    decision = engine.decide(routing_req)
    return RouteOut(
        primary_model=decision.primary_model.model_id,
        served_by=decision.primary_model.model_id,
        tier=decision.tier.value,
        decision_reason=decision.decision_reason,
        failover_chain=[m.model_id for m in decision.failover_chain],
        estimated_cost_usd=round(decision.estimated_cost_usd, 6),
    )


@router.get("/models")
async def list_models(tier: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """List registered router models. Public — no auth required."""
    models = list(MODEL_REGISTRY.values())
    if tier:
        models = [m for m in models if m.tier.value == tier]
    return {
        "models": [
            {
                "id": m.model_id,
                "display_name": m.display_name,
                "provider": m.provider.value,
                "tier": m.tier.value,
                "input_price_per_mtok": m.input_price_per_mtok,
                "output_price_per_mtok": m.output_price_per_mtok,
                "context_window": m.context_window,
                "quality_score": m.quality_score,
                "capabilities": [c.value for c in m.capabilities],
                "failover_to": list(m.failover_to),
                "available_via": list(m.available_via),
            }
            for m in models
        ]
    }


@router.get("/quota")
async def get_quota(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return current quota status for the workspace."""
    workspace_id = await _get_workspace_id(db, ws)
    await require_workspace_access(workspace_id, me)

    within, current, limit = await check_quota(db, workspace_id)
    return {
        "within_quota": within,
        "current_usage_usd": current,
        "monthly_quota_usd": limit,
        "remaining_usd": max(0.0, limit - current),
        "percent_used": round(current / limit * 100, 1) if limit > 0 else 0.0,
    }
