-- ============================================================================
-- Migration 080 — Design Standards 2022-2024 (Charter V1.1)
--
-- Seeds 6 Vietnamese Construction Codes (Bộ Xây Dựng) that the 6 KTS Design
-- Agents must reference in their system prompts:
--   1. QCVN 06:2022/BXD — An toàn cháy (PCCC)
--   2. QCVN 09:2017/BXD — Tiết kiệm năng lượng (công trình >2500m²)
--   3. QCVN 10:2014/BXD — Tiếp cận cho người khuyết tật
--   4. TCVN 9362:2012  — Thiết kế nền móng nhà công trình
--   5. TT 13/2021/TT-BXD — Bóc tách khối lượng (thay QĐ 1129)
--   6. QCVN 02:2022/BXD — Chống sét (nhà cao tầng)
--
-- All idempotent via CREATE IF NOT EXISTS + INSERT ON CONFLICT (id) DO NOTHING.
-- Chairman approved: Phase 3 — Task #19, 2026-05-26.
-- ============================================================================

CREATE TABLE IF NOT EXISTS design_standards (
    id VARCHAR(40) PRIMARY KEY,
    code VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    name_vi VARCHAR(255),
    category VARCHAR(40),                       -- 'fire_safety','energy','accessibility','structural','boq','lightning'
    issued_year INT,
    issuing_body VARCHAR(60),                    -- 'BXD','BTC','BNNPTNT','BTNMT'
    is_mandatory BOOLEAN DEFAULT TRUE,
    scope_summary TEXT,
    scope_summary_vi TEXT,
    applies_to_agents TEXT[],                    -- ['KTSChief','StructuralEngineer','MEPEngineer','BOQCalculator','QAValidator','InteriorDesigner']
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_design_standards_category ON design_standards(category);
CREATE INDEX IF NOT EXISTS idx_design_standards_mandatory ON design_standards(is_mandatory);

-- ─── Seed 6 standards (idempotent) ─────────────────────────────
INSERT INTO design_standards
  (id, code, name, name_vi, category, issued_year, issuing_body, is_mandatory,
   scope_summary, scope_summary_vi, applies_to_agents) VALUES

  ('qcvn-06-2022-bxd',
   'QCVN 06:2022/BXD',
   'National Technical Regulation on Fire Safety for Buildings',
   'Quy chuẩn kỹ thuật quốc gia về An toàn cháy cho nhà và công trình',
   'fire_safety',
   2022,
   'BXD',
   TRUE,
   'Mandates evacuation widths, materials class, fire-rated walls, fire-fighting equipment. Replaces QCVN 06:2020/BXD. Applies to all civil & industrial buildings.',
   'Quy định bề rộng thoát hiểm tối thiểu 1.5m, cấp vật liệu B trở lên trong khu vực thoát hiểm, tường ngăn cháy EI-60+, thiết bị PCCC. Thay thế QCVN 06:2020.',
   ARRAY['KTSChief','StructuralEngineer','MEPEngineer','QAValidator']),

  ('qcvn-09-2017-bxd',
   'QCVN 09:2017/BXD',
   'National Technical Regulation on Energy Efficient Buildings',
   'Quy chuẩn kỹ thuật quốc gia về Tiết kiệm năng lượng cho công trình',
   'energy',
   2017,
   'BXD',
   TRUE,
   'Mandatory for civil buildings ≥2500m² floor area. Sets U-value envelope limits, lighting LPD, HVAC COP, hot-water system efficiency. Applies to new construction & major renovations.',
   'Bắt buộc với công trình dân dụng ≥2500m². Quy định hệ số U vỏ bao che, mật độ chiếu sáng (LPD), COP của hệ HVAC, hiệu suất nước nóng. Áp dụng xây mới + cải tạo lớn.',
   ARRAY['KTSChief','MEPEngineer','QAValidator']),

  ('qcvn-10-2014-bxd',
   'QCVN 10:2014/BXD',
   'National Technical Regulation on Construction for Disabled Access',
   'Quy chuẩn kỹ thuật quốc gia về Xây dựng công trình đảm bảo người khuyết tật tiếp cận sử dụng',
   'accessibility',
   2014,
   'BXD',
   TRUE,
   'Mandatory for public buildings. Ramp slope ≤1:12, door clear width ≥900mm, accessible WC, tactile paving, lift mirror at wheelchair height.',
   'Bắt buộc với công trình công cộng. Độ dốc ram ≤1:12, cửa rộng ≥900mm, WC tiếp cận, gạch dẫn hướng người khiếm thị, gương thang máy độ cao xe lăn.',
   ARRAY['KTSChief','InteriorDesigner','QAValidator']),

  ('tcvn-9362-2012',
   'TCVN 9362:2012',
   'Design Standard for Building Foundations',
   'Tiêu chuẩn thiết kế nền móng nhà và công trình',
   'structural',
   2012,
   'BXD',
   FALSE,
   'Recommended national standard for foundation design — bearing capacity, settlement, group action, soil-structure interaction. Complements TCVN 5574.',
   'Tiêu chuẩn thiết kế nền móng — sức chịu tải, độ lún, hiệu ứng nhóm cọc, tương tác đất-kết cấu. Bổ sung TCVN 5574.',
   ARRAY['StructuralEngineer','QAValidator']),

  ('tt-13-2021-bxd',
   'TT 13/2021/TT-BXD',
   'Circular on Construction Quantity Take-off & Cost Estimating Methodology',
   'Thông tư hướng dẫn phương pháp bóc tách khối lượng và xác định dự toán xây dựng',
   'boq',
   2021,
   'BXD',
   TRUE,
   'Replaces QĐ 1129/QĐ-BXD. Mandates 6-sheet BOQ format: Summary / Materials / Labor / Equipment / By-section / Combined-rates. Required for all public-funded projects.',
   'Thay thế QĐ 1129/QĐ-BXD. Quy định bóc tách BOQ 6 sheet: Tổng hợp / Vật liệu / Nhân công / Máy thi công / Theo hạng mục / Đơn giá tổng hợp. Bắt buộc với dự án có vốn nhà nước.',
   ARRAY['BOQCalculator','QAValidator']),

  ('qcvn-02-2022-bxd',
   'QCVN 02:2022/BXD',
   'National Technical Regulation on Lightning Protection',
   'Quy chuẩn kỹ thuật quốc gia về Chống sét cho công trình xây dựng',
   'lightning',
   2022,
   'BXD',
   TRUE,
   'Mandatory for high-rise buildings (≥9 floors or H≥28m). LPS class I-IV based on risk assessment, mesh conductor on roof, down-conductor spacing, earthing system ≤10 ohm.',
   'Bắt buộc với nhà cao tầng (≥9 tầng hoặc H≥28m). Cấp LPS I-IV theo đánh giá rủi ro, lưới chống sét trên mái, khoảng cách dây xuống ≤20m, hệ tiếp đất ≤10 ohm.',
   ARRAY['MEPEngineer','QAValidator'])

ON CONFLICT (id) DO NOTHING;
-- end of file
