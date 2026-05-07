from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Workspace ───────────────────────────────────────
class WorkspaceOut(BaseModel):
    id: str
    code: str
    name: str
    tagline: str | None = None
    color: str | None = None

    class Config:
        from_attributes = True


# ─── Project / L1 Compute ───────────────────────────
class ProjectCreateIn(BaseModel):
    # Cloud Run service name only allows [a-z0-9-], so project name follows same rule.
    # Frontend MUST convert workspace_id underscores to hyphens when prefixing.
    name: str = Field(min_length=3, max_length=48, pattern=r"^[a-z0-9][a-z0-9\-]{2,47}$")
    type: str = Field(pattern=r"^(web|api|worker|agent)$")
    runtime: str = Field(default="container", pattern=r"^(node20|python312|bun|go121|container)$")
    size: str = Field(default="s", pattern=r"^(xs|s|m|l)$")
    region: str = Field(default="us-central1", max_length=32)
    image: str = Field(min_length=3, max_length=512, description="Full Docker image URL (Artifact Registry, GCR, or Docker Hub)")
    port: int = Field(default=8080, ge=1, le=65535)
    env_vars: dict[str, str] | None = Field(default=None, description="Plain env vars")
    secrets: dict[str, str] | None = Field(default=None, description="Map ENV_NAME → secret-name[:version] from Secret Manager")
    allow_unauthenticated: bool = Field(default=True, description="Allow public access (set False for internal services)")
    git_ref: str | None = Field(default="main", max_length=64)


class ProjectOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    type: str
    runtime: str
    size: str
    region: str
    status: str
    instances: int
    cpu: str | None = None
    memory: str | None = None
    domain: str | None = None
    last_deploy: datetime | None = None
    version: str | None = None
    git_ref: str | None = None
    image: str | None = None
    cloud_run_service: str | None = None

    class Config:
        from_attributes = True


# ─── L2 Database ────────────────────────────────────
class DatabaseOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    kind: str
    description: str | None = None
    row_count: int
    dim: int | None = None
    size_bytes: int

    class Config:
        from_attributes = True


class QueryIn(BaseModel):
    qtype: str = Field(pattern=r"^(sql|vector|object)$")
    target: str = Field(min_length=1, max_length=128, description="schema/collection/bucket name")
    query: str = Field(min_length=1, max_length=4000)


# ─── L3 Agent / AI ──────────────────────────────────
class AgentCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    role: str | None = Field(default=None, max_length=128)
    model: str = Field(min_length=2, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=4000)


class AgentOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    role: str | None = None
    model: str
    calls: int
    cost_usd: Decimal
    status: str

    class Config:
        from_attributes = True


class InferenceIn(BaseModel):
    model: str = Field(min_length=2, max_length=64)
    prompt: str = Field(min_length=1, max_length=20000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    # NOTE: Gemini 2.5 uses internal "thinking" tokens that count against max_output_tokens.
    # Set reasonably high (default 2048) so end-user answers aren't truncated.
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    system: str | None = None


class InferenceOut(BaseModel):
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    output: str
    cost_usd: float
    latency_ms: int


# ─── L4 Connector / Automation ──────────────────────
class ConnectorOut(BaseModel):
    id: UUID
    workspace_id: str
    type: str
    status: str
    events_7d: int
    config: dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True


class ConnectorCreateIn(BaseModel):
    type: str = Field(min_length=2, max_length=64, description="webhook|slack|discord|email|<catalog name>")
    config: dict[str, Any] = Field(default_factory=dict, description="Connector-specific config (URL, headers, etc.)")


class EventFireIn(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    connector_id: UUID | None = Field(default=None, description="If set, dispatch only to this connector")


# ─── L5 Secrets / Identity ──────────────────────────
class SecretCreateIn(BaseModel):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    value: str = Field(min_length=1, max_length=8192)
    env: str = Field(default="prod", pattern=r"^(dev|staging|prod)$")


class SecretOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    env: str
    rotations: int
    updated_at: datetime

    class Config:
        from_attributes = True


class IdentityFlowIn(BaseModel):
    flow: str = Field(pattern=r"^(sso|rotate|invite)$")
    email: str | None = None
    resource: str | None = None


# ─── L6 Contracts / Web3 ────────────────────────────
class ContractOut(BaseModel):
    id: UUID
    workspace_id: str
    name: str
    description: str | None = None
    chain: str
    address: str | None = None
    status: str
    tx_hash: str | None = None
    deployed_at: datetime | None = None

    class Config:
        from_attributes = True


class Web3ExecIn(BaseModel):
    action: str = Field(pattern=r"^(loyalty|voucher|escrow|transfer)$")
    chain: str = Field(pattern=r"^(zeni_chain|polygon|base|arbitrum)$")
    wallet: str = Field(pattern=r"^(custodial|mpc|self)$")
    params: dict[str, Any] = Field(default_factory=dict)


# ─── Members ────────────────────────────────────────
class MemberInviteIn(BaseModel):
    email: str = Field(max_length=255)
    role: str = Field(pattern=r"^(Owner|Admin|Developer|Viewer)$")
    workspace_id: str = Field(max_length=32)


class MemberOut(BaseModel):
    id: UUID
    email: str
    name: str
    role: str
    workspace_id: str
    last_active: datetime | None = None


# ─── Audit / Billing ────────────────────────────────
class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor: str | None = None
    workspace_id: str | None = None
    action: str
    target: str | None = None
    severity: str


class BillingSummary(BaseModel):
    workspace_id: str
    total_usd: float
    by_layer: dict[str, float]
    by_action: dict[str, float]
