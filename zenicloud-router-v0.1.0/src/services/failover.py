"""
ZeniCloud Router - Failover Orchestrator.
Try primary → fallbacks in chain. Track which model actually served.
"""
import asyncio
import time

from src.adapters.base import (
    AuthError,
    BaseAdapter,
    CompletionRequest,
    CompletionResponse,
    ProviderError,
)
from src.adapters.factory import get_adapter
from src.core.config import settings
from src.core.logging import get_logger
from src.core.registry import ModelEntry
from src.services.routing_engine import RoutingDecision

logger = get_logger(__name__)


class FailoverExecutor:
    """Runs routing decision through primary + failover chain."""

    async def execute(
        self,
        decision: RoutingDecision,
        completion_req: CompletionRequest,
    ) -> CompletionResponse:
        chain: list[ModelEntry] = [decision.primary_model] + decision.failover_chain
        last_error: Exception | None = None
        attempts: list[dict] = []

        for idx, model in enumerate(chain):
            adapter = get_adapter(model)
            attempt_log = {"model": model.model_id, "provider": model.provider.value}
            start = time.perf_counter()
            try:
                # Update completion_req with current model
                completion_req.model = model
                resp = await asyncio.wait_for(
                    adapter.complete(completion_req),
                    timeout=settings.FAILOVER_TIMEOUT_SECONDS,
                )
                attempt_log["status"] = "success"
                attempt_log["latency_ms"] = int((time.perf_counter() - start) * 1000)
                attempts.append(attempt_log)

                # Annotate response with failover history
                if idx > 0:
                    logger.warning(
                        "failover_succeeded",
                        primary=decision.primary_model.model_id,
                        served_by=model.model_id,
                        attempts=len(attempts),
                    )
                resp.raw_response = (resp.raw_response or {}) | {"failover_attempts": attempts}
                return resp

            except AuthError as e:
                # Auth errors → don't retry this provider, escalate
                attempt_log["status"] = "auth_failed"
                attempt_log["error"] = str(e)[:200]
                attempts.append(attempt_log)
                last_error = e
                logger.error(
                    "auth_error_skipping_provider",
                    provider=model.provider.value,
                    model=model.model_id,
                )
                continue

            except (ProviderError, asyncio.TimeoutError) as e:
                attempt_log["status"] = "failed"
                attempt_log["error"] = str(e)[:200]
                attempts.append(attempt_log)
                last_error = e
                if not settings.ENABLE_FAILOVER:
                    raise
                logger.warning(
                    "primary_failed_trying_failover",
                    failed_model=model.model_id,
                    next_model=chain[idx + 1].model_id if idx + 1 < len(chain) else "none",
                )
                continue

        # All exhausted
        logger.error("all_failover_exhausted", attempts=attempts)
        raise RuntimeError(
            f"All {len(chain)} models in failover chain failed. Last error: {last_error}"
        )


failover_executor = FailoverExecutor()
