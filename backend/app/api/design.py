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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push, billing_push
from app.services.design_agents import DesignSession, orchestrate_project
from app.services.design_agents.brief_catalog import BRIEF_FORM, build_design_program
from app.services.design_agents.studio_page import STUDIO_HTML

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
    brief: str = Field(default="", max_length=10_000,
                       description="Mô tả tự do (tuỳ chọn nếu đã gửi 'form')")
    form: dict[str, Any] | None = Field(default=None,
                       description="Lựa chọn form có cấu trúc (xem GET /design/brief-form). "
                                   "Nếu có → build program XÁC ĐỊNH, ưu tiên hơn 'brief'.")
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
    # Imagen 3 luxury perspective renders (exterior + key rooms) as base64 data URIs.
    # Returned to the client for display; intentionally NOT persisted to the DB row.
    renders: dict[str, Any] = {"views": [], "count": 0, "cost_usd": 0.0}
    # Deterministic floor-plan geometry (Item 1): 2D SVG drawings (geometry["drawings"]) +
    # per-floor room layout + column grid + grounded structural/MEP/BOQ seeds. SVGs are small
    # (~6KB each); returned for display, intentionally NOT persisted to the DB row.
    geometry: dict[str, Any] | None = None
    # L5 Phong thủy Bát Trạch + Lỗ Ban (deterministic, $0) — cung mệnh gia chủ, đối chiếu
    # hướng nhà + từng phòng, tra Lỗ Ban cửa. None khi thiếu năm sinh gia chủ.
    fengshui: dict[str, Any] | None = None
    # DNA hash — mã băm trường khóa bất biến của dự án (spec §1.2), truy vết nhất quán.
    dna_hash: str = ""
    # Check functions deterministic (spec Chương 6): PA/CIRCULATION/CLEARANCE/BOQ traceability.
    checks: dict[str, Any] | None = None
    # Aesthetic Critic — chấm rubric 8 tiêu chí (toolkit file 03).
    aesthetic: dict[str, Any] | None = None
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

    # ── Resolve input: form lựa chọn (deterministic) ưu tiên hơn brief tự do ──
    program_override: dict[str, Any] | None = None
    if payload.form:
        prog = build_design_program(payload.form)
        run_brief = prog["brief_text"]
        run_floors = prog["num_floors"]
        run_residents = prog["num_residents"]
        run_style = prog["style_choice"]
        run_location = prog["location_province"]
        program_override = {
            "rooms_required": prog["rooms_required"],
            "layout_principles": prog["layout_principles"],
            "constraints": prog["constraints"],
            "fengshui_input": prog.get("fengshui_input"),  # L5 Bát Trạch + Lỗ Ban
        }
    else:
        run_brief = (payload.brief or "").strip()
        if len(run_brief) < 20:
            raise HTTPException(
                status_code=400,
                detail="Cần 'brief' ≥20 ký tự HOẶC 'form' lựa chọn có cấu trúc.",
            )
        run_floors = payload.num_floors
        run_residents = payload.num_residents
        run_style = payload.style_choice
        run_location = payload.location_province

    # Validate style (resolved)
    if run_style not in ALLOWED_STYLES:
        raise HTTPException(
            status_code=400,
            detail=f"style_choice không hợp lệ. Cho phép: {sorted(ALLOWED_STYLES)}",
        )

    log.info("[design.orchestrate] ws=%s actor=%s style=%s floors=%d residents=%d mode=%s",
             ws, me.email, run_style, run_floors, run_residents,
             "form" if payload.form else "brief")

    # Run orchestrator (async, ~30-60s — chấp nhận sync chờ vì user UX kỳ vọng "ngồi xem")
    try:
        session: DesignSession = await orchestrate_project(
            brief=run_brief,
            workspace_id=ws,
            style_choice=run_style,
            num_floors=run_floors,
            num_residents=run_residents,
            location_province=run_location,
            soil_data=payload.soil_data,
            program_override=program_override,
            form_answers=payload.form,  # cho PA_COMPLETENESS_CHECK
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

    # Persist design session for downstream artifacts + signoff (Task #20/#21)
    try:
        body = session.to_dict()
        await db.execute(
            text(
                """INSERT INTO design_sessions (
                    id, workspace_id, actor_email, brief, style_choice,
                    num_floors, num_residents, location_province, verdict,
                    agent_outputs, metrics, errors, duration_ms, total_cost_usd
                ) VALUES (
                    CAST(:id AS UUID), :ws, :actor, :brief, :style,
                    :nf, :nr, :loc, :verdict,
                    CAST(:agent_outputs AS JSONB), CAST(:metrics AS JSONB), CAST(:errors AS JSONB),
                    :duration_ms, :cost
                ) ON CONFLICT (id) DO NOTHING"""
            ),
            {
                "id": session.session_id,
                "ws": ws,
                "actor": me.email,
                "brief": run_brief[:4000],
                "style": run_style,
                "nf": run_floors,
                "nr": run_residents,
                "loc": run_location,
                "verdict": session.verdict,
                "agent_outputs": __import__("json").dumps(body["agents_results"], ensure_ascii=False),
                "metrics": __import__("json").dumps(body["metrics"], ensure_ascii=False),
                "errors": __import__("json").dumps(body["errors"], ensure_ascii=False),
                "duration_ms": session.duration_ms,
                "cost": round(session.total_cost_usd, 6),
            },
        )
    except Exception as persist_err:
        log.warning("[design.orchestrate] session persist failed (table may not exist yet): %s", persist_err)

    await db.commit()

    return DesignOrchestrateOut(**body)


@router.get("/styles", response_model=list[str])
async def list_supported_styles(
    me: CurrentUser = Depends(get_current_user),
) -> list[str]:
    """Trả về danh sách style được LoRA model support."""
    _ = me  # placeholder: chỉ cần authenticated
    return sorted(ALLOWED_STYLES)


@router.get("/brief-form", response_model=list[dict[str, Any]])
async def get_brief_form() -> list[dict[str, Any]]:
    """
    Catalog câu hỏi có cấu trúc cho gia chủ/KTS CHỌN (thay vì viết prompt).

    Trả về danh sách câu hỏi (single/multi/number/text) kèm mô tả chi tiết từng
    lựa chọn — giống mục PA — để client render thành form. Khi gia chủ submit
    (POST /design/orchestrate với `form`), toàn bộ 6 agents build kết quả KHỚP
    ĐÚNG các mục đã chọn.

    Public (chỉ là metadata catalog, không lộ dữ liệu người dùng). Việc chạy
    thật ở /orchestrate vẫn yêu cầu auth.
    """
    return BRIEF_FORM


@router.get("/studio", response_class=HTMLResponse)
async def design_studio() -> HTMLResponse:
    """
    Studio thiết kế — trang HTML hướng dẫn gia chủ CHỌN nhu cầu (mô tả chi tiết)
    + ô nhập cá nhân hoá, rồi gọi /orchestrate để 6 agents ra mặt bằng + render
    khớp đúng lựa chọn. Public HTML (đăng nhập ngay trong trang để lấy token).
    """
    return HTMLResponse(content=STUDIO_HTML)


@router.get("/health")
async def design_health() -> dict[str, str]:
    """Health check cho design subsystem."""
    return {
        "status": "ok",
        "agents": "6",
        "supported_styles": str(len(ALLOWED_STYLES)),
    }
