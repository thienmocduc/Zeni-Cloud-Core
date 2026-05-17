from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.db.models import Agent
from app.schemas.resources import AgentCreateIn, AgentOut, InferenceIn, InferenceOut
from app.services.audit import audit_push, billing_push
from app.services.llm_gateway import list_available_models, run_inference

router = APIRouter(prefix="/ai", tags=["ai"])

# L3 fail-fast: hard cap independent of provider; schema also caps max_tokens<=32768.
_AI_MAX_TOKENS_HARD_LIMIT = 32768


def _validate_ai_request(model: str, max_tokens: int) -> None:
    """Pre-flight checks so users get 422 with hint instead of silent mock/500.

    Mirrors L1 pattern in projects.py: validate sync before kicking off provider call.
    """
    known = {m["id"] for m in list_available_models()}
    if model not in known:
        sample = ", ".join(sorted(known)[:5])
        raise HTTPException(
            status_code=422,
            detail=(
                f"Model '{model}' không support. "
                f"Dùng GET /api/v1/ai/models để list models hợp lệ "
                f"(ví dụ: {sample})."
            ),
        )
    if max_tokens > _AI_MAX_TOKENS_HARD_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"max_tokens={max_tokens} vượt giới hạn {_AI_MAX_TOKENS_HARD_LIMIT}.",
        )


@router.get("/models")
async def get_models(me: CurrentUser = Depends(get_current_user)) -> list[dict]:
    return list_available_models()


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        select(Agent).where(Agent.workspace_id == ws).order_by(Agent.created_at.desc())
    )).scalars().all()
    return [AgentOut.model_validate(r) for r in rows]


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    ws: str,
    data: AgentCreateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được tạo agent")

    agent = Agent(
        workspace_id=ws,
        name=data.name,
        role=data.role,
        model=data.model,
        system_prompt=data.system_prompt,
        status="active",
    )
    db.add(agent)
    await audit_push(db, actor=me.email, workspace_id=ws, action="ai.agent_create", target=data.name, severity="ok")
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}/toggle", response_model=AgentOut)
async def toggle_agent(
    ws: str,
    agent_id: UUID,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    await require_workspace_access(ws, me)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == ws))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    agent.status = "paused" if agent.status == "active" else "active"
    await audit_push(db, actor=me.email, workspace_id=ws, action="ai.agent_toggle", target=agent.name, severity="info",
                     metadata={"new_status": agent.status})
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


@router.post("/complete", response_model=InferenceOut)
async def complete(
    ws: str,
    data: InferenceIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InferenceOut:
    await require_workspace_access(ws, me)
    _validate_ai_request(data.model, data.max_tokens)

    # ── Quota check pre-flight (best-effort: dùng max_tokens làm proxy) ──
    # Vision tokens dùng cho multimodal (Gemini Pro với image input).
    # Reasoning tokens dùng cho text-only.
    from app.services.quota_check import check_quota, increment_usage
    is_vision = bool(getattr(data, "image_url", None) or getattr(data, "image_base64", None))
    quota_kind = "vision" if is_vision else "reasoning"
    await check_quota(db, ws, kind=quota_kind, amount=data.max_tokens)

    result = await run_inference(
        model=data.model,
        prompt=data.prompt,
        system=data.system,
        temperature=data.temperature,
        max_tokens=data.max_tokens,
    )

    # Increment usage actual tokens used (sau success)
    total_tokens = (result.input_tokens or 0) + (result.output_tokens or 0)
    await increment_usage(db, ws, kind=quota_kind, amount=total_tokens)

    await audit_push(db, actor=me.email, workspace_id=ws, action="ai.inference", target=data.model, severity="ok",
                     metadata={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
    await billing_push(db, workspace_id=ws, layer="L3", action="ai.inference", cost_usd=result.cost_usd)
    await db.commit()

    return InferenceOut(
        model=result.model,
        provider=result.provider,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        output=result.output,
        cost_usd=round(result.cost_usd, 8),
        latency_ms=result.latency_ms,
    )
