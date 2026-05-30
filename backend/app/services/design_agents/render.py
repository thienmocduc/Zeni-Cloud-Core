"""
Render phase (Item 2) — Imagen 3 luxury perspective renders for the design orchestrator.

Reuses the validated photography recipe (Hasselblad H6D-100c / 8K / Kodak Portra 400
cinematic grade) and a per-style descriptor so every key view comes out genuinely
luxury — not a placeholder. Grounds each interior prompt in the InteriorDesignerAgent's
locked palette + materials so renders match the design spec.

Routes through ai_core.generate_image (Imagen 3 on Vertex AI) — RULE-9 compliant,
no third-party image provider. $0.04/image.

Public entry: `await render_concept(dna, interior_spec, style, num_floors, ...)`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services import ai_core

log = logging.getLogger("zeni.design_render")

# imagen-3.0-fast-generate-001 — quota 20/min (vs 1/min for generate-002), near-equal
# luxury quality, faster + cheaper. Validated against the generate-002 baseline.
RENDER_MODEL = "imagen-3.0-fast-generate-001"
_MAX_RETRIES = 2          # extra attempts on transient 429 / rate limit
_RETRY_BACKOFF_S = 12.0   # base backoff; grows per attempt

# Shared negative prompt — inlined (the flat design_agents.py module is shadowed by
# this package, so it can't be imported here).
NEGATIVE_PROMPT_BASE = (
    "blurry, distorted perspective, oversaturated, cartoonish, "
    "AI artifacts, deformed objects, unrealistic proportions, plastic textures, "
    "extra limbs, bad anatomy, mannequin people, generic stock photo, "
    "low quality, low resolution, fake-looking, watermark, signature, text overlay, "
    "floor plan, blueprint, diagram, sketch, drawing"
)


# ─── Per-style descriptors (arch / interior furnishing / mood) ──────────────────
# Keys cover ALLOWED_STYLES in api/design.py; "_default" is the fallback.
STYLE_DESCRIPTORS: dict[str, dict[str, str]] = {
    "indochine": {
        "arch": ("Indochine fusion — French colonial symmetry meets Vietnamese tropical: "
                 "cream lime-washed walls with ochre undertone, deep-teak louvered shutters, "
                 "terracotta clay-tile pitched roof with generous eaves and exposed rafter tails, "
                 "arched ground-floor verandah on slender columns, encaustic cement floor tiles, "
                 "wrought-iron juliet balconies, transom fanlights, brass hardware"),
        "interior": ("warm Vietnamese teak parquet, lime-plaster walls with a vertical teak-slat "
                     "accent, white ceiling with exposed dark-stained timber beams, rattan ceiling "
                     "fan, cognac leather chesterfield, rattan & rosewood lounge chairs, "
                     "blue-and-white ceramic vases, encaustic-tile rug zone, woven bamboo pendants"),
        "mood": "timeless, elegant, serene, authentic Vietnamese colonial luxury",
    },
    "modern": {
        "arch": ("clean modern architecture — crisp white render and warm timber cladding, "
                 "large frameless floor-to-ceiling glazing, flat cantilevered roof planes, "
                 "stone-clad feature volumes, recessed warm linear facade lighting"),
        "interior": ("large-format honed travertine floor, smooth plaster walls, full-height glazing, "
                     "designer modular sofa in bouclé, sintered-stone coffee table, sculptural "
                     "pendant lighting, hand-knotted wool rug, curated minimal decor"),
        "mood": "clean, sophisticated, calm, contemporary luxury",
    },
    "luxury": {
        "arch": ("grand contemporary-classic luxury villa — book-matched marble cladding, "
                 "bronze-framed glazing, double-height portico, manicured formal landscaping, "
                 "dramatic architectural facade lighting"),
        "interior": ("polished marble floor with brass inlay, silk-finish walls, coffered ceiling, "
                     "velvet and full-grain leather seating, crystal chandelier, statement marble "
                     "fireplace, gold-leaf accents, curated art and sculpture"),
        "mood": "opulent, refined, grand, five-star luxury",
    },
    "tropical": {
        "arch": ("tropical-modern resort villa — deep overhanging roofs, natural stone and timber, "
                 "open breezeways, lush vertical greenery, infinity water feature, warm timber screens"),
        "interior": ("teak timber floor, natural stone feature walls, rattan and solid-wood furniture, "
                     "linen upholstery, abundant indoor plants, woven textures, indoor-outdoor flow, "
                     "warm layered lighting"),
        "mood": "relaxed, lush, airy, tropical resort luxury",
    },
    "japandi": {
        "arch": ("Japandi architecture — warm minimalist forms, oak and charred-cedar cladding, "
                 "clean horizontal lines, shoji-inspired screens, restrained tactile materials"),
        "interior": ("pale oak floor, micro-cement walls, low-profile natural-wood furniture, "
                     "linen and wool textiles, handmade ceramics, paper lantern lighting, "
                     "ikebana, quiet uncluttered composition"),
        "mood": "serene, warm, minimal, refined wabi-inspired calm",
    },
    "scandinavian": {
        "arch": ("Scandinavian architecture — pale timber cladding, gabled forms, generous glazing, "
                 "soft natural light, restrained palette"),
        "interior": ("light oak floor, white plaster walls, light-wood furniture, wool and sheepskin "
                     "textiles, soft neutral palette, greenery, warm hygge lighting"),
        "mood": "bright, cosy, airy, understated Nordic elegance",
    },
    "minimalist": {
        "arch": ("pure minimalist architecture — monolithic white volumes, hidden detailing, "
                 "frameless glazing, single warm timber accent, sharp shadow lines"),
        "interior": ("seamless micro-cement floor and walls, concealed joinery, a few sculptural "
                     "furniture pieces, monochrome palette with one warm accent, museum-quiet space"),
        "mood": "pure, calm, precise, quiet luxury",
    },
    "industrial": {
        "arch": ("refined industrial architecture — exposed brick and board-formed concrete, "
                 "black steel-framed glazing, warm timber accents, Crittall windows"),
        "interior": ("polished concrete floor, exposed brick wall, black steel and reclaimed-wood "
                     "furniture, leather seating, Edison-warm pendant lighting, curated vintage decor"),
        "mood": "raw, warm, characterful, elevated industrial luxury",
    },
    "wabi-sabi": {
        "arch": ("wabi-sabi architecture — hand-troweled lime-plaster walls, weathered timber, "
                 "raw natural stone, soft imperfect organic forms, deep tactile texture"),
        "interior": ("limewash walls, aged-oak floor, handmade ceramics, raw-linen textiles, "
                     "organic low furniture, dried botanicals, soft diffused natural light"),
        "mood": "tranquil, grounded, organic, imperfect natural beauty",
    },
    "boho": {
        "arch": ("warm bohemian villa — earthy stucco walls, arched openings, terracotta tones, "
                 "timber pergola, abundant climbing greenery"),
        "interior": ("warm terracotta floor, layered textiles and rugs, rattan and carved-wood "
                     "furniture, macramé, abundant plants, eclectic curated decor, warm ambient light"),
        "mood": "warm, eclectic, lived-in, artisanal bohemian luxury",
    },
    "art-deco": {
        "arch": ("Art-Deco architecture — symmetrical stepped massing, fluted stone, bronze and "
                 "black detailing, geometric ornament, grand vertical emphasis"),
        "interior": ("marble floor with geometric inlay, lacquered panelling, velvet seating, "
                     "brass and smoked-glass accents, fluted detailing, statement geometric lighting"),
        "mood": "glamorous, geometric, bold, 1920s deco luxury",
    },
    "mid-century": {
        "arch": ("mid-century modern villa — low-slung horizontal lines, post-and-beam structure, "
                 "warm timber and stone, clerestory glazing, indoor-outdoor connection"),
        "interior": ("warm walnut floor, wood-panel feature wall, iconic mid-century furniture, "
                     "leather and tweed upholstery, brass and globe lighting, curated retro decor"),
        "mood": "warm, iconic, timeless, mid-century luxury",
    },
    "contemporary": {
        "arch": ("contemporary villa — sculptural massing, mixed stone-timber-glass facade, "
                 "large cantilevers, full-height glazing, integrated architectural lighting"),
        "interior": ("large-format stone floor, plaster and timber feature walls, designer "
                     "contemporary furniture, layered neutral palette, sculptural lighting, curated art"),
        "mood": "sophisticated, sculptural, refined, contemporary luxury",
    },
    "eclectic": {
        "arch": ("elegant eclectic villa — confident mix of classic and modern forms, layered "
                 "materials, expressive facade, refined detailing"),
        "interior": ("layered materials and eras, statement art, mixed designer furniture, rich "
                     "textiles, curated collections, warm dramatic lighting"),
        "mood": "expressive, curated, rich, collected luxury",
    },
}
_DEFAULT_DESC = {
    "arch": ("refined contemporary villa — premium mixed materials, generous glazing, warm "
             "facade lighting, manicured landscaping"),
    "interior": ("premium natural materials, designer furniture, layered neutral palette, "
                 "curated decor, warm cinematic lighting"),
    "mood": "sophisticated, serene, livable luxury",
}


# ─── Prompt grounding helpers ───────────────────────────────────────────────────
def _palette_str(interior_spec: dict[str, Any]) -> str:
    pal = (interior_spec or {}).get("palette") or []
    out: list[str] = []
    for p in pal[:5]:
        if isinstance(p, dict):
            bits = [str(p.get("name", "")).strip(), str(p.get("hex", "")).strip()]
            usage = str(p.get("usage", "")).strip()
            label = " ".join(b for b in bits if b)
            if usage:
                label = f"{label} ({usage})" if label else usage
            if label:
                out.append(label)
        elif isinstance(p, str) and p.strip():
            out.append(p.strip())
    return ", ".join(out)


def _materials_str(interior_spec: dict[str, Any]) -> str:
    mats = (interior_spec or {}).get("materials") or []
    out: list[str] = []
    for m in mats[:6]:
        if isinstance(m, str) and m.strip():
            out.append(m.strip())
        elif isinstance(m, dict):
            spec = str(m.get("spec", m.get("name", ""))).strip()
            if spec:
                out.append(spec)
    return ", ".join(out)


def _floors_txt(n: int) -> str:
    return {1: "single-storey", 2: "2-storey", 3: "3-storey", 4: "4-storey"}.get(n, f"{n}-storey")


# ─── Geometry grounding: make every render bespoke to THIS floor plan ───────────
def _all_rooms(geometry: dict | None) -> list[dict]:
    return [r for fl in (geometry or {}).get("floors", []) for r in fl.get("rooms", [])]


def _find_room(geometry: dict | None, keys: tuple[str, ...],
               avoid: tuple[str, ...] = ()) -> dict | None:
    """Largest non-wet, non-circulation room whose name matches a keyword."""
    best = None
    for r in _all_rooms(geometry):
        nm = str(r.get("name", "")).lower()
        if r.get("kind") == "wet" or r.get("role") in ("corridor", "stair"):
            continue
        if any(a in nm for a in avoid) or not any(k in nm for k in keys):
            continue
        if best is None or r.get("area_m2", 0) > best.get("area_m2", 0):
            best = r
    return best


def _ceiling_m(geometry: dict | None, num_floors: int) -> float:
    # Clear ceiling = floor-to-floor (locked 3.3m) minus slab+finishes (~0.55m) ≈ 3.0m.
    bh = (geometry or {}).get("building_height_m")
    if bh and num_floors:
        return max(2.7, min(3.3, round(bh / num_floors - 0.55, 1)))
    return 3.0


def _glazing(room: dict) -> str:
    per = room.get("on_perimeter") or {}
    names = {"left": "left", "right": "right", "top": "far", "bottom": "entrance-side"}
    walls = [names[e] for e in ("left", "right", "top", "bottom") if per.get(e)]
    if not walls:
        return "no large exterior window (interior room lit by a clerestory / light well)"
    if len(walls) == 1:
        return f"floor-to-ceiling glazing along the {walls[0]} wall, daylight from one side"
    return ("floor-to-ceiling corner glazing wrapping the "
            + " and ".join(walls) + " walls (bright dual-aspect corner room)")


def _interior_shell(room: dict | None, ceiling_m: float) -> str:
    """One precise line tying the render to the room's true footprint + apertures."""
    if not room:
        return ""
    w = float(room.get("w_m", 0) or 0)
    d = float(room.get("h_m", 0) or 0)
    if w <= 0 or d <= 0:
        return ""
    lo, hi = sorted((w, d))
    ratio = hi / max(lo, 0.1)
    if ratio >= 1.55:
        geom = (f"a LONG rectangular room, {w:.1f}m wide × {d:.1f}m deep, ~{ceiling_m:.1f}m ceiling — "
                f"compose the camera looking down its length so the true proportion reads; "
                f"this is NOT a square studio set")
    elif ratio <= 1.18:
        geom = f"a nearly square room, {w:.1f}m × {d:.1f}m, ~{ceiling_m:.1f}m ceiling"
    else:
        geom = f"a rectangular room, {w:.1f}m × {d:.1f}m, ~{ceiling_m:.1f}m ceiling"
    return (f"\nARCHITECTURAL SHELL (match THIS real room — not a stock photo): {geom}; "
            f"{_glazing(room)}; scale all furniture to fit these exact dimensions.")


def _exterior_shell(geometry: dict | None, num_floors: int) -> str:
    fp = (geometry or {}).get("footprint") or {}
    W, Dp = fp.get("w_m"), fp.get("d_m")
    if not (W and Dp):
        return ""
    feats: list[str] = []
    rooms = _all_rooms(geometry)
    gar = next((r for r in rooms
                if "gara" in str(r.get("name", "")).lower()
                or "garage" in str(r.get("name", "")).lower()), None)
    if gar:
        import re
        m = re.search(r"(\d+)", str(gar.get("name", "")))
        n = m.group(1) if m else ("2" if gar.get("area_m2", 0) > 28 else "1")
        feats.append(f"an integrated {n}-car garage at street level")
    if any(k in str(r.get("name", "")).lower()
           for r in rooms for k in ("ban công", "sân thượng", "hiên", "terrace", "balcon")):
        feats.append("planted upper-floor terraces / balconies")
    tail = (", " + ", ".join(feats)) if feats else ""
    return (f"\nBUILDING FORM (match exactly): a {_floors_txt(num_floors)} villa on a "
            f"{W:.0f}m × {Dp:.0f}m rectangular footprint{tail}. "
            f"Render exactly this storey count and massing — bespoke to these plans, not a generic house.")


def _exterior_prompt(style: str, num_floors: int, location: str, desc: dict[str, str],
                     shell: str = "") -> str:
    return f"""MASTER SHOT: Award-winning architectural photography of a luxury {_floors_txt(num_floors)} {style} villa in {location}, Vietnam, late golden-hour 5PM warm light easing into blue dusk.

ARCHITECTURE: {desc['arch']}.{shell}

LANDSCAPE: lush tropical planting (frangipani, areca palm), manicured lawn, natural-stone path, warm uplighting washing the facade, a calm reflecting water feature, first interior lights glowing softly from the windows.

CAMERA: Hasselblad H6D-100c, 24mm tilt-shift, f/9, ISO 100, perfectly corrected verticals, 3/4 hero angle.
8K, tack-sharp, Kodak Portra 400 cinematic warm color grade, deep blue dusk sky.

ATMOSPHERE: {desc['mood']}, magazine-cover architectural quality."""


# Room-specific hero furniture so each view reads as the right room type. The style
# descriptor supplies materials/textures/mood; this supplies what must dominate the frame.
ROOM_FOCUS: dict[str, str] = {
    "living_room": ("a generous lounge arrangement is the hero — sofa plus accent lounge chairs "
                    "around a low coffee table, a styled console/bookshelf behind; this is a "
                    "LIVING ROOM (no bed, no dining table)"),
    "master_bedroom": ("a luxurious king-size bed with an upholstered headboard and crisp linen "
                       "bedding is the unmistakable focal centerpiece, flanked by matching "
                       "nightstands with lamps, an upholstered bench at the foot of the bed, a "
                       "reading armchair in the corner, sheer linen drapes and integrated wardrobe "
                       "joinery; this is a BEDROOM — the bed MUST dominate the frame (no sofa-lounge "
                       "set, no dining table)"),
}


def _room_prompt(view: str, room_label: str, style: str, location: str, desc: dict[str, str],
                 palette_str: str, materials_str: str, shell: str = "") -> str:
    pal = f"\nCOLOR PALETTE (render exactly): {palette_str}" if palette_str else ""
    mat = f"\nKEY MATERIALS (render exactly): {materials_str}" if materials_str else ""
    focus = ROOM_FOCUS.get(view, f"a tasteful {room_label} arrangement is the hero of the frame")
    return f"""MASTER SHOT: Hyper-realistic interior photography of a luxury {style} {room_label} in a {location} villa, warm golden-hour natural light.

ROOM PURPOSE & HERO: {focus}.{shell}

STYLE, MATERIALS & TEXTURES: {desc['interior']}.{pal}{mat}

VIEW: 3/4 corner perspective at 1.6m eye height, slight upward tilt, rule-of-thirds.

LIGHTING (the hero — it builds depth and reveals every material): warm late-afternoon sun rakes through the glazing, filtered by tropical foliage so soft DAPPLED leaf-shadows (hoa nắng) fall across the floor and walls with visible god-ray shafts; a concealed 3000K cove lifts the shadows; 2700K accents graze the feature wall to reveal grain and texture.

CAMERA: Hasselblad H6D-100c, 24mm tilt-shift, f/8, ISO 200, corrected verticals; 8K tack-sharp, Kodak Portra 400 warm grade.

MICRO-DETAILS: real wood grain, fabric weave, plaster texture, dust motes in the god-rays, realistic plants, soft layered shadows.

ATMOSPHERE: {desc['mood']}, serene and livable."""


# ─── Single render (non-fatal, retries transient 429) ───────────────────────────
def _is_rate_limit(msg: str) -> bool:
    m = msg.lower()
    return "429" in m or "quota" in m or "exceeded" in m or "resource exhausted" in m


async def _gen_one(view: str, label: str, prompt: str) -> dict[str, Any]:
    last_err = "no image returned"
    for attempt in range(_MAX_RETRIES + 1):
        try:
            res = await ai_core.generate_image(
                prompt=prompt[:2200],  # grounded prompts run longer; keep the shell + tail intact
                aspect_ratio="16:9",
                n=1,
                safety_filter="block_some",
                negative_prompt=NEGATIVE_PROMPT_BASE,
                model=RENDER_MODEL,
            )
            imgs = res.get("images", [])
            if imgs:
                return {
                    "view": view,
                    "label": label,
                    "prompt_used": prompt[:280] + "…",
                    "data_uri": imgs[0]["data_uri"],
                    "size_bytes": imgs[0].get("size_bytes", 0),
                    "cost_usd": res.get("cost_usd", 0.0),
                }
            last_err = "no image returned"
        except Exception as e:  # noqa: BLE001 — render failure must not sink the project
            last_err = str(e)
            if attempt < _MAX_RETRIES and _is_rate_limit(last_err):
                wait = _RETRY_BACKOFF_S * (attempt + 1)
                log.warning("[render] view=%s rate-limited, retry %d/%d in %.0fs",
                            view, attempt + 1, _MAX_RETRIES, wait)
                await asyncio.sleep(wait)
                continue
            log.warning("[render] view=%s failed: %s", view, last_err[:200])
            break
    return {"view": view, "label": label, "data_uri": None,
            "error": last_err[:200], "cost_usd": 0.0}


# ─── Public entry ───────────────────────────────────────────────────────────────
async def render_concept(
    *,
    dna: dict[str, Any],
    interior_spec: dict[str, Any] | None,
    style: str,
    num_floors: int = 2,
    num_residents: int = 4,
    location_province: str = "Hà Nội",
    workspace_id: str = "vietcontech",
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate the key luxury perspective renders for a design concept.

    Each prompt is grounded in the deterministic floor-plan `geometry` (real room
    dimensions, window walls, footprint, storeys) so renders are bespoke to THIS
    house, not generic stock interiors.

    Returns:
        {"model", "views":[{view,label,data_uri,size_bytes,cost_usd}|{...,error}],
         "count": <successful>, "cost_usd": <total>}
    """
    _ = (num_residents, workspace_id)  # reserved for future billing tags
    interior_spec = interior_spec or {}
    desc = STYLE_DESCRIPTORS.get(style, _DEFAULT_DESC)
    palette_str = _palette_str(interior_spec)
    materials_str = _materials_str(interior_spec)
    loc = location_province or "Việt Nam"

    # Ground prompts in the real plan (fall back gracefully if geometry is missing).
    ceil = _ceiling_m(geometry, num_floors)
    ext_shell = _exterior_shell(geometry, num_floors)
    living = _find_room(geometry, ("khách",), avoid=("wc", "vệ sinh", "toilet"))
    master = (_find_room(geometry, ("master", "ngủ master"))
              or _find_room(geometry, ("ngủ", "bedroom")))
    living_shell = _interior_shell(living, ceil)
    master_shell = _interior_shell(master, ceil)
    if geometry:
        log.info("[render] grounded: ext=%s living=%s master=%s",
                 bool(ext_shell),
                 f"{living.get('w_m')}x{living.get('h_m')}" if living else None,
                 f"{master.get('w_m')}x{master.get('h_m')}" if master else None)

    tasks = [
        _gen_one("exterior", "Phối cảnh ngoại thất",
                 _exterior_prompt(style, num_floors, loc, desc, ext_shell)),
        _gen_one("living_room", "Phòng khách",
                 _room_prompt("living_room", "living room", style, loc, desc,
                              palette_str, materials_str, living_shell)),
        _gen_one("master_bedroom", "Phòng ngủ master",
                 _room_prompt("master_bedroom", "master bedroom", style, loc, desc,
                              palette_str, materials_str, master_shell)),
    ]
    views = await asyncio.gather(*tasks)
    total_cost = sum(float(v.get("cost_usd", 0.0)) for v in views)
    ok = sum(1 for v in views if v.get("data_uri"))
    log.info("[render] style=%s views_ok=%d/%d cost=$%.4f", style, ok, len(views), total_cost)
    return {
        "model": RENDER_MODEL,
        "views": views,
        "count": ok,
        "cost_usd": total_cost,
    }
