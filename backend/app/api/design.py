"""
Zeni Cloud Core — Design Agents API.

Endpoint chính cho hệ thống 6 KTS AI Agents (kiến trúc + nội thất + kết cấu +
MEP + BOQ + QA). Build cho Viet Contech — bao quát luôn workspace khác có nhu cầu
ngành thiết kế xây dựng.

Endpoint:
    POST /api/v1/design/orchestrate    → orchestrate 6 agents → 1 đầu ra tổng hợp
    GET  /api/v1/design/sessions/{id}  → poll session status (sau khi async hoá)

Pattern: copy từ Coder Council — adapted cho design industry.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.design_agents import DesignSession, orchestrate_project

log = logging.getLogger("zeni.api.design")
router = APIRouter(prefix="/design", tags=["design"])


# ─── Allowed style choices (lock to known LoRA models) ─────────
ALLOWED_STYLES = {
    "indochine", "japandi", "scandinavian", "minimalist", "luxury",
    "tropical", "industrial", "wabi-sabi", "boho", "eclectic",
    "modern", "art-deco", "mid-century", "contemporary",
}


class DesignOrchestrateIn(BaseModel):
    """Input cho POST /design/orchestrate.

    Brief càng chi tiết → DNA dự án càng chính xác.
    Tham khảo template:
        - "Biệt thự 3 tầng, 5 phòng ngủ, 4 thành viên gia đình"
        - "Cải tạo nhà phố 5x20m, phong cách Indochine, ngân sách 2 tỷ"
    """
    brief: str = Field(min_length=20, max_length=10_000,
                       description="Mô tả dự án bằng tiếng Việt (20-10000 ký tự)")
    style_choice: str = Field(default="indochine", max_length=32,
                              description=f"Phong cách: {sorted(ALLOWED_STYLES)}")
    num_floors: int = Field(default=2, ge=1, le=20,
                            description="Số tầng (1-20)")
    num_residents: int = Field(default=4, ge=1, le=200,
                               description="Số người cư trú/sử dụng (1-200)")
    location_province: str = Field(default="Hà Nội", max_length=64,
                                   description="Tỉnh/thành (cho TCVN gió + giá vật tư địa phương)")
    soil_data: dict[str, Any] | None = Field(default=None,
                                              description="Khảo sát địa chất (optional)")


class DesignOrchestrateOut(BaseModel):
    session_id: str
    workspace_id: str
    verdict: str
    agents_results: dict[str, Any]
    metrics: dict[str, Any]
    errors: list[str]


@router.post("/orchestrate", response_model=DesignOrchestrateOut)
async def orchestrate_design(
    payload: DesignOrchestrateIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DesignOrchestrateOut:
    """
    Run 6 KTS agents → trả về full deliverables cho project thiết kế.

    Workflow (~30-60s):
        Phase A: KTS Chief phân tích brief → DNA dự án (~5s)
        Phase B: Interior + Structural + MEP parallel (~15-25s)
        Phase C: BOQ Calculator dùng output structural + MEP (~5-10s)
        Phase D: QA Validator check toàn bộ output (~5-10s)

    Trả về `verdict`:
        - "ready_for_signoff"  → KTS chứng chỉ có thể ký
        - "needs_revision"     → có lỗi nhỏ, cần sửa
        - "major_issues"       → có lỗi nghiêm trọng, không deliver
    """
    await require_workspace_access(ws, me)
    if me.role in ("Viewer",):
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên để chạy design orchestration")

    # Validate style
    if payload.style_choice not in ALLOWED_STYLES:
        raise HTTPException(
            status_code=400,
            detail=f"style_choice không hợp lệ. Cho phép: {sorted(ALLOWED_STYLES)}",
        )

    log.info("[design.orchestrate] ws=%s actor=%s style=%s floors=%d residents=%d",
             ws, me.email, payload.style_choice, payload.num_floors, payload.num_residents)

    # Run orchestrator (async, ~30-60s — chấp nhận sync chờ vì user UX kỳ vọng "ngồi xem")
    try:
        session: DesignSession = await orchestrate_project(
            brief=payload.brief,
            workspace_id=ws,
            style_choice=payload.style_choice,
            num_floors=payload.num_floors,
            num_residents=payload.num_residents,
            location_province=payload.location_province,
            soil_data=payload.soil_data,
        )
    except Exception as e:
        log.exception("[design.orchestrate] failed ws=%s: %s", ws, e)
        raise HTTPException(status_code=502, detail=f"Design orchestration failed: {e}")

    # Audit log
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="design.orchestrate",
        target=session.session_id,
        severity="info" if session.verdict in ("ready_for_signoff",) else "warn",
        metadata={
            "verdict": session.verdict,
            "style": payload.style_choice,
            "duration_ms": session.duration_ms,
            "cost_usd": round(session.total_cost_usd, 6),
            "errors": session.errors[:5],  # cap to 5
        },
    )

    # Billing: ghi cost vào workspace (L3 AI layer)
    if session.total_cost_usd > 0:
        await billing_push(
            db,
            workspace_id=ws,
            layer="ai",
            action="design.orchestrate",
            cost_usd=session.total_cost_usd,
        )

    await db.commit()

    body = session.to_dict()
    return DesignOrchestrateOut(**body)


@router.get("/styles", response_model=list[str])
async def list_supported_styles(
    me: CurrentUser = Depends(get_current_user),
) -> list[str]:
    """Trả về danh sách style được LoRA model support."""
    _ = me  # placeholder: chỉ cần authenticated
    return sorted(ALLOWED_STYLES)


@router.get("/health")
async def design_health() -> dict[str, str]:
    """Health check cho design subsystem."""
    return {
        "status": "ok",
        "agents": "6",
        "supported_styles": str(len(ALLOWED_STYLES)),
    }
