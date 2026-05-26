"""
CAD Generator — DXF drawings from JSON spec output of 6 KTS Design Agents.

Phase 3 — Task #20A · Chairman approved 2026-05-26.

Generates 6 standard construction drawings as DXF (AutoCAD R2018 compatible):
  1. Floor plan (mặt bằng)        — generate_floor_plan
  2. Cross section (mặt cắt)      — generate_cross_section
  3. Elevation (mặt đứng)         — generate_elevation
  4. Structural detail (móng/cột) — generate_structural_detail
  5. Electrical schematic (điện)  — generate_electrical_schematic
  6. Water isometric (nước)       — generate_water_isometric

The 7th function `generate_full_package` orchestrates all 6 + returns dict of
{filename: bytes}. Output bytes can be uploaded to GCS directly.

Notes:
  - Geometry is structurally-correct placeholder (rooms = labeled rectangles,
    walls = lines, doors = arcs). For v1 — KTS chứng chỉ will refine in CAD app.
  - All drawings include title block bottom-right with project + scale + date.
  - Layers follow VN convention: WALLS/DOORS/WINDOWS/FURNITURE/DIM/TEXT/TITLE.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("zeni.cad_generator")


# ─── Standard layer definitions (color = AutoCAD Color Index) ──
LAYERS = {
    "WALLS":     {"color": 7, "lineweight": 50},  # white, 0.5mm
    "DOORS":     {"color": 1, "lineweight": 25},  # red
    "WINDOWS":   {"color": 5, "lineweight": 25},  # blue
    "FURNITURE": {"color": 3, "lineweight": 18},  # green
    "DIM":       {"color": 2, "lineweight": 13},  # yellow
    "TEXT":      {"color": 7, "lineweight": 13},  # white
    "TITLE":     {"color": 6, "lineweight": 25},  # magenta
    "STRUCT":    {"color": 4, "lineweight": 35},  # cyan
    "MEP_ELEC":  {"color": 1, "lineweight": 18},  # red
    "MEP_WATER": {"color": 5, "lineweight": 18},  # blue
    "AXES":      {"color": 8, "lineweight": 13},  # dark grey
}


def _new_doc():
    """Create a fresh DXF document with VN-standard layers."""
    import ezdxf  # type: ignore

    doc = ezdxf.new(dxfversion="R2018", setup=True)
    for name, props in LAYERS.items():
        if name not in doc.layers:
            ly = doc.layers.add(name)
            ly.color = props["color"]
            ly.lineweight = props["lineweight"]
    # Set drawing units to millimetres (VN convention)
    doc.header["$INSUNITS"] = 4  # 4 = millimetres
    return doc


def _add_title_block(
    msp: Any,
    project_name: str = "Zeni Cloud — Design Project",
    drawing_title: str = "Drawing",
    scale: str = "1:100",
    sheet_no: str = "A-01",
) -> None:
    """Title block bottom-right (180mm x 60mm) per VN standard A3 sheet."""
    x0, y0 = 250.0, -100.0
    w, h = 180.0, 60.0
    # Outer rectangle
    msp.add_lwpolyline(
        [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)],
        dxfattribs={"layer": "TITLE"},
    )
    # Horizontal dividers
    msp.add_line((x0, y0 + 15), (x0 + w, y0 + 15), dxfattribs={"layer": "TITLE"})
    msp.add_line((x0, y0 + 30), (x0 + w, y0 + 30), dxfattribs={"layer": "TITLE"})
    msp.add_line((x0, y0 + 45), (x0 + w, y0 + 45), dxfattribs={"layer": "TITLE"})
    # Texts (height 3.5mm)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [
        (y0 + 49, f"PROJECT: {project_name[:48]}"),
        (y0 + 34, f"DRAWING: {drawing_title[:48]}"),
        (y0 + 19, f"SCALE: {scale}   |   SHEET: {sheet_no}"),
        (y0 + 4,  f"DATE: {now}   |   By: Zeni KTS AI"),
    ]
    for y, txt in rows:
        msp.add_text(
            txt,
            dxfattribs={"layer": "TEXT", "height": 3.5},
        ).set_placement((x0 + 3, y))


def _add_room_rect(
    msp: Any,
    x: float, y: float, w: float, h: float, name: str,
    wall_thickness: float = 0.220,  # 220mm in metres? — we use mm throughout
) -> None:
    """Draw a room as a rectangle on WALLS layer, label on TEXT layer."""
    # Walls
    msp.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)],
        close=True, dxfattribs={"layer": "WALLS"},
    )
    # Room name placed at center (use simple insert point — avoid enum coupling
    # across ezdxf versions; alignment-aware placement is best-effort).
    try:
        t = msp.add_text(name, dxfattribs={"layer": "TEXT", "height": 250})
        # Approximate center by left-aligned anchor offset by half label width.
        t.set_placement((x + w / 2 - len(name) * 60, y + h / 2 - 100))
    except Exception:
        pass


def _add_door_arc(msp: Any, cx: float, cy: float, radius: float, start_deg: float = 0.0) -> None:
    """Door swing arc — 90° quarter circle from doorhinge."""
    msp.add_arc(
        center=(cx, cy),
        radius=radius,
        start_angle=start_deg,
        end_angle=start_deg + 90.0,
        dxfattribs={"layer": "DOORS"},
    )


def _bytes_from_doc(doc: Any) -> bytes:
    """Serialize ezdxf document to DXF bytes."""
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


# ───────────────────────────────────────────────────────────────
# 1. Floor plan
# ───────────────────────────────────────────────────────────────
def generate_floor_plan(spec: dict[str, Any], scale: str = "1:100") -> bytes:
    """
    Generate floor plan DXF.

    Args:
        spec: dict with keys 'rooms' (list of {name, area_m2}), 'project_name', etc.
        scale: drawing scale (default 1:100)

    Returns:
        DXF bytes — opens in AutoCAD / LibreCAD / DraftSight.
    """
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")

    rooms = spec.get("rooms") or spec.get("rooms_required") or [
        {"name": "Phòng khách", "area_m2": 30},
        {"name": "Phòng ngủ master", "area_m2": 20},
        {"name": "Phòng ngủ 2", "area_m2": 14},
        {"name": "Bếp + ăn", "area_m2": 18},
        {"name": "WC", "area_m2": 5},
    ]

    # Lay rooms in 2-column grid, scaled to mm (1m = 1000mm).
    cursor_x, cursor_y = 0.0, 0.0
    row_max_h = 0.0
    col = 0
    for i, r in enumerate(rooms):
        area = float(r.get("area_m2", 12) or 12)
        side = max((area ** 0.5), 2.0)  # square approximation, ≥2m side
        w_mm = side * 1000.0
        h_mm = side * 1000.0
        _add_room_rect(msp, cursor_x, cursor_y, w_mm, h_mm, r.get("name", f"Room {i+1}"))
        # Door at midpoint of bottom wall
        _add_door_arc(msp, cursor_x + w_mm / 2, cursor_y, 800.0, 0.0)
        cursor_x += w_mm + 220.0  # 220mm wall gap
        row_max_h = max(row_max_h, h_mm)
        col += 1
        if col >= 2:
            col = 0
            cursor_x = 0.0
            cursor_y += row_max_h + 220.0
            row_max_h = 0.0

    # Dimension axes (gridlines, simplified)
    for i, x in enumerate([0.0, 5000.0, 10000.0, 15000.0]):
        msp.add_line((x, -500), (x, cursor_y + 5000), dxfattribs={"layer": "AXES"})
        msp.add_text(
            chr(ord("A") + i),
            dxfattribs={"layer": "TEXT", "height": 350},
        ).set_placement((x - 100, -1200))

    _add_title_block(msp, project_name=project_name, drawing_title="MẶT BẰNG TẦNG 1",
                     scale=scale, sheet_no="A-01")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 2. Cross section
# ───────────────────────────────────────────────────────────────
def generate_cross_section(spec: dict[str, Any], scale: str = "1:50") -> bytes:
    """Cross-section drawing showing floor heights, slab + roof."""
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")
    num_floors = int(spec.get("num_floors", 2) or 2)
    floor_height_mm = int(spec.get("floor_height_mm", 3300))
    width_mm = int(spec.get("section_width_mm", 12000))

    # Ground line
    msp.add_line((0, 0), (width_mm, 0), dxfattribs={"layer": "STRUCT"})

    # Floors
    for lvl in range(num_floors + 1):  # +1 = roof
        y = lvl * floor_height_mm
        # Slab (200mm)
        msp.add_lwpolyline(
            [(0, y), (width_mm, y), (width_mm, y + 200), (0, y + 200), (0, y)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        msp.add_text(
            f"+{y/1000:.2f}m  Sàn tầng {lvl}" if lvl > 0 else "+0.00m  Sàn nền",
            dxfattribs={"layer": "TEXT", "height": 200},
        ).set_placement((width_mm + 500, y + 50))

    # Walls left + right
    total_h = (num_floors + 1) * floor_height_mm
    msp.add_line((0, 0), (0, total_h), dxfattribs={"layer": "WALLS"})
    msp.add_line((width_mm, 0), (width_mm, total_h), dxfattribs={"layer": "WALLS"})

    # Roof slope (simple pitched roof at top)
    roof_peak = total_h + 1500
    msp.add_line((0, total_h), (width_mm / 2, roof_peak), dxfattribs={"layer": "STRUCT"})
    msp.add_line((width_mm / 2, roof_peak), (width_mm, total_h), dxfattribs={"layer": "STRUCT"})

    # Foundation (below ground)
    msp.add_lwpolyline(
        [(0, -1500), (width_mm, -1500), (width_mm, -200), (0, -200), (0, -1500)],
        close=True, dxfattribs={"layer": "STRUCT"},
    )
    msp.add_text("Móng băng / Cọc",
                 dxfattribs={"layer": "TEXT", "height": 180}).set_placement((100, -1100))

    _add_title_block(msp, project_name=project_name, drawing_title=f"MẶT CẮT A-A ({num_floors} tầng)",
                     scale=scale, sheet_no="A-02")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 3. Elevation
# ───────────────────────────────────────────────────────────────
def generate_elevation(spec: dict[str, Any], direction: str = "front") -> bytes:
    """Front/back/left/right elevation drawing."""
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")
    num_floors = int(spec.get("num_floors", 2) or 2)
    floor_height_mm = int(spec.get("floor_height_mm", 3300))
    width_mm = int(spec.get("width_mm", 8000))

    direction_vi = {
        "front": "MẶT ĐỨNG CHÍNH (HƯỚNG NAM)",
        "back":  "MẶT ĐỨNG SAU (HƯỚNG BẮC)",
        "left":  "MẶT ĐỨNG TRÁI (HƯỚNG ĐÔNG)",
        "right": "MẶT ĐỨNG PHẢI (HƯỚNG TÂY)",
    }.get(direction, "MẶT ĐỨNG")

    total_h = num_floors * floor_height_mm + 1500  # +roof

    # Outline
    msp.add_lwpolyline(
        [(0, 0), (width_mm, 0), (width_mm, total_h - 1500),
         (width_mm / 2, total_h), (0, total_h - 1500), (0, 0)],
        close=True, dxfattribs={"layer": "WALLS"},
    )

    # Floor lines
    for lvl in range(1, num_floors + 1):
        y = lvl * floor_height_mm
        msp.add_line((0, y), (width_mm, y), dxfattribs={"layer": "STRUCT"})

    # Windows per floor (2 per floor, 1.5m wide x 1.5m tall, height 1m from floor)
    for lvl in range(num_floors):
        wy = lvl * floor_height_mm + 1000
        for j, wx_center in enumerate([width_mm * 0.25, width_mm * 0.75]):
            x0 = wx_center - 750
            msp.add_lwpolyline(
                [(x0, wy), (x0 + 1500, wy), (x0 + 1500, wy + 1500), (x0, wy + 1500), (x0, wy)],
                close=True, dxfattribs={"layer": "WINDOWS"},
            )

    # Main door (ground floor center)
    dx = width_mm / 2 - 750
    msp.add_lwpolyline(
        [(dx, 0), (dx + 1500, 0), (dx + 1500, 2400), (dx, 2400), (dx, 0)],
        close=True, dxfattribs={"layer": "DOORS"},
    )

    _add_title_block(msp, project_name=project_name, drawing_title=direction_vi,
                     scale="1:100", sheet_no=f"A-03-{direction[:1].upper()}")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 4. Structural detail
# ───────────────────────────────────────────────────────────────
def generate_structural_detail(spec: dict[str, Any], element: str = "foundation") -> bytes:
    """Structural detail drawing: foundation / column / beam / slab."""
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")

    if element == "foundation":
        title = "CHI TIẾT MÓNG ĐƠN — BTCT M250"
        # Foundation footing 1500x1500x400 + neck 300x300
        msp.add_lwpolyline(
            [(0, 0), (1500, 0), (1500, 400), (0, 400), (0, 0)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        msp.add_lwpolyline(
            [(600, 400), (900, 400), (900, 900), (600, 900), (600, 400)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        # Rebar (top + bottom) lines
        for x in (100, 300, 500, 700, 900, 1100, 1300):
            msp.add_line((x, 50), (x, 350), dxfattribs={"layer": "STRUCT"})
        msp.add_text("Thép chủ d16 a200, đai d8 a150",
                     dxfattribs={"layer": "TEXT", "height": 80}).set_placement((100, -200))

    elif element == "column":
        title = "CHI TIẾT CỘT ĐIỂN HÌNH — BTCT M250 — 300x300"
        msp.add_lwpolyline(
            [(0, 0), (300, 0), (300, 300), (0, 300), (0, 0)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        # 4 corner rebars (circles)
        for cx, cy in [(40, 40), (260, 40), (260, 260), (40, 260)]:
            msp.add_circle((cx, cy), 9, dxfattribs={"layer": "STRUCT"})
        # 4 intermediate rebars
        for cx, cy in [(150, 40), (150, 260), (40, 150), (260, 150)]:
            msp.add_circle((cx, cy), 9, dxfattribs={"layer": "STRUCT"})
        # Stirrup outline
        msp.add_lwpolyline(
            [(30, 30), (270, 30), (270, 270), (30, 270), (30, 30)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        msp.add_text("8d18 + đai d8 a200",
                     dxfattribs={"layer": "TEXT", "height": 30}).set_placement((50, -50))

    elif element == "beam":
        title = "CHI TIẾT DẦM ĐIỂN HÌNH — BTCT M250 — 200x350"
        msp.add_lwpolyline(
            [(0, 0), (200, 0), (200, 350), (0, 350), (0, 0)],
            close=True, dxfattribs={"layer": "STRUCT"},
        )
        # Top rebars (3d18)
        for cx in (40, 100, 160):
            msp.add_circle((cx, 310), 9, dxfattribs={"layer": "STRUCT"})
        # Bottom rebars (3d20)
        for cx in (40, 100, 160):
            msp.add_circle((cx, 40), 10, dxfattribs={"layer": "STRUCT"})
        msp.add_text("Top 3d18 · Bot 3d20 · Đai d8 a150",
                     dxfattribs={"layer": "TEXT", "height": 30}).set_placement((10, -50))
    else:
        title = f"CHI TIẾT KẾT CẤU — {element.upper()}"
        msp.add_text(f"Element: {element}",
                     dxfattribs={"layer": "TEXT", "height": 100}).set_placement((0, 0))

    _add_title_block(msp, project_name=project_name, drawing_title=title,
                     scale="1:20", sheet_no=f"S-{element[:3].upper()}")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 5. Electrical schematic
# ───────────────────────────────────────────────────────────────
def generate_electrical_schematic(spec: dict[str, Any]) -> bytes:
    """Single-line electrical diagram + circuit list."""
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")

    main_panel = spec.get("main_panel", {"total_kva": 12, "phases": 1})
    circuits = spec.get("circuits") or [
        {"name": "Chiếu sáng tầng 1", "wire_mm2": 1.5, "breaker_a": 10},
        {"name": "Ổ cắm tầng 1",     "wire_mm2": 2.5, "breaker_a": 16},
        {"name": "Bếp",               "wire_mm2": 4.0, "breaker_a": 20},
        {"name": "Điều hoà",          "wire_mm2": 2.5, "breaker_a": 16},
        {"name": "Bình nóng lạnh",    "wire_mm2": 2.5, "breaker_a": 20},
    ]

    # Main bus (horizontal line)
    msp.add_line((0, 2000), (8000, 2000), dxfattribs={"layer": "MEP_ELEC"})
    # Source label
    msp.add_text(
        f"Lưới điện 220V/{main_panel.get('phases', 1)}P — {main_panel.get('total_kva', 12)} kVA",
        dxfattribs={"layer": "TEXT", "height": 120},
    ).set_placement((0, 2100))

    # Per-circuit branches
    for i, c in enumerate(circuits):
        x = 500 + i * 1400
        msp.add_line((x, 2000), (x, 1200), dxfattribs={"layer": "MEP_ELEC"})
        # Breaker symbol (rectangle)
        msp.add_lwpolyline(
            [(x - 100, 1600), (x + 100, 1600), (x + 100, 1750), (x - 100, 1750), (x - 100, 1600)],
            close=True, dxfattribs={"layer": "MEP_ELEC"},
        )
        msp.add_text(f"MCB {c.get('breaker_a', 16)}A",
                     dxfattribs={"layer": "TEXT", "height": 80}).set_placement((x - 200, 1500))
        msp.add_text(c.get("name", f"C{i+1}"),
                     dxfattribs={"layer": "TEXT", "height": 100}).set_placement((x - 250, 1100))
        msp.add_text(f"{c.get('wire_mm2', 2.5)} mm²",
                     dxfattribs={"layer": "TEXT", "height": 70}).set_placement((x - 150, 950))

    _add_title_block(msp, project_name=project_name, drawing_title="SƠ ĐỒ ĐIỆN MỘT SỢI",
                     scale="N/A", sheet_no="E-01")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 6. Water isometric
# ───────────────────────────────────────────────────────────────
def generate_water_isometric(spec: dict[str, Any]) -> bytes:
    """Plumbing isometric (cold + hot + drainage)."""
    doc = _new_doc()
    msp = doc.modelspace()
    project_name = spec.get("project_name", "Zeni KTS Design")

    num_floors = int(spec.get("num_floors", 2) or 2)
    floor_h = 3300

    # Cold water riser (D25 PPR — blue)
    msp.add_line((0, 0), (0, num_floors * floor_h + 1000), dxfattribs={"layer": "MEP_WATER"})
    msp.add_text("Cấp lạnh D25 PPR",
                 dxfattribs={"layer": "TEXT", "height": 120}).set_placement((100, -300))

    # Hot water riser (D20 PPR insulated)
    msp.add_line((800, 0), (800, num_floors * floor_h + 1000), dxfattribs={"layer": "MEP_WATER"})
    msp.add_text("Cấp nóng D20 PPR (bọc cách nhiệt)",
                 dxfattribs={"layer": "TEXT", "height": 120}).set_placement((900, -300))

    # Drain (D110 PVC)
    msp.add_line((2000, 0), (2000, num_floors * floor_h + 1000), dxfattribs={"layer": "MEP_WATER"})
    msp.add_text("Thoát thải D110 PVC",
                 dxfattribs={"layer": "TEXT", "height": 120}).set_placement((2100, -300))

    # Per-floor connections
    for lvl in range(num_floors):
        y = lvl * floor_h + 1500
        msp.add_line((0, y), (-500, y), dxfattribs={"layer": "MEP_WATER"})
        msp.add_line((800, y), (1300, y), dxfattribs={"layer": "MEP_WATER"})
        msp.add_line((2000, y), (1500, y), dxfattribs={"layer": "MEP_WATER"})
        msp.add_text(f"Tầng {lvl + 1}",
                     dxfattribs={"layer": "TEXT", "height": 150}).set_placement((-1500, y))

    # Septic tank
    msp.add_lwpolyline(
        [(1500, -2000), (3500, -2000), (3500, -500), (1500, -500), (1500, -2000)],
        close=True, dxfattribs={"layer": "MEP_WATER"},
    )
    msp.add_text("Bể tự hoại 3m³",
                 dxfattribs={"layer": "TEXT", "height": 150}).set_placement((1700, -1300))

    _add_title_block(msp, project_name=project_name, drawing_title="SƠ ĐỒ KHÔNG GIAN CẤP THOÁT NƯỚC",
                     scale="N/A", sheet_no="P-01")
    return _bytes_from_doc(doc)


# ───────────────────────────────────────────────────────────────
# 7. Full package orchestrator
# ───────────────────────────────────────────────────────────────
def generate_full_package(session_id: str, agent_outputs: dict[str, Any]) -> dict[str, bytes]:
    """
    Generate all 6 CAD drawings from orchestrator agent outputs.

    Args:
        session_id: design session UUID (for logging)
        agent_outputs: dict with keys 'kts_chief', 'structural', 'mep' (each = AgentResult.output)

    Returns:
        {filename: dxf_bytes} — e.g. {"floor-plan.dxf": b"...", ...}
    """
    log.info("[cad_generator] start full package session=%s", session_id)

    kts = (agent_outputs.get("kts_chief") or {}).get("dna") or {}
    structural = agent_outputs.get("structural") or {}
    mep = agent_outputs.get("mep") or {}

    project_name = (agent_outputs.get("project_name")
                    or kts.get("project_name")
                    or f"Zeni Design — {session_id[:8]}")
    num_floors = int(agent_outputs.get("num_floors", 2) or 2)

    common_spec = {
        "project_name": project_name,
        "num_floors": num_floors,
        "rooms": kts.get("rooms_required", []),
        "main_panel": (mep.get("electrical") or {}).get("main_panel"),
    }

    result: dict[str, bytes] = {}
    try:
        result["floor-plan.dxf"] = generate_floor_plan(common_spec, scale="1:100")
        result["cross-section.dxf"] = generate_cross_section(common_spec, scale="1:50")
        result["elevation-front.dxf"] = generate_elevation(common_spec, direction="front")
        result["structural-foundation.dxf"] = generate_structural_detail(common_spec, element="foundation")
        result["structural-column.dxf"] = generate_structural_detail(common_spec, element="column")
        result["electrical-schematic.dxf"] = generate_electrical_schematic({**common_spec, **mep.get("electrical", {})})
        result["water-isometric.dxf"] = generate_water_isometric(common_spec)
    except Exception as e:
        log.exception("[cad_generator] session=%s failed: %s", session_id, e)
        raise

    log.info("[cad_generator] session=%s generated %d drawings (%d bytes total)",
             session_id, len(result), sum(len(b) for b in result.values()))
    return result


__all__ = [
    "generate_floor_plan",
    "generate_cross_section",
    "generate_elevation",
    "generate_structural_detail",
    "generate_electrical_schematic",
    "generate_water_isometric",
    "generate_full_package",
    "LAYERS",
]
# end of file
