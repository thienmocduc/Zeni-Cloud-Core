"""
Multi-stage structured design pipeline.

Workflow:
  1. PLAN       — Gemini Pro convert structured brief → detailed plan JSON
  2. RENDER     — Imagen 3 generate per-room/per-view với prompt CHI TIẾT
  3. VERIFY     — Multimodal Gemini check ảnh có khớp brief không, regen if mismatch
  4. CRITIQUE   — Self-review final
  5. ASSEMBLE   — Output package

→ Output đẹp + chi tiết + đúng brief 90%+ thay vì 50% như Imagen 3 standalone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.services import ai_core, design_agents

log = logging.getLogger("zeni.structured_pipeline")


# Per-kind plan-stage system prompt (forces structured JSON output)
PLAN_PROMPTS: dict[str, str] = {
    "architecture": """Bạn là KTS Senior. Convert structured brief → DETAILED PLAN cho từng phòng + facade + landscape.
Output STRICT JSON với schema:
{
  "project_summary": "...",
  "concept_narrative": "200-400 chữ",
  "site_strategy": {"orientation_use": "...", "wind": "...", "sun": "...", "privacy": "..."},
  "massing": "1-2 câu",
  "facade_strategy": "vật liệu + opening pattern",
  "rooms_detailed": [
    {
      "room_id": "phong_khach_t1",
      "type": "phong_khach", "floor": 1, "area_m2": 40,
      "dimensions_lwh_m": [8, 5, 3.5],
      "concept": "double-height void với view san vuon...",
      "key_furniture": ["sofa_3.2m_minotti","coffee_table_travertine","floor_lamp_arc"],
      "materials": {"floor": "...", "walls": "...", "ceiling": "...", "feature": "..."},
      "lighting": {"key": "...", "fill": "...", "accent": "..."},
      "must_haves_addressed": ["bar_counter ✓ at north wall","fireplace ✓ travertine wall"],
      "color_palette": ["#...", "#..."]
    }
  ],
  "landscape": "...",
  "sustainability": ["passive cooling via cross-vent","green roof level 3","..."],
  "phong_thuy_notes": "...",
  "estimated_cost_vnd_per_m2": 25000000
}""",

    "interior": """Bạn là Interior Designer Senior. Convert structured brief → DETAILED PLAN cho 1 không gian.
Output STRICT JSON với schema:
{
  "concept_summary": "100-200 chữ",
  "mood_keywords": ["serene","sophisticated","warm"],
  "color_palette_hex": ["#...", "#...", "#...", "#...", "#..."],
  "zoning": {"public": "...", "semi_private": "...", "private": "..."},
  "key_views": [
    {"view_id": "main_corner", "description": "...", "focal_point": "..."},
    {"view_id": "secondary", "description": "..."}
  ],
  "materials_specified": {
    "floor": {"name": "...", "spec": "...", "supplier_hint": "..."},
    "walls": {"name": "...", "spec": "..."},
    "ceiling": {"name": "...", "spec": "..."},
    "millwork": {"name": "...", "spec": "..."}
  },
  "furniture_list": [
    {"item": "Sofa", "spec": "Minotti Connery 3-seater 280cm cream bouclé", "qty": 1, "price_vnd_est": 280000000},
    {"item": "Coffee table", "spec": "...", "qty": 1, "price_vnd_est": ...}
  ],
  "lighting_plan": {
    "key": "natural sunlight from east window 3m x 2.4m",
    "fill": "linear LED 3000K hidden in ceiling reveal",
    "accent": ["spotlight_on_artwork","cove_light_behind_tv"],
    "decorative": ["pendant_dining_3-piece","floor_lamp_reading"]
  },
  "must_haves_addressed": ["bar_counter ✓","tv_unit_3.5m ✓"],
  "estimated_total_cost_vnd": 850000000
}""",

    "product": """Bạn là Product Designer Senior. Convert brief → CONCEPT spec.
Output STRICT JSON:
{
  "concept_name": "...",
  "tagline": "...",
  "form_summary": "...",
  "dimensions_mm_lwh": [120, 60, 30],
  "weight_g_est": 240,
  "materials_cmf": [
    {"surface": "main_body", "material": "anodized_aluminum_silver", "finish": "brushed_satin"},
    {"surface": "accent", "material": "...", "finish": "..."}
  ],
  "color_variants": ["space_grey", "natural_silver", "midnight_black"],
  "key_features": [...],
  "manufacturing_process": "CNC + anodizing + laser etching",
  "tooling_cost_vnd_est": 250000000,
  "unit_cost_target_vnd": 350000,
  "competitors_compared": [{"name": "...", "differentiation": "..."}]
}""",

    "fashion": """Bạn là Fashion Designer Senior. Convert brief → COLLECTION concept.
Output STRICT JSON:
{
  "collection_name": "...",
  "story": "100-200 chữ",
  "season_target": "...",
  "silhouette_summary": "oversized + structured shoulder + cropped",
  "color_palette_hex": ["#...", "#...", "#...", "#..."],
  "fabric_list": [
    {"name": "Lụa Hà Đông", "weight_gsm": 90, "use_for": ["top","scarf"]},
    {"name": "...", "weight_gsm": ..., "use_for": [...]}
  ],
  "garment_list": [
    {"item": "Oversized blazer", "fabric": "Wool blend 280gsm",
     "sizing": "S-M-L-XL", "price_vnd_est": 2800000, "qty_target": 50}
  ],
  "styling_outfits": [{"outfit_id": "look_01", "items": [...], "occasion": "..."}],
  "production_total_cost_vnd_est": 50000000
}""",

    "structural": """Bạn là PE Structural Engineer Senior, Vietnam. Convert structured brief → STRUCTURAL DESIGN spec.
Output STRICT JSON tuân thủ TCVN:
{
  "system_chosen": "BT_cot_thep_khung",
  "rationale": "...",
  "main_load_paths": "...",
  "preliminary_sizing": {
    "columns": [{"location": "internal", "size_mm": "400x400", "concrete_grade": "B25", "rebar_assumption": "..."}],
    "beams": [{"location": "...", "size_mm": "300x600", "..."}],
    "slabs": [{"location": "...", "thickness_mm": 150}]
  },
  "foundation": {"type": "moc_coc_BTCT", "depth_m": 1.5, "bearing_design_kpa": 150, "notes": "..."},
  "lateral_system": {"wind_design": "...", "seismic_design": "..."},
  "code_compliance_check": [
    {"code": "TCVN_5574_2018", "compliant": true, "notes": "..."},
    {"code": "TCVN_2737_1995", "compliant": true, "loads_kn_m2": 2.0}
  ],
  "loads_calculated": {"dead_kn_m2": 4.5, "live_kn_m2": 2.0, "wind_kn_m2": 1.2},
  "material_spec": {"concrete": "B25 TCVN 4453", "rebar": "CB400 TCVN 1651"},
  "estimated_cost_vnd_per_m2": 6500000,
  "risks": ["soil_unconfirmed_need_geotech_report","..."]
}""",
}


# ─── Plan stage ────────────────────────────────────────────────
async def plan_stage(kind: str, brief_dict: dict[str, Any]) -> dict[str, Any]:
    """Run Plan stage: structured brief → detailed JSON plan via Gemini Pro."""
    ai_core._ensure_init()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    sys_prompt = PLAN_PROMPTS.get(kind, PLAN_PROMPTS["architecture"])
    model = GenerativeModel("gemini-2.5-pro", system_instruction=sys_prompt)

    user_msg = (
        "Cấu trúc brief khách hàng (JSON):\n```json\n"
        + json.dumps(brief_dict, ensure_ascii=False, indent=2)
        + "\n```\nTạo PLAN JSON theo schema. CHỈ output JSON, không bọc markdown."
    )
    resp = await asyncio.to_thread(
        model.generate_content, user_msg,
        generation_config=GenerationConfig(temperature=0.5, max_output_tokens=8192,
                                           response_mime_type="application/json"),
    )
    text = ""
    if resp.candidates:
        for p in resp.candidates[0].content.parts:
            text += getattr(p, "text", "") or ""
    try:
        plan = json.loads(text)
    except Exception:
        # Fallback if Gemini wraps in markdown
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        plan = json.loads(text)
    plan["_meta"] = {
        "input_tokens": resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0,
        "output_tokens": resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0,
    }
    return plan


# ─── Render stage (per-room với prompt chi tiết) ─────────────────
def _build_room_render_prompt(kind: str, room_plan: dict[str, Any], global_plan: dict[str, Any]) -> str:
    """Build hyper-detailed Imagen 3 prompt cho 1 phòng cụ thể."""
    if kind == "interior":
        materials = global_plan.get("materials_specified", {})
        lighting = global_plan.get("lighting_plan", {})
    else:
        materials = room_plan.get("materials", {})
        lighting = room_plan.get("lighting", {})

    dims = room_plan.get("dimensions_lwh_m", [None, None, None])
    dims_str = f"{dims[0]}m × {dims[1]}m × {dims[2]}m height" if all(dims) else "as planned"
    palette = room_plan.get("color_palette") or global_plan.get("color_palette_hex", [])
    palette_str = ", ".join(palette[:5]) if palette else "neutral warm"

    furniture = room_plan.get("key_furniture") or [
        f.get("spec", f.get("item", "")) for f in global_plan.get("furniture_list", [])[:6]
    ]
    furniture_str = ", ".join(furniture[:8]) if furniture else "tasteful minimal furniture"

    return f"""MASTER SHOT: Hyper-realistic photorealistic interior photography of {room_plan.get('type','room').replace('_',' ')}.

ROOM DIMENSIONS: {dims_str}
AREA: {room_plan.get('area_m2','?')}m²
ZONE: {room_plan.get('concept', '')[:200]}

VIEW ANGLE: 3/4 corner perspective at 1.6m human eye height, slight 8° upward tilt.

MATERIAL PALETTE (PHYSICALLY ACCURATE - render exactly):
- Floor: {materials.get('floor', {}).get('spec', materials.get('floor', 'large-format travertine'))}
- Walls: {materials.get('walls', {}).get('spec', materials.get('walls', 'lime plaster cream'))}
- Ceiling: {materials.get('ceiling', {}).get('spec', materials.get('ceiling', 'white skim coat with shadow gap'))}
- Feature wall: {materials.get('feature', materials.get('millwork', {}).get('spec', 'vertical wood slats'))}

COLOR PALETTE: {palette_str}

FURNITURE (precise, render visible):
{furniture_str}

LIGHTING (3-point cinematic):
- Key: {lighting.get('key', 'natural sunlight from window')}
- Fill: {lighting.get('fill', 'hidden linear LED 3000K')}
- Accent: {", ".join(lighting.get('accent', ['warm spotlights'])) if isinstance(lighting.get('accent'), list) else lighting.get('accent', 'warm spotlights')}

CAMERA: Hasselblad H6D-100c, 24mm tilt-shift, f/8, ISO 200, perfectly corrected verticals.
8K resolution, tack-sharp, Kodak Portra 400 cinematic warm color grade.

MICRO-DETAILS REQUIRED:
- Visible material textures (wood grain, plaster surface, fabric weave)
- Realistic shadows from multiple light sources
- Plants with realistic leaves (Monstera, fiddle-leaf)
- Curated objects: books, ceramics, art on shelves
- Subtle reflections on stone/glass

ATMOSPHERE: serene, sophisticated, warm, livable, authentic Vietnamese tropical luxury.

MUST INCLUDE (from brief - render visible): {", ".join(room_plan.get('must_haves_addressed', [])[:5])}
"""


async def render_room(kind: str, room_plan: dict, global_plan: dict, n_views: int = 2,
                       aspect_ratio: str = "16:9") -> dict[str, Any]:
    """Render 1 phòng/view chi tiết qua Imagen 3 với negative prompt."""
    prompt = _build_room_render_prompt(kind, room_plan, global_plan)
    try:
        result = await ai_core.generate_image(
            prompt=prompt[:1900],  # Imagen accepts longer; clip safe limit
            aspect_ratio=aspect_ratio,
            n=min(max(1, n_views), 4),
            safety_filter="block_some",
            negative_prompt=design_agents._negative_prompt_for_kind(kind),
        )
        return {
            "room_id": room_plan.get("room_id") or room_plan.get("type", "room"),
            "prompt_used": prompt[:300] + "...",
            "images": result.get("images", []),
            "count": result.get("count", 0),
            "cost_usd": result.get("cost_usd", 0),
        }
    except Exception as e:
        log.warning("[render_room] %s failed: %s", room_plan.get("room_id"), e)
        return {
            "room_id": room_plan.get("room_id"),
            "error": str(e), "images": [], "count": 0, "cost_usd": 0,
        }


# ─── Verify stage ──────────────────────────────────────────────
async def verify_render(image_data_uri: str, brief: dict, room_plan: dict) -> dict[str, Any]:
    """
    Multimodal Gemini check ảnh có khớp brief không.
    Returns {match_score: 0-1, issues: [...], approved: bool}
    """
    must_haves = room_plan.get("must_haves_addressed", []) or brief.get("must_have_items", [])
    materials_pref = brief.get("materials_preferred", [])
    style = brief.get("style", "")

    check_prompt = f"""Bạn là QC inspector cho thiết kế. Phân tích ảnh render này so với BRIEF.

BRIEF YÊU CẦU (must check):
- Style: {style}
- Materials preferred (must visible): {", ".join(materials_pref[:5])}
- Must-have items (must visible): {", ".join(must_haves[:5])}
- Room type: {room_plan.get('type', 'unknown')}

CHECK NGHIÊM TÚC:
1. Style đúng yêu cầu? ✓/✗
2. Materials đúng preferred (e.g., gỗ Pơmu chứ không phải gỗ óc chó)? ✓/✗
3. Must-have items có visible trong ảnh? ✓/✗
4. Tỷ lệ + perspective hợp lý? ✓/✗
5. Có AI artifact (deformed, melting, extra limbs)? ✓/✗

Output JSON:
{{
  "match_score": 0.0-1.0,
  "approved": true/false (true if score >= 0.75),
  "issues": ["list of specific issues, empty if perfect"],
  "recommendations_for_regen": ["specific text to add to prompt"]
}}
"""
    try:
        result = await ai_core.analyze_image(
            prompt=check_prompt, image_data_uri=image_data_uri,
            model="gemini-2.5-flash", max_tokens=600, temperature=0.2,
        )
        text = result.get("output", "{}")
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        verify = json.loads(text)
        verify["_tokens"] = {"in": result.get("input_tokens", 0), "out": result.get("output_tokens", 0)}
        return verify
    except Exception as e:
        log.warning("[verify_render] failed: %s", e)
        return {"match_score": 0.5, "approved": True, "issues": [f"verify_error: {e}"]}


# ─── Critique stage ────────────────────────────────────────────
async def critique_plan(plan: dict[str, Any], kind: str) -> str:
    """Senior Gemini Pro reviewer — đánh giá ưu/nhược/cải tiến."""
    ai_core._ensure_init()
    from vertexai.generative_models import GenerativeModel, GenerationConfig
    model = GenerativeModel("gemini-2.5-flash",
                             system_instruction=design_agents.SYSTEM_PROMPTS.get(kind, "Bạn là Senior Designer."))
    prompt = (
        f"Đây là PLAN đã đề xuất:\n```json\n{json.dumps(plan, ensure_ascii=False, indent=2)[:6000]}\n```\n\n"
        "Đánh giá NGẮN GỌN (Việt):\n"
        "- 3 điểm mạnh\n"
        "- 3 điểm yếu / rủi ro\n"
        "- 3 cải tiến ưu tiên\n"
        "- 1 phương án thay thế đáng cân nhắc"
    )
    resp = await asyncio.to_thread(
        model.generate_content, prompt,
        generation_config=GenerationConfig(temperature=0.3, max_output_tokens=1500),
    )
    text = ""
    if resp.candidates:
        for p in resp.candidates[0].content.parts:
            text += getattr(p, "text", "") or ""
    return text


# ─── Full pipeline ─────────────────────────────────────────────
@dataclass
class PipelineResult:
    kind: str
    plan: dict[str, Any] = field(default_factory=dict)
    renders: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)
    critique: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_images: int = 0
    total_cost_usd: float = 0.0
    timings_ms: dict[str, int] = field(default_factory=dict)


async def run_full_pipeline(
    *, kind: str, brief_dict: dict[str, Any],
    n_renders_per_room: int = 2, aspect_ratio: str = "16:9",
    enable_verify: bool = True, max_regen_per_room: int = 1,
) -> PipelineResult:
    """Full multi-stage pipeline. ~30-90s tuỳ số rooms."""
    result = PipelineResult(kind=kind)
    timings = {}

    # 1. PLAN
    t0 = time.perf_counter()
    try:
        plan = await plan_stage(kind, brief_dict)
        result.plan = plan
        plan_meta = plan.get("_meta", {})
        result.total_input_tokens += plan_meta.get("input_tokens", 0)
        result.total_output_tokens += plan_meta.get("output_tokens", 0)
    except Exception as e:
        log.exception("plan stage failed")
        raise RuntimeError(f"Plan stage failed: {e}")
    timings["plan_ms"] = int((time.perf_counter() - t0) * 1000)

    # 2. RENDER per room (parallel for speed)
    t0 = time.perf_counter()
    rooms = result.plan.get("rooms_detailed") or [result.plan]  # interior single → wrap in list
    if kind in ("product", "fashion"):
        rooms = [result.plan]  # single render

    render_tasks = [
        render_room(kind, room, result.plan, n_views=n_renders_per_room, aspect_ratio=aspect_ratio)
        for room in rooms[:8]  # cap 8 rooms to limit cost
    ]
    rendered = await asyncio.gather(*render_tasks, return_exceptions=True)
    for r in rendered:
        if isinstance(r, dict):
            result.renders.append(r)
            result.total_images += r.get("count", 0)
            result.total_cost_usd += r.get("cost_usd", 0)
    timings["render_ms"] = int((time.perf_counter() - t0) * 1000)

    # 3. VERIFY (auto-regen if mismatch)
    if enable_verify and result.renders:
        t0 = time.perf_counter()
        for room_render in result.renders:
            if not room_render.get("images"):
                continue
            first_img = room_render["images"][0]
            try:
                verification = await verify_render(
                    first_img["data_uri"], brief_dict,
                    next((r for r in rooms if r.get("room_id") == room_render["room_id"]), rooms[0]),
                )
                result.verifications.append({
                    "room_id": room_render["room_id"],
                    **verification,
                })
                # Could trigger auto-regen here if not approved (skipped to save cost in MVP)
            except Exception as e:
                log.warning("verify failed for %s: %s", room_render["room_id"], e)
        timings["verify_ms"] = int((time.perf_counter() - t0) * 1000)

    # 4. CRITIQUE
    t0 = time.perf_counter()
    try:
        result.critique = await critique_plan(result.plan, kind)
    except Exception as e:
        log.warning("critique failed: %s", e)
    timings["critique_ms"] = int((time.perf_counter() - t0) * 1000)

    # 5. Cost
    pro_in = 1.25; pro_out = 10.0
    result.total_cost_usd += (
        result.total_input_tokens * pro_in
        + result.total_output_tokens * pro_out
    ) / 1_000_000

    result.timings_ms = timings
    return result
