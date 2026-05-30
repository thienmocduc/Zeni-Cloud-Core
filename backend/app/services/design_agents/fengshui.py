# -*- coding: utf-8 -*-
"""
L5 — BỘ MÔN PHONG THỦY (Bát Trạch + Lỗ Ban) — engine DETERMINISTIC, $0, no-LLM.

Giải quyết trực tiếp lỗi Chairman nêu: "agent không nắm phong thủy, không biết
kích thước Lỗ Ban". Spec Zeni_Design_Agent_Company_Spec_v1 §L5 + §6.3 FENGSHUI_CHECK.

NGUỒN SỰ THẬT (GĐ2 roadmap): bảng tra nạp từ data/bat_trach.json + data/lo_ban.json
(toolkit Chairman cấp, Zeni_Design_Agent_Toolkit_v1). Module này = lớp orchestration
trên dữ liệu đó: tính cung phi, đối chiếu từng phòng từ geometry, tra Lỗ Ban cửa, sinh
nguyên tắc + cảnh báo. Nếu JSON lỗi → fallback bảng hard-code (đã đối chiếu trùng khít).

Ba năng lực:
  1. cung_menh(năm sinh, giới tính) → Cung phi Bát Trạch (Đông/Tây tứ mệnh) + ngũ hành.
  2. Bát Trạch: cung mệnh → 8 hướng ↔ 8 du niên (4 cát / 4 hung) + bố trí phòng theo du niên.
  3. Thước Lỗ Ban 52.2cm (thông thủy — cửa/cổng) + 42.9cm (dương trạch — bệ/bậc/ban thờ):
     tra kích thước → cung tốt/xấu + gợi ý kích thước tốt gần nhất.

Ranh giới: tư vấn phong thủy SƠ BỘ theo Bát Trạch (tri thức dân gian, không phải khoa học;
xếp DƯỚI an toàn kết cấu + quy chuẩn pháp lý). Năm sinh dùng dương lịch; người sinh đầu
năm (trước Lập Xuân/Tết) nên kiểm lại theo âm lịch.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

# 8 hướng theo chiều kim đồng hồ từ Bắc (0°).
DIRECTIONS = ["Bắc", "Đông Bắc", "Đông", "Đông Nam",
              "Nam", "Tây Nam", "Tây", "Tây Bắc"]
_DIR_ANGLE = {d: i * 45 for i, d in enumerate(DIRECTIONS)}

# Mã hướng trong form (lot_orientation) → tên hướng chuẩn.
ORIENT_CODE = {
    "bac": "Bắc", "dong_bac": "Đông Bắc", "dong": "Đông", "dong_nam": "Đông Nam",
    "nam": "Nam", "tay_nam": "Tây Nam", "tay": "Tây", "tay_bac": "Tây Bắc",
}

# Quái số (1..9, bỏ 5) → ngũ hành cung (bổ trợ; JSON không có cột ngũ hành).
_QUE_NGU_HANH = {
    "Khảm": "Thủy", "Khôn": "Thổ", "Chấn": "Mộc", "Tốn": "Mộc",
    "Càn": "Kim", "Đoài": "Kim", "Cấn": "Thổ", "Ly": "Hỏa",
}

CAT = {"Sinh Khí", "Thiên Y", "Diên Niên", "Phục Vị"}

# ── Fallback hard-code (đã đối chiếu TRÙNG KHÍT bat_trach.json) — dùng nếu JSON lỗi ──
_FALLBACK_BAT_TRACH: dict[str, dict[str, str]] = {
    "Khảm": {"Bắc": "Phục Vị", "Đông Bắc": "Ngũ Quỷ", "Đông": "Thiên Y", "Đông Nam": "Sinh Khí",
             "Nam": "Diên Niên", "Tây Nam": "Tuyệt Mệnh", "Tây": "Họa Hại", "Tây Bắc": "Lục Sát"},
    "Ly":   {"Bắc": "Diên Niên", "Đông Bắc": "Lục Sát", "Đông": "Sinh Khí", "Đông Nam": "Thiên Y",
             "Nam": "Phục Vị", "Tây Nam": "Ngũ Quỷ", "Tây": "Tuyệt Mệnh", "Tây Bắc": "Họa Hại"},
    "Chấn": {"Bắc": "Thiên Y", "Đông Bắc": "Lục Sát", "Đông": "Phục Vị", "Đông Nam": "Diên Niên",
             "Nam": "Sinh Khí", "Tây Nam": "Họa Hại", "Tây": "Tuyệt Mệnh", "Tây Bắc": "Ngũ Quỷ"},
    "Tốn":  {"Bắc": "Sinh Khí", "Đông Bắc": "Tuyệt Mệnh", "Đông": "Diên Niên", "Đông Nam": "Phục Vị",
             "Nam": "Thiên Y", "Tây Nam": "Ngũ Quỷ", "Tây": "Lục Sát", "Tây Bắc": "Họa Hại"},
    "Càn":  {"Bắc": "Lục Sát", "Đông Bắc": "Thiên Y", "Đông": "Ngũ Quỷ", "Đông Nam": "Họa Hại",
             "Nam": "Tuyệt Mệnh", "Tây Nam": "Diên Niên", "Tây": "Sinh Khí", "Tây Bắc": "Phục Vị"},
    "Khôn": {"Bắc": "Tuyệt Mệnh", "Đông Bắc": "Sinh Khí", "Đông": "Họa Hại", "Đông Nam": "Ngũ Quỷ",
             "Nam": "Lục Sát", "Tây Nam": "Phục Vị", "Tây": "Thiên Y", "Tây Bắc": "Diên Niên"},
    "Cấn":  {"Bắc": "Ngũ Quỷ", "Đông Bắc": "Phục Vị", "Đông": "Lục Sát", "Đông Nam": "Tuyệt Mệnh",
             "Nam": "Họa Hại", "Tây Nam": "Sinh Khí", "Tây": "Diên Niên", "Tây Bắc": "Thiên Y"},
    "Đoài": {"Bắc": "Họa Hại", "Đông Bắc": "Diên Niên", "Đông": "Tuyệt Mệnh", "Đông Nam": "Lục Sát",
             "Nam": "Ngũ Quỷ", "Tây Nam": "Thiên Y", "Tây": "Phục Vị", "Tây Bắc": "Sinh Khí"},
}
_FALLBACK_QUE_BY_CUNGPHI = {1: "Khảm", 2: "Khôn", 3: "Chấn", 4: "Tốn",
                            6: "Càn", 7: "Đoài", 8: "Cấn", 9: "Ly"}
_FALLBACK_LOBAN = {
    "thong_thuy_52_2": {"ten": "Thước Thông Thủy 52.2cm", "chu_ky_mm": 522, "do_rong_cung_mm": 65.25,
        "cung": [{"ten": "Quý Nhân", "tot": True, "y_nghia": "gặp quý nhân phù trợ"},
                 {"ten": "Hiểm Họa", "tot": False, "y_nghia": "tai họa"},
                 {"ten": "Thiên Tai", "tot": False, "y_nghia": "tai ương"},
                 {"ten": "Thiên Tài", "tot": True, "y_nghia": "tài lộc"},
                 {"ten": "Nhân Lộc", "tot": True, "y_nghia": "lộc về người"},
                 {"ten": "Cô Độc", "tot": False, "y_nghia": "cô quạnh"},
                 {"ten": "Thiên Tặc", "tot": False, "y_nghia": "mất mát"},
                 {"ten": "Tể Tướng", "tot": True, "y_nghia": "công danh"}]},
}
_FALLBACK_BOTRI = {
    "cua_chinh": ["Sinh Khí", "Diên Niên", "Thiên Y", "Phục Vị"],
    "phong_khach": ["Sinh Khí", "Diên Niên"],
    "phong_ngu_chu": ["Thiên Y", "Diên Niên", "Sinh Khí"],
    "bep": ["toa_hung_huong_cat"],
    "phong_tho": ["Phục Vị", "Sinh Khí", "Thiên Y"],
    "wc": ["Tuyệt Mệnh", "Ngũ Quỷ", "Lục Sát", "Họa Hại"],
    "kho": ["Tuyệt Mệnh", "Ngũ Quỷ", "Họa Hại"],
}


# ─────────────────────────────────────────────────────────────────────
#  Nạp dữ liệu chuẩn (bat_trach.json + lo_ban.json) — 1 lần khi import
# ─────────────────────────────────────────────────────────────────────
def _load_json(name: str) -> Optional[dict]:
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_BT = _load_json("bat_trach.json")
_LB = _load_json("lo_ban.json")

# BAT_TRACH[que][hướng] = du niên — đảo từ huong_tot_xau_theo_que (du_nien→hướng).
if _BT and _BT.get("huong_tot_xau_theo_que"):
    BAT_TRACH = {que: {d: dn for dn, d in tbl.items()}
                 for que, tbl in _BT["huong_tot_xau_theo_que"].items()}
    _QUE_BY_CUNGPHI = {int(k): v for k, v in _BT.get("que_menh_theo_cung_phi", {}).items()
                       if k.isdigit()}
    _BOTRI = _BT.get("bo_tri_phong_theo_du_nien", _FALLBACK_BOTRI)
    _DU_NIEN_DESC = {**_BT.get("du_nien", {}).get("cat", {}),
                     **_BT.get("du_nien", {}).get("hung", {})}
    _DATA_SOURCE = "bat_trach.json"
else:  # fallback
    BAT_TRACH = _FALLBACK_BAT_TRACH
    _QUE_BY_CUNGPHI = _FALLBACK_QUE_BY_CUNGPHI
    _BOTRI = _FALLBACK_BOTRI
    _DU_NIEN_DESC = {}
    _DATA_SOURCE = "fallback"

_LOBAN_RULERS = (_LB.get("thuoc") if _LB else None) or _FALLBACK_LOBAN
_DONG_TU = set((_BT or {}).get("nhom_menh", {}).get("Dong_tu_trach", {}).get(
    "cac_que", ["Khảm", "Ly", "Chấn", "Tốn"]))


def du_nien_meaning(dn: str) -> str:
    return _DU_NIEN_DESC.get(dn, "")


# ─────────────────────────────────────────────────────────────────────
#  1. CUNG MỆNH (Bát Trạch) — năm sinh + giới tính → cung phi
# ─────────────────────────────────────────────────────────────────────
def _reduce_digit(n: int) -> int:
    n = abs(int(n))
    while n >= 10:
        n = sum(int(c) for c in str(n))
    return n


def cung_menh(birth_year: int, gender: str = "nam") -> Optional[dict[str, Any]]:
    """Tính Cung phi Bát Trạch từ năm sinh dương lịch + giới tính (theo fengshui_tools).

      • s = tổng 2 số cuối năm, rút về 1 chữ số.
      • 1900–1999: Nam = 10 − s ; Nữ = 5 + s
      • 2000–2099: Nam =  9 − s ; Nữ = 6 + s
      • Rút về 1..9; cung 0 → 9; cung 5: Nam → Khôn, Nữ → Cấn.

    Returns None nếu năm sinh không hợp lệ (≤1900 / 0 → bỏ qua phong thủy).
    """
    try:
        y = int(birth_year)
    except (TypeError, ValueError):
        return None
    if y < 1901 or y > 2099:
        return None
    is_nam = str(gender).strip().lower() not in ("nu", "nữ", "female", "f", "woman")

    s = _reduce_digit(y % 100)
    if y >= 2000:
        cung = (9 - s) if is_nam else (6 + s)
    else:
        cung = (10 - s) if is_nam else (5 + s)
    cung = _reduce_digit(cung) if cung > 9 else cung
    if cung <= 0:
        cung = 9
    if cung == 5:
        que = "Khôn" if is_nam else "Cấn"
    else:
        que = _QUE_BY_CUNGPHI.get(cung)
    if not que or que not in BAT_TRACH:
        return None

    table = BAT_TRACH[que]
    cat_dirs = [d for d in DIRECTIONS if table.get(d) in CAT]
    hung_dirs = [d for d in DIRECTIONS if table.get(d) not in CAT]
    best = next((d for d in DIRECTIONS if table.get(d) == "Sinh Khí"), cat_dirs[0] if cat_dirs else "Nam")
    group = "Đông tứ mệnh" if que in _DONG_TU else "Tây tứ mệnh"
    return {
        "birth_year": y,
        "gender": "nữ" if not is_nam else "nam",
        "quai_so": cung,
        "cung": que,
        "ngu_hanh": _QUE_NGU_HANH.get(que, ""),
        "menh_group": group,
        "huong_tot": cat_dirs,
        "huong_xau": hung_dirs,
        "huong_dep_nhat": best,
        "du_nien_table": dict(table),
    }


def _dir_verdict(que: str, direction: str) -> dict[str, Any]:
    dn = BAT_TRACH.get(que, {}).get(direction)
    if not dn:
        return {"direction": direction, "du_nien": None, "good": None}
    return {"direction": direction, "du_nien": dn, "meaning": du_nien_meaning(dn),
            "nature": _NATURE.get(dn, ""), "good": dn in CAT}


_NATURE = {"Sinh Khí": "đại cát", "Thiên Y": "cát", "Diên Niên": "cát", "Phục Vị": "tiểu cát",
           "Họa Hại": "hung nhẹ", "Lục Sát": "hung", "Ngũ Quỷ": "đại hung", "Tuyệt Mệnh": "đại hung"}


# ─────────────────────────────────────────────────────────────────────
#  2. THƯỚC LỖ BAN — tra kích thước (52.2cm thông thủy / 42.9cm dương trạch)
# ─────────────────────────────────────────────────────────────────────
def _tra_lo_ban(mm: float, ruler_key: str) -> dict[str, Any]:
    t = _LOBAN_RULERS.get(ruler_key) or _LOBAN_RULERS.get("thong_thuy_52_2")
    chu_ky = float(t["chu_ky_mm"])
    rong = float(t["do_rong_cung_mm"])
    cung_list = t["cung"]
    try:
        mm_f = float(mm)
    except (TypeError, ValueError):
        return {"mm": mm, "valid": False}
    pos = mm_f % chu_ky
    idx = min(len(cung_list) - 1, int(pos // rong))
    cung = cung_list[idx]
    out = {"mm": round(mm_f), "thuoc": t.get("ten"), "cung": cung["ten"],
           "good": bool(cung["tot"]), "meaning": cung.get("y_nghia", ""),
           "cycle_mm": round(pos, 1)}
    if not cung["tot"]:
        out["suggest_mm"] = _nearest_good(mm_f, chu_ky, rong, cung_list)
    return out


def _nearest_good(mm: float, chu_ky: float, rong: float, cung_list: list) -> Optional[int]:
    base = round(mm / 5.0) * 5
    for delta in range(0, 205, 5):
        for cand in ({base + delta, base - delta} if delta else {base}):
            if cand <= 0:
                continue
            ci = min(len(cung_list) - 1, int((cand % chu_ky) // rong))
            if cung_list[ci]["tot"]:
                return int(cand)
    return None


def lo_ban_thong_thuy(mm: float) -> dict[str, Any]:
    """Tra kích thước thông thủy (mm) trên thước Lỗ Ban 52.2cm (cửa/cổng/cửa sổ)."""
    return _tra_lo_ban(mm, "thong_thuy_52_2")


def lo_ban_duong_trach(mm: float) -> dict[str, Any]:
    """Tra kích thước trên thước Dương Trạch 42.9cm (bệ/bậc/ban thờ/nội thất)."""
    return _tra_lo_ban(mm, "duong_trach_42_9")


# Kích thước cửa "đẹp" chuẩn (mm) — thông thủy lọt cung tốt Lỗ Ban 52.2cm.
GOOD_DOOR_WIDTHS = {
    "cua_chinh_2_canh": 1090,   # Quý Nhân
    "cua_chinh_4_canh": 2090,
    "cua_phong": 810,           # Nhân Lộc
    "cua_phong_lon": 850,
    "cua_so": 690,
}


# ─────────────────────────────────────────────────────────────────────
#  3. PHÂN HƯỚNG PHÒNG TỪ MẶT BẰNG (geometry) → đối chiếu cát/hung
# ─────────────────────────────────────────────────────────────────────
# Quy ước geometry: y=0 = mặt tiền (hướng nhà), y tăng về HẬU; x=0..W trục ngang.


def _room_direction(rx, ry, rw, rh, W, D, facing: str) -> str:
    """Hướng la bàn (sơ bộ) của 1 phòng theo vị trí trong mặt bằng + hướng nhà."""
    a_f = _DIR_ANGLE.get(facing, 180)
    cx, cy = rx + rw / 2.0, ry + rh / 2.0
    fx = cx / max(W, 0.1) - 0.5
    fy = cy / max(D, 0.1) - 0.5
    back = fy > 0.18
    front = fy < -0.18
    side = abs(fx) > 0.22
    ang = a_f
    if back and not front:
        ang = a_f + 180
    if side:
        ang += (45 if fx < 0 else -45)
    ang %= 360
    return DIRECTIONS[round(ang / 45.0) % 8]


# Loại phòng (geometry) → khóa bố trí trong bo_tri_phong_theo_du_nien + intent cát/hung.
def _room_fs_intent(name: str, kind: str, role: str) -> Optional[tuple[str, str]]:
    """Returns (intent, botri_key) hoặc None nếu phòng không cần luận phong thủy."""
    n = (name or "").lower()
    if role in ("corridor", "stair") or kind == "service":
        return None
    if "thờ" in n:
        return ("cat_strong", "phong_tho")
    if "master" in n or "ngủ" in n:
        return ("cat", "phong_ngu_chu")
    if "khách" in n or "sinh hoạt" in n:
        return ("cat", "phong_khach")
    if "bếp" in n:
        return ("kitchen", "bep")
    if kind == "wet" or any(k in n for k in ("wc", "vệ sinh")):
        return ("hung", "wc")
    if any(k in n for k in ("kho", "giặt")):
        return ("hung", "kho")
    return None


def _intent_ok(intent: str, dir_is_good: Optional[bool]) -> bool:
    if dir_is_good is None:
        return True
    if intent in ("cat", "cat_strong"):
        return dir_is_good
    if intent in ("hung", "kitchen"):
        return not dir_is_good   # tọa hung là TỐT cho WC/kho/bếp
    return True


def _intent_warning(name: str, intent: str, d: str, dv: dict) -> str:
    dn = dv.get("du_nien")
    if intent in ("cat", "cat_strong"):
        return (f"{name} đang ở hướng {d} = {dn} (hung). Nên chuyển về hướng tốt "
                "(Sinh Khí/Thiên Y/Diên Niên) để hợp mệnh gia chủ.")
    if intent == "kitchen":
        return (f"Bếp đang ở hướng {d} = {dn} (cát). Theo 'tọa hung hướng cát', vị trí bếp "
                "nên ở cung xấu, miệng bếp quay về hướng tốt (Thiên Y/Sinh Khí).")
    if intent == "hung":
        return (f"{name} đang ở hướng {d} = {dn} (cát). WC/kho nên 'tọa hung' (đặt ở cung "
                "xấu để trấn), nhường cung tốt cho phòng ở.")
    return ""


# ─────────────────────────────────────────────────────────────────────
#  4. ENTRY: analyze_fengshui — gom tất cả thành 1 báo cáo cho orchestrator
# ─────────────────────────────────────────────────────────────────────
def analyze_fengshui(
    *,
    birth_year: int = 0,
    gender: str = "nam",
    lot_orientation: str = "nam",
    geometry: Optional[dict] = None,
    door_widths_mm: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Báo cáo phong thủy Bát Trạch + Lỗ Ban (deterministic).

    Returns dict luôn có khóa ``enabled``. Nếu thiếu năm sinh hợp lệ → enabled=False
    nhưng VẪN trả nhận xét hướng nhà + Lỗ Ban cửa (không cần cung mệnh).
    """
    facing = ORIENT_CODE.get(str(lot_orientation), "Nam")
    cm = cung_menh(birth_year, gender)

    report: dict[str, Any] = {
        "enabled": cm is not None,
        "facing": facing,
        "school": "Bát Trạch (Bát Trạch Minh Kính)",
        "data_source": _DATA_SOURCE,
        "disclaimer": "Tư vấn phong thủy sơ bộ (tri thức dân gian, xếp dưới an toàn kết cấu "
                      "+ quy chuẩn pháp lý) — gia chủ nên đối chiếu chuyên gia; năm sinh tính "
                      "dương lịch, người sinh trước Lập Xuân kiểm lại âm lịch.",
        "principles": [],
        "warnings": [],
    }

    # ── Lỗ Ban cửa (luôn chạy, không cần cung mệnh) ──
    doors = dict(door_widths_mm or {})
    doors.setdefault("cua_chinh", GOOD_DOOR_WIDTHS["cua_chinh_2_canh"])
    doors.setdefault("cua_phong", GOOD_DOOR_WIDTHS["cua_phong"])
    report["lo_ban_doors"] = [{"label": k, **lo_ban_thong_thuy(v)} for k, v in doors.items()]
    report["good_door_sizes_mm"] = GOOD_DOOR_WIDTHS

    if cm is None:
        report["principles"].append(
            f"Cửa chính & phòng khách hướng {facing} — đón sáng, đón khách "
            "(chưa có năm sinh gia chủ nên chưa luận cung mệnh)."
        )
        return report

    report["cung_menh"] = cm

    # ── Hướng nhà (facing) vs cung mệnh — headline ──
    fv = _dir_verdict(cm["cung"], facing)
    report["facing_verdict"] = fv
    if fv["good"]:
        report["principles"].append(
            f"Hướng nhà {facing} = {fv['du_nien']} ({fv['nature']}) — HỢP mệnh "
            f"{cm['cung']} ({cm['menh_group']}). {fv['meaning']}."
        )
    else:
        report["warnings"].append(
            f"Hướng nhà {facing} = {fv['du_nien']} ({fv['nature']}) — KHÔNG hợp mệnh "
            f"{cm['cung']}. Hướng đẹp nhất cho gia chủ: {cm['huong_dep_nhat']} (Sinh Khí); "
            f"các hướng tốt: {', '.join(cm['huong_tot'])}. "
            "Hóa giải: đặt cửa/bếp/ban thờ quay về hướng tốt, dùng bình phong – tiểu cảnh."
        )

    # ── Nguyên tắc bố trí theo cung ──
    report["principles"].append(
        f"Gia chủ {cm['menh_group']} ({cm['cung']} – {cm['ngu_hanh']}). "
        f"Cửa chính/bếp/phòng ngủ chủ/ban thờ ưu tiên hướng tốt: {', '.join(cm['huong_tot'])}. "
        f"WC/kho dồn về hướng xấu (tọa hung): {', '.join(cm['huong_xau'])}."
    )

    # ── Đối chiếu từng phòng từ geometry (nếu có) ──
    room_checks: list[dict[str, Any]] = []
    if geometry and geometry.get("floors"):
        que = cm["cung"]
        for fl in geometry.get("floors", []):
            fp = fl.get("footprint") or geometry.get("footprint") or {}
            W = fp.get("w_m") or 10.0
            D = fp.get("d_m") or 12.0
            for rm in fl.get("rooms", []):
                it = _room_fs_intent(rm.get("name", ""), rm.get("kind", ""), rm.get("role", ""))
                if it is None:
                    continue
                intent, botri_key = it
                rx = rm.get("x_m", rm.get("x", 0.0)); ry = rm.get("y_m", rm.get("y", 0.0))
                rw = rm.get("w_m", rm.get("w", 1.0)); rh = rm.get("h_m", rm.get("h", 1.0))
                d = _room_direction(rx, ry, rw, rh, W, D, facing)
                dv = _dir_verdict(que, d)
                ok = _intent_ok(intent, dv["good"])
                room_checks.append({
                    "floor": fl.get("floor"), "room": rm.get("name"),
                    "direction": d, "du_nien": dv["du_nien"], "good_dir": dv["good"],
                    "intent": intent, "botri": botri_key, "pass": ok,
                })
                if not ok:
                    report["warnings"].append(_intent_warning(rm.get("name", ""), intent, d, dv))
    report["room_checks"] = room_checks
    npass = sum(1 for c in room_checks if c["pass"])
    report["room_summary"] = {"total": len(room_checks), "pass": npass,
                              "fail": len(room_checks) - npass}
    return report
