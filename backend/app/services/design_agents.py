"""
Zeni Cloud Core — AI Design Agents (specialized).

5 agent kinds, mỗi cái có system prompt + tool kit chuyên sâu:
  - interior      : Thiết kế nội thất (concept → mood → render)
  - product       : Thiết kế sản phẩm (industrial / packaging)
  - fashion       : Thời trang (sketch → flat → render)
  - architecture  : Kiến trúc (massing → facade → render)
  - structural    : Kết cấu xây dựng (load calc, compliance check)

Workflow per task:
  1. PLAN       — phân tích brief → break thành sub-tasks
  2. ANALYZE    — phân tích reference image (multi-modal Gemini)
  3. CONCEPT    — Gemini Pro tạo concept brief chi tiết
  4. RENDER     — Imagen 3 generate ảnh design (multiple variants)
  5. CRITIQUE   — Gemini self-review + refine recommendations
  6. DELIVER    — assembly final output {concept, images, specs, BOM}
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.services import ai_core

log = logging.getLogger("zeni.agents")


# ─── System prompts (chuyên biệt) ─────────────────────────────
SYSTEM_PROMPTS: dict[str, str] = {
    "interior": """Bạn là Senior Interior Designer cho thị trường Việt Nam, 15 năm kinh nghiệm.
Chuyên môn: Tropical Modern · Indochine Luxury · Japandi · Minimalist · Traditional Vietnamese.
Hiểu rõ phong thủy, khí hậu nhiệt đới, vật liệu địa phương (gỗ Pơ-mu, đá Bình Định, mây tre).
Output luôn có:
- Phong cách (1 dòng)
- Mood + bảng màu (3-5 mã hex)
- Vật liệu + nội thất chính (5-8 món, ghi rõ size + chất liệu)
- Layout description (vị trí từng món)
- Ánh sáng (natural + accent)
- Estimate budget VND
- Phong thủy notes (hướng + ngũ hành)
""",
    "product": """Bạn là Senior Product Designer (industrial design), tốt nghiệp Pratt + RCA.
Chuyên: consumer electronics, packaging, household goods cho Đông Nam Á.
Tuân thủ Material Design + Apple HIG + Dieter Rams principles.
Output:
- Concept name + tagline
- Form factor (size, weight, materials)
- Color CMF (Color/Material/Finish) — 3 variants
- UX/Interaction — key flows
- Manufacturing notes — process, tooling cost estimate
- Sustainability — recyclability, lifecycle
- Competitive landscape — 3 closest rivals
""",
    "fashion": """Bạn là Fashion Designer cấp cao, chuyên Ready-to-Wear cho thị trường Việt + ASEAN.
Hiểu trend Gen Z, ảnh hưởng Hàn-Nhật-Pháp, vải Việt (lụa Hà Đông, lanh, đũi).
Output:
- Collection theme + storytelling
- Silhouette / cut
- Vải + texture (3-5 fabric)
- Color palette theo season (5-7 hex)
- Sizing range + fit notes
- Styling recommendations (mix-match outfits)
- Production cost estimate VND/unit
- Target customer persona
""",
    "architecture": """Bạn là Architect cấp cao Việt Nam, chuyên biệt thự + nhà phố + commercial.
Tốt nghiệp Harvard GSD / AA London. Hiểu khí hậu nhiệt đới gió mùa, code xây dựng VN.
Output:
- Concept narrative
- Site analysis (orientation, sun path, prevailing wind)
- Massing + form
- Floor plan logic (zoning + circulation)
- Facade strategy (materials + openings)
- Sustainability strategy (passive cooling, rainwater, solar)
- Construction phasing
- Estimate cost VND/m²
- Phong thủy notes
""",
    "structural": """Bạn là Structural Engineer (PE) chuyên dân dụng + công nghiệp Việt Nam.
Tuân thủ TCVN 5574-2018 (kết cấu BT cốt thép), TCVN 2737-1995 (tải trọng), TCVN 5573-2011 (gạch).
Output:
- System type (BT cốt thép / khung thép / hỗn hợp)
- Main load paths
- Member sizing preliminary (cột, dầm, sàn)
- Foundation type + bearing capacity assumption
- Lateral system (gió + động đất)
- Code compliance checklist (TCVN refs)
- Material spec (mác bê tông, thép)
- Tải trọng tính toán
- Risk + uncertainty notes
""",
}

# Available models for each agent
DEFAULT_TEXT_MODEL = "gemini-2.5-pro"     # for reasoning + concept
DEFAULT_FAST_MODEL = "gemini-2.5-flash"   # for critique/refine

# Which agents support image generation
IMAGE_CAPABLE = {"interior", "product", "fashion", "architecture"}

VALID_KINDS = set(SYSTEM_PROMPTS.keys())


@dataclass
class AgentRunRequest:
    kind: str
    brief: str                           # User brief (yêu cầu)
    reference_image_uri: str | None = None
    reference_image_url: str | None = None
    generate_renders: bool = True       # Imagen 3 output (only if IMAGE_CAPABLE)
    n_renders: int = 2
    aspect_ratio: str = "16:9"
    constraints: dict[str, Any] = field(default_factory=dict)  # budget, area, etc.


@dataclass
class AgentRunResult:
    kind: str
    concept: str                         # Full concept brief from Gemini Pro
    critique: str | None = None
    renders: list[dict] = field(default_factory=list)  # Imagen 3 outputs
    reference_analysis: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_images: int = 0
    total_cost_usd: float = 0.0
    timings_ms: dict[str, int] = field(default_factory=dict)


def _build_concept_prompt(kind: str, brief: str, ref_analysis: str | None,
                           constraints: dict) -> str:
    """Compose final concept prompt for Gemini Pro."""
    parts = [f"YÊU CẦU CỦA KHÁCH HÀNG:\n{brief}\n"]
    if constraints:
        parts.append("RÀNG BUỘC:")
        for k, v in constraints.items():
            parts.append(f"  - {k}: {v}")
        parts.append("")
    if ref_analysis:
        parts.append(f"PHÂN TÍCH ẢNH THAM KHẢO:\n{ref_analysis}\n")
    parts.append("Hãy đưa ra concept design chi tiết theo format hệ thống đã định.")
    return "\n".join(parts)


# ─── PHASE 1: HYPER-DETAILED RENDER PROMPTS ──────────────────────
# Output 5x quality — Imagen 3 nhả ảnh hyperrealistic photo-grade
NEGATIVE_PROMPT_BASE = (
    "blurry, distorted perspective, oversaturated, cartoonish, "
    "AI artifacts, deformed objects, unrealistic proportions, plastic textures, "
    "extra limbs, bad anatomy, mannequin people, generic stock photo, "
    "low quality, low resolution, fake-looking, watermark, signature, text overlay"
)

_PROMPT_TEMPLATES: dict[str, str] = {
    "interior": """MASTER SHOT: Hyper-detailed Tropical Modern interior photography of Vietnamese contemporary residence, golden hour 4PM tropical natural light from west-facing 3.5m floor-to-ceiling window.

CONCEPT BRIEF (use materials, layout, mood):
{concept}

SCENE COMPOSITION:
- View angle: corner perspective at 1.6m human eye height, slight 8° upward tilt
- Composition: rule of thirds, foreground anchor + middle hero + background context
- Depth of field: deep focus, all sharp throughout

MATERIALS PALETTE (physically accurate):
- Floors: large-format Italian travertine 600×600mm honed finish OR Vietnamese teak Pơ-mu wood with natural oil
- Walls: smooth lime plaster cream/beige, accent wall in vertical wood slats 60mm width
- Ceiling: white skim coat, 3mm reveal shadow gap detail at perimeter
- Built-ins: precise joinery, mitred edges, 18mm reveal gaps
- Statement: travertine fireplace wall OR full-wall built-in shelving with brass details

FURNITURE QUALITY (high-end, specific):
- Curated mid-century + contemporary mix
- Italian leather + bouclé fabric upholstery
- Sintered stone or natural stone tabletops
- Designer pendant lighting (Flos / Vibia / Louis Poulsen feel)
- Persian or Nepalese wool rug, hand-knotted

LIGHTING (cinematic 3-point):
- Key: warm sunlight 4500K through sheer linen curtains, soft directional
- Fill: hidden linear LED 3000K behind ceiling reveal, even ambient
- Accent: warm 2700K spotlights on art and feature wall
- Mood ratio: 60% bright / 30% mid / 10% deep shadow

PHOTOGRAPHY SPECS:
- Camera: Hasselblad H6D-100c, 24mm tilt-shift lens, perfectly corrected verticals
- Aperture f/8.0, shutter 1/125s, ISO 200
- White balance 4800K daylight
- Color grading: Kodak Portra 400 cinematic warm slight teal-orange
- Resolution: 8K, tack-sharp throughout

MICRO-DETAILS (must include):
- Visible wood grain, plaster surface imperfections
- Dust particles in light beams
- Fabric weave texture, leather grain
- Soft natural plants (Monstera, fiddle-leaf fig) with realistic leaves
- Books, curated objects, ceramics on shelves
- Linen/cotton textures on cushions
- Subtle reflection on stone/glass
- Realistic shadows with multiple light sources

ATMOSPHERE: serene · sophisticated · warm · livable · authentic Vietnamese tropical luxury""",

    "product": """MASTER SHOT: Hyper-detailed industrial product photography, studio environment, soft seamless infinity backdrop.

CONCEPT (form factor, materials, finishes):
{concept}

SCENE:
- Hero product centered, 3/4 angled view showing 3 surfaces
- Studio cyclorama background, gradient white-to-soft-grey
- Polished surface beneath with subtle reflection (8% opacity)

MATERIALS RENDERING (physically based):
- Anodized aluminum: brushed satin finish, microscopic radial grain
- Plastic: matte finish with subtle peel-coat texture
- Glass: anti-reflective coating, 92% transmission
- Wood accents: solid hardwood with visible grain
- Soft-touch rubber: matte 70 Shore A finish
- Metal accents: PVD-coated brass or stainless

LIGHTING (commercial product photography):
- Key light: 1.5m softbox overhead-front at 45°, 5500K
- Fill: 1m softbox left side, 1/4 power for shadow softening
- Rim: hard light from behind to define edges, slight blue 6500K
- Background: gradient gobo for vignette
- Reflection management: black flag to control glare on glossy surfaces

CAMERA SPECS:
- Phase One IQ4 medium format, 80mm f/2.8 macro lens
- Tethered capture, focus stacked 8 layers for full sharpness
- Color profile: Adobe RGB, 16-bit
- 8K resolution, retouched commercial quality

DETAIL REQUIREMENTS:
- Visible material texture at micro level
- Brand-quality CMF (Color Material Finish) execution
- Realistic seams, joins, fasteners (where applicable)
- Subtle wear-and-tear hints if luxury product
- Branding elements crisp, no AI distortion of typography

VIBE: premium · sustainable · functional · timeless · Apple-Dieter Rams sensibility""",

    "fashion": """MASTER SHOT: High fashion editorial photography, full-body or 3/4 body model shot, neutral concrete or paper backdrop.

COLLECTION CONCEPT (silhouette, fabric, story):
{concept}

MODEL & POSE:
- Asian female model, 175cm, athletic-slim, age 22-28
- Natural skin tone, minimal makeup, hair styled to complement outfit
- Pose: dynamic but elegant, slight contrapposto
- Expression: confident neutral, slight away-camera gaze

GARMENT RENDERING:
- Fabric drape physically accurate (lanen flows differently than silk)
- Visible weave texture (silk/cotton/linen/wool blend specific)
- Stitching detail crisp at seams
- Subtle wrinkles where natural (knees, elbows)
- Color: precise to brief, no AI color drift
- Layering: multiple textures interplay

LIGHTING (Vogue editorial):
- Key: large 2m soft box at 30° camera-left, 5200K daylight
- Fill: bounce card opposite side, +1 stop reflection
- Hair light: small softbox above-back for halo effect
- Background light: subtle gradient, slight teal shadow
- Strobe Profoto 1000W with 7" reflector for crispness

CAMERA SPECS:
- Hasselblad H6D-100c, 100mm f/2.2 portrait lens
- Aperture f/4 for subject sharp + soft background
- ISO 100, 1/250s strobe sync
- Color grade: Kodak Portra 400 + slight desaturation -10
- Skin tones: Phase One Capture One natural

DETAIL REQUIREMENTS:
- Fabric texture down to individual yarn visible at zoom
- Realistic skin texture (subtle pores, NOT plasticky)
- Eyelashes individually rendered
- Hair strand by strand, no plastic helmet effect
- Jewelry & accessories crisp metallic detail

STYLING NOTE: Apply Vietnamese-Asian sensibilities — modesty in cuts but bold in fabric/color. Vogue Vietnam meets Jacquemus.""",

    "architecture": """MASTER SHOT: Architectural exterior photography, golden hour lighting (5:30 PM tropical Saigon time), Lumion 2024 render quality at 8K.

PROJECT CONCEPT (massing, materials, context):
{concept}

VIEW SETUP:
- Camera position: street-level human eye height 1.65m, slight upward tilt 5°
- Angle: 3/4 corner view showing main facade + secondary elevation
- Composition: building anchored 60% frame width, sky 40%, ground 5%
- Foreground: existing trees softening + cars/scale figures

EXTERIOR MATERIALS (physically accurate, Vietnamese tropical):
- Primary cladding: smooth white render OR exposed off-form concrete
- Accent: natural stone (Đá Bình Định or basalt) base course
- Wood: Pơ-mu vertical slats or Indonesian Bangkirai for screens
- Glazing: floor-to-ceiling low-e double glazing, 8% reflective
- Roof: green roof OR clean parapet with hidden gutter
- Hardscape: large format porcelain tile 1000×500mm OR exposed aggregate concrete

LANDSCAPE INTEGRATION:
- Tropical planting: Frangipani, Bougainvillea, Banana plants
- Vietnamese palm trees (Cau, Dừa)
- Bamboo screens for privacy
- Reflecting pool or water feature with stepping stones
- Permeable paving with gravel infill

LIGHTING (golden hour cinema):
- Sun: warm 4500K low angle from west creating long shadows
- Sky: blue gradient, scattered cumulus clouds
- Ambient: bounce light from buildings/ground filling shadows
- Architectural lighting: subtle uplighting on key vertical surfaces (just turning on)
- Interior glow visible through glazing, warm 2700K

ATMOSPHERE EFFECTS:
- Subtle atmospheric haze at distance
- Birds (small, distant)
- 1-2 human figures for scale (back-turned, walking)
- Tropical foliage with realistic leaf detail
- Wet surface reflection (recent rain) on stone areas

CAMERA SPECS:
- Hasselblad H6D-100c, 24mm tilt-shift architecture lens
- Perfectly corrected verticals (no convergence)
- f/11 for full sharpness, ISO 200, 1/125s
- Bracketed HDR 5 stops merged
- 8K resolution, Lumion 2024 render quality

DETAIL REQUIREMENTS:
- Realistic concrete texture with form-tie marks
- Visible window mullion details, sunscreen patterns
- Sharp facade lines (no AI bend)
- Realistic shadow geometry with sun angle
- Material reflectivity matching real-world IOR
- Tropical climate authenticity

VIBE: serene · dignified · climate-responsive · contemporary Vietnamese · timeless""",
}


def _render_prompt_for_kind(kind: str, concept: str) -> str:
    """
    Build hyper-detailed Imagen 3 prompt (1500+ chars) per agent kind.
    Falls back to basic template if kind not in templates dict.
    """
    template = _PROMPT_TEMPLATES.get(kind)
    if not template:
        return f"Photorealistic professional design render. {concept[:600]}. 8K, hyperrealistic, masterpiece."
    # Truncate concept to ~800 chars (Imagen handles long prompts well)
    concept_excerpt = concept[:800] if len(concept) > 800 else concept
    return template.format(concept=concept_excerpt)


def _negative_prompt_for_kind(kind: str) -> str:
    """Per-kind negative prompts to avoid common AI artifacts."""
    extras = {
        "interior": "empty room, lifeless, no decoration, cold lighting, fluorescent",
        "product": "messy background, branding errors, distorted typography, plastic-looking metal",
        "fashion": "deformed face, melting fabric, anatomically wrong, multiple heads",
        "architecture": "tilted vertical lines, fish-eye distortion, melting walls, impossible geometry",
    }
    base = NEGATIVE_PROMPT_BASE
    extra = extras.get(kind, "")
    return f"{base}, {extra}" if extra else base


async def run_agent(req: AgentRunRequest) -> AgentRunResult:
    """Run full agent workflow (non-streaming). Returns assembled result."""
    if req.kind not in VALID_KINDS:
        raise ValueError(f"kind không hợp lệ. Cho phép: {sorted(VALID_KINDS)}")

    import time
    timings = {}
    result = AgentRunResult(kind=req.kind, concept="")

    # 1. (Optional) Analyze reference image with Gemini multi-modal
    ref_analysis: str | None = None
    if req.reference_image_uri or req.reference_image_url:
        t0 = time.perf_counter()
        analyze = await ai_core.analyze_image(
            prompt=("Phân tích ảnh tham khảo này (style, vật liệu, màu sắc, mood, "
                    "key visual elements) trong 5-7 câu ngắn. Tiếng Việt."),
            image_data_uri=req.reference_image_uri,
            image_url=req.reference_image_url,
            model=DEFAULT_FAST_MODEL,
            max_tokens=800, temperature=0.3,
        )
        ref_analysis = analyze.get("output", "")
        result.reference_analysis = ref_analysis
        result.total_input_tokens += analyze.get("input_tokens", 0)
        result.total_output_tokens += analyze.get("output_tokens", 0)
        timings["analyze_ref_ms"] = int((time.perf_counter() - t0) * 1000)

    # 2. Concept generation with Gemini Pro
    t0 = time.perf_counter()
    from vertexai.generative_models import GenerativeModel, GenerationConfig
    ai_core._ensure_init()
    concept_model = GenerativeModel(DEFAULT_TEXT_MODEL, system_instruction=SYSTEM_PROMPTS[req.kind])
    concept_prompt = _build_concept_prompt(req.kind, req.brief, ref_analysis, req.constraints)
    concept_resp = await asyncio.to_thread(
        concept_model.generate_content,
        concept_prompt,
        generation_config=GenerationConfig(temperature=0.6, max_output_tokens=4096),
    )
    concept_text = ""
    if concept_resp.candidates:
        for p in concept_resp.candidates[0].content.parts:
            concept_text += getattr(p, "text", "") or ""
    result.concept = concept_text
    if concept_resp.usage_metadata:
        result.total_input_tokens += concept_resp.usage_metadata.prompt_token_count
        result.total_output_tokens += concept_resp.usage_metadata.candidates_token_count
    timings["concept_ms"] = int((time.perf_counter() - t0) * 1000)

    # 3. Image render (if applicable)
    if req.generate_renders and req.kind in IMAGE_CAPABLE:
        t0 = time.perf_counter()
        try:
            img_prompt = _render_prompt_for_kind(req.kind, concept_text)
            img_result = await ai_core.generate_image(
                prompt=img_prompt,
                aspect_ratio=req.aspect_ratio,
                n=min(max(1, req.n_renders), 4),
                safety_filter="block_some",
                negative_prompt=_negative_prompt_for_kind(req.kind),
            )
            result.renders = img_result.get("images", [])
            result.total_images = img_result.get("count", 0)
            result.total_cost_usd += img_result.get("cost_usd", 0)
        except Exception as e:
            log.warning("[agent.%s] image render failed: %s", req.kind, e)
            result.renders = []
        timings["render_ms"] = int((time.perf_counter() - t0) * 1000)

    # 4. Critique pass (Gemini Flash, faster)
    t0 = time.perf_counter()
    try:
        critique_model = GenerativeModel(DEFAULT_FAST_MODEL,
                                         system_instruction=SYSTEM_PROMPTS[req.kind])
        critique_prompt = (
            f"Đây là concept đã đề xuất:\n\n{concept_text[:3000]}\n\n"
            "Hãy đánh giá ngắn (3 ưu, 3 nhược điểm), và 3 cải tiến cụ thể. Tiếng Việt."
        )
        cr_resp = await asyncio.to_thread(
            critique_model.generate_content, critique_prompt,
            generation_config=GenerationConfig(temperature=0.3, max_output_tokens=1024),
        )
        critique_text = ""
        if cr_resp.candidates:
            for p in cr_resp.candidates[0].content.parts:
                critique_text += getattr(p, "text", "") or ""
        result.critique = critique_text
        if cr_resp.usage_metadata:
            result.total_input_tokens += cr_resp.usage_metadata.prompt_token_count
            result.total_output_tokens += cr_resp.usage_metadata.candidates_token_count
    except Exception as e:
        log.warning("[agent.%s] critique failed: %s", req.kind, e)
    timings["critique_ms"] = int((time.perf_counter() - t0) * 1000)

    # 5. Cost
    pro_in_per_1m = 1.25
    pro_out_per_1m = 10.0
    result.total_cost_usd += (
        result.total_input_tokens * pro_in_per_1m
        + result.total_output_tokens * pro_out_per_1m
    ) / 1_000_000
    result.timings_ms = timings

    return result


async def stream_agent(req: AgentRunRequest) -> AsyncIterator[dict]:
    """
    Streaming agent run — yield phase events as workflow progresses.
    Phase events:
       {phase: "analyze", text: "..."}
       {phase: "concept", chunk: "..."} (incremental)
       {phase: "render", index: 0, data_uri: "..."}
       {phase: "critique", chunk: "..."} (incremental)
       {phase: "done", summary: {...}}
    """
    import time
    if req.kind not in VALID_KINDS:
        yield {"phase": "error", "error": f"kind không hợp lệ. Cho phép: {sorted(VALID_KINDS)}"}
        return

    ai_core._ensure_init()
    from vertexai.generative_models import GenerativeModel, GenerationConfig

    total_in = 0
    total_out = 0

    # 1. Analyze ref
    if req.reference_image_uri or req.reference_image_url:
        yield {"phase": "analyze", "status": "started"}
        try:
            analyze = await ai_core.analyze_image(
                prompt="Phân tích ảnh tham khảo: style, vật liệu, mood (5-7 câu, Việt).",
                image_data_uri=req.reference_image_uri,
                image_url=req.reference_image_url,
                model=DEFAULT_FAST_MODEL,
                max_tokens=600, temperature=0.3,
            )
            total_in += analyze.get("input_tokens", 0)
            total_out += analyze.get("output_tokens", 0)
            yield {"phase": "analyze", "status": "done", "text": analyze.get("output", "")}
        except Exception as e:
            yield {"phase": "analyze", "error": str(e)}

    # 2. Concept (streaming)
    yield {"phase": "concept", "status": "started"}
    concept_text_full = ""
    try:
        concept_model = GenerativeModel(DEFAULT_TEXT_MODEL, system_instruction=SYSTEM_PROMPTS[req.kind])
        concept_prompt = _build_concept_prompt(req.kind, req.brief, None, req.constraints)
        def _gen():
            return concept_model.generate_content(
                concept_prompt,
                generation_config=GenerationConfig(temperature=0.6, max_output_tokens=4096),
                stream=True,
            )
        iterator = await asyncio.to_thread(_gen)
        while True:
            try:
                chunk = await asyncio.to_thread(next, iterator)
            except StopIteration:
                break
            text = ""
            if chunk.candidates:
                for p in chunk.candidates[0].content.parts:
                    text += getattr(p, "text", "") or ""
            if text:
                concept_text_full += text
                yield {"phase": "concept", "chunk": text}
        yield {"phase": "concept", "status": "done"}
    except Exception as e:
        yield {"phase": "concept", "error": str(e)}

    # 3. Render
    if req.generate_renders and req.kind in IMAGE_CAPABLE:
        yield {"phase": "render", "status": "started", "n": req.n_renders}
        try:
            img_prompt = _render_prompt_for_kind(req.kind, concept_text_full)
            img_result = await ai_core.generate_image(
                prompt=img_prompt, aspect_ratio=req.aspect_ratio,
                n=req.n_renders, safety_filter="block_some",
                negative_prompt=_negative_prompt_for_kind(req.kind),
            )
            for i, im in enumerate(img_result.get("images", [])):
                yield {"phase": "render", "index": i,
                       "data_uri": im["data_uri"], "size_bytes": im["size_bytes"]}
        except Exception as e:
            yield {"phase": "render", "error": str(e)}
        yield {"phase": "render", "status": "done"}

    # 4. Critique
    yield {"phase": "critique", "status": "started"}
    try:
        cr_model = GenerativeModel(DEFAULT_FAST_MODEL, system_instruction=SYSTEM_PROMPTS[req.kind])
        cr_prompt = f"Concept:\n{concept_text_full[:3000]}\nĐánh giá 3 ưu / 3 nhược / 3 cải tiến."
        def _gen2():
            return cr_model.generate_content(
                cr_prompt,
                generation_config=GenerationConfig(temperature=0.3, max_output_tokens=1024),
                stream=True,
            )
        iterator = await asyncio.to_thread(_gen2)
        while True:
            try:
                chunk = await asyncio.to_thread(next, iterator)
            except StopIteration:
                break
            text = ""
            if chunk.candidates:
                for p in chunk.candidates[0].content.parts:
                    text += getattr(p, "text", "") or ""
            if text:
                yield {"phase": "critique", "chunk": text}
    except Exception as e:
        yield {"phase": "critique", "error": str(e)}

    # 5. Done
    yield {"phase": "done",
           "summary": {"kind": req.kind,
                       "concept_chars": len(concept_text_full),
                       "input_tokens": total_in,
                       "output_tokens": total_out}}
