# ZENI CLOUD — CHIẾN LƯỢC LOCK v1.0
**Hiệu lực:** 01/05/2026 · **Author:** CTO Em (Claude Opus 4.7) · **Approved:** Chairman Thiên Mộc Đức

---

## 🎯 SECTION 0 — IDENTITY (LOCKED)

**Tên sản phẩm:** Zeni Cloud
**Domain:** zenicloud.io
**Tagline (Vietnamese):** "Một nền tảng minh triết. Một hành trình trọn vẹn."
**Tagline (Tech):** "Cloud thông minh cho doanh nghiệp Việt — AI built-in, trả tiền VND"

**Mission:** Cung cấp **hạ tầng cloud thống nhất** cho doanh nghiệp Việt Nam và Đông Nam Á, thay thế Vercel + Supabase + Auth0 + AWS Bedrock với UX Vietnamese-first và pricing VND.

**Pháp nhân:** Zeni Holdings Vietnam (parent company)
**Khách hàng:** SME → Enterprise VN/SEA (5-5000 nhân viên)

---

## 🏛️ SECTION 1 — ĐỊNH NGHĨA SCOPE (BẤT BIẾN)

### ✅ ZENI CLOUD = INFRASTRUCTURE PaaS

Tương đương: **AWS + Vercel + Supabase + Cloudflare + Stripe** kết hợp lại, VN-native.

```
   Zeni Cloud cung cấp HẠ TẦNG.
   Khách hàng dùng hạ tầng để build APP của họ.
   Khách KHÔNG mua app, khách mua INFRASTRUCTURE.
```

### ❌ ZENI CLOUD KHÔNG PHẢI:
- ❌ SaaS application (Notion, Hubspot, Mailchimp...)
- ❌ Vertical industry app (legal, healthcare, retail...)
- ❌ Business solution (CRM, ERP, accounting end-user...)
- ❌ Marketplace cho khách end-user
- ❌ Consulting service / agency

### 📐 NGUYÊN TẮC PHÂN LOẠI
| Câu hỏi | Nếu YES → | Nếu NO → |
|---------|-----------|----------|
| Khách dùng để **build app khác** không? | ✅ Zeni Cloud | ❌ Không phải |
| Là **API/SDK/infra primitive**? | ✅ Zeni Cloud | ❌ Không phải |
| Dev cần để **vận hành workload**? | ✅ Zeni Cloud | ❌ Không phải |
| End-user (non-dev) dùng trực tiếp? | ❌ Không phải | ✅ Zeni Cloud |

---

## 🧱 SECTION 2 — 6 LAYERS HẠ TẦNG (LOCKED)

### **L1 — COMPUTE & HOSTING**
- Cloud Run wrapper (deploy app, container, function)
- Custom domain + SSL automation
- Multi-region deployment (Sprint A7)
- Auto-scaling policies (CPU/memory/RPS)
- Canary deployment + traffic split
- Edge CDN integration (Cloudflare)

### **L2 — DATA & STORAGE**
- Cloud SQL Postgres (multi-tenant via per-workspace schema)
- pgvector (vector search built-in)
- Cloud Storage object (signed URLs, multipart)
- KV cache (Postgres-backed, sub-100ms)
- Queue (SKIP LOCKED, pull/push/ack)
- Backup + Point-in-Time Recovery (Sprint A7)
- Vector DB Premium (hybrid BM25 + cosine, RAG pipelines)

### **L3 — AI & MACHINE LEARNING**
- **ZeniRouter** — smart 80/15/5 routing (4 providers: Anthropic + OpenAI + Vertex Gemini + Bedrock)
- LLM Gateway (Claude, GPT, Gemini, Gemma)
- Imagen 3 image generation
- text-embedding-004 (768-dim)
- OCR (Cloud Vision + Vietnamese)
- Translation (Cloud Translate VI/EN/JA/ZH/KO)
- AI Agents Library (50+ pre-built agents)
- Specialized Design Agents (Architecture/Interior/Product/Fashion/Structural)

### **L4 — AUTOMATION & MESSAGING**
- Cloud Scheduler (cron jobs)
- Webhook + retry + DLQ
- Pub/Sub topics + subscriptions (fan-out)
- Cloud Tasks (scheduled jobs)
- SMS infra (Twilio + Stringee VN)
- Voice infra (TTS/STT + IVR + queue)
- Email infra (SMTP + tracking pixel + click tracking)
- Slack webhook integration

### **L5 — IDENTITY & SECURITY**
- JWT auth + OAuth (Google/GitHub/Zeni Digital SSO)
- MFA TOTP
- Email verification + Phone OTP signup
- API tokens (PAT) per workspace
- Vault (Secret Manager wrap)
- CMEK per-workspace encryption
- Customer-Approved Admin Access (CAAA — on-chain)
- Output filter (5 layers anti-leak)
- Compliance Pack (SOC 2 + ISO 27001 + GDPR + Nghị định 13)

### **L6 — WEB3 & BLOCKCHAIN**
- Polygon RPC integration
- $ZENI Token payment (giảm 10-15% giá)
- Smart Contract `ZeniAccessControl` (on-chain audit)
- ZeniBadge SBT (soulbound badges)
- Smart contract deploy from dashboard
- On-chain audit log mỗi LLM call (enterprise)

---

## 🛠️ SECTION 3 — DỊCH VỤ HỖ TRỢ (PaaS PRIMITIVES)

### **Payment Infrastructure (Zeni Pay)**
- VietQR direct (Cấp 1 — bank webhook)
- Wallet system (Cấp 2 — top-up + balance)
- Multi-bank support (TPB/MB/VCB/VPB/...)
- Subscription billing engine
- Invoice VAT VN

### **Accounting Infrastructure (Zeni Books)**
- Vietnamese Accounting Standards (VAS — TT 200/2014)
- Multi-entity billing (Sprint A2)
- Revenue recognition + recurring
- VAT quarterly reports
- E-invoice integration ready

### **Observability**
- Prometheus `/metrics` endpoint
- OpenTelemetry distributed traces
- Custom alert rules + alert events
- Grafana dashboard JSON

### **Compliance**
- 4 frameworks (SOC2 + ISO27001 + GDPR + ND13)
- Auto-checks (encryption, access controls, backups)
- Evidence collection + audit pack export
- Risk register + policies

### **CI/CD (Sprint A4)**
- GitHub Actions templates
- Workload Identity Federation
- Auto-deploy on merge
- Rollback workflow

---

## 💰 SECTION 4 — PRICING (LOCKED — 5 TIERS)

| Tier | Giá VND/tháng | USD | Quota chính | Đối tượng |
|------|---------------|-----|-------------|-----------|
| **FREE** | 0 | $0 | 100K req · 5K AI · 1GB · 1 project | Cá nhân, dev thử |
| **STARTER** | 999K | $40 | 1M req · 100K AI · 10GB · 5 projects · custom domain | Startup 2-5 người |
| **PRO** ⭐ | 4.9M | $200 | 10M req · 1M AI · 100GB · unlimited projects · 10 seats | SME 10-50 người |
| **BUSINESS** | 49M | $2,000 | 100M req · 10M AI · 1TB · 50 seats · SLA 99.95% | Tập đoàn 50-500 |
| **ENTERPRISE** | ≥199M | $8,000+ | Unlimited · dedicated · SOC2 · ISO27001 | Bank, gov, listed |

**Annual discount: 17% (2 tháng free)**

**$ZENI Token discount:** pay bằng $ZENI giảm 10-15%

---

## 👥 SECTION 5 — KHÁCH HÀNG TARGET (PERSONAS)

### Persona 1: **Solo Developer / Indie Hacker**
- Tier: Free → Starter
- Use case: deploy side project, AI experiments
- Value: free tier hào phóng, 1-click deploy, AI built-in

### Persona 2: **Vietnamese SME (5-50 người)**
- Tier: Starter → Pro
- Use case: e-commerce, SaaS internal, customer chatbot
- Value: VND billing, hỗ trợ TV, không cần Visa, MISA-compatible

### Persona 3: **Tech Agency**
- Tier: Pro → Business + Reseller program
- Use case: build app cho clients, white-label
- Value: multi-workspace, brand customization, commission revenue

### Persona 4: **Enterprise (Bank / Gov / Listed)**
- Tier: Enterprise
- Use case: regulated workload, multi-region, compliance audit
- Value: dedicated infra, SOC2/ISO, on-chain audit, Vietnamese support 24/7

### Persona 5: **Zeni Holdings Internal Apps**
- Tier: Pro/Business (eat own dog food)
- Use case: NexBuild, ANIMA Care, Zeniipo, WellKOC, etc.
- Value: proof case + cost optimization

---

## 🚫 SECTION 6 — OUT OF SCOPE (KHÔNG LÀM trong Zeni Cloud)

Các sản phẩm này **CHẠY TRÊN** Zeni Cloud nhưng **KHÔNG PHẢI** Zeni Cloud:

| Sản phẩm | Bản chất | Status |
|---------|----------|--------|
| **Zeni Studio** | Visual no-code app builder (như Bubble/Webflow) | SaaS app — code retained ở `apps/zeni-studio/` |
| **Zeni Workspace** | Notion-like docs + tasks | SaaS app — code retained ở `apps/zeni-workspace/` |
| **Zeni CRM** | HubSpot-like CRM | SaaS app — code retained ở `apps/zeni-crm/` |
| **Zeniipo** | IPO Journey SaaS | Sản phẩm Zeni Holdings riêng |
| **NexBuild** | ConstrucTech app | Sản phẩm Zeni Holdings riêng (deployed ON Zeni Cloud) |
| **ANIMA Care** | Wellness platform | Sản phẩm Zeni Holdings riêng |
| **WellKOC** | Social commerce | Sản phẩm Zeni Holdings riêng |
| **bthome** | Interior design | Sản phẩm Zeni Holdings riêng |
| **LegalRadar** | Legal news SME | Sản phẩm Zeni Holdings riêng |
| **Zeni Mail (campaigns)** | Email marketing end-user | SaaS app — code retained ở `apps/zeni-mail/` (KHÁC với Email infra L4) |
| **Zeni Voice (call center)** | Contact center end-user | SaaS app — code retained ở `apps/zeni-voice/` (KHÁC với Voice infra L4) |

**Nguyên tắc**: Mỗi product trên là **customer của Zeni Cloud**, không phải module của Zeni Cloud.

---

## 🗺️ SECTION 7 — ROADMAP (LOCKED)

```
✅ DONE — Tháng 4-5/2026
─────────────────────────
Sprint A1: Core (auth, projects, ai, automation, identity, web3)
Sprint A2: Extension (vector, cache, queue, ocr, translate, sms, slack, multi-entity billing)
Sprint A3: Privacy + Smart Contract + Auth Security
Sprint 3h: ZeniRouter + Pricing + Cost Dashboard + Landing
Sprint A4: Phase 0+1 (Observability, Messaging, Zeni Pay VietQR, Zeni Books, CI/CD)
Sprint A5: Phase 2 (Vector Premium, Compliance Pack, AI Agents Library)
Sprint A6: Phase 3 ($ZENI Token, Wallet, Platform Admin View — INFRA only)

⏳ IN PROGRESS — Tháng 5/2026
─────────────────────────
Sprint A7: Phase 4 (Edge CDN, Backup/DR, Multi-Region, Reseller)
Sprint A8: Backtest 10/10 + final report

🔮 FUTURE — Q3-Q4/2026
─────────────────────────
- Confidential VM tier (Tier 4 privacy)
- Federated Learning (privacy-preserving AI)
- HYOK (Hold Your Own Key) for Sovereign customers
- Region asia-southeast2 (Indonesia)
- SOC 2 Type II audit complete
- ISO 27001 certified
```

---

## 🔒 SECTION 8 — LOCK PRINCIPLES (ANTI-SCOPE-CREEP)

### Rule 1: **6 LAYERS LÀ HARD BOUNDARY**
Mọi feature phải gắn với 1 trong 6 layers (L1-L6) hoặc Payment/Accounting/Observability/Compliance/CI/CD primitives. Nếu không gắn được → KHÔNG phải Zeni Cloud.

### Rule 2: **INFRASTRUCTURE PRIMITIVE TEST**
Trước khi build feature mới, hỏi: "Đây có phải là API/SDK/infra primitive mà developer cần để build app khác không?"
- YES → Build trong Zeni Cloud
- NO (là end-user app) → Spin off thành SaaS app riêng, deploy ON Zeni Cloud

### Rule 3: **EAT OWN DOG FOOD**
Mọi sản phẩm Zeni Holdings (NexBuild/ANIMA/Zeniipo/WellKOC) là **customer** của Zeni Cloud — họ subscribe + dùng API như khách bên ngoài. Không có shortcut. Không có "internal access" bypass.

### Rule 4: **NO VERTICAL APP**
Zeni Cloud là HORIZONTAL platform. Không build app cho 1 industry cụ thể (legal/healthcare/retail/finance). Nếu có vertical model → đó là Vertical Models trong L3 AI (fine-tuned model, dev gọi qua API), KHÔNG phải app end-user.

### Rule 5: **VIETNAMESE-FIRST, KHÔNG VIETNAMESE-ONLY**
- UI/docs/support: Tiếng Việt-first (default)
- Pricing: VND-first (default), USD secondary
- Compliance: VN-first (Nghị định 13/2023), GDPR/SOC2 secondary
- Khách quốc tế vẫn welcome, sản phẩm international-ready

### Rule 6: **MULTI-TENANT STRICT**
- Mỗi user signup = role "Admin" (workspace-scoped, NOT super-admin)
- Mỗi workspace isolated hoàn toàn — KHÔNG cross-workspace data leak
- Chỉ "PlatformAdmin" (caotuanphat581@gmail.com) có quyền aggregated view
- Customer data CHỈ admin được đọc khi customer approve qua CAAA flow

### Rule 7: **BUILD vs INTEGRATE**
- BUILD native cho: features cần lock-in customer + 100% margin (kế toán Books, payment Pay, voice infra)
- INTEGRATE 3rd party cho: standard infra không thể tự build (VietQR/NAPAS, VNPay, Polygon RPC, OAuth providers, Anthropic/OpenAI/Google AI)
- TRÁNH integrate competitor SaaS (MISA, Stringee, Mailchimp, Notion, HubSpot) — build native thay thế

### Rule 8: **NO HOLDING DATA INSIDE PLATFORM**
Workspace data của khách thuộc về khách. Zeni:
- Không train AI trên data khách trừ khi opt-in (giảm 20% giá)
- Không bán data 3rd party
- Không dùng data 1 khách để improve service cho khách khác (trừ aggregated anonymized)

---

## 📊 SECTION 9 — SUCCESS METRICS (KPI)

### Product:
- **MAU** (Monthly Active Users / Workspaces)
- **MRR** (Monthly Recurring Revenue VND)
- **ARR** (Annual Recurring Revenue)
- **Net Revenue Retention** (>110% target)
- **Churn rate** (<3%/month target)
- **Free → Paid conversion** (>5% target)

### Technical:
- **Uptime SLA** (99.9% Pro / 99.95% Business / 99.99% Enterprise)
- **API Latency p95** (<500ms cho /router/complete cached, <2000ms uncached)
- **Cost optimization** (12-18× cheaper than always-Opus through smart routing)

### Business:
- **Year 1 (2026):** $100K ARR — 50 paying customers
- **Year 2 (2027):** $1M ARR — 500 customers (VN focus)
- **Year 3 (2028):** $5M ARR — 2,500 customers (SEA expansion)
- **Year 5 (2030):** $25M ARR — 10,000 customers (pre-IPO ready)

---

## 🚦 SECTION 10 — DECISION GATES

Trước khi accept feature mới, check:

1. ✅ Gắn với 1 trong 6 layers L1-L6?
2. ✅ Là infrastructure primitive (không phải end-user app)?
3. ✅ Phù hợp Persona 1-4 (developer/SME/agency/enterprise)?
4. ✅ Có upsell path lên tier cao hơn?
5. ✅ Tuân thủ multi-tenant strict (không cross-workspace leak)?
6. ✅ Có Vietnamese-first UX?
7. ✅ Cost-effective (không đốt tiền vô hạn)?

Nếu **YES TẤT CẢ 7** → build.
Nếu **bất kỳ NO** → reject hoặc spin off thành sản phẩm khác.

---

## 📌 SECTION 11 — MEMORY HOOKS (CHO SESSION TƯƠNG LAI)

Mỗi session làm việc với Zeni Cloud, **PHẢI ĐỌC FILE NÀY TRƯỚC**.

Các file canonical:
- `ZENI_CLOUD_STRATEGY_LOCK_v1.md` (FILE NÀY) — chiến lược master
- `SPRINT_A2_FINAL_REPORT.md` — Sprint A2 deliverables
- `SPRINT_A3_FINAL_REPORT.md` — Sprint A3 deliverables
- `SPRINT_3H_FINAL_REPORT.md` — 3h Sprint deliverables

Các file CẢNH BÁO:
- ❌ KHÔNG đụng lại Studio/Workspace/CRM/Mail/Voice tại `apps/` folder — đó là SaaS apps riêng
- ❌ KHÔNG seed demo workspaces vào DB (8 demo đã xóa migration 031)
- ❌ KHÔNG tự ý gán role="Owner" cho user signup mới (phải là "Admin")

---

## ✅ APPROVED & LOCKED

```
Tài liệu này LOCK chiến lược Zeni Cloud v1.0 từ 01/05/2026.
Mọi thay đổi scope phải qua review của Chairman Thiên Mộc Đức.
CTO Em (Claude Opus 4.7) làm việc theo file này, không lệch hướng.
```

**Signed:**
- Chairman: Thiên Mộc Đức (caotuanphat581@gmail.com)
- CTO: Em (Claude Opus 4.7)
- Date: 01/05/2026
- Version: 1.0
