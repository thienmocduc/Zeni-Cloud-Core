# 🎯 MULTI-AGENT TEAM WORKFLOW — ZENI CLOUD PRODUCTION SPRINT

> **Mục tiêu:** Hoàn thiện 100% gaps trước launch. Skip payment (admin grant Pro tier theo chỉ định).
> **Strategy:** Chia work thành **5 streams song song độc lập**. Rút ngắn từ 6-7 ngày → **2-3 ngày** với multi-agent.
> **Chairman session** (Zeni Cloud Core) = orchestrator. Mỗi stream = 1 agent session riêng.

---

## 📐 NGUYÊN TẮC PHÂN CHIA

1. **Streams độc lập** — không block nhau. Mỗi stream có scope rõ ràng, contract API rõ.
2. **Chairman branch** = `main` trên GitHub. Mỗi agent stream làm trên branch riêng `stream-A-backend-gaps`, etc.
3. **Daily merge** vào main, deploy v31, v32, ... liên tục.
4. **Test contract first**: mỗi endpoint có test E2E trước khi merge.
5. **Anh CEO chốt blocker** trong vòng <2h khi 1 stream cần input (vd OAuth Client ID).

---

## 🚦 5 STREAMS PHÂN CÔNG

### **STREAM A — Backend Gaps** (Chairman session làm, ~12h)

**Scope:** API endpoints còn thiếu. Sửa bug, thêm feature backend-only.

| # | Task | Endpoint | Thời gian |
|---|------|----------|----------|
| A1 | Fix admin grant endpoint (đang trả empty) | `POST /billing/admin/grant-tier` | 30m |
| A2 | Bulk grant Pro tier API (cấp nhiều ws cùng lúc) | `POST /billing/admin/bulk-grant` | 1h |
| A3 | Custom domain mapping per workspace | `POST /projects/{id}/domain` | 4h |
| A4 | Webhook retry/DLQ (Cloud Tasks) | hook vào `/automation/events/fire` | 3h |
| A5 | Email quota-near-limit alerts (cron daily) | Cloud Scheduler internal job | 2h |
| A6 | Cost dashboard API (per workspace, time series) | `GET /billing/usage-summary` | 2h |
| A7 | Stress test 100 concurrent users | Locust script + report | 2h |

**Output:** Backend code + tests + 1 build merged.
**Acceptance:** Tất cả endpoint trả 200/201/202 đúng spec. E2E test pass.

---

### **STREAM B — Frontend Dashboard Real Data** (Agent #2, ~14h)

**Scope:** Refactor `frontend/index.html` và `zeni-realdata.js` để 6 tabs hiển thị data thật.

| # | Task | View name | Thời gian |
|---|------|-----------|----------|
| B1 | L1 Compute tab: list projects from API | `compute` | 2h |
| B2 | L2 Data tab: list tables + run query UI | `data` | 2h |
| B3 | L3 AI tab: model selector + chat playground | `ai` | 2h |
| B4 | L4 Automation tab: connectors + cron jobs | `auto` | 2h |
| B5 | L5 Identity tab: secrets + MFA setup modal | `identity` | 2h |
| B6 | L6 Web3 tab: live read $ZENI/Badge contracts | `web3` | 1h |
| B7 | Cost dashboard tab (subscribe + wallet + history) | `billing` | 2h |
| B8 | Public signup form (`/signup` page on landing) | landing | 1h |

**Output:** Updated index.html + zeni-realdata.js + new signup.html.
**Acceptance:** Khách login → tab nào cũng thấy data thật (không demo mock).

**Contract với Stream A:** dùng các endpoint backend đã có sẵn. Nếu thiếu endpoint, tag Stream A.

---

### **STREAM C — Auth + OAuth** (Agent #3, ~6h, **chờ anh tạo OAuth Client trước**)

**Scope:** OAuth Google + GitHub + self-service signup.

| # | Task | Endpoint | Thời gian | Blocker |
|---|------|----------|----------|---------|
| C0 | **(Anh CEO)** Tạo Google OAuth Client ID Console | — | 5m | Anh |
| C0b | **(Anh CEO)** Tạo GitHub OAuth App | — | 5m | Anh |
| C1 | OAuth Google authorize/callback flow backend | `GET /auth/oauth/google/{authorize,callback}` | 2h | C0 |
| C2 | OAuth GitHub flow | tương tự | 1.5h | C0b |
| C3 | Link OAuth account → existing user (account merging) | `POST /auth/oauth/link` | 1h | — |
| C4 | Public signup form backend handler | `POST /auth/signup` | 1h | — |
| C5 | Email verify token (signup → email confirm link) | `GET /auth/verify-email?token=` | 30m | — |

**Output:** OAuth + signup live. Khách click "Continue with Google" trên `/app` → login OK.
**Acceptance:** Test 3/3 flow: signup email/pass, signup Google, signup GitHub.

---

### **STREAM D — Documentation + SDK** (Agent #4, ~10h, **làm song song hoàn toàn độc lập**)

**Scope:** Tutorial cho khách + SDK auto-gen.

| # | Task | Output | Thời gian |
|---|------|--------|----------|
| D1 | Quick-start guide (Vietnamese) | `docs/QUICKSTART.md` | 2h |
| D2 | Architecture agent tutorial (NexDesign use case) | `docs/agents-architecture-tutorial.md` | 1h |
| D3 | Interior agent tutorial (BTHome use case) | `docs/agents-interior-tutorial.md` | 1h |
| D4 | Image gen tutorial + best prompts | `docs/imagen-best-practices.md` | 1h |
| D5 | SDK Node.js auto-gen từ OpenAPI | npm package `@zenicloud/sdk` | 2h |
| D6 | SDK Python auto-gen | PyPI package `zenicloud` | 2h |
| D7 | Postman collection import file | `docs/zenicloud.postman_collection.json` | 1h |

**Output:** `docs/` folder đầy đủ + 2 SDK packages publish.
**Acceptance:** Khách mới đọc 1 trang QUICKSTART → hiểu + chạy được trong 10 phút.

---

### **STREAM E — Migration & Onboarding** (Agent #5, ~10h, **chạy sau khi A+B+C done 50%**)

**Scope:** Execute migration cho 3 customer #1: ANIMA, NexDesign, BTHome.

| # | Task | Customer | Thời gian |
|---|------|----------|----------|
| E1 | ANIMA migration Phase 1-3 (dockerize + DB migrate + deploy) | ANIMA | 4h |
| E2 | ANIMA migration Phase 4-5 (cron + DNS cutover) | ANIMA | 2h |
| E3 | NexDesign onboarding (hook agents API + test live) | NexDesign | 1h |
| E4 | BTHome onboarding (Interior agent integration) | BTHome | 1h |
| E5 | Stress test với 3 customer thật concurrent | All | 1h |
| E6 | Bug fix sau soft launch | All | 1h |

**Output:** 3 customer LIVE trên Zeni Cloud, bị duy không downtime.
**Acceptance:** Daily uptime check pass 7 ngày liên tục.

---

## ⏰ TIMELINE 3 NGÀY (PARALLEL)

```
NGÀY 1 (đồng loạt khởi động):
  A1 (30m)        — Chairman fix grant bug
  A2 (1h)         — Bulk grant API
  A3-A4 (7h)      — Custom domain + webhook retry
  B1-B4 (8h)      — Frontend tabs Compute/Data/AI/Auto
  C0 (anh)        — Tạo OAuth Client ID Google + GitHub
  D1-D2 (3h)      — Quickstart + Architecture tutorial
  E1 partial      — Anima dockerize start

NGÀY 2:
  A5-A7 (5h)      — Email alerts + cost API + stress test
  B5-B8 (6h)      — Frontend tabs Identity/Web3/Billing/Signup
  C1-C5 (6h)      — OAuth backend + signup
  D3-D5 (4h)      — Interior tutorial + Image best prac + SDK Node
  E1 finish + E2  — Anima migrate phase 4-5

NGÀY 3 (validation + soft launch):
  Tổng test E2E (3h) — toàn bộ flow real customer
  E3-E4 (2h)         — NexDesign + BTHome onboarding
  D6-D7 (3h)         — SDK Python + Postman
  Bulk grant Pro     — Cấp tier cho 5-10 doanh nghiệp chỉ định
  E5-E6 (2h)         — Stress test concurrent + bug fix
  
  → 23h end-of-day-3: SOFT LAUNCH cho 10 khách beta
```

---

## 📐 CONTRACTS GIỮA STREAMS (tránh conflict)

### Stream A → Stream B
- Stream A expose endpoint, Stream B consume.
- Format response JSON cố định. Stream A KHÔNG đổi schema sau khi B đã code.
- File contract: `docs/api-contracts.json` (OpenAPI spec).

### Stream B → Stream C
- Stream B render OAuth buttons với href: `/api/v1/auth/oauth/google/authorize?return=/app`
- Stream C trả 302 redirect → Stream B catch ở callback URL render success/failure UI.

### Stream A → Stream D
- Stream A update OpenAPI spec → Stream D auto-regen SDK.
- Hooks via Cloud Build trigger: push OpenAPI thay đổi → rebuild SDK.

### Stream E phụ thuộc A + B + C
- E chỉ start sau khi A endpoints + B UI + C OAuth deployed v33+.
- E run E2E test trước, không làm "skip phase B6 hold-over."

---

## 🎚️ QUALITY GATES

Mỗi stream **phải pass** trước khi merge vào `main`:

```
Stream X PR → main:
  ✓ All endpoints có test E2E (Pytest cho backend, Playwright cho frontend)
  ✓ OpenAPI spec updated
  ✓ Audit log + billing log đúng (mỗi action ghi đủ metadata)
  ✓ Security review:
    - Auth check (JWT or PAT scope)
    - SQL injection chặn
    - Rate limit kế thừa Cloud Armor
    - Workspace isolation (require_workspace_access)
  ✓ Build + deploy v(NN+1) PASS trên Cloud Run
  ✓ Smoke test production sau deploy: GET /health = 200, login = OK
```

---

## 🛠️ TOOLS & PROCESS

### Communication
- **Slack channel:** #zeni-cloud-sprint (tạo)
- **Daily standup:** 9AM (15 phút) — mỗi stream report 1 dòng: what done, what next, what blocker
- **Doc URL:** Google Doc shared với anh CEO

### Git workflow
```
main                          ← chairman merge
├── stream-A-backend-gaps     ← Agent #1
├── stream-B-frontend         ← Agent #2  
├── stream-C-oauth-signup     ← Agent #3
├── stream-D-docs-sdk         ← Agent #4
└── stream-E-migration        ← Agent #5

Daily merge:
  EOD ngày 1: merge A+D
  EOD ngày 2: merge B+C+E partial
  EOD ngày 3: merge all + tag v1.0
```

### Deploy strategy
- Mỗi merge → build mới → tag image v(N+1)
- Auto-deploy lên `staging.zenicloud.io` qua Cloud Build trigger
- Manual approval → promote lên prod `zenicloud.io`

---

## 🎯 BULK GRANT PRO TIER (sau khi xong)

Sau khi tất cả streams xong + soft launch validate, chairman chạy:

```bash
# Bulk grant Pro tier 12 tháng cho 10 doanh nghiệp chỉ định
for WS in nexbuild bthome anima wellkoc capital digital holdings zeniipo \
          partner-nguyen-design partner-thien-construction; do
  curl -X POST "https://zenicloud.io/api/v1/billing/admin/grant-tier" \
    -H "Authorization: Bearer $CHAIRMAN_JWT" \
    -H "Content-Type: application/json" \
    -d "{\"workspace_id\":\"$WS\",\"tier\":\"pro\",\"duration_months\":12,\"reason\":\"Internal partner Zeni Holdings\"}"
done
```

**Hoặc dùng endpoint mới (Stream A2 sẽ build):**
```bash
curl -X POST "https://zenicloud.io/api/v1/billing/admin/bulk-grant" \
  -H "Authorization: Bearer $CHAIRMAN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "workspace_ids": ["nexbuild","bthome","anima","wellkoc","capital","digital","holdings","zeniipo"],
    "tier": "pro",
    "duration_months": 12,
    "reason": "Zeni Holdings internal partners — full Pro access"
  }'
```

→ Tất cả workspace nhận tier Pro 12 tháng, 300 runs + 500 ảnh + 5M tokens/tháng × 12.

---

## ✅ DEFINITION OF DONE — TOÀN SPRINT

```
[ ] Tất cả 5 streams merged vào main, deployed v33+
[ ] Frontend dashboard 6 tabs hiện data thật (no mock)
[ ] OAuth Google + GitHub login OK
[ ] Self-service signup public hoạt động
[ ] Cost dashboard cho khách hoạt động
[ ] Custom domain mapping per workspace API live
[ ] Webhook retry/DLQ active
[ ] Email quota alerts gửi đúng
[ ] SDK Node.js + Python publish
[ ] 7-page tutorial documentation đầy đủ
[ ] Postman collection downloadable
[ ] ANIMA Care MIGRATED 100% lên Zeni Cloud, downtime <30 phút
[ ] NexDesign + BTHome onboarded, dùng API real
[ ] Stress test 100 concurrent users: 95th percentile < 500ms
[ ] Backup restore drill: PASS (PITR Cloud SQL)
[ ] Bulk grant Pro tier cho 8-10 internal workspaces
[ ] Chairman tag v1.0.0 LAUNCH
[ ] Public marketing landing zenicloud.io update
```

---

## 🚀 KỊCH BẢN HARD LAUNCH

```
Ngày 4 (sau sprint): Public Hard Launch
  - Post landing page final với 4 tier pricing
  - Marketing email cho 100 doanh nghiệp tiềm năng
  - Discord/Telegram support channel
  - Founder demo livestream

Tháng 1 sau launch:
  - Track: signup rate, conversion paid, MRR, churn, NPS
  - Daily morning standup chairman + 3 founder
  - Iterate dựa feedback khách thật
```

---

**Hết workflow. Mỗi stream agent đọc + làm theo scope của mình. Chairman session điều phối + merge + bulk grant cuối sprint.**
