"""
Geometry / floor-plan layout stage (Item 1) — DETERMINISTIC, no LLM, $0, reproducible.

Turns the KTS Chief's room program (rooms_required: name + area_m2 + priority)
into a REAL dimensioned floor plan instead of a bare room list:

  - rooms packed into a footprint as non-overlapping rectangles (squarified treemap)
  - one shared footprint across floors  → columns line up vertically (structural sense)
  - a structural column grid (3.0–4.5 m module)
  - exterior / partition walls, doors (with swing), windows on perimeter
  - per-floor GFA, building height, and derived engineering quantities
  - a clean 2D SVG drawing per floor (walls, doors, windows, grid, dimensions, title block)

The geometry + quantities feed Structural / MEP / BOQ so their figures are grounded in
actual dimensions rather than parametric guesses.

⚠️ AI SCHEMATIC ONLY. This is a zoning/áreas-correct schematic plan to anchor the
estimate — a licensed KTS produces the buildable construction drawing.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

log = logging.getLogger("zeni.design_geometry")

# ── physical constants (VN residential, schematic) ───────────────
FLOOR_HEIGHT_M = 3.3
ROOF_RISE_M = 1.6
WALL_EXT_M = 0.22
WALL_INT_M = 0.11
SLAB_THK_M = 0.12
BEAM_W_M, BEAM_H_M = 0.22, 0.35
COL_W_M = 0.30
CIRCULATION_FRAC = 0.15            # hành lang + cầu thang
GRID_MIN_M, GRID_MAX_M = 3.0, 4.5  # structural module band
MIN_ROOM_DIM_M = 1.8
FOOTPRINT_RATIO = 0.85             # frontage(W) : depth(D)
STEEL_KG_PER_M3 = 120.0            # rough BTCT steel ratio
BRICK_PER_M2_110 = 55.0            # viên gạch / m² tường 110

# ── room classification by Vietnamese keywords ───────────────────
_WET = ("wc", "vệ sinh", "tắm", "toilet", "nhà vệ sinh")
_BED = ("ngủ", "master", "phòng con", "guest", "khách ngủ")
_PUBLIC = ("khách", "bếp", "ăn", "thờ", "sinh hoạt", "giải trí", "sảnh", "tiền sảnh")
_SERVICE = ("gara", "garage", "kho", "giặt", "sân", "kỹ thuật", "phụ")


def _classify(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in _WET):
        return "wet"
    if any(k in n for k in _BED):
        return "bed"
    if any(k in n for k in _SERVICE):
        return "service"
    if any(k in n for k in _PUBLIC):
        return "public"
    return "public"


def _is_altar(name: str) -> bool:
    """Ancestral-altar room (phòng/gian/bàn thờ) — needs culturally-correct placement:
    dignified, never sharing an edge with a WC (uế khí taboo), never a leftover corner."""
    return "thờ" in (name or "").lower()


def _is_kitchen(name: str) -> bool:
    n = (name or "").lower()
    return ("bếp" in n) or ("ăn" in n)


# ── squarified treemap (Bruls/Huizing/van Wijk) ──────────────────
def _worst(areas: list[float], length: float) -> float:
    s = sum(areas)
    if s <= 0 or length <= 0:
        return float("inf")
    mx, mn = max(areas), min(areas)
    return max(length * length * mx / (s * s), s * s / (length * length * mn))


def _squarify(items: list[tuple[float, Any]], x: float, y: float, w: float, h: float) -> list[tuple]:
    """items: [(area, payload)]; returns [(payload, x, y, w, h)] tiling the rect exactly."""
    total = sum(a for a, _ in items)
    if total <= 0 or w <= 0 or h <= 0:
        return []
    scale = (w * h) / total
    work = sorted(((a * scale, p) for a, p in items), key=lambda t: -t[0])
    out: list[tuple] = []
    _sq(work, x, y, w, h, out)
    return out


def _sq(items: list[tuple[float, Any]], x: float, y: float, w: float, h: float, out: list[tuple]) -> None:
    if not items:
        return
    if len(items) == 1:
        a, p = items[0]
        out.append((p, x, y, w, h))
        return
    short = min(w, h)
    row: list[tuple[float, Any]] = []
    rest = items[:]
    while rest:
        nxt = rest[0][0]
        cur = [a for a, _ in row]
        if not row or _worst(cur, short) >= _worst(cur + [nxt], short):
            row.append(rest.pop(0))
        else:
            break
    s = sum(a for a, _ in row)
    if w >= h:
        rw = s / h
        cy = y
        for a, p in row:
            rh = a / rw
            out.append((p, x, cy, rw, rh))
            cy += rh
        _sq(rest, x + rw, y, w - rw, h, out)
    else:
        rh = s / w
        cx = x
        for a, p in row:
            rww = a / rh
            out.append((p, cx, y, rww, rh))
            cx += rww
        _sq(rest, x, y + rh, w, h - rh, out)


def _relax_slivers(cells: list[dict], min_dim: float = 1.5, eps: float = 0.04) -> None:
    """Widen any sub-min_dim room by borrowing from an equal-cross-section neighbour.
    Only transfers between cells that share a full edge (identical opposite span),
    so the tiling stays gap/overlap-free."""
    for _ in range(2):
        for c in cells:
            if c["w"] < min_dim - 1e-6:
                need = min_dim - c["w"]
                for n in cells:
                    if n is c or not (abs(n["y"] - c["y"]) < eps and abs(n["h"] - c["h"]) < eps):
                        continue
                    if abs(n["x"] - (c["x"] + c["w"])) < eps and n["w"] - need >= min_dim:  # right
                        n["x"] += need; n["w"] -= need; c["w"] += need; break
                    if abs((n["x"] + n["w"]) - c["x"]) < eps and n["w"] - need >= min_dim:  # left
                        n["w"] -= need; c["x"] -= need; c["w"] += need; break
            if c["h"] < min_dim - 1e-6:
                need = min_dim - c["h"]
                for n in cells:
                    if n is c or not (abs(n["x"] - c["x"]) < eps and abs(n["w"] - c["w"]) < eps):
                        continue
                    if abs(n["y"] - (c["y"] + c["h"])) < eps and n["h"] - need >= min_dim:  # below
                        n["y"] += need; n["h"] -= need; c["h"] += need; break
                    if abs((n["y"] + n["h"]) - c["y"]) < eps and n["h"] - need >= min_dim:  # above
                        n["h"] -= need; c["y"] -= need; c["h"] += need; break


def _snap_cells(cells: list[dict], ndp: int = 2, tol: float = 0.02) -> None:
    """Round every cell edge through a shared coordinate set so two rooms sharing a
    boundary keep the SAME rounded value — removes the sub-cm overlap/gap slivers that
    independent 2-dp rounding of (x,w) otherwise produces. Tiling stays exact."""
    def snapper(vals: list[float]):
        clusters: list[list[float]] = []  # [representative, rounded]
        for v in sorted(vals):
            if not clusters or abs(v - clusters[-1][0]) > tol:
                clusters.append([v, round(v, ndp)])
        return lambda v: min(clusters, key=lambda u: abs(u[0] - v))[1]

    sx = snapper([e for c in cells for e in (c["x"], c["x"] + c["w"])])
    sy = snapper([e for c in cells for e in (c["y"], c["y"] + c["h"])])
    for c in cells:
        x0, x1 = sx(c["x"]), sx(c["x"] + c["w"])
        y0, y1 = sy(c["y"]), sy(c["y"] + c["h"])
        c["x"], c["w"] = x0, round(x1 - x0, ndp)
        c["y"], c["h"] = y0, round(y1 - y0, ndp)


# ── floor program ────────────────────────────────────────────────
def _normalize_rooms(rooms_required: list[dict]) -> list[dict]:
    rooms: list[dict] = []
    for r in rooms_required or []:
        name = str(r.get("name") or "Phòng").strip()
        try:
            area = float(r.get("area_m2") or 0)
        except (TypeError, ValueError):
            area = 0.0
        if area <= 0:
            area = 12.0
        rooms.append({"name": name, "area_m2": area, "priority": r.get("priority", "normal"),
                      "kind": _classify(name)})
    if not rooms:  # defensive fallback program
        rooms = [
            {"name": "Phòng khách", "area_m2": 30, "kind": "public", "priority": "high"},
            {"name": "Bếp + ăn", "area_m2": 20, "kind": "public", "priority": "high"},
            {"name": "Phòng ngủ master", "area_m2": 22, "kind": "bed", "priority": "high"},
            {"name": "Phòng ngủ 2", "area_m2": 16, "kind": "bed", "priority": "normal"},
            {"name": "WC", "area_m2": 5, "kind": "wet", "priority": "normal"},
        ]
    return rooms


# rooms that belong on the ground floor (entrance-bound) vs ones free to move upstairs to
# balance the per-floor area (so upper floors aren't near-empty → no giant filler balcony)
_GROUND_LOCK = ("khách", "sảnh", "tiền", "bếp", "ăn", "thờ", "gara", "garage")
_FLEX_UP = ("gym", "cinema", "phim", "giải trí", "thư", "đọc", "library",
            "kho", "giặt", "studio", "làm việc")


def _assign_floors(rooms: list[dict], num_floors: int) -> list[list[dict]]:
    nf = max(1, int(num_floors or 1))
    if nf == 1:
        return [rooms]

    def has(n: str, keys) -> bool:
        n = n.lower()
        return any(k in n for k in keys)

    floors: list[list[dict]] = [[] for _ in range(nf)]
    ground: list[dict] = []
    bedrooms = [r for r in rooms if r["kind"] == "bed"]
    wets = [r for r in rooms if r["kind"] == "wet"]
    flex: list[dict] = []
    for r in rooms:
        if r["kind"] in ("bed", "wet"):
            continue
        if has(r["name"], _FLEX_UP):
            flex.append(r)           # free to move upstairs
        else:
            ground.append(r)         # entrance-bound public/service stays on ground
    if wets:
        ground.append(wets.pop(0))   # khách WC stays on ground
    floors[0] = ground
    net = [sum(r["area_m2"] for r in f) for f in floors]

    # fill upper floors largest-first, always topping up the emptiest one (balances area)
    for r in sorted(bedrooms + flex + wets, key=lambda r: -r["area_m2"]):
        k = min(range(1, nf), key=lambda i: net[i])
        floors[k].append(r); net[k] += r["area_m2"]

    for fi in range(1, nf):  # no empty upper floor
        if not floors[fi]:
            donor = max(range(nf), key=lambda k: len(floors[k]) if k != fi else -1)
            if len(floors[donor]) > 1:
                floors[fi].append(floors[donor].pop())
    return floors


# ── grid + perimeter helpers ─────────────────────────────────────
def _pick_module(length: float) -> float:
    best, best_err = (GRID_MIN_M + GRID_MAX_M) / 2, 1e9
    n = max(1, round(length / GRID_MIN_M))
    for k in range(max(1, n - 3), n + 4):
        m = length / k
        if GRID_MIN_M - 0.4 <= m <= GRID_MAX_M + 0.4:
            err = abs(m - 3.8)  # prefer ~3.8 m bays
            if err < best_err:
                best, best_err = m, err
    return round(best, 3)


def _on_perimeter(rx, ry, rw, rh, W, D, eps=0.05):
    """Return which room edges sit on the building perimeter."""
    return {
        "left": rx <= eps, "right": rx + rw >= W - eps,
        "top": ry <= eps, "bottom": ry + rh >= D - eps,
    }


# ── SVG drawing ──────────────────────────────────────────────────
_FILLS = ["#eef4f7", "#f3eee7", "#eef2ea", "#f4eef2", "#eef0f4", "#f2f1e9"]


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _room_class(rm: dict) -> str:
    """Map a room to a furniture archetype so the plan shows real bố trí nội thất."""
    n = (rm.get("name") or "").lower()
    k = rm.get("kind")
    role = rm.get("role")
    if role in ("corridor", "stair"):
        return role
    if "thờ" in n:
        return "altar"
    if k == "wet":
        return "wc"
    if "gara" in n or "garage" in n:
        return "garage"
    if "bếp" in n:
        return "kitchen"
    if "ăn" in n:
        return "dining"
    if k == "bed":
        return "bed"
    if any(s in n for s in ("khách", "sinh hoạt", "giải trí", "phim", "cinema")):
        return "living"
    if any(s in n for s in ("làm việc", "đọc", "thư", "studio", "library")):
        return "study"
    if "gym" in n:
        return "gym"
    if any(s in n for s in ("ban công", "sân", "hiên", "terrace")):
        return "terrace"
    if any(s in n for s in ("kho", "giặt", "kỹ thuật", "thay đồ")):
        return "store"
    return "generic"


def _svg_floor(fd: dict) -> str:
    W, D = fd["footprint"]["w_m"], fd["footprint"]["d_m"]
    pxm = max(16.0, min(34.0, 820.0 / max(W, 1.0)))
    M = 66.0  # margin for dimensions + title
    cw, ch = W * pxm + 2 * M, D * pxm + 2 * M + 30

    def X(mx): return M + mx * pxm
    def Y(my): return M + my * pxm

    p: list[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw:.0f}" height="{ch:.0f}" '
             f'viewBox="0 0 {cw:.0f} {ch:.0f}" font-family="Helvetica,Arial,sans-serif">')
    p.append(f'<rect width="{cw:.0f}" height="{ch:.0f}" fill="#ffffff"/>')

    # column grid (behind)
    gx, gy = fd["column_grid"]["module_x_m"], fd["column_grid"]["module_y_m"]
    nx, ny = fd["column_grid"]["nx"], fd["column_grid"]["ny"]
    for i in range(nx):
        x = X(min(i * gx, W))
        p.append(f'<line x1="{x:.1f}" y1="{Y(0):.1f}" x2="{x:.1f}" y2="{Y(D):.1f}" '
                 f'stroke="#dbe2ea" stroke-width="1" stroke-dasharray="3 4"/>')
    for j in range(ny):
        y = Y(min(j * gy, D))
        p.append(f'<line x1="{X(0):.1f}" y1="{y:.1f}" x2="{X(W):.1f}" y2="{y:.1f}" '
                 f'stroke="#dbe2ea" stroke-width="1" stroke-dasharray="3 4"/>')

    # corridor band → orient furniture + draw real room↔corridor doors (hành lang)
    _corr = next((r for r in fd["rooms"] if r.get("role") == "corridor"), None)
    cy0 = _corr["y_m"] if _corr else None
    cy1 = (_corr["y_m"] + _corr["h_m"]) if _corr else None
    FS, WD, SOFT = "#8d96a1", "#be9d68", "#ffffff"

    def _R(x, y, w, h, fill="none", st=FS, sw=1.3, r=0.0):
        return (f'<rect x="{X(x):.1f}" y="{Y(y):.1f}" width="{max(0.0,w)*pxm:.1f}" '
                f'height="{max(0.0,h)*pxm:.1f}" rx="{r*pxm:.1f}" fill="{fill}" '
                f'stroke="{st}" stroke-width="{sw}"/>')

    def _L(x1, y1, x2, y2, st=FS, sw=1.3):
        return (f'<line x1="{X(x1):.1f}" y1="{Y(y1):.1f}" x2="{X(x2):.1f}" y2="{Y(y2):.1f}" '
                f'stroke="{st}" stroke-width="{sw}"/>')

    def _Cc(x, y, rr, fill="none", st=FS, sw=1.3):
        return (f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="{rr*pxm:.1f}" '
                f'fill="{fill}" stroke="{st}" stroke-width="{sw}"/>')

    def _door(rm, rear, front):
        rx, ry, rw, rh = rm["x_m"], rm["y_m"], rm["w_m"], rm["h_m"]
        role = rm.get("role")
        if role == "corridor":
            return []
        up = False
        if rear:
            ey = ry + rh
        elif front:
            ey = ry; up = True
        elif role == "ensuite":
            if rm["on_perimeter"].get("top"):
                ey = ry + rh
            else:
                ey = ry; up = True
        else:
            return []
        hx = rx + rw / 2
        dw = min(0.9, rw * 0.6)
        s = [f'<line x1="{X(hx-dw/2):.1f}" y1="{Y(ey):.1f}" x2="{X(hx+dw/2):.1f}" '
             f'y2="{Y(ey):.1f}" stroke="#ffffff" stroke-width="4"/>']
        dy = -dw if up else dw
        sweep = 1 if up else 0
        s.append(f'<path d="M {X(hx-dw/2):.1f} {Y(ey):.1f} a {dw*pxm:.1f} {dw*pxm:.1f} '
                 f'0 0 {sweep} {dw*pxm:.1f} {dy*pxm:.1f}" fill="none" '
                 f'stroke="#aeb6bf" stroke-width="1.1"/>')
        return s

    def _furn(rm, rear, front):
        rx, ry, rw, rh = rm["x_m"], rm["y_m"], rm["w_m"], rm["h_m"]
        cls = _room_class(rm)
        back = "top" if rear else "bottom"   # perimeter wall opposite the corridor
        mg = min(0.3, rw * 0.14, rh * 0.14)
        ix, iy, iw, ih = rx + mg, ry + mg, rw - 2 * mg, rh - 2 * mg
        o: list[str] = []
        if iw <= 0.4 or ih <= 0.4 or cls == "corridor":
            return o
        if cls == "bed":
            cwd = min(0.6, iw * 0.25)
            o.append(_R(ix, iy, cwd, ih, fill="#f1ece3", st=WD))     # wardrobe
            baw = iw - cwd - 0.3
            bw = min(baw, 1.9 if baw > 2.2 else baw)
            bh = min(ih * 0.62, 2.05)
            bx = ix + cwd + 0.15 + (baw - bw) / 2
            by = iy if back == "top" else iy + ih - bh
            o.append(_R(bx, by, bw, bh, fill="#fbf7f0", st=WD, sw=1.4, r=0.12))
            py = by + 0.1 if back == "top" else by + bh - 0.45
            o.append(_R(bx + 0.12, py, bw / 2 - 0.18, 0.34, fill=SOFT, r=0.08))
            o.append(_R(bx + bw / 2 + 0.06, py, bw / 2 - 0.18, 0.34, fill=SOFT, r=0.08))
        elif cls == "living":
            sh = min(0.85, ih * 0.3)
            sy = iy if back == "top" else iy + ih - sh
            o.append(_R(ix, sy, iw * 0.72, sh, fill="#eef0ef", st=FS, sw=1.4, r=0.1))
            o.append(_R(ix + iw * 0.16, iy + ih * 0.44, iw * 0.32, min(ih * 0.16, 0.5),
                        fill="#f3ece1", st=WD, r=0.06))                # coffee table
            ty = iy + ih - 0.3 if back == "top" else iy
            o.append(_R(ix + iw * 0.22, ty, iw * 0.4, 0.28, fill="#ece7de", st=WD))  # TV console
        elif cls == "kitchen":
            o.append(_R(ix, iy if back == "top" else iy + ih - 0.6, iw, 0.6,
                        fill="#e9ece9", st=FS))                        # counter run
            o.append(_R(ix, iy, 0.6, ih, fill="#e9ece9", st=FS))       # return leg
            o.append(_Cc(ix + 0.3, iy + ih * 0.5, 0.15))               # sink
            if iw > 2.6 and ih > 2.6:
                o.append(_R(ix + iw * 0.42, iy + ih * 0.42, min(1.8, iw * 0.4),
                            min(0.9, ih * 0.3), fill="#f3ece1", st=WD, r=0.06))  # island
        elif cls == "dining":
            tw, th = min(iw * 0.6, 1.6), min(ih * 0.5, 0.9)
            tx, ty = ix + (iw - tw) / 2, iy + (ih - th) / 2
            o.append(_R(tx, ty, tw, th, fill="#f3ece1", st=WD, r=0.08))
            for cxp in (tx + tw * 0.28, tx + tw * 0.72):
                o.append(_R(cxp - 0.17, ty - 0.32, 0.34, 0.26, fill=SOFT))
                o.append(_R(cxp - 0.17, ty + th + 0.06, 0.34, 0.26, fill=SOFT))
        elif cls == "altar":
            cw2, cd = min(iw * 0.8, 1.8), 0.55
            cx2 = ix + (iw - cw2) / 2
            cyy = iy if back == "top" else iy + ih - cd
            o.append(_R(cx2, cyy, cw2, cd, fill="#efe3cf", st=WD, sw=1.5))
            o.append(_Cc(cx2 + cw2 / 2, cyy + cd / 2, 0.13, st="#b6892f"))
        elif cls == "wc":
            o.append(_R(ix, iy, min(0.6, iw * 0.5), min(0.7, ih * 0.4),
                        fill=SOFT, st=FS, r=0.18))                     # toilet
            o.append(_R(ix, iy + ih - 0.42, min(0.7, iw * 0.6), 0.38,
                        fill=SOFT, st=FS, r=0.05))                     # basin
            if iw * ih > 3.2:
                o.append(_R(ix + iw - 0.92, iy + ih - 0.92, 0.88, 0.88,
                            fill="#eef2f4", st=FS))
                o.append(_L(ix + iw - 0.92, iy + ih - 0.92, ix + iw - 0.04, iy + ih - 0.04))
        elif cls == "garage":
            n_car = 2 if iw > 4.6 else 1
            cwid = (iw - 0.3 * (n_car + 1)) / n_car
            car_l = min(ih * 0.82, 4.6)
            for i in range(n_car):
                cx0 = ix + 0.3 + i * (cwid + 0.3)
                o.append(_R(cx0, iy + (ih - car_l) / 2, min(cwid, 2.0), car_l,
                            fill="#eef0f4", st=FS, r=0.25))
        elif cls == "stair":
            if rw >= rh:
                n = max(5, int(iw / 0.27)); step = iw / n
                for i in range(1, n):
                    o.append(_L(ix + i * step, iy, ix + i * step, iy + ih, sw=1.0))
            else:
                n = max(5, int(ih / 0.27)); step = ih / n
                for i in range(1, n):
                    o.append(_L(ix, iy + i * step, ix + iw, iy + i * step, sw=1.0))
        elif cls == "study":
            dw2 = min(iw * 0.7, 1.4)
            o.append(_R(ix + (iw - dw2) / 2, iy if back == "top" else iy + ih - 0.6,
                        dw2, 0.6, fill="#f3ece1", st=WD))              # desk
            o.append(_Cc(ix + iw / 2, iy + 0.9 if back == "top" else iy + ih - 0.9, 0.22))
        elif cls == "gym":
            o.append(_R(ix, iy, min(0.9, iw * 0.4), min(2.0, ih * 0.7),
                        fill="#eef0f4", st=FS, r=0.1))                 # treadmill
            o.append(_R(ix + iw - min(1.6, iw * 0.5), iy + ih - min(1.2, ih * 0.4),
                        min(1.6, iw * 0.5), min(1.2, ih * 0.4), fill="#eef2ea", st=FS))  # mat
        elif cls == "terrace":
            for k in range(3):
                o.append(_R(ix + 0.1, iy + 0.1 + k * (ih / 3), 0.4, 0.4,
                            fill="#eef2ea", st="#cfd6dd"))             # planters
        elif cls == "store":
            o.append(_R(ix, iy, iw, 0.42, fill="#f0ece4", st="#cfd6dd"))
            if ih > 1.2:
                o.append(_R(ix, iy + 0.6, iw, 0.42, fill="#f0ece4", st="#cfd6dd"))
        return o

    # rooms
    for idx, rm in enumerate(fd["rooms"]):
        rx, ry, rw, rh = rm["x_m"], rm["y_m"], rm["w_m"], rm["h_m"]
        cls = _room_class(rm)
        rear = cy0 is not None and abs((ry + rh) - cy0) < 0.1
        front = cy1 is not None and abs(ry - cy1) < 0.1
        fill = "#f7f4ee" if cls == "corridor" else "#eef0f4" if cls == "stair" \
            else _FILLS[idx % len(_FILLS)]
        p.append(f'<rect x="{X(rx):.1f}" y="{Y(ry):.1f}" width="{rw*pxm:.1f}" '
                 f'height="{rh*pxm:.1f}" fill="{fill}" stroke="#5b6470" stroke-width="2"/>')

        if cls == "corridor":  # dashed centreline so the hành lang reads as circulation
            if rw >= rh:
                p.append(_L(rx + 0.3, ry + rh / 2, rx + rw - 0.3, ry + rh / 2,
                            st="#c2b69a", sw=1.0))
            else:
                p.append(_L(rx + rw / 2, ry + 0.3, rx + rw / 2, ry + rh - 0.3,
                            st="#c2b69a", sw=1.0))

        # furniture + door into corridor
        p.extend(_furn(rm, rear, front))
        p.extend(_door(rm, rear, front))

        # windows on perimeter edges
        per = _on_perimeter(rx, ry, rw, rh, W, D)
        for edge, on in per.items():
            if not on or cls in ("corridor",):
                continue
            if edge in ("top", "bottom"):
                wlen = min(2.2, rw * 0.5)
                x0 = X(rx + (rw - wlen) / 2)
                yy = Y(0 if edge == "top" else D)
                p.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{x0+wlen*pxm:.1f}" y2="{yy:.1f}" '
                         f'stroke="#2f7fd6" stroke-width="4"/>')
            else:
                wlen = min(2.2, rh * 0.5)
                y0 = Y(ry + (rh - wlen) / 2)
                xx = X(0 if edge == "left" else W)
                p.append(f'<line x1="{xx:.1f}" y1="{y0:.1f}" x2="{xx:.1f}" y2="{y0+wlen*pxm:.1f}" '
                         f'stroke="#2f7fd6" stroke-width="4"/>')

        # label with a soft white halo so it stays readable over furniture.
        # Narrow tall columns (WC, stair, maid…) rotate the label into the long
        # axis and auto-shrink the font — exactly how a KTS annotates slim rooms.
        cx, cy = X(rx + rw / 2), Y(ry + rh / 2)
        nm = _esc(rm["name"])
        if len(nm) > 22:
            nm = nm[:21] + "…"
        dim = f'{rw:.1f}×{rh:.1f}m · {rm["area_m2"]:.0f}m²'
        longest = max(len(nm), len(dim))
        room_pw, room_ph = rw * pxm, rh * pxm
        need_pw = longest * 7.0 + 12
        vertical = room_pw < need_pw and room_ph > room_pw and rh > rw * 1.2
        avail = (room_ph if vertical else room_pw) - 12
        fs = max(7.5, min(12.0, avail / (longest * 0.6)))
        halo_w = min((room_ph if vertical else room_pw) - 3, longest * fs * 0.6 + 10)
        sub_fs = fs * 0.82
        lbl = [
            f'<rect x="{cx-halo_w/2:.1f}" y="{cy-fs-2:.1f}" width="{halo_w:.1f}" '
            f'height="{fs*2+8:.1f}" rx="5" fill="#ffffff" opacity="0.76"/>',
            f'<text x="{cx:.1f}" y="{cy-2:.1f}" font-size="{fs:.1f}" font-weight="600" '
            f'fill="#2b3138" text-anchor="middle">{nm}</text>',
            f'<text x="{cx:.1f}" y="{cy+sub_fs+2:.1f}" font-size="{sub_fs:.1f}" '
            f'fill="#6b7480" text-anchor="middle">{dim}</text>',
        ]
        if vertical:
            p.append(f'<g transform="rotate(-90 {cx:.1f} {cy:.1f})">')
            p.extend(lbl)
            p.append('</g>')
        else:
            p.extend(lbl)

    # exterior wall (thick)
    p.append(f'<rect x="{X(0):.1f}" y="{Y(0):.1f}" width="{W*pxm:.1f}" height="{D*pxm:.1f}" '
             f'fill="none" stroke="#1d2329" stroke-width="5"/>')

    # main entrance on ground floor (front = bottom)
    if fd["floor"] == 1:
        elen = 1.2 * pxm
        ex = X(W / 2 - 0.6)
        p.append(f'<rect x="{ex:.1f}" y="{Y(D)-3:.1f}" width="{elen:.1f}" height="6" fill="#ffffff" '
                 f'stroke="#1d2329" stroke-width="2"/>')
        p.append(f'<text x="{X(W/2):.1f}" y="{Y(D)+20:.1f}" font-size="10" fill="#1d2329" '
                 f'text-anchor="middle">▲ LỐI VÀO</text>')

    # overall dimensions
    p.append(f'<text x="{X(W/2):.1f}" y="{M-24:.1f}" font-size="12" font-weight="700" '
             f'fill="#2f7fd6" text-anchor="middle">{W:.1f} m</text>')
    p.append(f'<line x1="{X(0):.1f}" y1="{M-18:.1f}" x2="{X(W):.1f}" y2="{M-18:.1f}" '
             f'stroke="#2f7fd6" stroke-width="1"/>')
    p.append(f'<text x="{M-30:.1f}" y="{Y(D/2):.1f}" font-size="12" font-weight="700" fill="#2f7fd6" '
             f'text-anchor="middle" transform="rotate(-90 {M-30:.1f} {Y(D/2):.1f})">{D:.1f} m</text>')
    p.append(f'<line x1="{M-22:.1f}" y1="{Y(0):.1f}" x2="{M-22:.1f}" y2="{Y(D):.1f}" '
             f'stroke="#2f7fd6" stroke-width="1"/>')

    # north arrow
    nax, nay = cw - 34, 40
    p.append(f'<circle cx="{nax}" cy="{nay}" r="15" fill="none" stroke="#5b6470" stroke-width="1"/>')
    p.append(f'<path d="M {nax} {nay-12} l 5 12 l -5 -4 l -5 4 z" fill="#1d2329"/>')
    p.append(f'<text x="{nax}" y="{nay+24}" font-size="9" fill="#5b6470" text-anchor="middle">B</text>')

    # title block
    grid = fd["column_grid"]
    title = (f'MẶT BẰNG TẦNG {fd["floor"]}  ·  GFA {fd["gfa_m2"]:.0f} m²  ·  '
             f'{W:.1f}×{D:.1f} m  ·  Lưới cột {grid["module_x_m"]:.1f}×{grid["module_y_m"]:.1f} m')
    p.append(f'<text x="{M:.1f}" y="{ch-26:.1f}" font-size="12.5" font-weight="700" '
             f'fill="#1d2329">{_esc(title)}</text>')
    p.append(f'<text x="{M:.1f}" y="{ch-10:.1f}" font-size="10" fill="#8a929c">'
             f'Tỉ lệ ~1:100 · Sơ đồ AI (diện tích &amp; lưới cột đúng) — KTS chứng chỉ hoàn thiện bản vẽ thi công</text>')
    p.append('</svg>')
    return "".join(p)


def _svg_data_uri(svg: str) -> str:
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


# ── floor layout: cultural zoning, then squarified treemap ───────
def _squarify_cells(placed: list[dict], W: float, D: float) -> list[dict]:
    rects = _squarify([(r["area_m2"], r) for r in placed], 0.0, 0.0, W, D)
    return [{"p": p, "x": rx, "y": ry, "w": rw, "h": rh} for p, rx, ry, rw, rh in rects]


def _layout_with_altar(placed: list[dict], W: float, D: float, altar: dict) -> list[dict]:
    """Place the altar as a dignified rear-centre room, isolated from EVERY wet room by a
    full-width buffer strip so bàn thờ never shares an edge with a WC and is never a corner.

    Footprint is split top→bottom into three bands that tile exactly:
        [ rear band : flanker | ALTAR | flanker ]   ← altar centred, on the back wall
        [ buffer strip (hành lang thờ), full width ] ← separates altar from everything wet
        [ front zone : squarified rooms incl. all WCs ]
    """
    others = [r for r in placed if r is not altar]
    wet = [r for r in others if r["kind"] == "wet"]
    if not wet:
        # no WC on this floor → taboo cannot occur → normal treemap (prettier, fewer edge cases)
        return _squarify_cells(placed, W, D)

    buffer_room = next((r for r in others if r.get("_buffer")), None)
    if buffer_room is None:
        buffer_room = {"name": "Hành lang đệm", "area_m2": round(W * 1.2, 1),
                       "kind": "service", "priority": "normal", "_buffer": True}
        others.append(buffer_room)
    dry = [r for r in others if r is not buffer_room and r["kind"] != "wet"]

    # flankers = two smallest non-kitchen/garage rooms → altar centred, big public rooms
    # (phòng khách…) stay in the front zone near the entrance.
    cand = sorted([r for r in dry if not (_is_kitchen(r["name"])
                   or "gara" in r["name"].lower() or "garage" in r["name"].lower())],
                  key=lambda r: r["area_m2"])
    if len(cand) < 2:
        cand = sorted(dry, key=lambda r: r["area_m2"])
    flankers: list[dict] = []
    for r in cand:
        flankers.append(r)
        if len(flankers) >= 2:
            break
    flank_ids = {id(f) for f in flankers}
    front_rooms = [r for r in dry if id(r) not in flank_ids] + wet  # wet last → far from altar

    band_area = float(altar["area_m2"]) + sum(r["area_m2"] for r in flankers)
    da = min(max(band_area / W, MIN_ROOM_DIM_M), 0.5 * D) if W > 0 else MIN_ROOM_DIM_M

    # balance flankers on both sides so the altar is in the middle (never a corner)
    left, right = [], []
    sl = sr = 0.0
    for f in sorted(flankers, key=lambda r: -r["area_m2"]):
        if sl <= sr:
            left.append(f); sl += f["area_m2"]
        else:
            right.append(f); sr += f["area_m2"]
    band_order = left + [altar] + list(reversed(right))

    cells: list[dict] = []
    x = 0.0
    for r in band_order:
        w = (r["area_m2"] / band_area) * W if band_area > 0 else W / max(1, len(band_order))
        cells.append({"p": r, "x": x, "y": 0.0, "w": w, "h": da})
        x += w
    cells[-1]["w"] = W - cells[-1]["x"]  # remove float drift → rear band fills W exactly

    # full-width buffer strip directly below the altar band
    dc = min(max(buffer_room["area_m2"] / W, 1.0), 2.0, max(0.6, (D - da) * 0.5))
    cells.append({"p": buffer_room, "x": 0.0, "y": da, "w": W, "h": dc})

    # remaining rooms (incl. every WC) squarified into the front zone
    fy = da + dc
    fh = D - fy
    if front_rooms and fh > 0.05:
        for p, rx, ry, rw, rh in _squarify([(r["area_m2"], r) for r in front_rooms],
                                           0.0, fy, W, fh):
            cells.append({"p": p, "x": rx, "y": ry, "w": rw, "h": rh})
    else:  # nothing to fill the front → grow buffer to close the floor (rare degenerate case)
        cells[-1]["h"] = D - da
    return cells


# ── circulation-correct layout: double-loaded corridor "comb" ─────
# Every habitable room shares a FULL edge with a central corridor (hành lang) → it always
# has a real way in (fixes "phòng không lối đi"). Quiet rooms (thờ/ngủ/bếp/làm việc) take
# the rear band; public + service (khách/sảnh/gara/WC chung) the front band by the entrance;
# the full-width corridor runs between them → it also separates the altar from every WC
# (uế khí taboo solved by construction). Spare WCs become ensuite notches inside their
# bedroom (entered from the bedroom, on the outer wall) so they need no corridor frontage.
CORRIDOR_MIN_M, CORRIDOR_MAX_M = 1.3, 2.2
STAIR_AREA_M2 = 11.0

_REAR_KEYS = ("thờ", "ngủ", "master", "phòng con", "bếp", "ăn", "làm việc",
              "đọc", "thư", "studio", "thay đồ", "kho", "giặt")
_FRONT_KEYS = ("khách", "sảnh", "tiền", "gara", "garage", "wc", "vệ sinh", "tắm",
               "toilet", "gym", "giải trí", "cinema", "phim", "sân", "hiên",
               "ban công", "kỹ thuật", "cầu thang")


def _rear_or_front(r: dict) -> str:
    n = (r.get("name") or "").lower()
    if _is_altar(n):
        return "rear"
    if r.get("_stair"):
        return "front"
    for k in _FRONT_KEYS:
        if k in n:
            return "front"
    for k in _REAR_KEYS:
        if k in n:
            return "rear"
    return "rear" if r.get("kind") == "bed" else "front"


def _pair_ensuite(rooms: list[dict]) -> list[dict]:
    """Attach each spare WC to a bedroom as an ensuite child (drawn inside the bedroom
    column, on the outer wall). One common WC is reserved on floors that have public rooms
    (khách WC); the rest become ensuite. Bedrooms must be ≥14 m² to host one."""
    beds = sorted((r for r in rooms if r["kind"] == "bed"), key=lambda r: -r["area_m2"])
    wets = [r for r in rooms if r["kind"] == "wet"]
    has_public = any(r["kind"] == "public" for r in rooms)
    reserve = 1 if (has_public and wets) else 0
    ensuite_wets = wets[: max(0, len(wets) - reserve)]
    out = list(rooms)
    bi = 0
    for w in ensuite_wets:
        while bi < len(beds) and (beds[bi]["area_m2"] < 14.0 or beds[bi].get("_ensuite")):
            bi += 1
        if bi >= len(beds):
            break
        bed = beds[bi]; bi += 1
        ens_area = round(min(w["area_m2"], bed["area_m2"] * 0.32), 1)
        bed["_ensuite"] = {"name": w["name"], "area_m2": ens_area, "kind": "wet"}
        bed["area_m2"] = round(bed["area_m2"] + ens_area, 1)  # column now holds bed + WC
        if w in out:
            out.remove(w)
    return out


def _balance_center(rooms: list[dict], target: dict | None) -> list[dict]:
    """Order a row so `target` (altar / foyer) sits in the MIDDLE, never a corner."""
    others = [r for r in rooms if r is not target]
    left, right, sl, sr = [], [], 0.0, 0.0
    for r in sorted(others, key=lambda r: -r["area_m2"]):
        if sl <= sr:
            left.append(r); sl += r["area_m2"]
        else:
            right.append(r); sr += r["area_m2"]
    mids = [target] if target is not None else []
    return left + mids + list(reversed(right))


def _row_cells(row: list[dict], y0: float, W: float, depth: float, corridor_side: str) -> list[dict]:
    """Lay a row of rooms side-by-side across width W, each spanning the full row `depth`
    (so each touches the corridor on `corridor_side`). Any ensuite WC is split off along
    the depth onto the OUTER wall (away from the corridor), entered from its bedroom."""
    cells: list[dict] = []
    if not row or depth <= 0 or W <= 0:
        return cells
    area = sum(r["area_m2"] for r in row) or 1.0
    x = 0.0
    for i, r in enumerate(row):
        w = (r["area_m2"] / area) * W
        if i == len(row) - 1:
            w = W - x  # kill float drift → row fills W exactly
        ens = r.get("_ensuite")
        if ens and w > 0:
            wc_d = min(max(ens["area_m2"] / w, 1.4), depth * 0.45)
            bed_d = depth - wc_d
            wc = {"name": ens["name"], "kind": "wet", "_ensuite_host": r.get("name")}
            if corridor_side == "bottom":   # corridor below → bedroom at bottom, WC on rear wall (top)
                cells.append({"p": wc, "x": x, "y": y0, "w": w, "h": wc_d})
                cells.append({"p": r, "x": x, "y": y0 + wc_d, "w": w, "h": bed_d})
            else:                            # corridor above → bedroom at top, WC on front wall (bottom)
                cells.append({"p": r, "x": x, "y": y0, "w": w, "h": bed_d})
                cells.append({"p": wc, "x": x, "y": y0 + bed_d, "w": w, "h": wc_d})
        else:
            cells.append({"p": r, "x": x, "y": y0, "w": w, "h": depth})
        x += w
    return cells


def _protect_altar(rear: list[dict], front: list[dict], altar: dict) -> None:
    """Guarantee ≥2 'safe' rear rooms (no WC, no ensuite) to flank the altar so bàn thờ
    never shares an edge with a WC and is never a corner. If short, demote the smallest
    ensuite bedrooms back to a plain bedroom + a standalone common WC moved to the front row."""
    def safe_count() -> int:
        return sum(1 for r in rear if r is not altar
                   and not r.get("_ensuite") and r.get("kind") != "wet")
    risky_beds = sorted((r for r in rear if r.get("_ensuite")), key=lambda r: r["area_m2"])
    i = 0
    while safe_count() < 2 and i < len(risky_beds):
        bed = risky_beds[i]; i += 1
        ens = bed.pop("_ensuite")
        bed["area_m2"] = round(bed["area_m2"] - ens["area_m2"], 1)
        front.append({"name": ens["name"], "area_m2": ens["area_m2"],
                      "kind": "wet", "priority": "normal"})


def _order_rear_altar(rear: list[dict], altar: dict) -> list[dict]:
    """Altar centred, flanked by SAFE rooms; any WC-bearing room pushed to the outer ends."""
    safe = [r for r in rear if r is not altar and not r.get("_ensuite") and r.get("kind") != "wet"]
    risky = [r for r in rear if r is not altar and (r.get("_ensuite") or r.get("kind") == "wet")]
    core = _balance_center(safe, altar)
    le, ri, sl, sr = [], [], 0.0, 0.0
    for r in sorted(risky, key=lambda r: -r["area_m2"]):
        if sl <= sr:
            le.append(r); sl += r["area_m2"]
        else:
            ri.append(r); sr += r["area_m2"]
    return le + core + ri


def _waterfill(widths: list[float], mins: list[float], total: float) -> list[float]:
    """Raise any sub-min width up to its minimum by borrowing proportionally from the wider
    columns, keeping the row total fixed (so the tiling stays exact)."""
    w = list(widths)
    if sum(mins) >= total:  # can't satisfy all → scale minimums to fit
        s = total / sum(mins) if sum(mins) > 0 else 0.0
        return [m * s for m in mins]
    for _ in range(12):
        deficit = sum(max(0.0, mins[i] - w[i]) for i in range(len(w)))
        if deficit < 1e-6:
            break
        donors = [i for i in range(len(w)) if w[i] > mins[i] + 1e-6]
        pool = sum(w[i] - mins[i] for i in donors) or 1.0
        for i in range(len(w)):
            if w[i] < mins[i]:
                w[i] = mins[i]
        for i in donors:
            w[i] -= (w[i] - mins[i]) / pool * deficit
    return w


def _relax_rows(cells: list[dict]) -> None:
    """Enforce a believable minimum width on every column of each row (rear/front), so small
    rooms (WC chung, kho, cầu thang) stop becoming 0.5 m slivers. Borrows width from the
    widest sibling in the same row; the row keeps filling W exactly."""
    corr = next((c for c in cells if c["p"].get("_corridor")), None)
    if not corr:
        return
    cy0 = corr["y"]
    for band_is_rear in (True, False):
        cs = [c for c in cells if not c["p"].get("_corridor")
              and ((c["y"] < cy0 - 1e-6) == band_is_rear)]
        if not cs:
            continue
        cols: dict[float, list[dict]] = {}
        for c in cs:
            cols.setdefault(round(c["x"], 3), []).append(c)
        xs = sorted(cols)
        widths = [cols[x][0]["w"] for x in xs]
        total = sum(widths)
        mins = []
        for x in xs:
            p = cols[x][0]["p"]
            mins.append(1.6 if p.get("kind") == "wet"
                        else 1.4 if p.get("_stair") else 2.0)
        widths = _waterfill(widths, mins, total)
        nx = 0.0
        for x, wnew in zip(xs, widths):
            for c in cols[x]:
                c["x"] = nx; c["w"] = wnew
            nx += wnew


def _compose_floor(rooms: list[dict], W: float, D: float, altar: dict | None,
                   floor_idx: int, num_floors: int) -> list[dict]:
    placed = _pair_ensuite([dict(r) for r in rooms])
    altar = next((r for r in placed if _is_altar(r["name"])), None)
    if num_floors > 1:
        placed.append({"name": "Cầu thang", "area_m2": STAIR_AREA_M2,
                       "kind": "service", "priority": "high", "_stair": True})

    rear = [r for r in placed if _rear_or_front(r) == "rear"]
    front = [r for r in placed if _rear_or_front(r) == "front"]
    if not rear and front:
        rear.append(front.pop(max(range(len(front)), key=lambda i: front[i]["area_m2"])))
    if not front and rear:
        front.append(rear.pop(max(range(len(rear)), key=lambda i: rear[i]["area_m2"])))
    if altar in rear:
        _protect_altar(rear, front, altar)

    rear_area = sum(r["area_m2"] for r in rear)
    front_area = sum(r["area_m2"] for r in front)
    total = rear_area + front_area

    # corridor height absorbs the footprint slack; clamp to a believable band
    ch = (W * D - total) / W if W > 0 else CORRIDOR_MIN_M
    if ch < CORRIDOR_MIN_M:
        f = (W * D - W * CORRIDOR_MIN_M) / total if total > 0 else 1.0
        for r in placed:
            r["area_m2"] = round(r["area_m2"] * f, 2)
            if r.get("_ensuite"):
                r["_ensuite"]["area_m2"] = round(r["_ensuite"]["area_m2"] * f, 2)
        rear_area *= f; front_area *= f; ch = CORRIDOR_MIN_M
    elif ch > CORRIDOR_MAX_M:
        extra = round((ch - CORRIDOR_MAX_M) * W, 1)
        # absorb the slack into USEFUL rooms instead of one giant void balcony
        if floor_idx > 1 and extra > 22:
            fam = round(min(extra * 0.6, 30.0), 1)
            front.append({"name": "Phòng sinh hoạt chung", "area_m2": fam,
                          "kind": "public", "priority": "normal"})
            extra = round(extra - fam, 1)
        label = "Sảnh / hiên" if floor_idx == 1 else "Sân thượng / ban công"
        front.append({"name": label, "area_m2": extra, "kind": "service", "priority": "low"})
        front_area += round((ch - CORRIDOR_MAX_M) * W, 1); ch = CORRIDOR_MAX_M

    rd = rear_area / W if W > 0 else 0.0
    fd = front_area / W if W > 0 else 0.0

    if altar in rear:
        rear = _order_rear_altar(rear, altar)
    foyer_t = next((r for r in front if r.get("_stair") or "sảnh" in r["name"].lower()
                    or "khách" in r["name"].lower()), None)
    front = _balance_center(front, foyer_t)

    cells: list[dict] = []
    cells += _row_cells(rear, 0.0, W, rd, "bottom")
    cells.append({"p": {"name": "Hành lang", "kind": "service", "_corridor": True},
                  "x": 0.0, "y": rd, "w": W, "h": ch})
    cells += _row_cells(front, rd + ch, W, fd, "top")
    return cells


def _layout_floor(placed: list[dict], W: float, D: float, altar: dict | None,
                  floor_idx: int = 1, num_floors: int = 1) -> list[dict]:
    return _compose_floor(placed, W, D, altar, floor_idx, num_floors)


# ── one floor: pack + grid + walls + openings + quantities ────────
def _build_floor(floor_idx: int, rooms: list[dict], W: float, D: float, num_floors: int = 1) -> dict:
    altar = next((r for r in rooms if _is_altar(r["name"])), None)
    cells = _compose_floor(list(rooms), W, D, altar, floor_idx, num_floors)
    _relax_rows(cells)
    _snap_cells(cells)

    corr = next((c for c in cells if c["p"].get("_corridor")), None)
    cy0 = corr["y"] if corr else None
    cy1 = (corr["y"] + corr["h"]) if corr else None

    out_rooms = []
    for c in cells:
        rx, ry, rw, rh = c["x"], c["y"], c["w"], c["h"]
        p = c["p"]
        role = ("corridor" if p.get("_corridor") else "stair" if p.get("_stair")
                else "ensuite" if p.get("_ensuite_host") else None)
        # access = the cell is itself circulation, an ensuite reached from its host bedroom,
        # or it shares its near edge with the corridor band (real door into the hành lang)
        touches = bool(corr) and (
            (cy0 is not None and abs((ry + rh) - cy0) < 0.06)
            or (cy1 is not None and abs(ry - cy1) < 0.06)
        )
        access = bool(p.get("_corridor") or p.get("_stair") or p.get("_ensuite_host") or touches)
        out_rooms.append({
            "name": p["name"], "kind": p["kind"],
            "x_m": round(rx, 2), "y_m": round(ry, 2),
            "w_m": round(rw, 2), "h_m": round(rh, 2),
            "area_m2": round(rw * rh, 1),
            "on_perimeter": _on_perimeter(rx, ry, rw, rh, W, D),
            "role": role,
            "ensuite_host": p.get("_ensuite_host"),
            "access": access,
        })

    mx, my = _pick_module(W), _pick_module(D)
    nx, ny = round(W / mx) + 1, round(D / my) + 1
    col_count = nx * ny

    # walls: perimeter + internal partition edges (sum of room edges not on perimeter,
    # halved because shared between two rooms)
    perim_len = 2 * (W + D)
    interior_edge = 0.0
    for r in out_rooms:
        per = r["on_perimeter"]
        if not per["top"]:
            interior_edge += r["w_m"]
        if not per["bottom"]:
            interior_edge += r["w_m"]
        if not per["left"]:
            interior_edge += r["h_m"]
        if not per["right"]:
            interior_edge += r["h_m"]
    partition_len = round(interior_edge / 2, 1)
    # wall area = (exterior perimeter + interior partitions) × storey height
    wall_area = round((perim_len + partition_len) * FLOOR_HEIGHT_M, 1)

    doors = len(out_rooms) + (1 if floor_idx == 1 else 0)
    windows, win_area = 0, 0.0
    for r in out_rooms:
        if r["kind"] in ("service",):
            continue
        edges_on = sum(r["on_perimeter"].values())
        if edges_on:
            windows += edges_on
            win_area += min(2.2, max(r["w_m"], r["h_m"]) * 0.5) * 1.5  # ~1.5 m tall
    win_area = round(win_area, 1)

    fd = {
        "floor": floor_idx,
        "footprint": {"w_m": round(W, 2), "d_m": round(D, 2)},
        "gfa_m2": round(W * D, 1),
        "rooms": out_rooms,
        "column_grid": {"module_x_m": mx, "module_y_m": my, "nx": nx, "ny": ny,
                        "count": col_count, "max_span_m": round(max(mx, my), 2)},
        "walls": {"perimeter_m": round(perim_len, 1), "partition_m": partition_len,
                  "ext_thickness_m": WALL_EXT_M, "int_thickness_m": WALL_INT_M,
                  "height_m": FLOOR_HEIGHT_M, "total_area_m2": wall_area},
        "openings": {"doors": doors, "windows": windows, "window_area_m2": win_area},
    }
    fd["svg_data_uri"] = _svg_data_uri(_svg_floor(fd))
    return fd


# ── public entry ─────────────────────────────────────────────────
def compute_geometry(
    *,
    rooms_required: list[dict],
    num_floors: int = 2,
    layout_principles: list[str] | None = None,
    constraints: list | None = None,
) -> dict:
    """Deterministic floor-plan geometry from the room program. Never raises on bad input."""
    rooms = _normalize_rooms(rooms_required)
    nf = max(1, int(num_floors or 1))
    floor_rooms = _assign_floors(rooms, nf)

    # shared footprint sized to the busiest floor (keeps columns continuous)
    per_floor_area = []
    for fr in floor_rooms:
        net = sum(r["area_m2"] for r in fr)
        per_floor_area.append(net * (1 + CIRCULATION_FRAC))
    foot_area = max(per_floor_area) if per_floor_area else 80.0
    W = round((foot_area * FOOTPRINT_RATIO) ** 0.5, 1)
    D = round(foot_area / W, 1) if W else 8.0
    W = max(W, MIN_ROOM_DIM_M * 2)
    D = max(D, MIN_ROOM_DIM_M * 2)

    floors = [_build_floor(i + 1, fr, W, D, nf) for i, fr in enumerate(floor_rooms)]

    total_gfa = round(sum(f["gfa_m2"] for f in floors), 1)
    building_h = round(nf * FLOOR_HEIGHT_M + ROOF_RISE_M, 1)

    # ── derived engineering quantities (grounded in real geometry) ──
    slab_area = total_gfa
    slab_conc = slab_area * SLAB_THK_M
    beam_len = sum((f["column_grid"]["nx"] * W + f["column_grid"]["ny"] * D) for f in floors)
    beam_conc = beam_len * BEAM_W_M * BEAM_H_M
    col_count = floors[0]["column_grid"]["count"] if floors else 0
    col_conc = col_count * COL_W_M * COL_W_M * building_h
    concrete_m3 = round(slab_conc + beam_conc + col_conc, 1)
    wall_area = round(sum(f["walls"]["total_area_m2"] for f in floors), 1)
    max_span = max((f["column_grid"]["max_span_m"] for f in floors), default=4.0)

    boq_seed = {
        "total_gfa_m2": total_gfa,
        "footprint_m2": round(W * D, 1),
        "slab_area_m2": round(slab_area, 1),
        "concrete_m3": concrete_m3,
        "rebar_kg_est": round(concrete_m3 * STEEL_KG_PER_M3, 0),
        "wall_area_m2": wall_area,
        "brick_110_count_est": round(wall_area * BRICK_PER_M2_110, 0),
        "column_count": col_count,
        "beam_length_m": round(beam_len, 1),
    }
    structural_seed = {
        "num_floors": nf,
        "building_height_m": building_h,
        "footprint_w_m": W, "footprint_d_m": D,
        "column_grid": floors[0]["column_grid"] if floors else {},
        "max_span_m": round(max_span, 2),
        "slab_area_per_floor_m2": round(W * D, 1),
        "tributary_area_m2": round(
            (floors[0]["column_grid"]["module_x_m"] * floors[0]["column_grid"]["module_y_m"])
            if floors else 14.0, 1),
    }
    mep_seed = {
        "rooms": [{"name": r["name"], "area_m2": r["area_m2"], "kind": r["kind"], "floor": f["floor"]}
                  for f in floors for r in f["rooms"] if r["kind"] != "service"],
        "wet_room_count": sum(1 for f in floors for r in f["rooms"] if r["kind"] == "wet"),
        "total_gfa_m2": total_gfa,
        "floors": nf,
    }

    return {
        "engine": "deterministic-packer-v1",
        "num_floors": nf,
        "total_gfa_m2": total_gfa,
        "building_height_m": building_h,
        "footprint": {"w_m": W, "d_m": D},
        "floors": floors,
        "drawings": [{"floor": f["floor"], "label": f"Mặt bằng tầng {f['floor']}",
                      "svg_data_uri": f["svg_data_uri"]} for f in floors],
        "structural_seed": structural_seed,
        "mep_seed": mep_seed,
        "boq_seed": boq_seed,
        "note": "AI schematic — diện tích & lưới cột đúng; KTS chứng chỉ hoàn thiện bản vẽ thi công.",
    }
