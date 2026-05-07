"""
Zeni Studio AI assist — natural-language → component tree, theme, and
improvement suggestions.

We don't talk to LLM providers directly here. Instead we go through the
existing ZeniRouter (`app/services/router/`) so:

    * Quota / cost accounting stays in one place
    * Failover chain (Claude → Gemini → fallback) works for free
    * The rest of Zeni Cloud only has to register one router

Public API
----------
- ``generate_tree_from_prompt(prompt, framework, *, ws_id, db)`` -> ``dict``
- ``suggest_improvements(component, context, *, ws_id, db)`` -> ``list[dict]``
- ``generate_theme(description, *, ws_id, db)`` -> ``dict``  (theme tokens)

Each helper returns plain Python dicts that match the canvas tree / theme
schemas used by the API + renderer.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm_gateway import run_inference
from app.services.router.registry import Capability, MODEL_REGISTRY, ModelEntry, Tier
from app.services.router.routing_engine import RoutingEngine, RoutingRequest

log = logging.getLogger("zeni.studio.ai")
_engine = RoutingEngine()


# Allowed component types — reduces hallucination by giving the model a closed
# vocabulary. Mirrored from `studio_renderer._TAG_MAP`.
ALLOWED_TYPES = (
    "container", "section", "main", "layout",
    "navbar", "sidebar", "footer",
    "heading", "text", "paragraph",
    "button", "link", "image",
    "input", "textarea", "form",
    "grid", "card", "stat_card",
    "table", "chart",
    "post_list", "post_detail", "product_grid",
)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _route_and_run(
    *,
    workspace_id: str,
    prompt: str,
    system: str,
    task_type: str = "code_generate",
    max_tokens: int = 3000,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Pick a model via the router and run it via llm_gateway.

    Returns ``{"text": "...", "model": "...", "input_tokens": ..., "output_tokens": ..., "cost_usd": ...}``.
    Synchronous wrapper around our async gateway is avoided — callers ``await`` this.
    """
    routing_req = RoutingRequest(
        tenant_id=workspace_id,
        product="zeni-studio",
        task_type=task_type,
        estimated_input_tokens=max(1, len(prompt) // 4),
        expected_output_tokens=max_tokens,
        required_capabilities=[Capability.STRUCTURED] if "STRUCTURED" in Capability.__members__ else [],
        explicit_model_id=None,
        explicit_tier=Tier.FRONTIER if "design" in task_type else None,
    )
    decision = _engine.decide(routing_req)
    chain: list[ModelEntry] = [decision.primary_model] + decision.failover_chain
    return {"_chain": chain, "_decision": decision}


async def _run_chain(
    *,
    chain: list[ModelEntry],
    prompt: str,
    system: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Walk the failover chain until one model returns a result."""
    last_error: Exception | None = None
    for model in chain:
        real = getattr(model, "real_model_name", None) or model.provider_model_name
        try:
            result = await run_inference(
                model=real,
                prompt=prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return {
                "text": result.output,
                "model": real,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": float(result.cost_usd),
            }
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
    raise RuntimeError(f"All models failed: {last_error}")


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response.

    Models often wrap JSON in ```json fences``` or include lead-in prose.
    """
    if not text:
        raise ValueError("empty response")

    # Strip code fences
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    candidate = fence.group(1).strip() if fence else text.strip()

    # Find the first {...} or [...] block
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    chunk = candidate[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        break
    # Final attempt — raw parse
    return json.loads(candidate)


def _sanitise_tree(node: Any, depth: int = 0) -> dict[str, Any]:
    """Clamp model output to our component schema. Drops unknown fields and
    enforces a finite recursion depth."""
    if depth > 12:
        return {"type": "text", "props": {"text": "(depth limit reached)"}, "children": []}
    if not isinstance(node, dict):
        return {"type": "text", "props": {"text": str(node)}, "children": []}

    ctype = str(node.get("type", "container")).lower()
    if ctype not in ALLOWED_TYPES:
        ctype = "container"

    props = node.get("props")
    props = props if isinstance(props, dict) else {}
    style = node.get("style")
    style = style if isinstance(style, dict) else {}
    events = node.get("events")
    events = events if isinstance(events, dict) else {}

    children_raw = node.get("children") or []
    if not isinstance(children_raw, list):
        children_raw = []
    children = [_sanitise_tree(c, depth + 1) for c in children_raw[:50]]

    out: dict[str, Any] = {
        "type": ctype,
        "props": props,
        "style": style,
        "events": events,
        "children": children,
    }
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:160]
    return out


# ════════════════════════════════════════════════════════════════════════════
# Tree generation
# ════════════════════════════════════════════════════════════════════════════
_TREE_SYSTEM = (
    "Bạn là Zeni Studio AI — một designer no-code chuyên tạo cây thành phần (component tree) "
    "cho ứng dụng web/mobile. Luôn trả về JSON DUY NHẤT, không kèm prose. "
    "Schema mỗi node: {type, name?, props, style, events, children:[...]}."
    " Các type cho phép: " + ", ".join(ALLOWED_TYPES) + "."
    " Style hỗ trợ field className (Tailwind utility classes)."
    " Cây phải có 1 root duy nhất."
)


async def generate_tree_from_prompt(
    prompt: str,
    framework: str = "next",
    *,
    workspace_id: str,
    db: AsyncSession | None = None,
    max_tokens: int = 3500,
) -> dict[str, Any]:
    """Generate a component tree from a natural-language prompt.

    Returns ``{"tree": {...}, "framework": str, "model": str, "cost_usd": float}``.
    """
    user = (
        f"Yêu cầu của khách: {prompt}\n\n"
        f"Framework đích: {framework}.\n"
        "Trả về JSON object có dạng:\n"
        "{\n"
        '  "tree": { /* component tree */ },\n'
        '  "data_sources": [ /* optional */ ],\n'
        '  "actions": [ /* optional */ ]\n'
        "}\n"
        "Chỉ JSON, không prose, không code fence."
    )
    routed = _route_and_run(
        workspace_id=workspace_id,
        prompt=user,
        system=_TREE_SYSTEM,
        task_type="code_generate",
        max_tokens=max_tokens,
        temperature=0.4,
    )
    result = await _run_chain(
        chain=routed["_chain"],
        prompt=user,
        system=_TREE_SYSTEM,
        max_tokens=max_tokens,
        temperature=0.4,
    )

    try:
        parsed = _extract_json(result["text"])
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("Studio AI tree parse failed: %s", e)
        parsed = {"tree": {"type": "container", "name": "page-root",
                            "props": {}, "style": {"className": "p-6"},
                            "children": [
                                {"type": "heading", "props": {"text": prompt[:120], "level": 1},
                                 "style": {"className": "text-3xl font-bold mb-4"}},
                                {"type": "text", "props": {"text": "AI generation failed — fallback page."},
                                 "style": {"className": "text-gray-600"}},
                            ]}}

    if isinstance(parsed, dict) and "tree" in parsed:
        tree = _sanitise_tree(parsed.get("tree") or {})
        data_sources = parsed.get("data_sources") or []
        actions = parsed.get("actions") or []
    else:
        tree = _sanitise_tree(parsed if isinstance(parsed, dict) else {})
        data_sources = []
        actions = []

    return {
        "tree": tree,
        "data_sources": data_sources if isinstance(data_sources, list) else [],
        "actions": actions if isinstance(actions, list) else [],
        "framework": framework,
        "model": result["model"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cost_usd": result["cost_usd"],
    }


# ════════════════════════════════════════════════════════════════════════════
# Improvement suggestions
# ════════════════════════════════════════════════════════════════════════════
_SUGGEST_SYSTEM = (
    "Bạn là Zeni Studio AI reviewer. Đề xuất cải thiện thiết kế / accessibility "
    "/ performance cho component đầu vào. Trả về JSON array: "
    "[{title, severity:'low'|'med'|'high', body, patch?}]. "
    "Chỉ JSON, không prose."
)


async def suggest_improvements(
    component: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    workspace_id: str,
    db: AsyncSession | None = None,
    max_tokens: int = 1200,
) -> list[dict[str, Any]]:
    """Suggest improvements for a single component (or subtree).

    Returns a list of ``{title, severity, body, patch?}`` dicts. Always at
    least 1 suggestion (even if the model fails — falls back to a generic note).
    """
    user = (
        "Component cần đánh giá:\n"
        f"```json\n{json.dumps(component, ensure_ascii=False, indent=2)[:4000]}\n```\n\n"
        f"Context: {json.dumps(context or {}, ensure_ascii=False)[:1500]}\n\n"
        "Đề xuất 3-6 cải thiện. JSON array."
    )
    routed = _route_and_run(
        workspace_id=workspace_id,
        prompt=user,
        system=_SUGGEST_SYSTEM,
        task_type="qa_complex",
        max_tokens=max_tokens,
        temperature=0.5,
    )
    try:
        result = await _run_chain(
            chain=routed["_chain"],
            prompt=user,
            system=_SUGGEST_SYSTEM,
            max_tokens=max_tokens,
            temperature=0.5,
        )
        parsed = _extract_json(result["text"])
    except Exception as e:  # noqa: BLE001
        log.warning("Studio AI suggest failed: %s", e)
        return [{
            "title": "Không thể tạo đề xuất tự động",
            "severity": "low",
            "body": "AI tạm thời không phản hồi. Hãy thử lại sau.",
        }]

    if not isinstance(parsed, list):
        parsed = [parsed] if isinstance(parsed, dict) else []

    out: list[dict[str, Any]] = []
    for item in parsed[:10]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")[:200]
        sev = str(item.get("severity") or "med").lower()
        if sev not in ("low", "med", "high"):
            sev = "med"
        body = str(item.get("body") or "")[:1500]
        patch = item.get("patch") if isinstance(item.get("patch"), dict) else None
        if title or body:
            out.append({"title": title, "severity": sev, "body": body, "patch": patch})

    if not out:
        out.append({
            "title": "Component đã ổn",
            "severity": "low",
            "body": "Không có đề xuất cụ thể.",
        })
    return out


# ════════════════════════════════════════════════════════════════════════════
# Theme generation
# ════════════════════════════════════════════════════════════════════════════
_THEME_SYSTEM = (
    "Bạn là Zeni Studio AI designer chuyên thiết kế design tokens. Đầu ra "
    "là JSON DUY NHẤT có schema:\n"
    "{ colors:{primary,bg,text,muted,border,success,danger,warning}, "
    "fonts:{heading,body,mono}, spacing:{xs,sm,md,lg,xl}, radius:{sm,md,lg}, "
    "shadows:{sm,md,lg} }. Mọi color là hex. Không prose, không code fence."
)


def _default_theme() -> dict[str, Any]:
    return {
        "colors": {
            "primary": "#ff6b35", "bg": "#ffffff", "text": "#0f172a",
            "muted": "#64748b", "border": "#e5e7eb",
            "success": "#10b981", "danger": "#ef4444", "warning": "#f59e0b",
        },
        "fonts": {
            "heading": "Inter, system-ui, sans-serif",
            "body": "Inter, system-ui, sans-serif",
            "mono": "JetBrains Mono, monospace",
        },
        "spacing": {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "48px"},
        "radius": {"sm": "4px", "md": "8px", "lg": "16px"},
        "shadows": {
            "sm": "0 1px 2px rgba(0,0,0,0.05)",
            "md": "0 4px 6px rgba(0,0,0,0.07)",
            "lg": "0 10px 25px rgba(0,0,0,0.1)",
        },
    }


def _sanitise_theme(theme: Any) -> dict[str, Any]:
    base = _default_theme()
    if not isinstance(theme, dict):
        return base
    for group in ("colors", "fonts", "spacing", "radius", "shadows"):
        if isinstance(theme.get(group), dict):
            base[group].update({
                str(k): str(v) for k, v in theme[group].items()
                if isinstance(k, str) and isinstance(v, (str, int, float))
            })
    return base


async def generate_theme(
    description: str,
    *,
    workspace_id: str,
    db: AsyncSession | None = None,
    max_tokens: int = 800,
) -> dict[str, Any]:
    """Turn a free-form description ("warm pastel for VN cafe") into theme tokens.

    Returns ``{"tokens": {...}, "model": "...", "cost_usd": ...}``.
    """
    user = (
        f"Mô tả: {description}\n\n"
        "Trả JSON theo schema. Mọi màu hex. Chỉ JSON."
    )
    routed = _route_and_run(
        workspace_id=workspace_id,
        prompt=user,
        system=_THEME_SYSTEM,
        task_type="design_tokens",
        max_tokens=max_tokens,
        temperature=0.7,
    )
    try:
        result = await _run_chain(
            chain=routed["_chain"],
            prompt=user,
            system=_THEME_SYSTEM,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        parsed = _extract_json(result["text"])
    except Exception as e:  # noqa: BLE001
        log.warning("Studio AI theme failed: %s", e)
        return {
            "tokens": _default_theme(),
            "model": "fallback",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }

    return {
        "tokens": _sanitise_theme(parsed),
        "model": result["model"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cost_usd": result["cost_usd"],
    }
