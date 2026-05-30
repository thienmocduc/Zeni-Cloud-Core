"""
Orchestrator — coordinator 6 KTS Design Agents.

Workflow:
  1. KTSChiefAgent.analyze_brief → DNA dự án
  2. (parallel) InteriorDesigner + StructuralEngineer + MEPEngineer
  3. BOQCalculator (after structural + MEP done)
  4. QAValidator (final check all outputs)
  5. Return DesignSession with all results

Pattern: copy từ Zeni Coder Council (deliberate function) — adapted cho design industry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .agents import (
    AestheticCriticAgent,
    BOQCalculatorAgent,
    InteriorDesignerAgent,
    KTSChiefAgent,
    MEPEngineerAgent,
    QAValidatorAgent,
    StructuralEngineerAgent,
    AgentResult,
)
from .render import render_concept
from .geometry import compute_geometry
from .elevation import build_elevations_sections
from .fengshui import analyze_fengshui
from . import checks as design_checks

log = logging.getLogger("zeni.design_orchestrator")


def _lean_geometry(geom: dict) -> dict:
    """Strip base64 SVGs so the geometry can be embedded in LLM prompts cheaply (numbers only)."""
    if not geom:
        return {}
    floors = [{k: v for k, v in f.items() if k != "svg_data_uri"} for f in geom.get("floors", [])]
    return {
        "footprint": geom.get("footprint"),
        "num_floors": geom.get("num_floors"),
        "total_gfa_m2": geom.get("total_gfa_m2"),
        "building_height_m": geom.get("building_height_m"),
        "floors": floors,
        "structural_seed": geom.get("structural_seed"),
        "mep_seed": geom.get("mep_seed"),
        "boq_seed": geom.get("boq_seed"),
    }


@dataclass
class DesignSession:
    """Full result of one design project orchestration."""
    session_id: str
    workspace_id: str
    brief: str
    style_choice: str = "indochine"  # default
    num_floors: int = 2
    num_residents: int = 4
    location_province: str = "Hà Nội"

    # Results from each agent
    kts_chief_result: Optional[AgentResult] = None
    interior_result: Optional[AgentResult] = None
    structural_result: Optional[AgentResult] = None
    mep_result: Optional[AgentResult] = None
    boq_result: Optional[AgentResult] = None
    qa_result: Optional[AgentResult] = None

    # Imagen 3 luxury renders (Item 2) — not an AgentResult; raw dict from render_concept
    render_result: Optional[dict] = None

    # Deterministic floor-plan geometry (Item 1) — 2D SVG drawings + structural/MEP/BOQ seeds
    geometry_result: Optional[dict] = None

    # L5 Phong thủy — Bát Trạch cung mệnh + Lỗ Ban (deterministic, $0, no-LLM)
    fengshui_result: Optional[dict] = None

    # Check functions deterministic (spec Chương 6): PA/CIRCULATION/CLEARANCE/BOQ traceability
    checks_result: Optional[dict] = None

    # Aesthetic Critic — chấm rubric 8 tiêu chí (toolkit file 03)
    aesthetic_result: Optional[AgentResult] = None

    # DNA dự án — mã băm các trường khóa (spec §1.2). Mọi output đối chiếu hash này.
    dna_hash: str = ""

    # Aggregate
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_ms: int = 0
    verdict: str = "pending"  # pending | ready_for_signoff | needs_revision | major_issues
    errors: list[str] = field(default_factory=list)

    def _public_geometry(self) -> Optional[dict]:
        """Floor-plan geometry for the API: SVGs kept only in drawings[]; floors[] = numbers."""
        g = self.geometry_result
        if not g:
            return None
        floors = [{k: v for k, v in f.items() if k != "svg_data_uri"} for f in g.get("floors", [])]
        return {**g, "floors": floors}

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "verdict": self.verdict,
            "agents_results": {
                "kts_chief": self.kts_chief_result and self.kts_chief_result.output,
                "interior": self.interior_result and self.interior_result.output,
                "structural": self.structural_result and self.structural_result.output,
                "mep": self.mep_result and self.mep_result.output,
                "boq": self.boq_result and self.boq_result.output,
                "qa": self.qa_result and self.qa_result.output,
            },
            # Top-level (NOT inside agents_results) so the API can return the base64
            # images to the client WITHOUT persisting ~5MB to design_sessions JSONB.
            "renders": self.render_result or {"views": [], "count": 0, "cost_usd": 0.0},
            # Floor-plan geometry (Item 1) — SVG drawings in geometry["drawings"], numbers in floors[].
            "geometry": self._public_geometry(),
            # L5 Phong thủy Bát Trạch + Lỗ Ban (deterministic) + DNA hash (spec §1.2).
            "fengshui": self.fengshui_result,
            "dna_hash": self.dna_hash,
            # Check functions deterministic (spec Chương 6) + Aesthetic Critic rubric (file 03).
            "checks": self.checks_result,
            "aesthetic": self.aesthetic_result.output if self.aesthetic_result else None,
            "metrics": {
                "total_cost_usd": round(self.total_cost_usd, 6),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "duration_ms": self.duration_ms,
                "render_count": (self.render_result or {}).get("count", 0),
                "render_cost_usd": round((self.render_result or {}).get("cost_usd", 0.0), 6),
            },
            "errors": self.errors,
        }


async def orchestrate_project(
    *,
    brief: str,
    workspace_id: str = "vietcontech",
    style_choice: str = "indochine",
    num_floors: int = 2,
    num_residents: int = 4,
    location_province: str = "Hà Nội",
    soil_data: Optional[dict] = None,
    program_override: Optional[dict] = None,
    form_answers: Optional[dict] = None,
) -> DesignSession:
    """
    Orchestrate 6 KTS agents to deliver full-stack design project.

    Returns DesignSession với complete results — ready for KTS chứng chỉ ký.

    ``program_override`` (từ form lựa chọn có cấu trúc → brief_catalog.build_design_program):
    khi có, ÉP rooms_required / layout_principles / constraints vào DNA — bảo đảm
    layout khớp 100% lựa chọn của khách, KHÔNG để LLM tự đoán công năng.
    """
    session = DesignSession(
        session_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        brief=brief,
        style_choice=style_choice,
        num_floors=num_floors,
        num_residents=num_residents,
        location_province=location_province,
    )
    t_start = time.perf_counter()

    log.info("[orchestrator] start session=%s ws=%s", session.session_id, workspace_id)

    # ─── PHASE A: KTS Chief analyze brief → DNA ───────────
    kts = KTSChiefAgent()
    session.kts_chief_result = await kts.analyze_brief(brief, workspace_id=workspace_id)
    if not session.kts_chief_result.success:
        session.errors.append(f"kts_chief: {session.kts_chief_result.error}")
        session.verdict = "major_issues"
        session.duration_ms = int((time.perf_counter() - t_start) * 1000)
        return session

    dna = session.kts_chief_result.output.get("dna", {})
    if session.kts_chief_result.output.get("verdict") == "reject_brief":
        session.verdict = "needs_revision"
        session.duration_ms = int((time.perf_counter() - t_start) * 1000)
        return session

    # ── ÉP program từ form lựa chọn (deterministic) — khách chọn gì, ra đúng cái đó ──
    # LLM Chief vẫn giữ phần "mềm": phong thuỷ, palette, budget_breakdown, feng_shui.
    # Nhưng công năng (số phòng, gian thờ, gara…) lấy từ override → KHÔNG cho LLM đổi.
    if program_override:
        if program_override.get("rooms_required"):
            dna["rooms_required"] = program_override["rooms_required"]
        merged_principles = list(program_override.get("layout_principles") or [])
        for p in (dna.get("layout_principles") or []):
            if p not in merged_principles:
                merged_principles.append(p)
        if merged_principles:
            dna["layout_principles"] = merged_principles
        if program_override.get("constraints"):
            base_c = dna.get("constraints") or []
            dna["constraints"] = list(program_override["constraints"]) + list(base_c)
        log.info("[orchestrator] program override applied: %d rooms locked from form",
                 len(program_override.get("rooms_required") or []))

    floor_plan_seed = {
        "rooms_required": dna.get("rooms_required", []),
        "layout_principles": dna.get("layout_principles", []),
    }

    # ─── PHASE A2: deterministic floor-plan geometry ($0, never raises) ───
    # Packs the room program into a real footprint → 2D SVG drawings + column grid/spans
    # + grounded structural/MEP/BOQ seeds. This is the geometric basis the engineers were
    # missing (previously they guessed from a bare room list).
    geom_lean: dict = {}
    try:
        geometry = compute_geometry(
            rooms_required=dna.get("rooms_required", []),
            num_floors=num_floors,
            layout_principles=dna.get("layout_principles", []),
            constraints=dna.get("constraints"),
        )
        session.geometry_result = geometry
        # L1.b/c — mặt đứng 4 hướng + mặt cắt qua thang (deterministic SVG, $0). Gắn vào
        # geometry → chảy qua API (_public_geometry); KHÔNG vào geom_lean nên không tốn prompt.
        try:
            es = build_elevations_sections(geometry, style_choice)
            geometry["elevations"] = es.get("elevations", [])
            geometry["sections"] = es.get("sections", [])
            log.info("[orchestrator] elevations: %d mặt đứng + %d mặt cắt",
                     len(geometry["elevations"]), len(geometry["sections"]))
        except Exception as _ee:  # phải KHÔNG bao giờ làm hỏng orchestration
            log.warning("[orchestrator] elevations failed: %s", _ee)
        geom_lean = _lean_geometry(geometry)
        floor_plan_seed["geometry"] = geom_lean  # grounds Structural + MEP prompts
        log.info(
            "[orchestrator] geometry ok: %s floors GFA %.0fm² grid %s cols",
            geometry.get("num_floors"), geometry.get("total_gfa_m2", 0),
            (geometry.get("structural_seed") or {}).get("column_grid", {}).get("count"),
        )
    except Exception as e:  # geometry must NEVER break orchestration
        log.warning("[orchestrator] geometry failed: %s", e)
        session.errors.append(f"geometry: {e}")

    # ─── PHASE A3: L5 Phong thủy Bát Trạch + Lỗ Ban ($0, no-LLM, never raises) ───
    # Tính cung mệnh gia chủ, đối chiếu hướng nhà + từng phòng theo Bát Trạch, tra Lỗ Ban
    # cửa. Inject hướng dẫn vào DNA → Chief/QA nhất quán; deterministic nên LUÔN đúng chuẩn.
    fs_input = (program_override or {}).get("fengshui_input") or {}
    try:
        session.fengshui_result = analyze_fengshui(
            birth_year=int(fs_input.get("birth_year") or 0),
            gender=str(fs_input.get("gender") or "nam"),
            lot_orientation=str(fs_input.get("lot_orientation") or "nam"),
            geometry=session.geometry_result,
        )
        fr = session.fengshui_result
        cm = fr.get("cung_menh") or {}
        dna["fengshui"] = {
            "enabled": fr.get("enabled"),
            "facing": fr.get("facing"),
            "cung_menh": cm.get("cung"),
            "menh_group": cm.get("menh_group"),
            "huong_tot": cm.get("huong_tot"),
            "facing_verdict": fr.get("facing_verdict"),
            "room_summary": fr.get("room_summary"),
        }
        merged_fs = list(dna.get("layout_principles") or [])
        for p in (fr.get("principles") or []):
            if p not in merged_fs:
                merged_fs.append(p)
        dna["layout_principles"] = merged_fs
        if fr.get("warnings"):
            dna["fengshui_warnings"] = fr["warnings"]
        log.info("[orchestrator] fengshui enabled=%s facing=%s cung=%s rooms=%s",
                 fr.get("enabled"), fr.get("facing"), cm.get("cung"), fr.get("room_summary"))
    except Exception as e:  # fengshui must NEVER break orchestration
        log.warning("[orchestrator] fengshui failed: %s", e)
        session.errors.append(f"fengshui: {e}")

    # ─── DNA hash (spec §1.2) — băm trường khóa bất biến để truy vết nhất quán ───
    try:
        locked = {
            "rooms": [r.get("name") for r in dna.get("rooms_required", [])],
            "principles": dna.get("layout_principles", []),
            "constraints": dna.get("constraints", []),
            "style": style_choice, "num_floors": num_floors,
            "facing": (session.fengshui_result or {}).get("facing"),
            "cung": (dna.get("fengshui") or {}).get("cung_menh"),
        }
        session.dna_hash = hashlib.sha256(
            _json.dumps(locked, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        session.dna_hash = ""

    # ─── PHASE B: Interior + Structural + MEP parallel ────
    interior_agent = InteriorDesignerAgent()
    structural_agent = StructuralEngineerAgent()
    mep_agent = MEPEngineerAgent()

    interior_task = interior_agent.design_style(
        dna=dna, style=style_choice, workspace_id=workspace_id,
    )
    structural_task = structural_agent.calculate_structure(
        floor_plan=floor_plan_seed,
        num_floors=num_floors,
        soil_data=soil_data or {"type": "đất sét", "bearing_capacity_kPa": 100},
        workspace_id=workspace_id,
    )
    mep_task = mep_agent.design_mep(
        floor_plan=floor_plan_seed,
        num_residents=num_residents,
        workspace_id=workspace_id,
    )

    (
        session.interior_result,
        session.structural_result,
        session.mep_result,
    ) = await asyncio.gather(
        interior_task, structural_task, mep_task, return_exceptions=False,
    )

    for ar in (session.interior_result, session.structural_result, session.mep_result):
        if not ar.success:
            session.errors.append(f"{ar.agent_role}: {ar.error}")

    # ─── PHASE C: BOQ Calculator + Imagen 3 renders (parallel) ────
    # BOQ depends on structural+MEP (done); render depends on interior (done).
    # Independent → run together so renders add ~0s wall-clock (overlap BOQ).
    boq_agent = BOQCalculatorAgent()
    # Lead architecture_spec with grounded quantities so they survive the prompt's [:2000]
    # truncation → BOQ bóc tách from REAL geometry, not hallucinated volumes.
    boq_arch_spec = session.kts_chief_result.output
    if geom_lean:
        boq_arch_spec = {
            "grounded_quantities_from_geometry": geom_lean.get("boq_seed"),
            "footprint": geom_lean.get("footprint"),
            "total_gfa_m2": geom_lean.get("total_gfa_m2"),
            "building_height_m": geom_lean.get("building_height_m"),
            **session.kts_chief_result.output,
        }
    boq_task = boq_agent.calculate_boq(
        architecture_spec=boq_arch_spec,
        structural_spec=session.structural_result.output,
        mep_spec=session.mep_result.output,
        location_province=location_province,
        workspace_id=workspace_id,
    )
    render_task = render_concept(
        dna=dna,
        interior_spec=session.interior_result.output if session.interior_result else {},
        style=style_choice,
        num_floors=num_floors,
        num_residents=num_residents,
        location_province=location_province,
        workspace_id=workspace_id,
        geometry=session.geometry_result,  # ground renders in the REAL floor plan
    )
    session.boq_result, session.render_result = await asyncio.gather(
        boq_task, render_task, return_exceptions=False,
    )
    if not session.boq_result.success:
        session.errors.append(f"boq: {session.boq_result.error}")
    if not session.render_result or session.render_result.get("count", 0) == 0:
        session.errors.append("render: no images generated")

    # ─── Check functions deterministic (spec Chương 6, $0, no-LLM) ───
    try:
        session.checks_result = design_checks.run_all(
            form=form_answers,
            geometry=session.geometry_result,
            boq_output=session.boq_result.output if session.boq_result else None,
            grounded=bool(geom_lean),
        )
        log.info("[orchestrator] checks: %s", (session.checks_result or {}).get("summary"))
    except Exception as e:
        log.warning("[orchestrator] checks failed: %s", e)
        session.errors.append(f"checks: {e}")

    # ─── PHASE D: QA Validator ‖ Aesthetic Critic (parallel) ──────────────
    # Phong thủy + checks bản GỌN (deterministic, authoritative) feed QA → verdict phản ánh
    # Bát Trạch + gate THẬT, không để LLM bịa. Critic chấm rubric 8 tiêu chí trên phương án nội thất.
    fr = session.fengshui_result or {}
    fs_lean = {
        "enabled": fr.get("enabled"), "facing": fr.get("facing"),
        "cung_menh": (fr.get("cung_menh") or {}).get("cung"),
        "menh_group": (fr.get("cung_menh") or {}).get("menh_group"),
        "facing_verdict": fr.get("facing_verdict"),
        "room_summary": fr.get("room_summary"),
        "warnings": (fr.get("warnings") or [])[:6],
        "lo_ban_doors": fr.get("lo_ban_doors"),
    }
    checks_lean = (session.checks_result or {}).get("checks")
    qa_agent = QAValidatorAgent()
    critic_agent = AestheticCriticAgent()
    qa_task = qa_agent.validate(
        all_agent_outputs={
            "kts_chief": session.kts_chief_result.output,
            "interior": session.interior_result.output,
            "structural": session.structural_result.output,
            "mep": session.mep_result.output,
            "boq": session.boq_result.output,
            "fengshui_bat_trach": fs_lean,
            "deterministic_checks": checks_lean,
            "dna_hash": session.dna_hash,
        },
        workspace_id=workspace_id,
    )
    critic_task = critic_agent.critique(
        concept=session.interior_result.output if session.interior_result else {},
        style=style_choice,
        dna=dna,
        fengshui=fs_lean,
        workspace_id=workspace_id,
    )
    session.qa_result, session.aesthetic_result = await asyncio.gather(
        qa_task, critic_task, return_exceptions=False,
    )
    if session.qa_result.success:
        session.verdict = session.qa_result.output.get("verdict", "needs_revision")
    else:
        session.errors.append(f"qa: {session.qa_result.error}")
        session.verdict = "major_issues"

    # ─── Aggregate metrics ────────────────────────────────
    for ar in (
        session.kts_chief_result, session.interior_result, session.structural_result,
        session.mep_result, session.boq_result, session.qa_result, session.aesthetic_result,
    ):
        if ar:
            session.total_cost_usd += ar.cost_usd
            session.total_input_tokens += ar.input_tokens
            session.total_output_tokens += ar.output_tokens

    # Imagen 3 render cost ($0.04/image) — separate from LLM agent costs.
    if session.render_result:
        session.total_cost_usd += float(session.render_result.get("cost_usd", 0.0))

    session.duration_ms = int((time.perf_counter() - t_start) * 1000)

    log.info(
        "[orchestrator] session=%s verdict=%s cost=$%.4f duration=%dms",
        session.session_id, session.verdict, session.total_cost_usd, session.duration_ms,
    )
    return session
