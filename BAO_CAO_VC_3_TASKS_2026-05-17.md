# Response Viet Contech 3 Tasks — 2026-05-17

**From:** Zeni CTO (Claude Opus 4.7)
**To:** Chairman Thien Moc Duc → forward Viet Contech
**Context:** VC forward 3 tasks block volume launch Phase 1

---

## Task 1 — SMTP env wire ✅ FIXED CODE

**VC report:** BE `/register/zalo-otp/start` upstream_error vì 4 env SMTP chưa wire.

**Em diagnose:**
- Cloud Run zeni-backend **ĐÃ wire** `SMTP_USER` + `SMTP_PASSWORD` (em verified)
- Backend code đọc `GMAIL_SMTP_USER` + `GMAIL_SMTP_PASSWORD` ← **mismatch tên**
- → empty → email skip → upstream_error

**Em fix:** PR #4 `fix/smtp-env-fallback` — code đọc cả 2 tên (or-chain).

**Pending:** Chairman merge PR #4 → deploy v172 → VC `/register/zalo-otp/start` trả 200 + OTP gửi đi.

**ETA unblock:** ~15 phút sau khi chairman merge.

---

## Task 2 — Wire 5 env vars Lớp 03 AI Engine

**VC propose:**
```
ZENI_L3_BASE_URL
ZENI_L3_API_KEY
ZENI_L3_DEFAULT_REASONING_MODEL=claude-sonnet-4-5
ZENI_L3_DEFAULT_VISION_MODEL=gemini-2.5-pro
ZENI_L3_DEFAULT_IMAGE_MODEL=flux-1-pro
```

**Em diagnose:**

| Model | Backend support | Status |
|---|---|---|
| `claude-sonnet-4-5` | ✅ `llm_gateway.py` + `router/registry.py` đã có | Production ready |
| `gemini-2.5-pro` | ✅ `llm_gateway.py` + `router/registry.py` đã có | Production ready |
| `flux-1-pro` | ❌ **CHƯA integrated** (BFL/Replicate client) | Cần code mới |
| `ZENI_L3_*` env vars | ❌ **Convention CHƯA TỪNG dùng** | Cần code adapt |

**Em propose 2 cách:**

### Cách A — Wire endpoint hiện có (VC adapt frontend, NHANH NHẤT)

VC đổi frontend gọi endpoint Zeni Cloud có sẵn:
- Reasoning: `POST /api/v1/ai/complete?ws=vietcontech` với `{model: "claude-sonnet-4-5", prompt, ...}`
- Vision: same endpoint với `{model: "gemini-2.5-pro", prompt, image_url, ...}`
- Image: chưa có (cần code Flux)

VC BYO Anthropic + Gemini key qua `POST /api/v1/workspaces/vietcontech/ai-providers` (migration 065 đã có).

**Cost:** Zeni master key serving — chairman trả tiền.

### Cách B — Implement L3 Gateway proxy (CODE MỚI, 1-2 ngày)

Em build `/api/v1/l3/{reasoning,vision,image}` endpoint mới:
- Read 5 `ZENI_L3_*` env vars
- Route request → llm_gateway.py
- Track quota per workspace
- Add Flux client (api.bfl.ai)

**Pros:** VC's frontend convention không đổi.
**Cons:** Em phải code mới + deploy.

**Em recommend Cách A** (đơn giản, không cần code mới).

---

## Task 3 — Quota L3 cho VC Phase 1

**VC request:**
- 10M tokens reasoning/tháng (claude-sonnet-4-5)
- 5M tokens vision/tháng (gemini-2.5-pro)
- 2,000 ảnh render (flux-1-pro)
- 10GB bucket `vietcontech-renders`

**Em estimate cost:**

| Mục | Volume | Cost/tháng |
|---|---|---|
| Claude Sonnet 4.5 input 5M × $3/M | | $15 |
| Claude Sonnet 4.5 output 5M × $15/M | | $75 |
| Gemini 2.5 Pro input 3M × $1.25/M | | $4 |
| Gemini 2.5 Pro output 2M × $5/M | | $10 |
| Flux 1.1 Pro 2,000 × $0.04 | | $80 |
| GCS bucket 10GB Standard × $0.02/GB | | $0.20 |
| **Tổng cost Zeni cho VC tháng** | | **~$184** |

**Đề xuất VC pricing (chairman quyết):**
- Hiện tại chairman tặng cho VC test phase = $0/tháng (Zeni eat cost)
- Khi launch commercial: VC trả $500-1000/tháng (margin 2.7x-5.4x)

**Em wire (sau khi chairman approve):**

1. **Tạo GCS bucket `vietcontech-renders`** (10GB Standard, us-central1)
2. **Migration 075 workspace AI quota** (em sẽ commit):
   ```sql
   CREATE TABLE workspace_ai_quotas (
     workspace_id VARCHAR(64),
     period_month VARCHAR(7),  -- '2026-05'
     reasoning_tokens_used BIGINT DEFAULT 0,
     reasoning_tokens_quota BIGINT DEFAULT 0,
     vision_tokens_used BIGINT DEFAULT 0,
     vision_tokens_quota BIGINT DEFAULT 0,
     image_count_used INT DEFAULT 0,
     image_count_quota INT DEFAULT 0,
     storage_gb_used FLOAT DEFAULT 0,
     storage_gb_quota FLOAT DEFAULT 0,
     PRIMARY KEY (workspace_id, period_month)
   );
   
   INSERT INTO workspace_ai_quotas VALUES (
     'vietcontech', '2026-05',
     0, 10000000,  -- 10M reasoning
     0, 5000000,   -- 5M vision
     0, 2000,      -- 2K images
     0, 10         -- 10GB
   );
   ```

3. **Add quota enforcement middleware** trong `/api/v1/ai/complete`:
   - Trước mỗi request: check quota workspace
   - Sau response: increment used counter
   - Trả 429 nếu vượt quota

---

## Tổng hợp action chairman

### URGENT (unblock VC ngay)
1. **Merge PR #4** (SMTP fix) → unblock VC register
2. **Approve em setup WIF + grant roles SA `github-deployer`** → deploy v172 → SMTP fix live

### Task 2-3 (sau khi VC register OK)
3. **Pick Cách A hay B** cho Task 2:
   - A: VC adapt frontend dùng `/api/v1/ai/complete` (em không cần code mới)
   - B: Em code `/api/v1/l3/*` proxy (1-2 ngày)
4. **Approve quota VC** (10M+5M+2K+10GB) — cost Zeni $184/tháng
5. **Có Anthropic + Gemini + Flux API keys chưa?**
   - Anthropic: chairman có sẵn?
   - Gemini: dùng GCP Vertex AI (cần enable Vertex AI API)
   - Flux: chưa có, em propose api.bfl.ai pay-as-you-go (link signup: https://api.us1.bfl.ai/)

### File em đã commit phiên này
- `PR #4` fix/smtp-env-fallback (Task 1 fix code)
- File này `BAO_CAO_VC_3_TASKS_2026-05-17.md`

---

**Em standby chờ chairman quyết.** Sau khi approve, em proceed code + setup tương ứng.
