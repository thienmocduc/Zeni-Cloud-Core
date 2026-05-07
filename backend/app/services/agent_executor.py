"""
Agent Executor — runs an installed workspace agent through ZeniRouter.

Flow (`execute_agent`):
  1. Load catalog config + workspace overrides (system_prompt, model, tools)
  2. Build prompt = system_prompt + input_data formatted as a user message
  3. Call ZeniRouter via the same RoutingEngine + llm_gateway pipeline
     used by `/router/complete` (so caching, quota, failover all apply)
  4. Validate output against output_schema if defined (optional/lenient)
  5. Insert agent_runs row with input/output/duration/cost/routing_decision
  6. Increment workspace_agents.total_runs + total_cost + last_run_at
  7. Return AgentRunResult to caller

Reuses the same routing decision + cache logic so we don't duplicate provider
SDK code — the router is the single source of truth for model execution.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import billing_push
from app.services.llm_gateway import run_inference
from app.services.router.cache import cache_get, cache_set, make_cache_key
from app.services.router.quota import check_quota, increment_usage
from app.services.router.registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
)
from app.services.router.routing_engine import (
    RoutingDecision,
    RoutingEngine,
    RoutingRequest,
)

log = logging.getLogger("zeni.services.agent_executor")
_engine = RoutingEngine()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------
@dataclass
class AgentRunResult:
    run_id: int
    status: str                                  # 'success' | 'failed'
    output_text: str
    output_data: dict[str, Any]
    cost_usd: float
    duration_ms: int
    routing_decision: dict[str, Any]
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    error_message: str | None = None
    catalog_id: str = ""
    instance_name: str = ""


@dataclass
class AgentConfig:
    """Resolved agent config — catalog + workspace overrides merged."""
    workspace_agent_id: int
    workspace_id: str
    catalog_id: str
    instance_name: str
    system_prompt: str
    model: str | None
    tools_enabled: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    cost_per_run_usd: float = 0.005
    pricing_tier: str = "starter"
    custom_config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
async def load_agent_config(db: AsyncSession, workspace_agent_id: int, workspace_id: str) -> AgentConfig:
    """Load catalog row + workspace_agents overrides into a single AgentConfig."""
    row = (await db.execute(text("""
        SELECT
            wa.id            AS workspace_agent_id,
            wa.workspace_id  AS workspace_id,
            wa.catalog_id    AS catalog_id,
            wa.instance_name AS instance_name,
            wa.custom_system_prompt,
            wa.custom_model,
            wa.custom_config,
            wa.is_active,
            ac.system_prompt    AS catalog_prompt,
            ac.default_model    AS catalog_model,
            ac.tools_enabled,
            ac.output_schema,
            ac.cost_per_run_usd,
            ac.pricing_tier
        FROM workspace_agents wa
        JOIN agent_catalog ac ON ac.id = wa.catalog_id
        WHERE wa.id = :id AND wa.workspace_id = :ws
    """), {"id": workspace_agent_id, "ws": workspace_id})).mappings().first()

    if not row:
        raise ValueError(f"workspace_agent {workspace_agent_id} not found in workspace {workspace_id}")
    if not row["is_active"]:
        raise ValueError(f"workspace_agent {workspace_agent_id} is disabled")

    return AgentConfig(
        workspace_agent_id=row["workspace_agent_id"],
        workspace_id=row["workspace_id"],
        catalog_id=row["catalog_id"],
        instance_name=row["instance_name"],
        system_prompt=row["custom_system_prompt"] or row["catalog_prompt"],
        model=row["custom_model"] or row["catalog_model"],
        tools_enabled=list(row["tools_enabled"] or []),
        output_schema=row["output_schema"],
        cost_per_run_usd=float(row["cost_per_run_usd"] or 0.005),
        pricing_tier=row["pricing_tier"] or "starter",
        custom_config=row["custom_config"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _format_input_as_user_message(input_data: dict[str, Any]) -> str:
    """Render input_data dict into a user-message string. JSON for complex
    inputs; plain text if there's a single 'text'/'prompt'/'input' key."""
    if not input_data:
        return ""
    # Common single-string keys → render as-is
    for k in ("text", "prompt", "input", "query", "message"):
        if k in input_data and isinstance(input_data[k], str) and len(input_data) == 1:
            return input_data[k]
    # Otherwise render as JSON for the model
    try:
        return json.dumps(input_data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(input_data)


def _build_routing_request(cfg: AgentConfig, input_text: str) -> RoutingRequest:
    estimated_input = len(input_text) // 4 + len(cfg.system_prompt) // 4
    # Map tools → required capabilities
    caps: list[Capability] = []
    if "vector" in cfg.tools_enabled:
        caps.append(Capability.LONG_CONTEXT)
    if "ocr" in cfg.tools_enabled:
        caps.append(Capability.VISION)
    # All agents benefit from text + structured for json outputs
    caps.append(Capability.TEXT)
    if cfg.output_schema:
        caps.append(Capability.STRUCTURED)

    return RoutingRequest(
        tenant_id=cfg.workspace_id,
        product="zenicloud",
        task_type="agent_run",
        estimated_input_tokens=estimated_input,
        expected_output_tokens=2048,
        required_capabilities=list(set(caps)),
        explicit_model_id=cfg.model,
        explicit_tier=None,
    )


def _routing_decision_to_dict(decision: RoutingDecision, served_by: ModelEntry,
                              failover_count: int, actual_cost: float,
                              cache_hit: bool, reason_override: str | None = None) -> dict[str, Any]:
    return {
        "primary_model": decision.primary_model.model_id,
        "served_by": served_by.model_id,
        "tier": decision.tier.value,
        "decision_reason": reason_override or decision.decision_reason,
        "failover_count": failover_count,
        "estimated_cost_usd": round(decision.estimated_cost_usd, 6),
        "actual_cost_usd": round(actual_cost, 6),
        "cache_hit": cache_hit,
    }


def _validate_against_schema(output_text: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Lenient output validation. If schema is set, try to JSON-parse the
    output and best-effort match shape. Returns the parsed dict (or wraps
    raw text into {"text": ...} on failure)."""
    if not schema:
        return {"text": output_text}
    # Try strict JSON parse
    try:
        parsed = json.loads(output_text.strip())
        return parsed if isinstance(parsed, dict) else {"value": parsed, "raw_text": output_text}
    except json.JSONDecodeError:
        # Try to find a JSON block inside markdown fences or text
        for marker in ("```json", "```"):
            if marker in output_text:
                chunks = output_text.split(marker)
                for chunk in chunks:
                    chunk_clean = chunk.strip().lstrip("```").strip()
                    try:
                        parsed = json.loads(chunk_clean)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        continue
        # Fallback — wrap raw text
        return {"text": output_text, "_validation_warning": "could not parse JSON per output_schema"}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _insert_run_pending(
    db: AsyncSession, *, cfg: AgentConfig, input_data: dict, user_email: str | None
) -> int:
    """Insert agent_runs row (pending) and return its id."""
    row = (await db.execute(text("""
        INSERT INTO agent_runs (workspace_agent_id, workspace_id, user_email, input_data, status)
        VALUES (:wid, :ws, :email, CAST(:input AS JSONB), 'running')
        RETURNING id
    """), {
        "wid": cfg.workspace_agent_id,
        "ws": cfg.workspace_id,
        "email": user_email,
        "input": json.dumps(input_data, ensure_ascii=False),
    })).mappings().first()
    await db.commit()
    return row["id"]


async def _finalize_run(
    db: AsyncSession,
    *,
    run_id: int,
    cfg: AgentConfig,
    status: str,
    output_data: dict[str, Any] | None,
    cost_usd: float,
    duration_ms: int,
    routing_decision: dict[str, Any],
    error_message: str | None,
) -> None:
    await db.execute(text("""
        UPDATE agent_runs
           SET status = :status,
               output_data = CAST(:output AS JSONB),
               completed_at = NOW(),
               duration_ms = :ms,
               cost_usd = :cost,
               error_message = :err,
               routing_decision = CAST(:rd AS JSONB)
         WHERE id = :rid
    """), {
        "status": status,
        "output": json.dumps(output_data or {}, ensure_ascii=False),
        "ms": duration_ms,
        "cost": cost_usd,
        "err": error_message,
        "rd": json.dumps(routing_decision, ensure_ascii=False),
        "rid": run_id,
    })
    # Update workspace_agents counters (only on success)
    if status == "success":
        await db.execute(text("""
            UPDATE workspace_agents
               SET total_runs = total_runs + 1,
                   total_cost_usd = total_cost_usd + :cost,
                   last_run_at = NOW()
             WHERE id = :wid
        """), {"cost": cost_usd, "wid": cfg.workspace_agent_id})
    await db.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def execute_agent(
    db: AsyncSession,
    *,
    workspace_agent_id: int,
    workspace_id: str,
    input_data: dict[str, Any],
    user_email: str | None = None,
    cache_enabled: bool = True,
    cache_ttl_seconds: int = 300,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> AgentRunResult:
    """End-to-end: load config → quota → route → cache → execute → finalize."""
    # 1) Load config
    cfg = await load_agent_config(db, workspace_agent_id, workspace_id)

    # 2) Quota guard (workspace-level monthly ceiling, same as router)
    within, current, limit = await check_quota(db, workspace_id)
    if not within:
        raise PermissionError(
            f"Monthly quota exceeded: ${current:.4f} / ${limit:.2f}. Upgrade tier to keep running agents."
        )

    # 3) Insert run row (so we have a stable run_id even on early failure)
    run_id = await _insert_run_pending(
        db, cfg=cfg, input_data=input_data, user_email=user_email
    )

    # 4) Build routing request
    user_message = _format_input_as_user_message(input_data)
    routing_req = _build_routing_request(cfg, user_message)
    decision = _engine.decide(routing_req)

    overall_start = time.perf_counter()

    # 5) Cache lookup
    messages = [
        {"role": "system", "content": cfg.system_prompt},
        {"role": "user", "content": user_message},
    ]
    cache_key = make_cache_key(
        workspace_id, messages, decision.primary_model.model_id, temperature
    )
    if cache_enabled:
        cached = await cache_get(db, cache_key)
        if cached:
            served_id = cached.get("model_id") or decision.primary_model.model_id
            served_entry = MODEL_REGISTRY.get(served_id, decision.primary_model)
            output_text = cached["response_text"]
            output_data = _validate_against_schema(output_text, cfg.output_schema)
            routing_dict = _routing_decision_to_dict(
                decision, served_entry, failover_count=0, actual_cost=0.0,
                cache_hit=True, reason_override="cache_hit",
            )
            duration_ms = int((time.perf_counter() - overall_start) * 1000)
            await _finalize_run(
                db, run_id=run_id, cfg=cfg, status="success",
                output_data=output_data, cost_usd=0.0,
                duration_ms=duration_ms, routing_decision=routing_dict,
                error_message=None,
            )
            return AgentRunResult(
                run_id=run_id, status="success",
                output_text=output_text, output_data=output_data,
                cost_usd=0.0, duration_ms=duration_ms,
                routing_decision=routing_dict, cache_hit=True,
                input_tokens=int(cached.get("input_tokens") or 0),
                output_tokens=int(cached.get("output_tokens") or 0),
                catalog_id=cfg.catalog_id, instance_name=cfg.instance_name,
            )

    # 6) Execute via llm_gateway, walking the failover chain
    chain: list[ModelEntry] = [decision.primary_model] + decision.failover_chain
    served_by: ModelEntry = decision.primary_model
    response_text: str = ""
    failover_count = 0
    actual_input_tokens = routing_req.estimated_input_tokens
    actual_output_tokens = 0
    actual_cost = 0.0
    actual_latency_ms = 0
    last_error: Exception | None = None

    for idx, model in enumerate(chain):
        real = getattr(model, "real_model_name", None) or model.provider_model_name
        try:
            result = await run_inference(
                model=real,
                prompt=user_message,
                system=cfg.system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
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
            log.warning("agent_executor: model %s failed (idx=%d): %s", real, idx, e)
            continue

    if not response_text:
        # All models failed → finalize as 'failed'
        duration_ms = int((time.perf_counter() - overall_start) * 1000)
        err = f"All models failed: {last_error}" if last_error else "All models returned empty"
        routing_dict = _routing_decision_to_dict(
            decision, decision.primary_model, failover_count=len(chain),
            actual_cost=0.0, cache_hit=False,
            reason_override=f"all_failover_failed: {err[:120]}",
        )
        await _finalize_run(
            db, run_id=run_id, cfg=cfg, status="failed",
            output_data=None, cost_usd=0.0,
            duration_ms=duration_ms, routing_decision=routing_dict,
            error_message=err,
        )
        return AgentRunResult(
            run_id=run_id, status="failed",
            output_text="", output_data={},
            cost_usd=0.0, duration_ms=duration_ms,
            routing_decision=routing_dict, cache_hit=False,
            input_tokens=0, output_tokens=0,
            error_message=err,
            catalog_id=cfg.catalog_id, instance_name=cfg.instance_name,
        )

    if actual_latency_ms == 0:
        actual_latency_ms = int((time.perf_counter() - overall_start) * 1000)

    # 7) Cache write
    if cache_enabled:
        try:
            await cache_set(
                db, cache_key, workspace_id, response_text,
                served_by.model_id, actual_input_tokens, actual_output_tokens,
                cache_ttl_seconds,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("cache_set failed: %s", e)

    # 8) Validate output
    output_data = _validate_against_schema(response_text, cfg.output_schema)

    # 9) Quota + billing increment
    try:
        await increment_usage(db, workspace_id, actual_cost)
        await billing_push(
            db, workspace_id=workspace_id, layer="L3",
            action=f"agent.{cfg.catalog_id}.run", cost_usd=actual_cost,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("quota/billing push failed: %s", e)

    # 10) Finalize agent_runs row + workspace_agents counters
    routing_dict = _routing_decision_to_dict(
        decision, served_by, failover_count=failover_count,
        actual_cost=actual_cost, cache_hit=False,
    )
    duration_ms = actual_latency_ms or int((time.perf_counter() - overall_start) * 1000)
    await _finalize_run(
        db, run_id=run_id, cfg=cfg, status="success",
        output_data=output_data, cost_usd=actual_cost,
        duration_ms=duration_ms, routing_decision=routing_dict,
        error_message=None,
    )

    return AgentRunResult(
        run_id=run_id, status="success",
        output_text=response_text, output_data=output_data,
        cost_usd=actual_cost, duration_ms=duration_ms,
        routing_decision=routing_dict, cache_hit=False,
        input_tokens=actual_input_tokens, output_tokens=actual_output_tokens,
        catalog_id=cfg.catalog_id, instance_name=cfg.instance_name,
    )
