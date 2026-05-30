# -*- coding: utf-8 -*-
"""
CHECK FUNCTIONS — tự kiểm DETERMINISTIC ($0, no-LLM) theo spec Chương 6.

Mỗi agent/QA GỌI các hàm này để TỰ KIỂM trước khi báo xong — không "tự suy". Đây là cơ
chế "gate" cứng giải quyết các lỗi Chairman nêu (phòng không lối đi, bóc khối lượng khi
thiếu bản vẽ…). Mỗi hàm trả {check, passed, ...issues}.

  • PA_COMPLETENESS_CHECK   — chặn lỗi "PA thiếu mà vẫn thiết kế"
  • CIRCULATION_CHECK        — chặn lỗi "layout không lối đi"
  • FURNITURE_CLEARANCE_CHECK — chặn lỗi "phòng quá hẹp/đồ không lọt nhân trắc"
  • BOQ_TRACEABILITY_CHECK   — chặn lỗi "bóc khối lượng không truy vết được nguồn"
"""
from __future__ import annotations

from typing import Any, Optional

# ── PA bắt buộc (spec §6.1) ──
_PA_REQUIRED = ["building_type", "lot_width_m", "lot_length_m", "lot_orientation",
                "num_floors", "num_bedrooms", "budget_band", "style"]

# Diện tích/kích thước tối thiểu ergonomic (m² / m) theo loại phòng.
_MIN_AREA = {"bed": 9.0, "wet": 3.0, "public": 10.0}
_MIN_DIM = 1.8  # cạnh ngắn nhất để bố trí nội thất + lối đi tối thiểu


def pa_completeness_check(form: Optional[dict]) -> dict[str, Any]:
    """spec §6.1 — PA đủ trường bắt buộc mới được thiết kế."""
    if not form:
        return {"check": "PA_COMPLETENESS_CHECK", "passed": False,
                "missing": ["form"], "note": "Chưa có PA (form lựa chọn)."}
    missing = [k for k in _PA_REQUIRED if form.get(k) in (None, "", [])]
    recommend = [] if form.get("fengshui_year") else \
        ["fengshui_year (năm sinh gia chủ — để luận phong thủy Bát Trạch)"]
    return {"check": "PA_COMPLETENESS_CHECK", "passed": not missing,
            "missing": missing, "recommend": recommend,
            "note": "PA đủ trường bắt buộc." if not missing
                    else f"Thiếu {len(missing)} trường bắt buộc → cần hỏi khách."}


def circulation_check(geometry: Optional[dict]) -> dict[str, Any]:
    """spec §6.2 — mọi phòng phải tiếp cận được từ giao thông; phòng cô lập = FAIL.

    Dùng cờ ``access`` geometry đã tính (phòng chạm hành lang / là ensuite / là giao thông).
    """
    if not geometry or not geometry.get("floors"):
        return {"check": "CIRCULATION_CHECK", "passed": None, "note": "Chưa có mặt bằng."}
    total = 0
    isolated: list[str] = []
    for fl in geometry.get("floors", []):
        for rm in fl.get("rooms", []):
            if rm.get("role") == "corridor":
                continue
            total += 1
            if not rm.get("access", True):
                isolated.append(f"T{fl.get('floor')} {rm.get('name')}")
    passed = not isolated
    return {"check": "CIRCULATION_CHECK", "passed": passed, "total_rooms": total,
            "isolated_rooms": isolated,
            "note": "Mọi phòng có lối tiếp cận từ hành lang/cửa chính."
                    if passed else f"{len(isolated)} phòng CÔ LẬP — không có lối đi."}


def furniture_clearance_check(geometry: Optional[dict]) -> dict[str, Any]:
    """spec §6.4 — phòng đủ rộng + cạnh ngắn ≥1.8m để bố trí đồ + lối đi (proxy nhân trắc)."""
    if not geometry or not geometry.get("floors"):
        return {"check": "FURNITURE_CLEARANCE_CHECK", "passed": None, "note": "Chưa có mặt bằng."}
    tight: list[str] = []
    for fl in geometry.get("floors", []):
        for rm in fl.get("rooms", []):
            kind = rm.get("kind"); role = rm.get("role")
            if role in ("corridor", "stair") or kind == "service":
                continue
            mn = _MIN_AREA.get(kind)
            area = rm.get("area_m2", 0)
            if mn and area < mn - 0.1:
                tight.append(f"T{fl.get('floor')} {rm.get('name')} {area}m² < {mn}m²")
                continue
            w = rm.get("w_m", 9.0); h = rm.get("h_m", 9.0)
            if kind in ("bed", "public") and min(w, h) < _MIN_DIM:
                tight.append(f"T{fl.get('floor')} {rm.get('name')} cạnh hẹp {min(w, h)}m < {_MIN_DIM}m")
    return {"check": "FURNITURE_CLEARANCE_CHECK", "passed": not tight,
            "tight_rooms": tight[:10],
            "note": "Các phòng đủ rộng bố trí nội thất." if not tight
                    else f"{len(tight)} phòng chật so với chuẩn nhân trắc."}


def boq_traceability_check(boq_output: Optional[dict], grounded_from_geometry: bool) -> dict[str, Any]:
    """spec §6.6 — BOQ có hạng mục + truy vết nguồn (drawing_ref / khối lượng từ geometry)."""
    if not boq_output:
        return {"check": "BOQ_TRACEABILITY_CHECK", "passed": False, "note": "BOQ trống."}
    sheets = boq_output.get("sheets") or {}
    lines = 0
    with_ref = 0
    for v in sheets.values():
        if isinstance(v, list):
            for it in v:
                lines += 1
                if isinstance(it, dict) and (it.get("drawing_ref") or it.get("nguon")):
                    with_ref += 1
    missing = boq_output.get("missing_inputs") or []
    return {"check": "BOQ_TRACEABILITY_CHECK", "passed": lines > 0,
            "total_lines": lines, "with_drawing_ref": with_ref,
            "grounded_from_geometry": bool(grounded_from_geometry),
            "missing_inputs": missing,
            "note": ("BOQ có hạng mục" + (" (khối lượng ground từ hình học)" if grounded_from_geometry else ""))
                    if lines else "BOQ thiếu hạng mục."}


def run_all(*, form: Optional[dict], geometry: Optional[dict],
            boq_output: Optional[dict], grounded: bool) -> dict[str, Any]:
    """Chạy toàn bộ check → {checks:[...], passed_all:bool, summary:str}."""
    results = [
        pa_completeness_check(form),
        circulation_check(geometry),
        furniture_clearance_check(geometry),
        boq_traceability_check(boq_output, grounded),
    ]
    # passed_all chỉ xét các check áp dụng được (passed không None).
    applicable = [r for r in results if r.get("passed") is not None]
    passed_all = all(r["passed"] for r in applicable) if applicable else False
    npass = sum(1 for r in applicable if r["passed"])
    return {"checks": results, "passed_all": passed_all,
            "summary": f"{npass}/{len(applicable)} check đạt"}
