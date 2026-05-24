"""
Zeni Router Orchestrator — persona-based ADAPTIVE routing wrapper.

Mỗi 6-vai persona có routing decision riêng tuân theo Zeni Router lock,
NHƯNG có cơ chế ADAPTIVE TIER ESCALATION cho task complex:

Tier mapping theo complexity:
  - simple    → FAST tier (Gemma 4 / Haiku 4.5 / Gemini Flash) — cheap
  - medium    → BALANCED tier (Sonnet 4.6 / Gemini 3.1 Pro / GPT-5.4) — smart
  - complex   → FRONTIER tier (Opus 4.7 / GPT-5.5) — deep reasoning
  - critical  → FRONTIER tier + double-check loop — production-grade

Persona defaults:
  - Architect/Planner: BALANCED → escalate FRONTIER cho complex/critical
  - Coder: FAST → escalate BALANCED cho complex code generation
  - Reviewer/Security: FAST → escalate BALANCED cho destructive task review
  - QA: FAST → escalate BALANCED cho production smoke test

FUTURE-PROOF: khi có model mới (Opus 5.0/Claude 5/GPT-6/Gemini 4),
chỉ cần thêm vào registry — orchestrator tự pick best-in-tier.

Pattern: route qua existing Zeni Router engine + log cost vào cto_council_votes.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.services.llm_gateway import run_inference
from app.services.router.registry import (
    Capability,
    MODEL_REGISTRY,
    ModelEntry,
    Tier,
)
from app.services.router.routing_engine import RoutingDecision, RoutingEngine, RoutingRequest

log = logging.getLogger("zeni.coder.orchestrator")
_engine = RoutingEngine()


# ─── Complexity → Tier escalation matrix ────────────────────
# Pattern: "luôn cập nhật giải pháp AI thông minh hơn" — task càng deep,
# tier càng cao. Đây là cơ chế ADAPTIVE — không lock 1 model.
COMPLEXITY_TIER_MAP: dict[str, dict[str, Tier]] = {
    # complexity → persona role → tier to use
    "simple": {
        "architect": Tier.BALANCED, "planner": Tier.BALANCED,
        "coder": Tier.FAST, "reviewer": Tier.FAST,
        "security": Tier.FAST, "qa": Tier.FAST,
    },
    "medium": {
        "architect": Tier.BALANCED, "planner": Tier.BALANCED,
        "coder": Tier.FAST, "reviewer": Tier.FAST,
        "security": Tier.FAST, "qa": Tier.FAST,
    },
    "complex": {
        # Architect/Planner cần FRONTIER cho deep reasoning
        "architect": Tier.FRONTIER, "planner": Tier.FRONTIER,
        # Coder/Reviewer escalate BALANCED (Sonnet) cho code review tinh tế
        "coder": Tier.BALANCED, "reviewer": Tier.BALANCED,
        # Security task complex → cũng cần Sonnet để bắt subtle vulns
        "security": Tier.BALANCED,
        "qa": Tier.FAST,
    },
    "critical": {
        # Production deploy / destructive ops → ALL FRONTIER
        # → Opus 4.7 cho mọi vai (trừ QA) → cost cao nhưng đáng cho critical
        "architect": Tier.FRONTIER, "planner": Tier.FRONTIER,
        "coder": Tier.FRONTIER, "reviewer": Tier.FRONTIER,
        "security": Tier.FRONTIER,
        "qa": Tier.BALANCED,
    },
}


# ─── Persona → Routing config (with adaptive tier) ──────────
PERSONA_CONFIG: dict[str, dict] = {
    "architect": {
        "default_tier": Tier.BALANCED,
        "deep_tier": Tier.FRONTIER,                # escalate cho complex/critical
        "task_type": "architecture_design",
        "capabilities": [Capability.REASONING, Capability.STRUCTURED],
        "max_tokens": 4096,
        "temperature": 0.4,
        "system_prompt": (
            "Bạn là ARCHITECT — kiến trúc sư phần mềm cao cấp tại Zeni Cloud.\n"
            "Nhiệm vụ: thiết kế tổng thể cho yêu cầu user.\n"
            "Output JSON FORMAT BẮT BUỘC:\n"
            '{"stack": ["next.js", "fastapi", ...], '
            '"components": [{"name": "...", "purpose": "..."}], '
            '"integrations": [{"service": "...", "config": {...}}], '
            '"risks": ["..."], "verdict": "feasible|needs_clarify|too_complex"}'
        ),
    },
    "planner": {
        "default_tier": Tier.BALANCED,
        "deep_tier": Tier.FRONTIER,
        "task_type": "task_planning",
        "capabilities": [Capability.STRUCTURED, Capability.REASONING],
        "max_tokens": 4096,
        "temperature": 0.3,
        "system_prompt": (
            "Bạn là PLANNER — chia task lớn thành sub-tasks có dependency.\n"
            "Output JSON FORMAT BẮT BUỘC:\n"
            '{"steps": [{"step": 1, "tool": "<tool_name>", "args": {...}, '
            '"depends_on": [], "description": "...", "risk": "safe|low|medium|high"}]}\n'
            "Quy tắc:\n"
            "- Mỗi step gọi 1 tool từ catalog provided\n"
            "- depends_on = list step indices (chạy sau khi xong)\n"
            "- Tổng max 10 steps, ưu tiên parallel khi không phụ thuộc\n"
            "- Tools destructive (delete) tránh dùng trừ khi user explicit"
        ),
    },
    "coder": {
        "default_tier": Tier.FAST,
        "deep_tier": Tier.BALANCED,
        "task_type": "code_generation",
        "capabilities": [Capability.CODE, Capability.STRUCTURED],
        "max_tokens": 4096,
        "temperature": 0.2,
        "system_prompt": (
            "Bạn là CODER — implement code chính xác, ngắn gọn, production-ready.\n"
            "Khi cần execute tool: return JSON {\"tool\": \"name\", \"args\": {...}}\n"
            "Khi cần generate code: return unified diff format hoặc full file content\n"
            "Quy tắc: KHÔNG comment phụ, code phải chạy được ngay."
        ),
    },
    "reviewer": {
        "default_tier": Tier.FAST,
        "deep_tier": Tier.BALANCED,
        "task_type": "code_review",
        "capabilities": [Capability.CODE, Capability.STRUCTURED],
        "max_tokens": 1024,
        "temperature": 0.3,
        "system_prompt": (
            "Bạn là REVIEWER — kiểm tra code quality + best practices.\n"
            "Output JSON FORMAT BẮT BUỘC:\n"
            '{"verdict": "pass|fail|warn", "issues": [{"line": N, "severity": "...", "msg": "..."}], '
            '"suggestions": ["..."], "approve": true|false}'
        ),
    },
    "security": {
        "default_tier": Tier.FAST,
        "deep_tier": Tier.BALANCED,
        "task_type": "security_scan",
        "capabilities": [Capability.CODE, Capability.STRUCTURED],
        "max_tokens": 1024,
        "temperature": 0.1,
        "system_prompt": (
            "Bạn là SECURITY — quét vulnerability + adversary patterns.\n"
            "Output JSON FORMAT BẮT BUỘC:\n"
            '{"verdict": "pass|fail|veto", "vulns": [{"type": "...", "severity": "low|medium|high|critical", '
            '"msg": "...", "fix": "..."}], "abort_recommended": true|false}\n'
            "Veto nếu phát hiện: SQL injection, XSS, hardcoded secret, "
            "exfiltrate data, modify .env, rm -rf outside workspace."
        ),
    },
    "qa": {
        "default_tier": Tier.FAST,
        "deep_tier": Tier.BALANCED,
        "task_type": "qa_test",
        "capabilities": [Capability.STRUCTURED],
        "max_tokens": 1024,
        "temperature": 0.3,
        "system_prompt": (
            "Bạn là QA — verify integration + test coverage.\n"
            "Output JSON FORMAT BẮT BUỘC:\n"
            '{"verdict": "pass|fail|warn", "tests_run": [...], "smoke_check": "...", '
            '"missing_coverage": [...], "approve": true|false}'
        ),
    },
}


# ─── Auto-detect complexity ─────────────────────────────────
# Pattern: heuristic dựa requirement length + keywords + target_workspace.
# Có thể later replace bằng ML classifier.
def detect_complexity(
    requirement: str,
    target_workspace: Optional[str] = None,
    target_project: Optional[str] = None,
    explicit: Optional[str] = None,
) -> str:
    """Return one of: simple|medium|complex|critical."""
    if explicit and explicit in COMPLEXITY_TIER_MAP:
        return explicit
    text_lower = (requirement or "").lower()
    word_count = len(text_lower.split())

    # CRITICAL signals — production-affecting
    critical_keywords = [
        "delete", "rm -rf", "drop table", "truncate", "rollback",
        "production", "live", "promote 100", "destructive",
        "rotate key", "rotate secret",
        "migrate", "migration", "schema change",
    ]
    if any(kw in text_lower for kw in critical_keywords):
        return "critical"
    # Production workspace = critical
    if target_workspace and target_workspace not in ("cto-internal", "test", "staging"):
        if any(w in text_lower for w in ["deploy", "release", "publish"]):
            return "critical"

    # COMPLEX signals — multi-step / architecture decisions
    complex_keywords = [
        "architecture", "redesign", "refactor large", "multi-region",
        "microservice", "scalability", "high availability",
        "kiến trúc", "thiết kế lại", "tái cấu trúc",
        "multi-tenant", "saga pattern", "event sourcing",
    ]
    if any(kw in text_lower for kw in complex_keywords):
        return "complex"
    if word_count > 200:
        return "complex"
    if word_count > 50 and any(w in text_lower for w in ["deploy", "build", "integrate"]):
        return "complex"

    # MEDIUM — default for normal tasks
    if word_count > 20:
        return "medium"
    return "simple"


@dataclass
class PersonaResponse:
    persona: str
    model_id: str
    real_model: str
    output_text: str
    output_json: Optional[dict]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    routing_decision: dict
    error: Optional[str] = None


def _select_model(persona: str, complexity: str = "medium") -> ModelEntry:
    """
    Chọn model cho persona THEO COMPLEXITY (adaptive tier escalation).

    Strategy:
    1. Lookup tier từ COMPLEXITY_TIER_MAP[complexity][persona]
    2. Trong tier đó, chọn HIGHEST QUALITY model có required capabilities
       → critical task → Opus 4.7 (FRONTIER, quality_score=0.97)
       → complex task architect → cũng Opus 4.7
       → medium task → Sonnet 4.6 (BALANCED)
       → simple task → Haiku 4.5 (FAST, cheapest)

    "Future-proof": khi có model mới (Opus 5.0/GPT-6/Claude 5/Gemini 4)
    chỉ cần thêm vào MODEL_REGISTRY — orchestrator tự pick model
    chất lượng nhất trong tier đó.
    """
    cfg = PERSONA_CONFIG.get(persona)
    if not cfg:
        return MODEL_REGISTRY.get("opus-4-7") or MODEL_REGISTRY["haiku-4-5"]

    tier = COMPLEXITY_TIER_MAP.get(complexity, COMPLEXITY_TIER_MAP["medium"]).get(persona, cfg["default_tier"])
    caps = cfg["capabilities"]

    # Find candidates trong tier với required capabilities
    candidates = [
        m for m in MODEL_REGISTRY.values()
        if m.tier == tier and all(c in m.capabilities for c in caps)
    ]
    if not candidates:
        # Relax capabilities — fallback to any model in tier
        candidates = [m for m in MODEL_REGISTRY.values() if m.tier == tier]

    # SORT BY QUALITY DESC (then cost ASC for tiebreak)
    # → critical task picks Opus 4.7 (quality 0.97) over GPT-5.5 (0.94)
    # → balanced picks Sonnet 4.6 (0.86) over GPT-5.4 (0.83)
    candidates.sort(key=lambda m: (-m.quality_score, m.input_price_per_mtok + m.output_price_per_mtok / 4))
    return candidates[0] if candidates else MODEL_REGISTRY["haiku-4-5"]


def _try_parse_json(text: str) -> Optional[dict]:
    """Best-effort JSON parsing — strip code fences, trim whitespace."""
    if not text:
        return None
    s = text.strip()
    # Strip ```json or ``` fences
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0].strip()
    # Try direct parse
    try:
        return json.loads(s)
    except Exception:
        pass
    # Try extract first {...} block
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


async def call_persona(
    persona: str,
    user_prompt: str,
    extra_context: Optional[str] = None,
    complexity: str = "medium",
) -> PersonaResponse:
    """
    Call 1 persona qua Zeni Router với ADAPTIVE TIER routing.

    Args:
        persona: 'architect'|'planner'|'coder'|'reviewer'|'security'|'qa'
        user_prompt: prompt cho persona
        extra_context: context block (architect design, file content, etc.)
        complexity: 'simple'|'medium'|'complex'|'critical' — quyết định model tier

    Examples:
        - simple QA task → Haiku 4.5 (FAST, cheap)
        - complex Architect task → Opus 4.7 (FRONTIER, deep reasoning)
        - critical production deploy review → Opus 4.7 (FRONTIER, max quality)
    """
    cfg = PERSONA_CONFIG.get(persona)
    if not cfg:
        raise ValueError(f"Unknown persona: {persona}")

    model_entry = _select_model(persona, complexity)
    full_system = cfg["system_prompt"]
    if extra_context:
        full_system += "\n\n# CONTEXT\n" + extra_context

    routing_decision = {
        "persona": persona,
        "complexity": complexity,
        "model_chosen": model_entry.model_id,
        "real_model": model_entry.real_model_name,
        "tier": model_entry.tier.value,
        "quality_score": model_entry.quality_score,
        "tokens_estimate": cfg["max_tokens"],
        "failover_chain": model_entry.failover_to,
        "selection_reason": (
            f"complexity={complexity} → tier={model_entry.tier.value} → "
            f"chose {model_entry.model_id} (quality={model_entry.quality_score})"
        ),
    }

    start = time.perf_counter()
    try:
        result = await run_inference(
            model=model_entry.real_model_name,
            prompt=user_prompt,
            system=full_system,
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
        )
        out_text = (result.output or "").strip()
        out_json = _try_parse_json(out_text)
        return PersonaResponse(
            persona=persona,
            model_id=model_entry.model_id,
            real_model=model_entry.real_model_name,
            output_text=out_text,
            output_json=out_json,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            routing_decision=routing_decision,
        )
    except Exception as e:
        log.warning("persona %s (complexity=%s) failed: %s", persona, complexity, e)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return PersonaResponse(
            persona=persona,
            model_id=model_entry.model_id,
            real_model=model_entry.real_model_name,
            output_text="",
            output_json=None,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=latency_ms,
            routing_decision=routing_decision,
            error=str(e),
        )


def get_personas() -> list[str]:
    return list(PERSONA_CONFIG.keys())


def get_persona_model(persona: str, complexity: str = "medium") -> str:
    return _select_model(persona, complexity).model_id


def get_routing_preview(complexity: str = "medium") -> dict[str, dict]:
    """Preview which model each persona would use for a complexity level."""
    out = {}
    for p in PERSONA_CONFIG.keys():
        m = _select_model(p, complexity)
        out[p] = {
            "model_id": m.model_id,
            "tier": m.tier.value,
            "quality_score": m.quality_score,
            "input_price_per_mtok": m.input_price_per_mtok,
            "output_price_per_mtok": m.output_price_per_mtok,
        }
    return out
