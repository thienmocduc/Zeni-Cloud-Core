"""
6-Vai Zeni Council — parallel deliberation pattern (CodeWits-equivalent).

Pattern lấy cảm hứng:
- ClawWits 6-vai meeting (Architect/Coder/Reviewer/Security/QA/Recon)
- Anthropic Constitutional AI critique loop
- Cognition Devin team-of-experts

Workflow:
  1. Architect designs overall stack
  2. Planner breaks into steps (using architect's design)
  3. Security pre-scans each step's tool args
  4. Reviewer reviews planned approach
  5. QA validates testability
  6. Consensus: 4/6 yes → proceed; SECURITY veto RED → abort

All 6 personas chạy PARALLEL qua asyncio.gather() — không sequential.
Cost tracked qua cto_council_votes table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .orchestrator import call_persona, detect_complexity, PersonaResponse

log = logging.getLogger("zeni.coder.council")


async def deliberate(
    db: AsyncSession,
    run_id: str,
    requirement: str,
    tool_catalog: list[dict],
    target_workspace: Optional[str] = None,
    target_project: Optional[str] = None,
) -> dict:
    """
    Run 6-vai council parallel deliberation.

    Returns:
        {
          "consensus": "approved" | "veto" | "conflict" | "pending",
          "architect_design": dict,
          "planner_steps": [dict],
          "votes": [{agent, vote, reasoning, cost}],
          "total_cost_usd": float,
          "total_input_tokens": int,
          "total_output_tokens": int,
          "abort_reason": str | None,
        }
    """
    # ─── Detect complexity TRƯỚC ─────────────────────────────
    complexity = detect_complexity(requirement, target_workspace, target_project)
    log.info("[council] run=%s complexity=%s starting deliberation: %s",
             run_id, complexity, requirement[:80])

    catalog_str = "\n".join([
        f"- {t['tool_name']} ({t['risk_level']}): {t['description']}"
        for t in tool_catalog
    ])
    context_block = (
        f"# REQUIREMENT\n{requirement}\n\n"
        f"# COMPLEXITY DETECTED: {complexity.upper()}\n"
        f"(complex/critical → routes to FRONTIER tier — Opus 4.7)\n\n"
        f"# TOOLS AVAILABLE\n{catalog_str}\n\n"
        f"# TARGET\n"
        f"workspace: {target_workspace or 'cto-internal'}\n"
        f"project: {target_project or 'N/A'}"
    )

    # ─── Phase A: Architect designs (complexity-routed) ─────
    architect_resp = await call_persona(
        "architect",
        f"User cần: {requirement}\n\nThiết kế stack + components + integrations.",
        extra_context=context_block,
        complexity=complexity,
    )
    await _save_vote(db, run_id, architect_resp, vote="abstain")

    architect_design = architect_resp.output_json or {"raw": architect_resp.output_text}

    # If Architect says too_complex → abort
    if architect_design.get("verdict") == "too_complex":
        log.warning("[council] run=%s architect says TOO_COMPLEX", run_id)
        return {
            "consensus": "veto",
            "architect_design": architect_design,
            "planner_steps": [],
            "votes": [_vote_dict(architect_resp, "veto")],
            "total_cost_usd": architect_resp.cost_usd,
            "total_input_tokens": architect_resp.input_tokens,
            "total_output_tokens": architect_resp.output_tokens,
            "abort_reason": "Architect verdict: too_complex",
        }

    # ─── Phase B: Planner + Security/Reviewer/QA parallel ────
    architect_summary = json.dumps(architect_design, ensure_ascii=False, indent=2)[:2000]
    planner_context = (
        context_block + "\n\n# ARCHITECT DESIGN\n" + architect_summary
    )

    planner_task = call_persona(
        "planner",
        f"Break thành steps với tool catalog. Requirement: {requirement}",
        extra_context=planner_context,
        complexity=complexity,
    )
    security_task = call_persona(
        "security",
        f"Scan requirement này có rủi ro security gì? Vulns? Adversary patterns?\nReq: {requirement}",
        extra_context=planner_context,
        complexity=complexity,
    )
    reviewer_task = call_persona(
        "reviewer",
        f"Review approach này — ổn không? Architect output:\n{architect_summary[:1500]}",
        extra_context=context_block,
        complexity=complexity,
    )
    qa_task = call_persona(
        "qa",
        f"Approach này có thể test được không? Smoke test sao?\nReq: {requirement}",
        extra_context=planner_context,
        complexity=complexity,
    )

    planner_resp, security_resp, reviewer_resp, qa_resp = await asyncio.gather(
        planner_task, security_task, reviewer_task, qa_task,
        return_exceptions=False,
    )

    # ─── Save votes ──────────────────────────────────────────
    planner_steps = (planner_resp.output_json or {}).get("steps", [])
    security_verdict = (security_resp.output_json or {}).get("verdict", "pass")
    security_abort = (security_resp.output_json or {}).get("abort_recommended", False)
    reviewer_approve = (reviewer_resp.output_json or {}).get("approve", True)
    qa_approve = (qa_resp.output_json or {}).get("approve", True)

    # Vote tally:
    # - architect: abstain (already done)
    # - planner: yes if produced steps
    # - security: yes if pass, veto if abort_recommended or verdict=veto
    # - reviewer: yes if approve
    # - qa: yes if approve
    planner_vote = "yes" if planner_steps else "no"
    security_vote = "veto" if (security_abort or security_verdict == "veto") else (
        "yes" if security_verdict == "pass" else "no"
    )
    reviewer_vote = "yes" if reviewer_approve else "no"
    qa_vote = "yes" if qa_approve else "no"

    # v160 FIX: SAVE SEQUENTIAL — AsyncSession KHÔNG thread-safe cho concurrent ops
    # Trước đây asyncio.gather 4 _save_vote song song trên cùng db session → race
    # condition, chỉ 1-2 vote save được (architect+planner), 3 vote khác (security,
    # reviewer, qa) mất silently. Fix: await sequential để mỗi commit hoàn tất riêng.
    await _save_vote(db, run_id, planner_resp, vote=planner_vote)
    await _save_vote(db, run_id, security_resp, vote=security_vote)
    await _save_vote(db, run_id, reviewer_resp, vote=reviewer_vote)
    await _save_vote(db, run_id, qa_resp, vote=qa_vote)

    # ─── Consensus ───────────────────────────────────────────
    if security_vote == "veto":
        consensus = "veto"
        abort_reason = "SECURITY veto: " + json.dumps(security_resp.output_json or {})[:200]
    else:
        yes_count = sum(1 for v in (planner_vote, security_vote, reviewer_vote, qa_vote) if v == "yes")
        # Architect abstain doesn't count; need 3/4 of remaining 4 personas
        if yes_count >= 3:
            consensus = "approved"
            abort_reason = None
        else:
            consensus = "conflict"
            abort_reason = f"Only {yes_count}/4 voted yes (need ≥3)"

    total_cost = sum(r.cost_usd for r in (architect_resp, planner_resp, security_resp, reviewer_resp, qa_resp))
    total_input = sum(r.input_tokens for r in (architect_resp, planner_resp, security_resp, reviewer_resp, qa_resp))
    total_output = sum(r.output_tokens for r in (architect_resp, planner_resp, security_resp, reviewer_resp, qa_resp))

    log.info("[council] run=%s complexity=%s consensus=%s steps=%d cost=$%.4f models=%s",
             run_id, complexity, consensus, len(planner_steps), total_cost,
             ",".join(r.model_id for r in (architect_resp, planner_resp, security_resp, reviewer_resp, qa_resp)))

    return {
        "consensus": consensus,
        "complexity": complexity,
        "architect_design": architect_design,
        "planner_steps": planner_steps,
        "votes": [
            _vote_dict(architect_resp, "abstain"),
            _vote_dict(planner_resp, planner_vote),
            _vote_dict(security_resp, security_vote),
            _vote_dict(reviewer_resp, reviewer_vote),
            _vote_dict(qa_resp, qa_vote),
        ],
        "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "abort_reason": abort_reason,
    }


async def review_step_output(
    db: AsyncSession,
    run_id: str,
    step_idx: int,
    tool_name: str,
    tool_args: dict,
    tool_result: Any,
    error: Optional[str] = None,
    complexity: str = "medium",
) -> tuple[str, str]:
    """
    Reviewer + QA verdict on a step's execution result.

    Routes through complexity tier — destructive tools auto-escalate.
    Returns: (reviewer_verdict, qa_verdict) — pass|fail|warn each
    """
    summary = json.dumps({
        "tool": tool_name,
        "args": tool_args,
        "result": tool_result,
        "error": error,
    }, ensure_ascii=False, default=str)[:2000]

    # Auto-escalate complexity for destructive/production tools
    destructive_tools = ("delete_project", "delete_secret", "rollback_deploy",
                         "promote_traffic", "deploy_canary")
    effective_complexity = "critical" if tool_name in destructive_tools else complexity

    reviewer_task = call_persona(
        "reviewer",
        f"Step {step_idx}: tool '{tool_name}' executed. Output OK chưa?\n\n{summary}",
        complexity=effective_complexity,
    )
    qa_task = call_persona(
        "qa",
        f"Step {step_idx}: tool '{tool_name}' done. Pass smoke check?\n\n{summary}",
        complexity=effective_complexity,
    )
    rv, qv = await asyncio.gather(reviewer_task, qa_task)
    # v160 FIX: save sequential (AsyncSession không thread-safe)
    await _save_vote(db, run_id, rv, vote=("yes" if (rv.output_json or {}).get("approve", True) else "no"))
    await _save_vote(db, run_id, qv, vote=("yes" if (qv.output_json or {}).get("approve", True) else "no"))

    rv_verdict = (rv.output_json or {}).get("verdict") or ("pass" if not error else "fail")
    qv_verdict = (qv.output_json or {}).get("verdict") or ("pass" if not error else "fail")
    return rv_verdict, qv_verdict


# ─── Helpers ─────────────────────────────────────────────
def _vote_dict(resp: PersonaResponse, vote: str) -> dict:
    return {
        "agent": resp.persona,
        "model": resp.model_id,
        "vote": vote,
        "cost_usd": resp.cost_usd,
        "tokens": resp.input_tokens + resp.output_tokens,
        "latency_ms": resp.latency_ms,
        "error": resp.error,
        "output_summary": (resp.output_text[:300] + "...") if len(resp.output_text) > 300 else resp.output_text,
    }


async def _save_vote(db: AsyncSession, run_id: str, resp: PersonaResponse, vote: str) -> None:
    """Persist 1 vote vào cto_council_votes."""
    try:
        await db.execute(text(
            "INSERT INTO cto_council_votes (id, run_id, agent_role, agent_model, "
            "vote, reasoning, output_json, input_tokens, output_tokens, cost_usd, "
            "latency_ms, router_decision) "
            "VALUES (:id, :rid, :role, :m, :v, :r, CAST(:oj AS jsonb), "
            ":it, :ot, :c, :lat, CAST(:rd AS jsonb))"
        ), {
            "id": str(uuid.uuid4()),
            "rid": run_id,
            "role": resp.persona,
            "m": resp.model_id,
            "v": vote,
            "r": (resp.output_text[:1000] if resp.output_text else (resp.error or "")),
            "oj": json.dumps(resp.output_json) if resp.output_json else None,
            "it": resp.input_tokens,
            "ot": resp.output_tokens,
            "c": resp.cost_usd,
            "lat": resp.latency_ms,
            "rd": json.dumps(resp.routing_decision),
        })
        await db.commit()
    except Exception as e:
        log.warning("[council] save_vote failed: %s", e)
