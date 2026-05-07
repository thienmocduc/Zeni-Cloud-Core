"""
ZeniCloud Router - HTTP request/response schemas.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., max_length=500_000)  # 500KB cap per message


class CompletionRequestSchema(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """POST /v1/route → /v1/complete body."""
    # Caller identity
    tenant_id: str = Field(..., min_length=1, max_length=64)
    product: str = Field(..., min_length=1, max_length=32, examples=["zenimake", "zenilaw", "zeniipo"])
    task_type: str = Field(..., min_length=1, max_length=64, examples=["code_generate", "rag_answer"])

    # Generation
    messages: list[Message] = Field(..., min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=50_000)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # Optional routing overrides
    model_id: str | None = Field(default=None, description="Bypass auto-routing")
    tier: Literal["fast", "balanced", "frontier"] | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    max_cost_usd: float | None = Field(default=None, ge=0.0, le=100.0)
    quality_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    # Hint for cost estimation
    estimated_input_tokens: int | None = Field(default=None, ge=0, le=2_000_000)
    expected_output_tokens: int | None = Field(default=None, ge=0, le=8192)


class RoutingMetadata(BaseModel):
    primary_model: str
    served_by: str  # may differ from primary if failover happened
    tier: str
    estimated_cost_usd: float
    actual_cost_usd: float
    decision_reason: str
    failover_count: int = 0


class CompletionResponseSchema(BaseModel):
    text: str
    routing: RoutingMetadata
    usage: dict
    latency_ms: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    mock_mode: bool
    providers: dict[str, bool]


class ModelListItem(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    display_name: str
    provider: str
    tier: str
    input_price_per_mtok: float
    output_price_per_mtok: float
    context_window: int
    quality_score: float
    capabilities: list[str]
