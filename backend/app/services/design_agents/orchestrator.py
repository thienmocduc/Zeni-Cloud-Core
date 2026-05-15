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
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .agents import (
    BOQCalculatorAgent,
    InteriorDesignerAgent,
    KTSChiefAgent,
    MEPEngineerAgent,
    QAValidatorAgent,
    StructuralEngineerAgent,
    AgentResult,
)

log = logging.getLogger("zeni.design_orchestrator")


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

    # Aggregate
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_ms: int = 0
    verdict: str = "pending"  # pending | ready_for_signoff | needs_revision | major_issues
    errors: list[str] = field(default_factory=list)

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
            "metrics": {
                "total_cost_usd": round(self.total_cost_usd, 6),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "duration_ms": self.duration_ms,
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
) -> DesignSession:
    """
    Orchestrate 6 KTS agents to deliver full-stack design project.

    Returns DesignSession với complete results — ready for KTS chứng chỉ ký.
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

    floor_plan_seed = {
        "rooms_required": dna.get("rooms_required", []),
        "layout_principles": dna.get("layout_principles", []),
    }

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

    # ─── PHASE C: BOQ Calculator (sau structural + MEP) ────
    boq_agent = BOQCalculatorAgent()
    session.boq_result = await boq_agent.calculate_boq(
        architecture_spec=session.kts_chief_result.output,
        structural_spec=session.structural_result.output,
        mep_spec=session.mep_result.output,
        location_province=location_province,
        workspace_id=workspace_id,
    )
    if not session.boq_result.success:
        session.errors.append(f"boq: {session.boq_result.error}")

    # ─── PHASE D: QA Validator (final check) ──────────────
    qa_agent = QAValidatorAgent()
    session.qa_result = await qa_agent.validate(
        all_agent_outputs={
            "kts_chief": session.kts_chief_result.output,
            "interior": session.interior_result.output,
            "structural": session.structural_result.output,
            "mep": session.mep_result.output,
            "boq": session.boq_result.output,
        },
        workspace_id=workspace_id,
    )
    if session.qa_result.success:
        session.verdict = session.qa_result.output.get("verdict", "needs_revision")
    else:
        session.errors.append(f"qa: {session.qa_result.error}")
        session.verdict = "major_issues"

    # ─── Aggregate metrics ────────────────────────────────
    for ar in (
        session.kts_chief_result, session.interior_result, session.structural_result,
        session.mep_result, session.boq_result, session.qa_result,
    ):
        if ar:
            session.total_cost_usd += ar.cost_usd
            session.total_input_tokens += ar.input_tokens
            session.total_output_tokens += ar.output_tokens

    session.duration_ms = int((time.perf_counter() - t_start) * 1000)

    log.info(
        "[orchestrator] session=%s verdict=%s cost=$%.4f duration=%dms",
        session.session_id, session.verdict, session.total_cost_usd, session.duration_ms,
    )
    return session
