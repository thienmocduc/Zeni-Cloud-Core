"""
Zeni Cloud Core — Specialized Design Agents API.

Endpoints (cho NexBuild + BTHome + WellKOC + Capital):
  POST /agents/{kind}/run     — One-shot full workflow, return JSON
  POST /agents/{kind}/stream  — SSE streaming workflow phases
  GET  /agents/kinds          — List supported agent kinds + capabilities
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.schemas.structured_brief import (
    StructuredArchitectureBrief, StructuredFashionBrief, StructuredInteriorBrief,
    StructuredProductBrief, StructuredStructuralBrief,
)
from app.services import design_agents, structured_pipeline
from app.services import pricing
from app.services.audit import audit_push, billing_push

log = logging.getLogger("zeni.api.agents")
router = APIRouter(prefix="/agents", tags=["agents"])


class AgentRunIn(BaseModel):
    brief: str = Field(min_length=10, max_length=8000,
                       description="Yêu cầu chi tiết của khách hàng")
    reference_image_uri: str | None = Field(default=None, max_length=20_000_000)
    reference_image_url: str | None = Field(default=None, pattern=r"^https?://.+", max_length=2048)
    generate_renders: bool = True
    n_renders: int = Field(default=2, ge=1, le=4)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(1:1|9:16|16:9|3:4|4:3)$")
    constraints: dict[str, Any] = Field(default_factory=dict,
                                        description="Budget VND, area m², deadline, etc.")


class AgentRefineIn(BaseModel):
    """Iterative refinement — augment a previous concept with user feedback."""
    previous_concept: str = Field(min_length=20, max_length=12000,
                                  description="Concept text từ run trước")
    feedback: str = Field(min_length=5, max_length=4000,
                          description="Feedback của khách: thêm cây, đổi màu...")
    n_renders: int = Field(default=2, ge=1, le=4)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(1:1|9:16|16:9|3:4|4:3)$")
    keep_concept: bool = Field(default=False,
                                description="True = chỉ regen ảnh; False = regen cả concept")


@router.get("/kinds")
async def list_kinds() -> dict:
    """Liệt kê 5 agent kinds + capabilities."""
    return {
        "agents": [
            {
                "kind": "interior",
                "name": "Interior Designer",
                "expertise": "Tropical Modern · Indochine · Japandi · Phong thủy",
                "supports_render": True,
                "use_cases": ["Phòng khách", "Phòng ngủ", "Bếp", "Văn phòng",
                              "Nhà hàng", "Café"],
                "recommended_for_workspaces": ["bthome", "anima"],
            },
            {
                "kind": "product",
                "name": "Product Designer",
                "expertise": "Industrial design · Packaging · CMF · Apple/Dieter Rams",
                "supports_render": True,
                "use_cases": ["Consumer electronics", "Bao bì", "Đồ gia dụng",
                              "Wearables"],
                "recommended_for_workspaces": ["wellkoc", "anima"],
            },
            {
                "kind": "fashion",
                "name": "Fashion Designer",
                "expertise": "Ready-to-wear · Vải Việt · Trend ASEAN/Korea/Japan",
                "supports_render": True,
                "use_cases": ["RTW collection", "Casual", "Workwear", "Streetwear"],
                "recommended_for_workspaces": ["wellkoc"],
            },
            {
                "kind": "architecture",
                "name": "Architect",
                "expertise": "Biệt thự · Nhà phố · Commercial · Bioclimatic VN",
                "supports_render": True,
                "use_cases": ["Biệt thự", "Nhà phố", "Văn phòng", "Trường học",
                              "Resort"],
                "recommended_for_workspaces": ["nexbuild", "capital"],
            },
            {
                "kind": "structural",
                "name": "Structural Engineer",
                "expertise": "BT cốt thép · Khung thép · TCVN compliance",
                "supports_render": False,
                "use_cases": ["Sizing cấu kiện", "Tải trọng", "Compliance check",
                              "Phương án móng"],
                "recommended_for_workspaces": ["nexbuild", "capital"],
            },
        ],
        "models_used": {
            "concept": "gemini-2.5-pro",
            "critique": "gemini-2.5-flash",
            "image_render": "imagen-3.0",
            "multimodal_input": "gemini-2.5-flash",
        },
    }


def _check_ai_scope(me: CurrentUser) -> None:
    if me.auth_scope and not any(s in me.auth_scope for s in ("ai", "full")):
        raise HTTPException(status_code=403, detail="Token thiếu scope 'ai'")


async def _charge_for_agent(db, ws: str, product_key: str, units: float, actor: str, ref_id: str | None):
    """Charge wallet for agent run. Raises 402 if insufficient balance."""
    try:
        return await pricing.charge(db, ws, product_key, units, actor=actor, ref_id=ref_id)
    except ValueError as e:
        raise HTTPException(status_code=402, detail=str(e))


# ─── STRUCTURED PIPELINE endpoints (Phase 1+2 — production grade) ─

_STRUCTURED_SCHEMAS = {
    "architecture": StructuredArchitectureBrief,
    "interior":     StructuredInteriorBrief,
    "product":      StructuredProductBrief,
    "fashion":      StructuredFashionBrief,
    "structural":   StructuredStructuralBrief,
}


@router.post("/{kind}/run-structured")
async def run_structured(
    kind: str,
    ws: str,
    brief: dict[str, Any],
    n_renders_per_room: int = 2,
    aspect_ratio: str = "16:9",
    enable_verify: bool = True,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Multi-stage structured pipeline:
      brief → plan (Gemini Pro) → render per room (Imagen 3) → verify (multimodal)
            → critique → output package.

    Output đẹp + chi tiết + đúng brief 90%+ thay vì 50% như endpoint /run thường.
    """
    await require_workspace_access(ws, me)
    _check_ai_scope(me)
    if kind not in _STRUCTURED_SCHEMAS:
        raise HTTPException(status_code=400, detail=f"kind không hợp lệ: {sorted(_STRUCTURED_SCHEMAS)}")

    # Validate brief với Pydantic schema
    schema_cls = _STRUCTURED_SCHEMAS[kind]
    try:
        validated = schema_cls(**brief)
        brief_dict = validated.model_dump(mode="json")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Brief structured không đúng schema: {e}")

    # Charge wallet upfront (estimate per kind)
    cost_key = "agent.run.with_2_render" if kind != "structural" else "agent.run.no_render"
    try:
        await _charge_for_agent(db, ws, cost_key, units=1.0,
                                 actor=me.email, ref_id=f"structured.{kind}")
    except HTTPException:
        raise

    # Run pipeline
    try:
        result = await structured_pipeline.run_full_pipeline(
            kind=kind, brief_dict=brief_dict,
            n_renders_per_room=n_renders_per_room,
            aspect_ratio=aspect_ratio,
            enable_verify=enable_verify,
        )
    except Exception as e:
        log.exception("structured pipeline failed")
        raise HTTPException(status_code=502, detail=f"Pipeline {kind} lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action=f"agent.{kind}.structured", target=brief_dict.get("project_name", "?")[:80],
        severity="ok",
        metadata={"input_tokens": result.total_input_tokens, "output_tokens": result.total_output_tokens,
                  "renders": result.total_images, "rooms": len(result.renders),
                  "timings_ms": result.timings_ms},
    )
    await billing_push(db, workspace_id=ws, layer="L3",
                       action=f"agent.{kind}.structured", cost_usd=result.total_cost_usd)
    await db.commit()

    return {
        "kind": result.kind,
        "plan": result.plan,
        "renders": result.renders,
        "verifications": result.verifications,
        "critique": result.critique,
        "tokens": {"input": result.total_input_tokens, "output": result.total_output_tokens},
        "renders_count": result.total_images,
        "rooms_count": len(result.renders),
        "cost_usd": round(result.total_cost_usd, 6),
        "timings_ms": result.timings_ms,
    }


@router.get("/{kind}/structured-schema")
async def get_structured_schema(kind: str) -> dict:
    """Trả về JSON Schema cho structured brief — dùng để build form UI cho khách."""
    if kind not in _STRUCTURED_SCHEMAS:
        raise HTTPException(status_code=404, detail="kind không tồn tại")
    return _STRUCTURED_SCHEMAS[kind].model_json_schema()


@router.post("/{kind}/run")
async def run_agent_endpoint(
    kind: str,
    ws: str,
    data: AgentRunIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One-shot agent run. Trả về full result (concept + renders + critique)."""
    await require_workspace_access(ws, me)
    _check_ai_scope(me)
    if kind not in design_agents.VALID_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind không hợp lệ. Cho phép: {sorted(design_agents.VALID_KINDS)}")

    req = design_agents.AgentRunRequest(
        kind=kind,
        brief=data.brief,
        reference_image_uri=data.reference_image_uri,
        reference_image_url=data.reference_image_url,
        generate_renders=data.generate_renders,
        n_renders=data.n_renders,
        aspect_ratio=data.aspect_ratio,
        constraints=data.constraints,
    )

    # Charge wallet upfront based on render count
    cost_key = "agent.run.with_2_render" if data.generate_renders else "agent.run.no_render"
    try:
        await _charge_for_agent(db, ws, cost_key, units=1.0,
                                 actor=me.email, ref_id=f"agent.{kind}.run")
    except HTTPException:
        raise

    try:
        result = await design_agents.run_agent(req)
    except Exception as e:
        log.exception("agent run failed: %s/%s", kind, ws)
        raise HTTPException(status_code=502, detail=f"Agent {kind} lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action=f"agent.{kind}.run",
        target=data.brief[:80], severity="ok",
        metadata={
            "input_tokens": result.total_input_tokens,
            "output_tokens": result.total_output_tokens,
            "renders": result.total_images,
            "timings_ms": result.timings_ms,
        },
    )
    await billing_push(db, workspace_id=ws, layer="L3",
                       action=f"agent.{kind}", cost_usd=result.total_cost_usd)
    await db.commit()

    return {
        "kind": result.kind,
        "concept": result.concept,
        "critique": result.critique,
        "reference_analysis": result.reference_analysis,
        "renders": result.renders,
        "tokens": {
            "input": result.total_input_tokens,
            "output": result.total_output_tokens,
        },
        "renders_count": result.total_images,
        "cost_usd": round(result.total_cost_usd, 6),
        "timings_ms": result.timings_ms,
    }


@router.post("/{kind}/refine")
async def refine_agent(
    kind: str,
    ws: str,
    data: AgentRefineIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Iterative refinement: lấy concept cũ + feedback → output mới.

    keep_concept=True: chỉ regen ảnh từ concept cũ (rẻ, ~2.000đ)
    keep_concept=False: regen cả concept với feedback merge (đầy đủ, ~9.000đ)
    """
    await require_workspace_access(ws, me)
    _check_ai_scope(me)
    if kind not in design_agents.VALID_KINDS:
        raise HTTPException(status_code=400, detail="kind không hợp lệ")

    if data.keep_concept:
        # Pure image regen with augmented prompt
        from app.services import ai_core
        augmented = data.previous_concept + "\n\nFEEDBACK CỦA KHÁCH (apply): " + data.feedback
        img_prompt = design_agents._render_prompt_for_kind(kind, augmented)
        try:
            res = await ai_core.generate_image(
                prompt=img_prompt,
                aspect_ratio=data.aspect_ratio,
                n=data.n_renders,
                negative_prompt=design_agents._negative_prompt_for_kind(kind),
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Render lỗi: {e}")
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action=f"agent.{kind}.refine_render", target=data.feedback[:80], severity="ok",
        )
        await billing_push(db, workspace_id=ws, layer="L3",
                           action=f"agent.{kind}.refine", cost_usd=res.get("cost_usd", 0))
        await db.commit()
        return {
            "kind": kind,
            "mode": "render-only",
            "renders": res.get("images", []),
            "renders_count": res.get("count", 0),
            "cost_usd": res.get("cost_usd", 0),
        }

    # Full refinement: regen concept + critique + render
    refined_brief = (
        f"REFINEMENT REQUEST.\nPREVIOUS CONCEPT:\n{data.previous_concept}\n\n"
        f"FEEDBACK CỦA KHÁCH:\n{data.feedback}\n\n"
        f"Hãy revise concept để phản ánh đúng feedback này, giữ tinh thần ban đầu."
    )
    req = design_agents.AgentRunRequest(
        kind=kind, brief=refined_brief,
        generate_renders=True, n_renders=data.n_renders, aspect_ratio=data.aspect_ratio,
    )
    try:
        result = await design_agents.run_agent(req)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Refine lỗi: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action=f"agent.{kind}.refine_full", target=data.feedback[:80], severity="ok",
        metadata={"input_tokens": result.total_input_tokens,
                  "output_tokens": result.total_output_tokens,
                  "renders": result.total_images},
    )
    await billing_push(db, workspace_id=ws, layer="L3",
                       action=f"agent.{kind}.refine", cost_usd=result.total_cost_usd)
    await db.commit()
    return {
        "kind": result.kind,
        "mode": "full-refine",
        "concept": result.concept,
        "critique": result.critique,
        "renders": result.renders,
        "renders_count": result.total_images,
        "tokens": {"input": result.total_input_tokens, "output": result.total_output_tokens},
        "cost_usd": round(result.total_cost_usd, 6),
    }


@router.post("/{kind}/stream")
async def stream_agent_endpoint(
    kind: str,
    ws: str,
    data: AgentRunIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE streaming agent run. Client nhận event từng phase: analyze → concept → render → critique → done."""
    await require_workspace_access(ws, me)
    _check_ai_scope(me)
    if kind not in design_agents.VALID_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind không hợp lệ. Cho phép: {sorted(design_agents.VALID_KINDS)}")

    req = design_agents.AgentRunRequest(
        kind=kind,
        brief=data.brief,
        reference_image_uri=data.reference_image_uri,
        reference_image_url=data.reference_image_url,
        generate_renders=data.generate_renders,
        n_renders=data.n_renders,
        aspect_ratio=data.aspect_ratio,
        constraints=data.constraints,
    )
    actor_email = me.email

    async def event_stream():
        try:
            async for event in design_agents.stream_agent(req):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            # Audit when done (in own session)
            from app.db.base import SessionLocal
            async with SessionLocal() as bg_db:
                await audit_push(
                    bg_db, actor=actor_email, workspace_id=ws,
                    action=f"agent.{kind}.stream", target=data.brief[:80], severity="ok",
                    metadata={"streamed": True},
                )
                await billing_push(bg_db, workspace_id=ws, layer="L3",
                                   action=f"agent.{kind}.stream", cost_usd=0.001)
                await bg_db.commit()
        except Exception as e:
            log.exception("stream_agent failed")
            yield f"data: {json.dumps({'phase': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
