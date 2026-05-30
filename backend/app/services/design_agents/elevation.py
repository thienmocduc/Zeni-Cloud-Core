# -*- coding: utf-8 -*-
"""
L1.b MẶT ĐỨNG (Elevation) + L1.c MẶT CẮT (Section) — deterministic SVG, $0, no-LLM.

Giải quyết lỗi Chairman nêu: "bản mặt đứng đâu, mặt cắt đâu". Trước đây chỉ có mặt bằng
(geometry._svg_floor). Module này dựng từ CHÍNH hình học đã chốt:
  • 4 mặt đứng (chính/sau/trái/phải) — cửa sổ lấy từ phòng giáp biên (on_perimeter), cửa
    chính + gara ở mặt tiền tầng trệt, mái + cao độ tầng theo building_height.
  • Mặt cắt A-A qua cầu thang — sàn từng tầng + chiều cao thông thủy + thang + mái + cao độ.

Đồng bộ ngôn ngữ đồ họa với mặt bằng: nền trắng, tường #1d2329, kích thước #2f7fd6,
kính pastel, gỗ #be9d68, chữ Helvetica. Quy ước geometry: y=0 = mặt tiền, x=0..W ngang.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

FLOOR_HEIGHT_M = 3.3
_WALL = "#1d2329"
_DIM = "#2f7fd6"
_WOOD = "#be9d68"
_GLASS = "#cfe0ee"
_GLASS_ST = "#7fa9cf"
_MUTE = "#8a929c"


# Bảng màu mặt ngoài theo phong cách (fill thân nhà, mảng nhấn, mái).
_STYLE = {
    "modern":       {"body": "#f3f4f6", "accent": "#d9c3a0", "roof": "#2b3138", "fin": True},
    "luxury":       {"body": "#efe9df", "accent": "#cbb88f", "roof": "#3a3027", "fin": False},
    "indochine":    {"body": "#efe7d6", "accent": "#b9966a", "roof": "#6b4a2f", "fin": False},
    "japandi":      {"body": "#f1ece4", "accent": "#cbb896", "roof": "#3a3a36", "fin": True},
    "tropical":     {"body": "#eef1ec", "accent": "#a7c0a0", "roof": "#3a4038", "fin": True},
    "scandinavian": {"body": "#f5f5f3", "accent": "#d8c4a4", "roof": "#33373a", "fin": True},
}


def _pal(style: str) -> dict:
    return _STYLE.get((style or "modern").lower(), _STYLE["modern"])


def _b64(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _facade_windows(geometry: dict, edge: str) -> list[list[dict]]:
    """Per-floor danh sách phòng giáp biên ``edge`` → vị trí cửa sổ trên mặt đứng.

    edge ∈ {bottom(mặt tiền), top(sau), left, right}. Trục: bottom/top theo x (rộng W),
    left/right theo y (sâu D).
    """
    out: list[list[dict]] = []
    for fl in geometry.get("floors", []):
        wins = []
        for rm in fl.get("rooms", []):
            per = rm.get("on_perimeter") or {}
            if not per.get(edge):
                continue
            if rm.get("role") in ("corridor", "stair"):
                continue
            if edge in ("bottom", "top"):
                pos, span = rm.get("x_m", 0.0), rm.get("w_m", 1.0)
            else:
                pos, span = rm.get("y_m", 0.0), rm.get("h_m", 1.0)
            wins.append({"pos": pos, "span": span, "kind": rm.get("kind"),
                         "name": rm.get("name", "")})
        out.append(wins)
    return out


def _svg_elevation(*, facade_name: str, axis_w: float, per_floor: list[list[dict]],
                   floor_h: float, style: str, with_entry: bool,
                   garage_at: Optional[tuple[float, float]] = None) -> str:
    pal = _pal(style)
    n = max(1, len(per_floor))
    total_h = n * floor_h
    parapet = 0.6
    pxm = max(15.0, min(34.0, 760.0 / max(axis_w, 1.0)))
    M = 64.0
    cw = axis_w * pxm + 2 * M
    ch = (total_h + parapet) * pxm + 2 * M + 24

    def X(mx): return M + mx * pxm
    # Y: cao độ 0 ở MẶT ĐẤT (đáy); cao độ h tính lên trên.
    def Y(h): return M + (total_h + parapet - h) * pxm

    p: list[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw:.0f}" height="{ch:.0f}" '
             f'viewBox="0 0 {cw:.0f} {ch:.0f}" font-family="Helvetica,Arial,sans-serif">')
    p.append(f'<rect width="{cw:.0f}" height="{ch:.0f}" fill="#ffffff"/>')
    # sky-to-ground hint: nhẹ
    p.append(f'<rect x="{X(0):.1f}" y="{Y(total_h+parapet):.1f}" width="{axis_w*pxm:.1f}" '
             f'height="{(total_h+parapet)*pxm:.1f}" fill="{pal["body"]}" stroke="{_WALL}" stroke-width="2.4"/>')

    # plinth (đế) tầng trệt đậm hơn chút
    plinth = min(0.6, floor_h * 0.18)
    p.append(f'<rect x="{X(0):.1f}" y="{Y(plinth):.1f}" width="{axis_w*pxm:.1f}" '
             f'height="{plinth*pxm:.1f}" fill="#e7e3da" stroke="{_WALL}" stroke-width="1.2"/>')

    # storey lines + cao độ
    for f in range(n + 1):
        h = f * floor_h
        sw_line = 1.4 if 0 < f < n else 2.0
        dash = ' stroke-dasharray="2 3"' if 0 < f < n else ''
        p.append(f'<line x1="{X(0):.1f}" y1="{Y(h):.1f}" x2="{X(axis_w):.1f}" y2="{Y(h):.1f}" '
                 f'stroke="{_MUTE}" stroke-width="{sw_line}"{dash}/>')
        # cao độ marker bên trái
        p.append(f'<text x="{X(0)-10:.1f}" y="{Y(h)-2:.1f}" font-size="10" fill="{_DIM}" '
                 f'text-anchor="end">+{h:.2f}</text>')

    # parapet cap (mái bằng)
    p.append(f'<rect x="{X(-0.1):.1f}" y="{Y(total_h+parapet):.1f}" width="{(axis_w+0.2)*pxm:.1f}" '
             f'height="{parapet*0.45*pxm:.1f}" fill="{pal["roof"]}" stroke="{_WALL}" stroke-width="1.6"/>')

    # cửa sổ từng tầng
    for fi, wins in enumerate(per_floor):
        base_h = fi * floor_h
        win_h = floor_h * 0.46
        sill = base_h + floor_h * (0.30 if fi == 0 else 0.26)
        for w in wins:
            span = w["span"]
            ww = max(0.7, span * 0.66)
            wx = w["pos"] + (span - ww) / 2.0
            wet = w.get("kind") == "wet"
            wh = win_h * (0.62 if wet else 1.0)
            # floor-to-ceiling cho phòng lớn tầng trệt (kính lớn)
            if fi == 0 and w.get("kind") == "public" and span >= 3.2:
                wh = floor_h * 0.66; sill = base_h + floor_h * 0.16
            p.append(f'<rect x="{X(wx):.1f}" y="{Y(sill+wh):.1f}" width="{ww*pxm:.1f}" '
                     f'height="{wh*pxm:.1f}" fill="{_GLASS}" stroke="{_GLASS_ST}" stroke-width="1.6"/>')
            # mullion giữa
            p.append(f'<line x1="{X(wx+ww/2):.1f}" y1="{Y(sill+wh):.1f}" x2="{X(wx+ww/2):.1f}" '
                     f'y2="{Y(sill):.1f}" stroke="{_GLASS_ST}" stroke-width="1"/>')

    # cửa chính + gara (mặt tiền tầng trệt)
    if with_entry:
        dw, dh = 1.2, floor_h * 0.72
        dx = axis_w / 2 - dw / 2
        if garage_at:
            dx = min(axis_w - dw - 0.3, garage_at[0] + garage_at[1] + 0.6)
        p.append(f'<rect x="{X(dx):.1f}" y="{Y(dh):.1f}" width="{dw*pxm:.1f}" height="{dh*pxm:.1f}" '
                 f'fill="{_WOOD}" stroke="{_WALL}" stroke-width="2"/>')
        p.append(f'<line x1="{X(dx+dw/2):.1f}" y1="{Y(dh):.1f}" x2="{X(dx+dw/2):.1f}" '
                 f'y2="{Y(0):.1f}" stroke="{_WALL}" stroke-width="1"/>')
        # canopy
        p.append(f'<line x1="{X(dx-0.4):.1f}" y1="{Y(dh+0.15):.1f}" x2="{X(dx+dw+0.4):.1f}" '
                 f'y2="{Y(dh+0.15):.1f}" stroke="{_WALL}" stroke-width="3"/>')
        if garage_at:
            gx, gw = garage_at
            gh = floor_h * 0.6
            p.append(f'<rect x="{X(gx+0.2):.1f}" y="{Y(gh):.1f}" width="{max(1.0,(gw-0.4))*pxm:.1f}" '
                     f'height="{gh*pxm:.1f}" fill="#d8dde2" stroke="{_WALL}" stroke-width="1.8"/>')
            for k in range(1, 4):
                yy = gh * k / 4
                p.append(f'<line x1="{X(gx+0.2):.1f}" y1="{Y(yy):.1f}" x2="{X(gx+gw-0.2):.1f}" '
                         f'y2="{Y(yy):.1f}" stroke="#aeb6bf" stroke-width="1"/>')

    # lam đứng nhấn (modern/japandi/scandi) — 1 bay bên
    if pal.get("fin") and axis_w > 4:
        fin_x0 = axis_w * 0.72
        for k in range(5):
            fx = fin_x0 + k * 0.34
            if fx > axis_w - 0.3:
                break
            p.append(f'<line x1="{X(fx):.1f}" y1="{Y(total_h):.1f}" x2="{X(fx):.1f}" '
                     f'y2="{Y(floor_h*0.1):.1f}" stroke="{pal["accent"]}" stroke-width="2.2"/>')

    # ground hatching
    gy = Y(0)
    p.append(f'<line x1="{X(-0.3):.1f}" y1="{gy:.1f}" x2="{X(axis_w+0.3):.1f}" y2="{gy:.1f}" '
             f'stroke="{_WALL}" stroke-width="3"/>')
    for k in range(int(axis_w * pxm / 12) + 1):
        hx = X(0) + k * 12
        p.append(f'<line x1="{hx:.1f}" y1="{gy:.1f}" x2="{hx-7:.1f}" y2="{gy+8:.1f}" '
                 f'stroke="{_MUTE}" stroke-width="1"/>')

    # dimension chiều cao (phải) + rộng (dưới)
    xr = X(axis_w) + 22
    p.append(f'<line x1="{xr:.1f}" y1="{Y(0):.1f}" x2="{xr:.1f}" y2="{Y(total_h):.1f}" '
             f'stroke="{_DIM}" stroke-width="1"/>')
    p.append(f'<text x="{xr+4:.1f}" y="{Y(total_h/2):.1f}" font-size="11" font-weight="700" '
             f'fill="{_DIM}">{total_h:.2f}m</text>')
    p.append(f'<text x="{X(axis_w/2):.1f}" y="{ch-26:.1f}" font-size="11" fill="{_DIM}" '
             f'text-anchor="middle">{axis_w:.1f} m</text>')

    # title
    p.append(f'<text x="{M:.1f}" y="26" font-size="14" font-weight="700" fill="{_WALL}">'
             f'MẶT ĐỨNG {facade_name}</text>')
    p.append(f'<text x="{M:.1f}" y="{ch-8:.1f}" font-size="9.5" fill="{_MUTE}">'
             f'Tỉ lệ ~1:100 · {n} tầng · cao {total_h:.2f}m · phong cách {style} — '
             f'thiết kế sơ bộ, cần KTS chứng chỉ ký</text>')
    p.append("</svg>")
    return "".join(p)


def _svg_section(*, depth: float, num_floors: int, floor_h: float, style: str,
                 stair_band: Optional[tuple[float, float]], rooms_by_floor: list[list[str]]) -> str:
    pal = _pal(style)
    total_h = num_floors * floor_h
    parapet = 0.6
    slab = 0.25
    pxm = max(15.0, min(34.0, 760.0 / max(depth, 1.0)))
    M = 70.0
    cw = depth * pxm + 2 * M
    ch = (total_h + parapet + 0.8) * pxm + 2 * M + 24

    def X(my): return M + my * pxm            # trục ngang = chiều sâu D
    def Y(h): return M + (total_h + parapet - h) * pxm

    p: list[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw:.0f}" height="{ch:.0f}" '
             f'viewBox="0 0 {cw:.0f} {ch:.0f}" font-family="Helvetica,Arial,sans-serif">')
    p.append(f'<rect width="{cw:.0f}" height="{ch:.0f}" fill="#ffffff"/>')

    # móng (đáy)
    p.append(f'<rect x="{X(-0.2):.1f}" y="{Y(0):.1f}" width="{(depth+0.4)*pxm:.1f}" '
             f'height="{0.7*pxm:.1f}" fill="#e7e3da" stroke="{_WALL}" stroke-width="1.4"/>')
    for k in range(int(depth * pxm / 14) + 1):
        hx = X(-0.2) + k * 14
        p.append(f'<line x1="{hx:.1f}" y1="{Y(0)+4:.1f}" x2="{hx+8:.1f}" y2="{Y(0)+0.7*pxm-2:.1f}" '
                 f'stroke="{_MUTE}" stroke-width="0.8"/>')

    # mỗi tầng: sàn (slab đậm) + khoảng thông thủy
    for f in range(num_floors):
        base = f * floor_h
        # sàn
        p.append(f'<rect x="{X(0):.1f}" y="{Y(base+slab):.1f}" width="{depth*pxm:.1f}" '
                 f'height="{slab*pxm:.1f}" fill="{_WALL}"/>')
        # tường bao 2 đầu khoảng tầng
        clear = floor_h - slab
        p.append(f'<rect x="{X(0):.1f}" y="{Y(base+floor_h):.1f}" width="{depth*pxm:.1f}" '
                 f'height="{clear*pxm:.1f}" fill="none" stroke="{_MUTE}" stroke-width="1"/>')
        # cao độ
        p.append(f'<path d="M {X(0)-26:.1f} {Y(base):.1f} l 8 -5 l 0 10 z" fill="{_DIM}"/>')
        p.append(f'<text x="{X(0)-30:.1f}" y="{Y(base)-2:.1f}" font-size="10" fill="{_DIM}" '
                 f'text-anchor="end">+{base:.2f}</text>')
        # nhãn chiều cao thông thủy
        p.append(f'<text x="{X(depth)+6:.1f}" y="{Y(base+clear/2):.1f}" font-size="9.5" '
                 f'fill="{_MUTE}">{clear:.2f}m</text>')
        # tên phòng (cắt qua) — tối đa 2 nhãn/tầng
        names = rooms_by_floor[f] if f < len(rooms_by_floor) else []
        for i, nm in enumerate(names[:2]):
            p.append(f'<text x="{X(depth*(0.25+0.45*i)):.1f}" y="{Y(base+clear*0.5):.1f}" '
                     f'font-size="10" fill="#6b7480" text-anchor="middle">{_esc(nm)}</text>')

    # mái + parapet
    p.append(f'<rect x="{X(0):.1f}" y="{Y(total_h+slab):.1f}" width="{depth*pxm:.1f}" '
             f'height="{slab*pxm:.1f}" fill="{_WALL}"/>')
    p.append(f'<rect x="{X(-0.15):.1f}" y="{Y(total_h+parapet):.1f}" width="{(depth+0.3)*pxm:.1f}" '
             f'height="{parapet*0.5*pxm:.1f}" fill="{pal["roof"]}" stroke="{_WALL}" stroke-width="1.4"/>')
    p.append(f'<text x="{X(0)-30:.1f}" y="{Y(total_h)-2:.1f}" font-size="10" fill="{_DIM}" '
             f'text-anchor="end">+{total_h:.2f}</text>')

    # cầu thang (zigzag từng tầng) trong stair_band
    if stair_band:
        sy0, sw = stair_band
        sx0 = max(0.2, min(depth - sw - 0.2, sy0))
        for f in range(num_floors - 1):
            base = f * floor_h
            steps = 8
            run = sw / steps
            rise = floor_h / steps
            pts = []
            for s in range(steps + 1):
                xx = sx0 + s * run
                yy = base + slab + s * rise
                pts.append((X(xx), Y(yy)))
            d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
            p.append(f'<path d="{d}" fill="none" stroke="{_WOOD}" stroke-width="2.4"/>')
            # bậc đứng nhẹ
            for s in range(steps):
                xx = sx0 + s * run
                yy = base + slab + s * rise
                p.append(f'<line x1="{X(xx):.1f}" y1="{Y(yy):.1f}" x2="{X(xx):.1f}" '
                         f'y2="{Y(yy+rise):.1f}" stroke="{_WOOD}" stroke-width="0.8"/>')

    # ground line
    p.append(f'<line x1="{X(-0.3):.1f}" y1="{Y(0):.1f}" x2="{X(depth+0.3):.1f}" y2="{Y(0):.1f}" '
             f'stroke="{_WALL}" stroke-width="3"/>')
    # dimension sâu
    p.append(f'<text x="{X(depth/2):.1f}" y="{ch-26:.1f}" font-size="11" fill="{_DIM}" '
             f'text-anchor="middle">{depth:.1f} m (chiều sâu)</text>')
    # title
    p.append(f'<text x="{M:.1f}" y="26" font-size="14" font-weight="700" fill="{_WALL}">'
             f'MẶT CẮT A–A (qua thang)</text>')
    p.append(f'<text x="{M:.1f}" y="{ch-8:.1f}" font-size="9.5" fill="{_MUTE}">'
             f'Tỉ lệ ~1:100 · cao tầng {floor_h:.2f}m · {num_floors} tầng — '
             f'thiết kế sơ bộ, cần KTS chứng chỉ ký</text>')
    p.append("</svg>")
    return "".join(p)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_elevations_sections(geometry: Optional[dict], style: str = "modern") -> dict[str, Any]:
    """Sinh 4 mặt đứng + 1 mặt cắt từ geometry. Trả {elevations:[...], sections:[...]}.

    Không bao giờ raise — lỗi → trả rỗng (orchestrator vẫn chạy).
    """
    if not geometry or not geometry.get("floors"):
        return {"elevations": [], "sections": []}
    try:
        fp = geometry.get("footprint") or geometry["floors"][0].get("footprint") or {}
        W = float(fp.get("w_m") or 10.0)
        D = float(fp.get("d_m") or 12.0)
        nf = int(geometry.get("num_floors") or len(geometry.get("floors", [])) or 1)
        bh = geometry.get("building_height_m")
        floor_h = round(bh / nf, 2) if (bh and nf) else FLOOR_HEIGHT_M
        floor_h = max(2.8, min(4.0, floor_h))

        # gara mặt tiền tầng trệt (nếu có) → vẽ cửa cuốn
        garage_at = None
        for rm in geometry["floors"][0].get("rooms", []):
            nm = (rm.get("name") or "").lower()
            if ("gara" in nm or "garage" in nm) and (rm.get("on_perimeter") or {}).get("bottom"):
                garage_at = (rm.get("x_m", 0.0), rm.get("w_m", 3.0))
                break

        elevations = []
        for edge, name, axis in (("bottom", "CHÍNH (mặt tiền)", W), ("top", "SAU", W),
                                 ("left", "BÊN TRÁI", D), ("right", "BÊN PHẢI", D)):
            per_floor = _facade_windows(geometry, edge)
            svg = _svg_elevation(facade_name=name, axis_w=axis, per_floor=per_floor,
                                 floor_h=floor_h, style=style,
                                 with_entry=(edge == "bottom"),
                                 garage_at=garage_at if edge == "bottom" else None)
            elevations.append({"view": edge, "label": f"Mặt đứng {name}", "svg_data_uri": _b64(svg)})

        # mặt cắt qua thang: tìm dải thang theo chiều sâu (y_m..y_m+h_m) ở tầng trệt
        stair_band = None
        for rm in geometry["floors"][0].get("rooms", []):
            if rm.get("role") == "stair" or "thang" in (rm.get("name") or "").lower():
                stair_band = (rm.get("y_m", D * 0.5), max(2.4, rm.get("h_m", 3.0)))
                break
        rooms_by_floor = []
        for fl in geometry.get("floors", []):
            names = [r.get("name", "") for r in fl.get("rooms", [])
                     if r.get("role") not in ("corridor", "stair") and r.get("kind") != "wet"]
            rooms_by_floor.append(names)
        sec = _svg_section(depth=D, num_floors=nf, floor_h=floor_h, style=style,
                           stair_band=stair_band, rooms_by_floor=rooms_by_floor)
        sections = [{"view": "A-A", "label": "Mặt cắt A–A (qua thang)", "svg_data_uri": _b64(sec)}]
        return {"elevations": elevations, "sections": sections,
                "floor_height_m": floor_h, "num_floors": nf}
    except Exception:
        return {"elevations": [], "sections": []}
