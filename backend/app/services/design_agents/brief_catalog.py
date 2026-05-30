# -*- coding: utf-8 -*-
"""
Structured design-brief catalog + DETERMINISTIC program builder.

Why this exists
---------------
Trước đây khách phải tự "viết prompt" (free text) → KTS Chief (LLM) đoán ra room
program → layout có thể KHÔNG đúng ý khách. Chairman muốn thay bằng một FORM mô tả
chi tiết để khách CHỌN (giống mục phương án), cộng một ô nhập mong muốn cá nhân hoá;
và khi bấm Enter thì 6 agent phải ra layout ĐÚNG Y các mục đã chọn.

Giải pháp: tách 2 phần
  1. BRIEF_FORM  — catalog câu hỏi + lựa chọn (mô tả kiểu KTS tư vấn). Frontend render
     data-driven, sửa option không cần build lại UI.
  2. build_design_program(answers) — biến lựa chọn thành room program XÁC ĐỊNH
     (rooms_required + layout_principles + constraints + brief_text). Geometry +
     Structural + MEP + BOQ đều ăn theo program này → kết quả khớp 100% lựa chọn.
     LLM Chief chỉ còn lo phần "mềm" (phong thuỷ, tường thuật, palette) — KHÔNG được
     tự ý đổi số phòng / công năng khách đã chốt.

Pure stdlib → unit-test free (không tốn LLM / tiền).
"""
from __future__ import annotations

import math
from typing import Any

# ─────────────────────────────────────────────────────────────────────
#  Tiêu chuẩn diện tích (m²) — mức "cao cấp" làm chuẩn, scale theo hoàn thiện.
#  Tên phòng cố ý chứa keyword để geometry._classify xếp đúng kind:
#    WET=("wc","vệ sinh","tắm","toilet")  BED=("ngủ","master")
#    PUBLIC=("khách","bếp","ăn","thờ","sinh hoạt","giải trí","sảnh")
#    SERVICE=("gara","garage","kho","giặt","sân","kỹ thuật","phụ")
# ─────────────────────────────────────────────────────────────────────
A_LIVING = 30.0
A_LIVING_LARGE = 42.0
A_KITCHEN_DINING = 24.0   # bếp + ăn gộp
A_KITCHEN = 14.0
A_KITCHEN_ISLAND = 20.0
A_DINING = 12.0
A_BED_MASTER = 24.0
A_BED_STD = 16.0
A_BED_ELDER = 18.0
A_ALTAR = 11.0
A_WC_GUEST = 4.0
A_WC_ENSUITE = 5.0
A_WC_SHARED = 4.0
A_GARAGE_1 = 18.0
A_GARAGE_PER_EXTRA = 15.0
A_STUDY = 12.0
A_HELPER = 8.0
A_GYM = 18.0
A_CINEMA = 20.0
A_LIBRARY = 12.0
A_STORE = 6.0
A_LAUNDRY = 6.0
A_ELEVATOR = 3.2
A_POOL = 32.0
A_BALCONY = 8.0

_MATERIAL_SCALE = {"co_ban": 0.92, "cao_cap": 1.0, "luxury": 1.15}

# Styles surfaced in the form (subset of ALLOWED_STYLES in api/design.py).
_STYLE_OPTIONS = [
    ("indochine", "Đông Dương (Indochine)",
     "Gỗ teak, gạch bông, mây tre, cửa lá sách — sang trọng hoài cổ, hợp khí hậu nhiệt đới."),
    ("modern", "Hiện đại (Modern)",
     "Đường nét tối giản, kính lớn, bê tông – gỗ, không gian mở thoáng."),
    ("luxury", "Tân cổ điển / Luxury",
     "Phào chỉ, đá marble, đèn chùm — bề thế, đẳng cấp."),
    ("japandi", "Japandi",
     "Tối giản Nhật + ấm áp Bắc Âu, gỗ sáng, tinh tế, thiền tịnh."),
    ("tropical", "Nhiệt đới (Tropical)",
     "Thông gió tự nhiên, cây xanh, vật liệu bản địa, mát mẻ."),
    ("scandinavian", "Scandinavian",
     "Sáng, gỗ tự nhiên, công năng, ấm cúng tối giản."),
]


def _single(label, options, default, *, section, qid, help=""):
    return {"id": qid, "label": label, "type": "single", "section": section,
            "help": help, "default": default,
            "options": [{"value": v, "label": l, "desc": d} for v, l, d in options]}


def _multi(label, options, *, section, qid, help=""):
    return {"id": qid, "label": label, "type": "multi", "section": section,
            "help": help, "default": [],
            "options": [{"value": v, "label": l, "desc": d} for v, l, d in options]}


def _number(label, *, qid, section, default, mn, mx, unit="", help=""):
    return {"id": qid, "label": label, "type": "number", "section": section,
            "help": help, "default": default, "min": mn, "max": mx, "unit": unit}


def _text(label, *, qid, section, placeholder="", help=""):
    return {"id": qid, "label": label, "type": "text", "section": section,
            "help": help, "default": "", "placeholder": placeholder}


# ─────────────────────────────────────────────────────────────────────
#  BRIEF_FORM — catalog khách chọn (thay cho việc tự viết prompt).
# ─────────────────────────────────────────────────────────────────────
BRIEF_FORM: list[dict[str, Any]] = [
    _single(
        "Loại công trình",
        [
            ("villa", "Biệt thự sân vườn", "Nhà ở độc lập 2–4 tầng, có sân vườn bao quanh."),
            ("townhouse", "Nhà phố / liền kề", "Mặt tiền hẹp, chiều sâu lớn, 1 mặt thoáng."),
            ("level4", "Nhà cấp 4 (1 tầng)", "Trệt, mái Thái/bằng — tiết kiệm, sinh hoạt 1 mặt sàn."),
            ("resort_villa", "Villa nghỉ dưỡng", "Ưu tiên view, hiên rộng, hồ bơi, không gian mở."),
            ("shophouse", "Nhà phố thương mại", "Tầng trệt kinh doanh, tầng trên ở."),
        ],
        "villa", section="1. Tổng quan", qid="building_type",
        help="Quyết định bố cục tổng thể và phân tầng công năng.",
    ),
    _number("Bề rộng khu đất", qid="lot_width_m", section="1. Tổng quan",
            default=8, mn=3, mx=40, unit="m", help="Cạnh mặt tiền (theo sổ đỏ)."),
    _number("Chiều sâu khu đất", qid="lot_length_m", section="1. Tổng quan",
            default=15, mn=5, mx=60, unit="m"),
    _single(
        "Hướng mặt tiền",
        [
            ("nam", "Nam", "Mát quanh năm, tránh nắng gắt — hướng đẹp nhất ở VN."),
            ("dong_nam", "Đông Nam", "Đón gió mát, nắng sáng dịu — rất tốt."),
            ("dong", "Đông", "Nắng sáng, chiều mát."),
            ("tay", "Tây", "Nắng gắt buổi chiều — cần chắn nắng kỹ."),
            ("bac", "Bắc", "Mát, ít nắng — cần lấy sáng bổ sung."),
            ("tay_nam", "Tây Nam / khác", "Tuỳ địa hình, sẽ tối ưu che nắng – lấy sáng."),
        ],
        "nam", section="1. Tổng quan", qid="lot_orientation",
        help="Dùng để bố trí phòng khách đón sáng, bếp – ban thờ hợp phong thuỷ.",
    ),
    _number("Số tầng", qid="num_floors", section="1. Tổng quan",
            default=2, mn=1, mx=5, unit="tầng"),

    _number("Số phòng ngủ", qid="num_bedrooms", section="2. Gia đình & phòng ngủ",
            default=3, mn=1, mx=8, unit="phòng", help="Tổng số phòng ngủ (gồm master)."),
    _number("Số người ở", qid="num_residents", section="2. Gia đình & phòng ngủ",
            default=4, mn=1, mx=12, unit="người", help="Dùng để tính cấp – thoát nước, điện."),
    _single(
        "Số thế hệ chung sống",
        [
            ("1", "1 thế hệ", "Độc thân / vợ chồng trẻ."),
            ("2", "2 thế hệ", "Bố mẹ + con cái."),
            ("3", "3 thế hệ", "Có ông bà — nên bố trí phòng tầng trệt."),
        ],
        "2", section="2. Gia đình & phòng ngủ", qid="generations",
    ),
    _single(
        "Phòng ngủ master",
        [
            ("yes", "Có master khép kín", "Phòng ngủ chính có WC + tủ đồ riêng."),
            ("no", "Không cần riêng", "Các phòng ngủ tương đương nhau."),
        ],
        "yes", section="2. Gia đình & phòng ngủ", qid="master_suite",
    ),
    _single(
        "Phòng ông bà ở tầng trệt",
        [
            ("yes", "Có (tầng trệt)", "Người cao tuổi không phải leo cầu thang."),
            ("no", "Không cần", ""),
        ],
        "no", section="2. Gia đình & phòng ngủ", qid="elder_room_ground",
        help="Nên chọn khi có 3 thế hệ.",
    ),

    _single(
        "Gian thờ",
        [
            ("room", "Phòng thờ riêng", "Không gian thờ tôn nghiêm, đặt hậu – trung tâm, KHÔNG cạnh WC, KHÔNG ở góc."),
            ("combined", "Ban thờ kết hợp", "Ban thờ trong phòng sinh hoạt chung, trang trọng vừa phải."),
            ("none", "Không có", "Bỏ qua không gian thờ."),
        ],
        "room", section="3. Công năng đặc thù", qid="altar",
        help="Văn hoá Việt: nơi linh thiêng — hệ thống tự đặt đúng phong thuỷ.",
    ),
    _single(
        "Gara ô tô",
        [
            ("0", "Không cần", "Chỉ để xe máy / sân."),
            ("1", "1 ô tô", "Gara 1 xe (~18m²)."),
            ("2", "2 ô tô", "Gara 2 xe (~33m²)."),
            ("3", "3 ô tô", "Gara 3 xe (~48m²)."),
        ],
        "1", section="3. Công năng đặc thù", qid="garage",
    ),
    _single(
        "Kiểu bếp",
        [
            ("island", "Bếp đảo + liên thông ăn", "Đảo bếp, liên thông phòng ăn — hiện đại, tiếp khách tiện."),
            ("open", "Bếp mở liên thông", "Liên thông khách – ăn, không gian thoáng."),
            ("closed", "Bếp kín riêng", "Tách biệt, hạn chế mùi — hợp nấu nhiều."),
        ],
        "open", section="3. Công năng đặc thù", qid="kitchen",
    ),
    _single(
        "Phòng ăn riêng",
        [
            ("yes", "Có phòng ăn riêng", "Bàn ăn tách khu bếp."),
            ("no", "Gộp bếp – ăn", "Tiết kiệm diện tích."),
        ],
        "no", section="3. Công năng đặc thù", qid="dining_separate",
    ),
    _single(
        "Quy mô phòng khách",
        [
            ("standard", "Tiêu chuẩn", "Phòng khách ~30m²."),
            ("large", "Lớn / thông tầng", "Phòng khách bề thế ~42m², trần cao đón sáng."),
        ],
        "standard", section="3. Công năng đặc thù", qid="living_scale",
    ),
    _single(
        "Phòng làm việc / thư phòng",
        [("yes", "Có", "Không gian làm việc – đọc sách yên tĩnh."), ("no", "Không", "")],
        "no", section="3. Công năng đặc thù", qid="work_room",
    ),
    _single(
        "Phòng giúp việc",
        [("yes", "Có", "Phòng nhỏ cho người giúp việc, gần khu phụ."), ("no", "Không", "")],
        "no", section="3. Công năng đặc thù", qid="helper_room",
    ),
    _single(
        "Sân vườn",
        [
            ("front", "Sân vườn trước", "Khoảng lùi mặt tiền, tiểu cảnh đón khách."),
            ("back", "Sân sau", "Sân phơi – giặt, bếp thoáng."),
            ("both", "Cả trước và sau", "Thông gió xuyên phòng, nhiều cây xanh."),
            ("none", "Không có", "Xây kín hết đất."),
        ],
        "front", section="3. Công năng đặc thù", qid="garden",
    ),
    _single(
        "Giếng trời",
        [("yes", "Có giếng trời", "Lấy sáng – thông gió giữa nhà, rất hợp nhà phố."),
         ("no", "Không", "")],
        "yes", section="3. Công năng đặc thù", qid="skylight",
    ),
    _multi(
        "Không gian đặc biệt (chọn nhiều)",
        [
            ("pool", "Hồ bơi", "Bể bơi sân vườn / sân sau."),
            ("gym", "Phòng gym", "Khu tập luyện tại gia."),
            ("cinema", "Phòng chiếu phim", "Phòng giải trí cách âm."),
            ("library", "Phòng đọc / thư viện", "Không gian sách yên tĩnh."),
            ("store", "Kho", "Kho chứa đồ."),
            ("laundry", "Phòng giặt – phơi", "Khu giặt sấy riêng."),
            ("elevator", "Thang máy", "Thang máy gia đình (nên có khi ≥3 tầng / có NCT)."),
            ("balcony", "Ban công lớn", "Ban công thư giãn tầng trên."),
        ],
        section="3. Công năng đặc thù", qid="special_spaces",
    ),

    _single("Phong cách thiết kế", _STYLE_OPTIONS, "indochine",
            section="4. Phong cách & ngân sách", qid="style",
            help="Quyết định vật liệu, màu sắc và ảnh phối cảnh render."),
    _single(
        "Mức hoàn thiện",
        [
            ("co_ban", "Cơ bản", "Vật tư phổ thông, tối ưu chi phí."),
            ("cao_cap", "Cao cấp", "Vật tư tốt, hoàn thiện chỉn chu."),
            ("luxury", "Sang trọng (Luxury)", "Vật liệu cao cấp, chi tiết tinh xảo, không gian rộng rãi hơn."),
        ],
        "cao_cap", section="4. Phong cách & ngân sách", qid="material_level",
    ),
    _single(
        "Ngân sách dự kiến",
        [
            ("under2", "Dưới 2 tỷ", ""),
            ("b2_4", "2 – 4 tỷ", ""),
            ("b4_7", "4 – 7 tỷ", ""),
            ("over7", "Trên 7 tỷ", ""),
        ],
        "b2_4", section="4. Phong cách & ngân sách", qid="budget_band",
        help="Để BOQ kiểm tra tính khả thi của dự toán.",
    ),
    _number("Năm sinh gia chủ (phong thuỷ)", qid="fengshui_year",
            section="4. Phong cách & ngân sách", default=0, mn=0, mx=2025, unit="",
            help="Tuỳ chọn — để 0 nếu bỏ qua. Dùng tính cung mệnh Bát Trạch + hướng hợp tuổi."),
    _single(
        "Giới tính gia chủ",
        [("nam", "Nam (gia chủ)", "Tính cung mệnh theo gia chủ nam — phổ biến nhất."),
         ("nu", "Nữ", "Tính cung mệnh theo gia chủ nữ.")],
        "nam", section="4. Phong cách & ngân sách", qid="owner_gender",
        help="Bát Trạch tính cung mệnh theo năm sinh + giới tính gia chủ.",
    ),
    _text("Mong muốn cá nhân hoá thêm", qid="personalization",
          section="5. Cá nhân hoá",
          placeholder="VD: thích không gian mở nhiều cây xanh, có góc trà đạo, "
                      "phòng master view vườn, tông màu trầm ấm…",
          help="Tự do mô tả ý thích riêng — agent sẽ cố gắng đưa vào thiết kế."),
]


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────
def _a(val: float, scale: float) -> float:
    return round(val * scale, 1)


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _opt_label(qid: str, value: str) -> str:
    """Human label of a chosen option (for the summary)."""
    for q in BRIEF_FORM:
        if q["id"] == qid and q["type"] in ("single", "multi"):
            for o in q.get("options", []):
                if o["value"] == value:
                    return o["label"]
    return str(value)


_ORIENT_VI = {
    "nam": "Nam", "dong_nam": "Đông Nam", "dong": "Đông",
    "tay": "Tây", "bac": "Bắc", "tay_nam": "Tây Nam",
}


# ─────────────────────────────────────────────────────────────────────
#  build_design_program — selections → deterministic program
# ─────────────────────────────────────────────────────────────────────
def build_design_program(answers: dict[str, Any]) -> dict[str, Any]:
    """Biến lựa chọn form → program XÁC ĐỊNH cho 6 agents.

    Returns:
        {
          "rooms_required": [{"name","area_m2","priority"}...],
          "layout_principles": [...],
          "constraints": [...],
          "brief_text": "<đoạn brief tự sinh, feed cho KTS Chief>",
          "num_floors": int, "num_residents": int,
          "style_choice": str, "location_province": str,
          "summary": [{"label","value"}...],   # để hiển thị lại cho khách
        }
    """
    a = answers or {}

    btype = a.get("building_type", "villa")
    nf = max(1, min(5, _as_int(a.get("num_floors"), 2)))
    if btype == "level4":
        nf = 1
    n_bed = max(1, min(8, _as_int(a.get("num_bedrooms"), 3)))
    n_res = max(1, min(12, _as_int(a.get("num_residents"), 4)))
    gens = _as_int(a.get("generations"), 2)
    style = a.get("style", "indochine")
    mat = a.get("material_level", "cao_cap")
    scale = _MATERIAL_SCALE.get(mat, 1.0)
    orient = a.get("lot_orientation", "nam")
    lot_w = _as_int(a.get("lot_width_m"), 8)
    lot_l = _as_int(a.get("lot_length_m"), 15)
    altar = a.get("altar", "room")
    garage = _as_int(a.get("garage"), 1)
    kitchen = a.get("kitchen", "open")
    dining_sep = a.get("dining_separate", "no") == "yes"
    living_large = a.get("living_scale", "standard") == "large"
    work_room = a.get("work_room", "no") == "yes"
    helper = a.get("helper_room", "no") == "yes"
    garden = a.get("garden", "front")
    skylight = a.get("skylight", "yes") == "yes"
    master = a.get("master_suite", "yes") == "yes"
    elder_ground = a.get("elder_room_ground", "no") == "yes"
    specials = a.get("special_spaces", []) or []
    if isinstance(specials, str):
        specials = [specials]
    fy = _as_int(a.get("fengshui_year"), 0)
    owner_gender = a.get("owner_gender", "nam")
    personalization = (a.get("personalization") or "").strip()

    rooms: list[dict[str, Any]] = []

    def add(name: str, area: float, priority: str = "normal") -> None:
        rooms.append({"name": name, "area_m2": round(area, 1), "priority": priority})

    # ── Common / public (ground) ──────────────────────────────
    add("Phòng khách", _a(A_LIVING_LARGE if living_large else A_LIVING, scale), "high")

    if dining_sep:
        add("Bếp" + (" có đảo" if kitchen == "island" else ""),
            _a(A_KITCHEN_ISLAND if kitchen == "island" else A_KITCHEN, scale), "high")
        add("Phòng ăn", _a(A_DINING, scale), "normal")
    else:
        add("Bếp + phòng ăn", _a(A_KITCHEN_DINING, scale), "high")

    if garage >= 1:
        g_area = A_GARAGE_1 + max(0, garage - 1) * A_GARAGE_PER_EXTRA
        add(f"Gara ô tô ({garage} xe)" if garage > 1 else "Gara ô tô", round(g_area, 1), "normal")

    if altar == "room":
        add("Gian thờ", _a(A_ALTAR, scale), "high")

    if work_room:
        add("Phòng làm việc (sinh hoạt)", _a(A_STUDY, scale), "normal")

    if elder_ground or gens >= 3:
        add("Phòng ngủ ông bà (tầng trệt)", _a(A_BED_ELDER, scale), "high")
        elder_counts_as_bed = True
    else:
        elder_counts_as_bed = False

    add("WC khách", round(A_WC_GUEST, 1), "normal")

    if helper:
        add("Phòng giúp việc (phụ)", round(A_HELPER, 1), "low")

    # ── Special spaces ────────────────────────────────────────
    if "gym" in specials:
        add("Phòng gym (giải trí)", _a(A_GYM, scale), "low")
    if "cinema" in specials:
        add("Phòng chiếu phim (giải trí)", _a(A_CINEMA, scale), "low")
    if "library" in specials:
        add("Phòng đọc (sinh hoạt)", _a(A_LIBRARY, scale), "low")
    if "store" in specials:
        add("Kho", round(A_STORE, 1), "low")
    if "laundry" in specials:
        add("Phòng giặt (phụ)", round(A_LAUNDRY, 1), "low")
    if "elevator" in specials or (nf >= 3 and elder_counts_as_bed):
        add("Thang máy (kỹ thuật)", round(A_ELEVATOR, 1), "normal")
    if "pool" in specials:
        add("Hồ bơi (sân vườn)", _a(A_POOL, scale), "low")
    if "balcony" in specials and nf >= 2:
        add("Ban công lớn (sân)", round(A_BALCONY, 1), "low")

    # ── Bedrooms (upper) ──────────────────────────────────────
    n_remaining = n_bed - (1 if elder_counts_as_bed else 0)
    n_remaining = max(0, n_remaining)
    upstairs_wc = 0
    if master and n_remaining >= 1:
        add("Phòng ngủ master", _a(A_BED_MASTER, scale), "high")
        add("WC master", round(A_WC_ENSUITE, 1), "normal")
        upstairs_wc += 1
        n_remaining -= 1
    for i in range(n_remaining):
        add(f"Phòng ngủ {i + 2}", _a(A_BED_STD, scale), "normal")
    # Shared WC: ~1 per 2 standard bedrooms upstairs
    n_shared_wc = math.ceil(max(0, n_remaining) / 2) if n_remaining else (0 if master else 1)
    for _ in range(n_shared_wc):
        add("WC chung", round(A_WC_SHARED, 1), "normal")

    # ── Layout principles (deterministic, văn hoá + phong thuỷ nhẹ) ──
    principles: list[str] = []
    o_vi = _ORIENT_VI.get(orient, "Nam")
    principles.append(f"Cửa chính & phòng khách hướng {o_vi}, đón sáng – đón khách")
    if orient == "tay":
        principles.append("Hướng Tây: bố trí lam/ban công chắn nắng gắt buổi chiều")
    if altar in ("room", "combined"):
        principles.append("Gian thờ tôn nghiêm: đặt hậu – trung tâm, KHÔNG cạnh WC, KHÔNG ở góc")
    if skylight:
        principles.append("Giếng trời giữa nhà để thông gió & lấy sáng tự nhiên")
    if garden in ("front", "both"):
        principles.append("Sân vườn trước tạo khoảng lùi, tiểu cảnh đón khách")
    if garden in ("back", "both"):
        principles.append("Sân sau cho bếp – giặt phơi thoáng khí")
    if elder_counts_as_bed:
        principles.append("Phòng ông bà ở tầng trệt, gần WC, tránh cầu thang")
    principles.append("Cầu thang & WC gom về lõi giao thông để tối ưu kỹ thuật")
    if fy and fy > 1900:
        principles.append(f"Cân nhắc phong thuỷ theo tuổi gia chủ (năm sinh {fy}): hướng bếp – ban thờ hợp mệnh")

    # ── Constraints ───────────────────────────────────────────
    constraints: list[str] = [
        f"Khu đất {lot_w}×{lot_l}m, loại hình: {_opt_label('building_type', btype)}",
        f"Quy mô: {nf} tầng, {n_bed} phòng ngủ, {n_res} người, {gens} thế hệ",
        f"Mức hoàn thiện: {_opt_label('material_level', mat)}; ngân sách: {_opt_label('budget_band', a.get('budget_band','b2_4'))}",
    ]

    # ── Auto brief text (feed Chief cho phần tường thuật/phong thuỷ/palette) ──
    sp_txt = ", ".join(_opt_label("special_spaces", s) for s in specials) or "không"
    brief_text = (
        f"{_opt_label('building_type', btype)} {nf} tầng, phong cách "
        f"{_opt_label('style', style)}, trên khu đất {lot_w}×{lot_l}m hướng {o_vi}. "
        f"Gia đình {n_res} người / {gens} thế hệ, {n_bed} phòng ngủ"
        f"{' (có master khép kín)' if master else ''}. "
        f"Bếp: {_opt_label('kitchen', kitchen)}; "
        f"{'có phòng ăn riêng' if dining_sep else 'bếp – ăn liên thông'}; "
        f"phòng khách {'lớn/thông tầng' if living_large else 'tiêu chuẩn'}. "
        f"Gian thờ: {_opt_label('altar', altar)}. Gara: {garage} ô tô. "
        f"Sân vườn: {_opt_label('garden', garden)}; "
        f"{'có giếng trời' if skylight else 'không giếng trời'}. "
        f"Không gian đặc biệt: {sp_txt}. "
        f"Mức hoàn thiện {_opt_label('material_level', mat)}, ngân sách "
        f"{_opt_label('budget_band', a.get('budget_band','b2_4'))}."
    )
    if fy and fy > 1900:
        brief_text += f" Gia chủ sinh năm {fy} — cân nhắc phong thuỷ hướng & cung mệnh."
    if personalization:
        brief_text += f"\n\nMong muốn cá nhân hoá của gia chủ: {personalization}"

    # ── Summary để hiển thị lại cho khách (xác nhận đúng lựa chọn) ──
    summary = [
        {"label": "Loại công trình", "value": _opt_label("building_type", btype)},
        {"label": "Khu đất / hướng", "value": f"{lot_w}×{lot_l}m · hướng {o_vi}"},
        {"label": "Quy mô", "value": f"{nf} tầng · {n_bed} phòng ngủ · {n_res} người"},
        {"label": "Gian thờ", "value": _opt_label("altar", altar)},
        {"label": "Gara", "value": f"{garage} ô tô" if garage else "Không"},
        {"label": "Phong cách", "value": _opt_label("style", style)},
        {"label": "Hoàn thiện / ngân sách",
         "value": f"{_opt_label('material_level', mat)} · {_opt_label('budget_band', a.get('budget_band','b2_4'))}"},
        {"label": "Không gian đặc biệt", "value": sp_txt},
    ]

    return {
        "rooms_required": rooms,
        "layout_principles": principles,
        "constraints": constraints,
        "brief_text": brief_text,
        "num_floors": nf,
        "num_residents": n_res,
        "style_choice": style,
        "location_province": a.get("location_province", "Hà Nội"),
        "summary": summary,
        # Đầu vào cho L5 Phong thủy (Bát Trạch + Lỗ Ban) — deterministic, dùng ở orchestrator.
        "fengshui_input": {
            "birth_year": fy,
            "gender": owner_gender,
            "lot_orientation": orient,
            "lot_width_m": lot_w,
            "lot_length_m": lot_l,
        },
    }
