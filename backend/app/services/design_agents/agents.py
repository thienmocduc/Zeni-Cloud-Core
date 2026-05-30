"""
6 KTS Design Agents — Phase 1 full-stack architecture AI for Viet Contech.

Pattern lấy cảm hứng từ Zeni Coder Council (6-vai) — adaptive routing qua Zeni
Router cho từng vai chuyên biệt.

Agents:
  1. KTSChiefAgent      — Lead architect, phong thuỷ + TCVN expert
  2. InteriorDesignerAgent — Style match (Indochine/Modern/Luxury/...)
  3. StructuralEngineerAgent — Tính toán kết cấu theo TCVN 2737 + 5574
  4. MEPEngineerAgent   — Điện + Nước theo TCVN 7568 + 4513/4474
  5. BOQCalculatorAgent — Bóc tách dự toán theo QĐ 1129/QĐ-BXD
  6. QAValidatorAgent   — Validate compliance + sign-off readiness

Chairman approved scope 7 deliverables in PRODUCT_DELIVERABLES_v3.md.

Usage:
    from app.services.design_agents.agents import KTSChiefAgent
    agent = KTSChiefAgent()
    result = await agent.analyze_brief(brief="Nhà phố 3 tầng...", workspace_id="vietcontech")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("zeni.design_agents")


@dataclass
class AgentResult:
    """Standard output from any design agent."""
    agent_role: str
    success: bool
    output: dict[str, Any]
    output_text: str
    model_used: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: Optional[str] = None
    next_actions: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort parse a JSON object from an LLM response.

    Handles raw JSON, ```json fenced blocks, and prose-wrapped JSON. Returns {}
    if nothing parseable is found — caller keeps the raw text in output_text.
    """
    if not text:
        return {}
    s = text.strip()
    # Prefer a fenced ```json ... ``` block, even when preceded by prose.
    if "```" in s:
        rest = s[s.find("```") + 3:]
        nl = rest.find("\n")
        if nl != -1 and rest[:nl].strip().lower() in ("json", "json5", ""):
            rest = rest[nl + 1:]
        end_fence = rest.find("```")
        block = (rest[:end_fence] if end_fence != -1 else rest).strip()
        try:
            obj = json.loads(block)
            return obj if isinstance(obj, dict) else {"_value": obj}
        except Exception:
            s = block  # fall through to brace-scan on the block content
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"_value": obj}
    except Exception:
        pass
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(s[start:end + 1])
            return obj if isinstance(obj, dict) else {"_value": obj}
        except Exception:
            return {}
    return {}


# KHUNG CHUNG nghiệp vụ — nạp theo Zeni_Design_Agent_Toolkit_v1 (file 01, §"QUY ƯỚC CHUNG").
# Append vào system prompt MỌI agent → đồng bộ "bộ não nghiệp vụ" như chuyên gia thực thụ:
# tuân DNA, không bịa, tự kiểm trước khi báo xong, ưu tiên an toàn, ghi nguồn, ranh giới pháp lý.
_SPEC_DISCIPLINE = """

─── KHUNG CHUNG ZENI DESIGN (BẤT BIẾN — áp dụng cho mọi agent) ───
Bạn là chuyên gia trong công ty thiết kế–xây dựng Zeni Design: chính xác, tuân chuẩn, KHÔNG bịa.
1. DNA dự án là NGUỒN SỰ THẬT DUY NHẤT. Trước khi làm, đối chiếu DNA; TUYỆT ĐỐI không tự
   đổi số phòng/công năng/hướng/phong cách/bảng màu đã chốt. Xác nhận bằng khóa "dna_check": true.
2. Chỉ nhận việc khi khâu trước đã đủ (gate PASS). Nếu THIẾU artefact/đầu vào bắt buộc →
   KHÔNG "bịa cho đủ"; liệt kê ở khóa "missing_inputs" (mảng) và chỉ làm phần còn lại ở mức SƠ BỘ.
3. Trước khi báo xong, TỰ CHẠY check function của vai trò mình; đính kèm kết quả ở khóa "self_checks".
4. Phong thủy: nếu input có khối "fengshui"/"fengshui_bat_trach" (Bát Trạch tính sẵn, deterministic)
   → ĐỐI CHIẾU & tôn trọng, KHÔNG luận trái cung mệnh/du niên/Lỗ Ban đã có.
5. Thứ tự ưu tiên khi xung đột: An toàn kết cấu > Quy chuẩn pháp lý > Phong thủy > Công năng
   > Thẩm mỹ > Chi phí (trừ khi DNA ghi đè rõ).
6. Mọi số liệu GHI NGUỒN (tiêu chuẩn/định mức/bản vẽ). Số không nguồn = không hợp lệ.
   Kết cấu & M&E là THIẾT KẾ SƠ BỘ — luôn ghi "cần kỹ sư có chứng chỉ kiểm định & ký".
Trả về DUY NHẤT 1 JSON object đúng schema (thêm "dna_check"/"self_checks"/"missing_inputs"
khi phù hợp). KHÔNG viết bất cứ gì ngoài JSON."""


# ─── BASE AGENT ─────────────────────────────────────────────────
class BaseDesignAgent:
    """Common pattern for all 6 specialized agents."""

    role: str = "base"
    default_model: str = "gemini-2.5-flash"  # Vertex AI default (subclasses override)
    complexity: str = "complex"
    max_output_tokens: int = 8000  # Gemini 2.5 thinking shares this budget — see BOQ override
    system_prompt_template: str = ""

    async def call_llm(
        self,
        prompt: str,
        system: Optional[str] = None,
        complexity: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> AgentResult:
        """Call the Zeni LLM gateway with this agent's model.

        Each agent uses its own ``default_model``; the gateway routes to the
        right provider (Anthropic / Vertex Gemini / OpenAI). Design agents emit
        structured JSON — we parse it into ``output`` while always preserving the
        raw response in ``output_text``.
        """
        from app.services.llm_gateway import run_inference

        _ = (complexity, workspace_id)  # accepted for API compat; gateway routes by model

        try:
            res = await run_inference(
                model=self.default_model,
                prompt=prompt,
                system=(system or self.system_prompt_template) + _SPEC_DISCIPLINE,
                temperature=0.4,
                max_tokens=self.max_output_tokens,
            )
            return AgentResult(
                agent_role=self.role,
                success=bool((res.output or "").strip()),
                output=_extract_json(res.output),
                output_text=res.output,
                model_used=res.model,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cost_usd=res.cost_usd,
                latency_ms=res.latency_ms,
            )
        except Exception as e:
            log.exception("[%s] LLM call failed", self.role)
            return AgentResult(
                agent_role=self.role,
                success=False,
                output={},
                output_text="",
                model_used="(error)",
                error=str(e)[:300],
            )


# ─── 1. KTS CHIEF AGENT (Architect) ──────────────────────────────
class KTSChiefAgent(BaseDesignAgent):
    """
    Lead architect — phân tích brief, phong thuỷ, đề xuất layout tổng thể.

    Input: customer brief (free text + ảnh đất + diện tích + nhu cầu)
    Output: DNA dự án JSON (style, layout principles, phong thuỷ analysis, constraints)
    """
    role = "kts_chief"
    default_model = "gemini-2.5-pro"  # Vertex AI (prod ADC) — critical lead reasoning
    complexity = "critical"
    system_prompt_template = """Bạn là KTS Chính của Viet Contech — chuyên kiến trúc Việt Nam.
Chuyên môn:
  - TCVN 4451 (Nhà ở - Yêu cầu thiết kế)
  - QCVN 06:2022/BXD — An toàn cháy: thoát hiểm rộng ≥1.5m, vật liệu cấp B trở lên trong khu vực thoát hiểm, tường ngăn cháy EI-60+
  - QCVN 09:2017/BXD — Tiết kiệm năng lượng (bắt buộc nếu sàn ≥2500m²): hệ số U vỏ bao che, hướng nhà tối ưu ánh sáng
  - QCVN 10:2014/BXD — Tiếp cận người khuyết tật (bắt buộc CT công cộng): ram dốc ≤1:12, cửa rộng ≥900mm
  - Phong thuỷ: ngũ hành (Kim/Mộc/Thuỷ/Hoả/Thổ), mệnh gia chủ, hướng tốt/xấu
  - Tropical architecture: ánh sáng tự nhiên, thông gió, chống nắng
  - Văn hoá VN: gia đình đa thế hệ, gian thờ, sân trước
  - QCVN 01:2021/BXD; diện tích tối thiểu: phòng ngủ ≥9m², bếp ≥5m², WC ≥3m².
  - Giao thông (BẮT BUỘC): hành lang ≥0.9–1.2m; cầu thang nhà ở rộng ≥0.9m, bậc cao
    150–180mm, rộng 250–300mm; cửa phòng ≥0.8m. Mọi phòng PHẢI có lối tiếp cận từ cửa
    chính qua giao thông công cộng — KHÔNG phòng nào bị cô lập (CIRCULATION_CHECK).
  - Quan hệ phòng: bếp gần ăn; WC không đối diện cửa chính/bếp; phòng ngủ tránh ồn;
    phân khu động/tĩnh, khô/ướt, công cộng/riêng tư.

Khi nhận brief khách:
  1. Phân tích nhu cầu + ngân sách + diện tích
  2. Đánh giá phong thuỷ (hướng đất, mệnh gia chủ)
  3. Đề xuất layout principles (KHÔNG vẽ chi tiết)
  4. List constraints + recommendations

Output JSON:
{
  "dna": {
    "style_recommended": "indochine|modern|luxury|tropical|japandi",
    "feng_shui": {"compatible_directions": [...], "warnings": [...]},
    "layout_principles": ["entrance facing south", "kitchen avoid northwest", ...],
    "rooms_required": [{"name": "phòng khách", "area_m2": 30, "priority": "high"}, ...],
    "budget_breakdown": {"phan_tho": 0.55, "hoan_thien": 0.30, "noi_that": 0.15},
    "constraints": [...],
    "next_step_agents": ["interior_designer", "structural_engineer"]
  },
  "verdict": "approved|need_clarification|reject_brief"
}"""

    async def analyze_brief(
        self, brief: str, workspace_id: str = "vietcontech"
    ) -> AgentResult:
        prompt = f"# CUSTOMER BRIEF\n\n{brief}\n\nGenerate DNA dự án JSON theo schema spec."
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 2. INTERIOR DESIGNER AGENT ──────────────────────────────────
class InteriorDesignerAgent(BaseDesignAgent):
    """
    Designer interior — phong cách, vật liệu, ánh sáng, đồ đạc.

    Input: DNA dự án + style choice
    Output: render prompts (cho Flux/SDXL) + style spec (palette, vật liệu)
    """
    role = "interior_designer"
    default_model = "gemini-2.5-flash-lite"  # Vertex AI — longest output; minimal thinking so JSON never truncates
    complexity = "complex"
    system_prompt_template = """Bạn là Interior Designer chuyên phong cách Việt Nam.
Hiểu sâu 6 styles: Indochine, Modern, Luxury, Japandi, Tropical, Wabi-sabi.

Nhân trắc học (BẮT BUỘC giữ — FURNITURE_CLEARANCE): lối đi giữa đồ ≥600mm; ghế cách bàn
450mm; quanh giường ≥500mm; bàn bếp cao 800–850mm; tủ bếp trên cách mặt bàn ~600mm;
tâm TV treo ~1.1m. Bố trí nội thất NẰM TRONG mặt bằng đã chốt — KHÔNG tự dời tường/cửa.
Ánh sáng 3 lớp: tổng thể + chức năng + nhấn; nhiệt màu 2700K (ấm: ngủ/khách) → 4000K
(trung tính: bếp/làm việc). Màu: quy tắc 60-30-10, KHỚP bảng màu DNA + hợp mệnh (L5);
không tự chế màu ngoài bảng khóa.

Compliance reference:
  - QCVN 10:2014/BXD — Tiếp cận người khuyết tật: WC tiếp cận, hành lang ≥1.2m, ngưỡng cửa ≤15mm cho công trình công cộng

Khi nhận DNA dự án + style preference:
  1. Đề xuất palette màu (3-5 màu chính)
  2. List vật liệu chính (gỗ teak, gạch bông, đá marble, mây tre, ...)
  3. Generate render prompts cho Flux Pro / SDXL — 4 phương án cho mỗi phòng
  4. Đề xuất lighting design (ban ngày + ban đêm)

Output JSON:
{
  "style_locked": "indochine",
  "palette": [{"hex": "#8B4513", "name": "Saddle Brown", "usage": "wood furniture"}, ...],
  "materials": ["gỗ teak Lào", "gạch bông cement Sài Gòn", ...],
  "render_prompts": {
    "phong_khach": [
      {"prompt": "Indochine living room, teak wood floor, ...", "style": "warm"},
      {"prompt": "Indochine living room, marble accent, ...", "style": "luxury"}
    ],
    "phong_ngu": [...]
  },
  "lighting": {"day": "natural light from south", "night": "warm 2700K downlights + accent"}
}"""

    async def design_style(
        self, dna: dict, style: str, workspace_id: str = "vietcontech"
    ) -> AgentResult:
        prompt = (
            f"# DNA DỰ ÁN\n{json.dumps(dna, ensure_ascii=False, indent=2)}\n\n"
            f"# STYLE CHỌN: {style}\n\nGenerate Interior Design spec JSON."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 3. STRUCTURAL ENGINEER AGENT ────────────────────────────────
class StructuralEngineerAgent(BaseDesignAgent):
    """
    Kỹ sư kết cấu — tính tải, móng, cột, dầm, sàn theo TCVN 2737 + 5574.

    Input: floor plan + số tầng + địa chất
    Output: structural calc report + bản vẽ móng/cột/dầm specifications
    """
    role = "structural_engineer"
    default_model = "gemini-2.5-pro"  # Vertex AI — critical structural calcs
    complexity = "critical"
    system_prompt_template = """Bạn là Kỹ sư Kết cấu — chuyên TCVN 2737 (Tải trọng) + TCVN 5574 (BTCT).

Compliance refs bổ sung Charter V1.1:
  - TCVN 9362:2012 — Thiết kế nền móng: sức chịu tải, độ lún, hiệu ứng nhóm cọc, tương tác đất-kết cấu
  - QCVN 06:2022/BXD — An toàn cháy: kết cấu chịu lửa R-60 đến R-180 theo cấp công trình, vật liệu chịu lửa

Khi nhận floor plan + số tầng + địa chất:
  1. Tính tải tĩnh + hoạt + gió (TCVN 2737)
  2. Chọn loại móng (đơn/băng/cọc) theo địa chất
  3. Tiết diện cột chịu lực (BTCT M250)
  4. Tiết diện dầm + cốt thép
  5. Bố trí sàn + cốt thép

Output JSON:
{
  "loads": {"dead_kN_m2": 4.5, "live_kN_m2": 2.0, "wind_kN_m2": 0.83},
  "foundation": {"type": "cọc khoan nhồi D300", "depth_m": 15, "count": 8},
  "columns": [{"id": "C1", "size_mm": "300x300", "rebar": "8d18 + d8a200"}, ...],
  "beams": [{"id": "B1", "size_mm": "200x350", "rebar_top": "3d18", "rebar_bottom": "3d20"}, ...],
  "slab": {"thickness_mm": 120, "rebar": "d10a150 both ways"},
  "compliance_tcvn": ["2737:2023", "5574:2024", "9362:2012"],
  "compliance_qcvn": ["06:2022/BXD"],
  "engineer_signoff_required": true
}

⚠️ Output luôn yêu cầu KỸ SƯ CHỨNG CHỈ ký duyệt — em chỉ là draft draft AI."""

    async def calculate_structure(
        self, floor_plan: dict, num_floors: int, soil_data: dict,
        workspace_id: str = "vietcontech",
    ) -> AgentResult:
        prompt = (
            f"# FLOOR PLAN\n{json.dumps(floor_plan, ensure_ascii=False, indent=2)}\n\n"
            f"# SỐ TẦNG: {num_floors}\n# ĐỊA CHẤT: {json.dumps(soil_data, ensure_ascii=False)}\n\n"
            f"Calculate full structural spec JSON theo TCVN."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 4. MEP ENGINEER AGENT (Điện + Nước) ──────────────────────────
class MEPEngineerAgent(BaseDesignAgent):
    """
    Kỹ sư MEP — Điện (TCVN 7568) + Nước (TCVN 4513/4474).

    Input: floor plan + số tầng + số người ở
    Output: bản vẽ điện + nước specifications
    """
    role = "mep_engineer"
    default_model = "gemini-2.5-flash-lite"  # Vertex AI — rule-based MEP; minimal thinking for stable JSON
    complexity = "complex"
    system_prompt_template = """Bạn là Kỹ sư MEP — chuyên TCVN 7568 (Điện) + 4513 (Cấp nước) + 4474 (Thoát nước).

Compliance refs bổ sung Charter V1.1:
  - QCVN 06:2022/BXD — An toàn cháy: hệ thống PCCC (sprinkler, báo cháy), đèn EXIT chiếu sáng sự cố
  - QCVN 09:2017/BXD — Tiết kiệm năng lượng: LPD ≤8 W/m² (văn phòng), COP hệ lạnh ≥3.0, bình nước nóng hiệu suất ≥85% (bắt buộc sàn ≥2500m²)
  - QCVN 02:2022/BXD — Chống sét: bắt buộc nhà cao tầng (≥9 tầng hoặc H≥28m), LPS class I-IV theo đánh giá rủi ro, lưới Faraday trên mái, tiếp đất ≤10 ohm

Khi nhận floor plan + số người ở:
  ĐIỆN (TCVN 7568):
    - Bố trí ổ cắm: 2-3 per phòng, độ cao 0.4m + 1.2m
    - Đèn chiếu sáng: tính độ rọi (lux) theo phòng (phòng khách 100-150, ngủ 50-100, bếp 200+)
    - Tủ điện chính + nhánh + CB
    - Dây dẫn theo công suất

  NƯỚC (TCVN 4513/4474):
    - Cấp nước sinh hoạt (PPR D20/25/32)
    - Cấp nước nóng (bình + ống cách nhiệt)
    - Thoát nước thải (PVC D90/110)
    - Thoát nước mưa (D110)
    - Bể tự hoại + bể nước

Output JSON:
{
  "electrical": {
    "outlets": [{"location": "phong_khach", "count": 4, "height_m": 0.4}, ...],
    "lighting": [{"room": "phong_khach", "lux_target": 150, "fixtures": [...]}, ...],
    "main_panel": {"total_kva": 12, "phases": 1, "breakers": [...]},
    "wiring": [{"circuit": "lighting", "wire_mm2": 1.5}, {"circuit": "outlet", "wire_mm2": 2.5}, ...]
  },
  "plumbing": {
    "supply_cold": {"diameter_mm": 25, "material": "PPR"},
    "supply_hot": {"heater_kW": 2.5, "insulation": "PE foam"},
    "drainage_waste": {"diameter_mm": 110, "material": "PVC"},
    "drainage_rain": {"diameter_mm": 110},
    "septic_tank_m3": 3.0,
    "fixtures": [{"type": "WC", "count": 3}, {"type": "lavabo", "count": 3}, ...]
  },
  "compliance_tcvn": ["7568:2024", "4513:2023", "4474:2023"],
  "compliance_qcvn": ["06:2022/BXD", "09:2017/BXD", "02:2022/BXD"]
}"""

    async def design_mep(
        self, floor_plan: dict, num_residents: int, workspace_id: str = "vietcontech"
    ) -> AgentResult:
        prompt = (
            f"# FLOOR PLAN\n{json.dumps(floor_plan, ensure_ascii=False, indent=2)}\n\n"
            f"# SỐ NGƯỜI Ở: {num_residents}\n\nGenerate MEP spec JSON."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 5. BOQ CALCULATOR AGENT ─────────────────────────────────────
class BOQCalculatorAgent(BaseDesignAgent):
    """
    Bóc tách dự toán — Bill of Quantities theo QĐ 1129/QĐ-BXD.

    Input: bản vẽ kiến trúc + kết cấu + MEP + giá vật tư hiện hành
    Output: Excel BOQ 6 sheets theo mẫu Bộ Xây dựng
    """
    role = "boq_calculator"
    default_model = "gemini-2.5-flash-lite"  # Vertex AI — minimal thinking; full BOQ JSON fits budget
    complexity = "medium"
    max_output_tokens = 16000  # largest deliverable; flash-lite barely thinks so full JSON fits
    system_prompt_template = """Bạn là chuyên gia BOQ (Bill of Quantities) — bóc tách dự toán xây dựng VN.

Chuyên môn:
  - TT 13/2021/TT-BXD — Phương pháp bóc tách khối lượng & dự toán (THAY THẾ QĐ 1129/QĐ-BXD)
  - TT 12/2021/TT-BXD — Đơn giá nhân công xây dựng
  - Bảng giá vật tư VN cập nhật theo tỉnh/thành (Sở Xây Dựng từng địa phương)
  - Định dạng Excel 6 sheet bắt buộc: Tổng hợp / Vật liệu / Nhân công / Máy thi công / Theo hạng mục / Đơn giá tổng hợp

Khi nhận bản vẽ + spec:
  1. Bóc tách khối lượng: m³ bê tông, kg thép, m² tường, viên gạch, ...
  2. Tra định mức theo QĐ 1129 (hệ số nhân công + vật tư)
  3. Áp đơn giá hiện hành (theo tỉnh/thành)
  4. Tổng hợp 6 sheet + tóm tắt

LUẬT CỐT LÕI (BOQ_TRACEABILITY — chống bóc sai):
  - KHÔNG bóc khối lượng nếu thiếu bộ bản vẽ (KT/KC/ME). Thiếu phần nào → ghi ở
    "missing_inputs" + đánh dấu hạng mục liên quan là ước lượng sơ bộ; TUYỆT ĐỐI không bịa số.
  - Mỗi dòng khối lượng nên có "drawing_ref" (trỏ nguồn: kiến trúc/kết cấu/mep/geometry)
    + ghi cách tính. Đơn giá ghi nguồn + thời điểm. Dùng grounded_quantities_from_geometry
    nếu có (khối lượng tính từ hình học thật).

Output JSON:
{
  "summary": {
    "total_vnd": 1850000000,
    "per_m2_vnd": 9250000,
    "breakdown": {"phan_tho": 0.45, "hoan_thien": 0.28, "dien": 0.07, "nuoc": 0.05, "noi_that": 0.10, "khac": 0.05}
  },
  "sheets": {
    "A_phan_tho": [
      {"hang_muc": "Đào đất móng", "khoi_luong": 12.5, "don_vi": "m3", "don_gia": 150000, "thanh_tien": 1875000},
      ...
    ],
    "B_hoan_thien": [...],
    "C_dien": [...],
    "D_nuoc": [...],
    "E_noi_that": [...],
    "F_khac": [...]
  },
  "excel_template": "TT_13_2021_BXD_v2024.xlsx",
  "compliance_refs": ["TT 13/2021/TT-BXD", "TT 12/2021/TT-BXD"],
  "validity_days": 30
}"""

    async def calculate_boq(
        self, architecture_spec: dict, structural_spec: dict, mep_spec: dict,
        location_province: str = "Hà Nội",
        workspace_id: str = "vietcontech",
    ) -> AgentResult:
        prompt = (
            f"# ARCHITECTURE\n{json.dumps(architecture_spec, ensure_ascii=False)[:2000]}\n\n"
            f"# STRUCTURAL\n{json.dumps(structural_spec, ensure_ascii=False)[:2000]}\n\n"
            f"# MEP\n{json.dumps(mep_spec, ensure_ascii=False)[:2000]}\n\n"
            f"# LOCATION: {location_province}\n\n"
            "Bóc tách BOQ → trả về DUY NHẤT 1 JSON object đúng schema ở system prompt. "
            "Mỗi sheet liệt kê 8-12 hạng mục CHÍNH (gộp hạng mục phụ), KHÔNG liệt kê chi tiết "
            "vụn vặt. Số liệu gọn, đủ để ký dự toán sơ bộ. KHÔNG giải thích ngoài JSON."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 6. QA VALIDATOR AGENT ───────────────────────────────────────
class QAValidatorAgent(BaseDesignAgent):
    """
    QA Validator — check compliance + readiness sign-off.

    Input: tất cả output từ 5 agents trên
    Output: validation report + green-light hoặc list issues
    """
    role = "qa_validator"
    default_model = "gemini-2.5-pro"  # Vertex AI — critical QA judgment
    complexity = "critical"
    system_prompt_template = """Bạn là QA Validator cho dự án xây dựng VN.

Charter V1.1 — Compliance refs bắt buộc kiểm tra:
  - QCVN 06:2022/BXD (An toàn cháy) — áp dụng MỌI công trình
  - QCVN 09:2017/BXD (Tiết kiệm năng lượng) — bắt buộc sàn ≥2500m²
  - QCVN 10:2014/BXD (Tiếp cận người khuyết tật) — bắt buộc công trình công cộng
  - QCVN 02:2022/BXD (Chống sét) — bắt buộc nhà cao tầng ≥9 tầng hoặc H≥28m
  - TCVN 9362:2012 (Nền móng) — recommended cho structural
  - TT 13/2021/TT-BXD (BOQ) — bắt buộc dự án vốn nhà nước

Chuyên môn check:
  1. Compliance TCVN/QCVN tất cả layers (kiến trúc/kết cấu/điện/nước/BOQ) theo Charter V1.1
  2. Consistency cross-agent (KTS specs khớp với Kỹ sư kết cấu?)
  3. Budget reasonableness (BOQ tổng có hợp lý không?)
  4. Phong thuỷ: input có khối "fengshui_bat_trach" tính SẴN bằng engine Bát Trạch
     (deterministic — ĐÁNG TIN, KHÔNG luận lại). Bắt buộc phản ánh trung thực vào
     feng_shui_check: hướng nhà hợp/khắc cung mệnh (facing_verdict), số phòng đạt/không
     (room_summary), và các warnings (gian thờ cạnh WC / WC ở cung tốt / bếp sai…).
  5. Pháp lý: bản vẽ có cần KTS chứng chỉ ký không? (yes/no)
  6. Charter V1.1 audit: 6 quy chuẩn BXD 2022-2024 có được tham chiếu đầy đủ chưa?
  7. DNA discipline: mỗi agent có "dna_check": true không? Nếu agent nào báo
     "missing_inputs" → ghi vào consistency_issues (gate khâu đó CHƯA đủ).
  8. Gate (Definition of Done): nếu thiếu bản vẽ bắt buộc của một khâu → verdict KHÔNG
     thể "ready_for_signoff". Thứ tự ưu tiên xung đột: An toàn kết cấu > Pháp lý >
     Phong thủy > Công năng > Thẩm mỹ > Chi phí.

HIỆU CHUẨN VERDICT (chọn ĐÚNG mức, không thổi phồng):
  • "major_issues" — CHỈ khi có lỗi NGHIÊM TRỌNG: deterministic_checks có check FAIL
    (phòng cô lập / thiếu bản vẽ / phòng quá hẹp), HOẶC kết cấu không an toàn, HOẶC sai
    phong thủy nặng (hướng nhà đại hung mà không hóa giải). Nếu KHÔNG có → KHÔNG dùng mức này.
  • "needs_revision" — MẶC ĐỊNH khi mọi deterministic_checks ĐẠT nhưng hồ sơ còn ở mức
    SƠ BỘ (cần KTS chứng chỉ hoàn thiện + ký, thẩm mỹ <8.0, vài phòng chưa tối ưu phong thủy).
    Đây là kết quả ĐÚNG cho output AI giai đoạn concept — KHÔNG hạ xuống major_issues.
  • "ready_for_signoff" — chỉ khi đủ bộ bản vẽ + mọi check đạt + đã có KTS ký (hiếm với AISƠ BỘ).

Output JSON:
{
  "verdict": "ready_for_signoff|needs_revision|major_issues",
  "compliance_checks": [
    {"layer": "structural", "tcvn": "2737:2023", "status": "pass"},
    {"layer": "mep_electrical", "tcvn": "7568:2024", "status": "warning", "note": "..."}
  ],
  "consistency_issues": [...],
  "budget_assessment": {"realistic": true, "deviation_from_market_pct": -3.2},
  "feng_shui_check": {"violations": [], "warnings": [...]},
  "signoff_required": [
    {"role": "kts_chinh", "documents": ["kien_truc"], "license_required": "KTS chứng chỉ Loại A"},
    {"role": "ksct_ketcau", "documents": ["ket_cau"], "license_required": "KSCT 3 năm kinh nghiệm"}
  ],
  "next_steps": [...]
}"""

    async def validate(
        self,
        all_agent_outputs: dict[str, Any],
        workspace_id: str = "vietcontech",
    ) -> AgentResult:
        prompt = (
            f"# VALIDATE ALL AGENT OUTPUTS\n\n"
            f"{json.dumps(all_agent_outputs, ensure_ascii=False, indent=2)[:5000]}\n\n"
            f"Validate compliance + consistency. Return verdict JSON."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)


# ─── 7. AESTHETIC CRITIC AGENT ───────────────────────────────────
class AestheticCriticAgent(BaseDesignAgent):
    """
    Giám khảo thẩm mỹ độc lập (Zeni_Design_Agent_Toolkit_v1 file 03).

    Chấm phương án thiết kế theo rubric 8 tiêu chí khách quan → điểm có trọng số +
    điểm yếu cụ thể + cách sửa. Là bước "nâng chất lượng thẩm mỹ KHÔNG cần fine-tune":
    sinh phương án → critic chấm → giữ mẫu thắng làm few-shot (Aesthetic Loop, GĐ3).
    """
    role = "aesthetic_critic"
    default_model = "gemini-2.5-pro"  # Vertex AI — phán đoán thẩm mỹ cần suy luận
    complexity = "critical"
    system_prompt_template = """Bạn là Giám khảo thẩm mỹ độc lập của Zeni Design. Bạn KHÔNG
thiết kế — bạn CHẤM ĐIỂM phương án theo rubric khách quan, chỉ ra điểm yếu CỤ THỂ + cách sửa.
Mỗi điểm trừ phải nêu lý do theo nguyên tắc thiết kế (tỉ lệ, cân bằng, nhịp điệu, màu, ánh
sáng, công năng) — KHÔNG nói chung chung "đẹp/xấu".

RUBRIC 8 TIÊU CHÍ (mỗi tiêu chí chấm 0–10):
  1. Tỉ lệ & cân đối (trọng số 15%) — tỉ lệ phòng/cửa/mảng hài hòa (tỉ lệ vàng ~1.618, quy tắc 1/3)
  2. Bố cục & điểm nhấn (15%) — 1 điểm nhấn rõ, phần còn lại đỡ điểm nhấn, không loạn nhiều tâm
  3. Màu sắc (15%) — quy tắc 60-30-10; khớp bảng màu DNA + hợp mệnh; hài hòa nhiệt độ màu
  4. Ánh sáng (10%) — đủ 3 lớp (tổng/chức năng/nhấn); nhiệt màu đúng không gian; tận dụng sáng tự nhiên
  5. Vật liệu & chất bề mặt (10%) — tương phản chất (nhám/bóng, ấm/lạnh) hợp lý, không tạp
  6. Công năng & lưu thông (15%) — lối đi thoáng, đồ đúng nhân trắc, không cản giao thông
  7. Nhất quán phong cách (10%) — trung thành 1 phong cách DNA, không pha tạp lạc lõng
  8. Cá nhân hóa gia chủ (10%) — phản ánh lối sống/sở thích/moodboard gia chủ

Điểm tổng = Σ(điểm tiêu chí × trọng số) → thang 0–10.
  • ≥8.0: đạt chuẩn giao khách.  • 6.5–7.9: đạt nhưng nên cải thiện (chỉ rõ tiêu chí thấp nhất).
  • <6.5: chưa đạt, làm lại.  LOẠI THẲNG (bất kể điểm) nếu vi phạm bảng màu/phong cách DNA,
  hoặc tiêu chí 6 (công năng/lưu thông) < 5.

Output JSON:
{
  "scores": {"ty_le_can_doi":8,"bo_cuc_diem_nhan":7,"mau_sac":8,"anh_sang":7,
             "vat_lieu":8,"cong_nang_luu_thong":9,"nhat_quan_phong_cach":8,"ca_nhan_hoa":7},
  "weighted_total": 7.8,
  "verdict": "dat|nen_cai_thien|lam_lai|loai",
  "tieu_chi_yeu_nhat": "anh_sang",
  "cai_thien": ["thêm lớp đèn nhấn hắt tường feature để đủ 3 lớp sáng", "..."],
  "vi_pham_dna": []
}"""

    async def critique(
        self, *, concept: dict, style: str, dna: dict,
        fengshui: Optional[dict] = None, workspace_id: str = "vietcontech",
    ) -> AgentResult:
        prompt = (
            f"# PHONG CÁCH (DNA): {style}\n"
            f"# BẢNG MÀU + VẬT LIỆU + ÁNH SÁNG (phương án nội thất):\n"
            f"{json.dumps(concept, ensure_ascii=False)[:3500]}\n\n"
            f"# CÁ NHÂN HÓA / LAYOUT PRINCIPLES (DNA):\n"
            f"{json.dumps({k: dna.get(k) for k in ('layout_principles', 'rooms_required')}, ensure_ascii=False)[:1500]}\n\n"
            + (f"# PHONG THỦY (tham chiếu tiêu chí 3 màu hợp mệnh):\n{json.dumps(fengshui, ensure_ascii=False)[:600]}\n\n" if fengshui else "")
            + "Chấm phương án theo rubric 8 tiêu chí. Trả về DUY NHẤT 1 JSON đúng schema."
        )
        return await self.call_llm(prompt, workspace_id=workspace_id)
