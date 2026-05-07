# Zeni Cloud · Workflow hướng dẫn dùng

> Bản 2026-04-25 · Production URL: https://zenicloud.io
> Admin: caotuanphat581@gmail.com / `Tuan6768@$`

## 🎯 Tổng quan

Zeni Cloud là **Cloud Operating System hợp nhất** — thay 5 vendor (Vercel + Supabase + Auth0 + OpenAI + Zapier) bằng **1 dashboard, 1 hoá đơn, 1 cloud**. Cấu trúc 6 lớp:

| Lớp | Tên | Chức năng |
|-----|-----|-----------|
| **L1** | Compute | Deploy container Cloud Run, auto-scale 0→N, HTTPS sẵn |
| **L2** | Data | PostgreSQL multi-tenant (per-workspace schema), SQL real |
| **L3** | AI | Vertex AI Gemini 2.5 + Claude + GPT-4 (gọi qua 1 API) |
| **L4** | Automation | Webhook + Slack + Discord, event dispatch real |
| **L5** | Identity | Vault Fernet, JWT auth, sắp có MFA + OAuth |
| **L6** | Web3 | Smart contract Polygon (đang là mock, M5 sẽ real) |

---

## 🚀 Workflow KHÁCH HÀNG: Deploy app lên Zeni Cloud

### Bước 0 — Chuẩn bị Docker image
Khách phải có Docker image của app (web, API, worker, AI agent...). Image phải push lên một trong các registry được phép:

- ✅ `us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/<my-app>:<tag>` (Zeni Artifact Registry)
- ✅ `docker.io/library/<official-image>` (Docker Hub official)
- ✅ `us-docker.pkg.dev/cloudrun/container/hello` (Google sample, dùng để test)

> **Hiện tại workflow để khách push image vào Zeni Artifact Registry:** chưa có UI tự-serve. Tạm thời khách email `caotuanphat581@gmail.com` xin permission, em grant Cloud Build trigger từ GitHub repo của khách.

### Bước 1 — Đăng nhập dashboard
1. Mở https://zenicloud.io
2. Click **Đăng nhập** (góc phải top)
3. Trang `/app` mở ra
4. Nhập email + password được Zeni cấp

### Bước 2 — Chọn workspace
Sidebar trái — chọn workspace tương ứng (anima / holdings / digital / ...). Owner thấy tất cả 8 workspaces; member chỉ thấy WS được phân quyền.

### Bước 3 — Deploy app (L1 Compute)
1. Sidebar → **Compute & Hosting** → **+ Project mới**
2. Wizard 4 bước:
   - **Bước 1:** Chọn loại — `web` / `api` / `worker` / `agent`
   - **Bước 2:** Đặt tên (chỉ chữ thường + số + gạch ngang, 2-48 ký tự)
   - **Bước 3:** Chọn size — `xs` (1 vCPU/512MB) → `l` (4 vCPU/4GB)
   - **Bước 4:** Review & Deploy
3. Click **Tạo & Deploy** → backend gọi Cloud Run API thật → service tạo trong ~30-60s
4. Toast hiện URL Cloud Run dạng `https://zeni-<ws>-<name>-...run.app` → click để xem app live

### Bước 4 — Chạy SQL (L2 Data)
1. Sidebar → **Data Lake & Vector**
2. Mỗi workspace có schema riêng `ws_<workspace>` với bảng sẵn `kv` + `events`
3. Tab SQL → gõ query (hỗ trợ SELECT/INSERT/UPDATE/CREATE TABLE...)
4. Click Run → kết quả real từ Postgres
5. Limit: max 1000 rows return / query, timeout 30s

### Bước 5 — Gọi AI (L3 AI Engine)
1. Sidebar → **AI Engine**
2. Chọn model: `gemini-2.5-pro` / `gemini-2.5-flash` / `claude-opus-4` / `gpt-4o` ...
3. Nhập prompt + context → Run Inference
4. Backend route đến đúng provider API → trả về response
5. Token usage + cost auto tracked vào billing

### Bước 6 — Setup Automation (L4)
1. Sidebar → **Automation** → **+ Connector**
2. Chọn type: `webhook` / `slack` / `discord` / `Zalo OA` / `Shopee` / ...
3. Cho `webhook|slack|discord`: nhập URL config (e.g., Slack incoming webhook)
4. Save
5. Fire event qua API:
   ```
   POST /api/v1/automation/events/fire?ws=anima
   { "source": "shop", "action": "order.paid", "payload": {...} }
   ```
   → backend dispatch real HTTP POST đến tất cả connectors

### Bước 7 — Lưu secrets (L5 Identity)
1. Sidebar → **Identity & Security** → **Secrets**
2. **+ Tạo secret** → name `STRIPE_API_KEY` (uppercase) + value
3. Backend mã hóa Fernet AES-256-GCM, lưu Postgres
4. Reveal lại được nếu role >= Owner; rotate sinh value mới

---

## 🛠️ Workflow QUẢN TRỊ (cho anh CEO)

### Login admin
- Email: `caotuanphat581@gmail.com`
- Pass: `Tuan6768@$`
- Role: `Owner` (full access 8 workspaces)

### Theo dõi
- **Audit log:** `https://zenicloud.io/api/v1/audit?limit=100` — mọi action trong system
- **Billing:** `https://zenicloud.io/api/v1/billing/summary` — chi phí theo workspace + layer
- **Health:** `https://zenicloud.io/health` (uptime check chạy mỗi 1 phút, alert email anh khi fail)
- **Logs Cloud Run:** Console GCP → Cloud Run → zeni-backend → Logs
- **Cloud SQL:** Console GCP → SQL → zeni-cloud-db (DB chính) — query trực tiếp qua gcloud sql connect

### Onboarding khách mới
1. Vào DB → INSERT user mới với password bcrypt + role + workspace assignment
   → hoặc khách tự register qua `POST /api/v1/auth/register` (default role=Developer, ws=digital)
2. Để khách có image registry riêng: tạo subfolder trong `zeni-images` repo Artifact Registry, grant `roles/artifactregistry.writer`
3. Email khách info đăng nhập + URL https://zenicloud.io/app

### Khi khách báo bug
- Anh check audit log lọc theo `actor=<email khách>`
- Cloud Run logs lọc theo workspace label
- Emergency rollback: `gcloud run services update-traffic zeni-backend --to-revisions=zeni-backend-XXXXX-yyy=100`

---

## 📡 API endpoints chính

| Method | Path | Mô tả |
|--------|------|-------|
| POST | `/api/v1/auth/login` | Login → JWT (hoặc `mfa_required` nếu MFA bật) |
| POST | `/api/v1/auth/register` | Đăng ký (Developer mặc định) |
| GET  | `/api/v1/auth/me` | Profile current user |
| POST | `/api/v1/auth/mfa/setup` | **L5: MFA setup → QR + secret** |
| POST | `/api/v1/auth/mfa/verify` | **L5: Enable MFA** |
| POST | `/api/v1/auth/mfa/login` | **L5: Login với pre_token + TOTP** |
| POST | `/api/v1/auth/mfa/disable` | **L5: Tắt MFA** |
| GET  | `/api/v1/workspaces` | List workspaces |
| POST | `/api/v1/projects?ws=<ws>` | **L1: Deploy Cloud Run REAL (async 202)** |
| GET  | `/api/v1/projects?ws=<ws>` | List projects |
| DELETE | `/api/v1/projects/{id}?ws=<ws>` | Xóa project + Cloud Run service |
| GET  | `/api/v1/data/tables?ws=<ws>` | **L2: List tables real (introspection)** |
| POST | `/api/v1/data/query?ws=<ws>` | **L2: Execute SQL real (per-workspace schema)** |
| POST | `/api/v1/ai/complete?ws=<ws>` | **L3: AI inference Vertex AI/Claude/GPT** |
| GET  | `/api/v1/automation/connectors?ws=<ws>` | List connectors |
| POST | `/api/v1/automation/connectors?ws=<ws>` | **L4: Add webhook/slack/discord** |
| POST | `/api/v1/automation/events/fire?ws=<ws>` | **L4: Fire event (real HTTP dispatch)** |
| GET  | `/api/v1/automation/events?ws=<ws>` | List event log |
| GET  | `/api/v1/automation/crons?ws=<ws>` | **L4: List cron jobs** |
| POST | `/api/v1/automation/crons?ws=<ws>` | **L4: Create cron (Cloud Scheduler REAL)** |
| POST | `/api/v1/automation/crons/{name}/run-now?ws=<ws>` | Force-run cron immediately |
| POST | `/api/v1/automation/crons/{name}/pause?ws=<ws>` | Pause cron |
| DELETE | `/api/v1/automation/crons/{name}?ws=<ws>` | Delete cron |
| POST | `/api/v1/identity/secrets?ws=<ws>` | L5: Tạo secret (Fernet vault) |
| POST | `/api/v1/members/invite` | **L5: Mời member + gửi email thật** |
| GET  | `/api/v1/web3/zeni-stack` | **L6: Live read 3 Zeni contracts trên Polygon** |
| GET  | `/api/v1/web3/chains` | **L6: Live status các chain (block, gas)** |
| POST | `/api/v1/web3/read` | **L6: Real read ERC20/ERC721/native** |
| POST | `/api/v1/web3/build-transfer?ws=<ws>` | L6: Build templated transfer tx |
| GET  | `/api/v1/web3/tx/{chain}/{tx_hash}` | Lookup tx receipt real |
| POST | `/api/v1/waitlist/signup` | Public landing waitlist |
| GET  | `/api/v1/audit` | Audit log toàn hệ |
| GET  | `/api/v1/billing/summary` | Billing per workspace + layer |
| GET  | `/api/v1/docs` | Swagger UI full API docs |

---

## ⏰ Lộ trình hoàn thiện

| Milestone | Trạng thái | Ngày |
|-----------|-----------|------|
| M1 — L1 Compute REAL deploy Cloud Run | ✅ DONE | 2026-04-24 |
| M2 — L2 Data REAL multi-tenant SQL | ✅ DONE | 2026-04-25 |
| M3 — L4 Automation REAL webhook dispatch | ✅ DONE | 2026-04-25 |
| Landing page + Compass UI | ✅ DONE | 2026-04-25 |
| M4 — L5 Identity (MFA + SMTP REAL) | ✅ DONE | 2026-04-25 |
| M4.OAuth — Google + GitHub login | ⏳ Chờ anh tạo OAuth Client | — |
| M5 — L6 Web3 REAL Polygon RPC | ✅ DONE (read-only) | 2026-04-25 |
| L4 Cron Scheduler — Cloud Scheduler integration | ✅ DONE | 2026-04-25 |
| M6 — Frontend fetch real data | ✅ DONE (zeni-realdata.js adapter) | 2026-04-25 |
| M7 — Full E2E + security review + GA | 🔄 đang làm | 2026-04-26 |
| L6.write — Custodial wallet KMS signing | ⏳ Future | TBD |
| DB migration tool — Supabase → Cloud SQL | ⏳ CLI guide có sẵn | TBD |

---

## 💡 Khi khách hỏi "deploy được chưa?"

**Trả lời: ĐƯỢC, ngay từ bây giờ** cho L1 Compute (deploy container) + L2 Data (SQL) + L3 AI + L4 Automation. Còn L5 OAuth login social + L6 Web3 sẽ hoàn thiện trong tuần.

---

## 🔐 Bảo mật đã hardened

- HTTPS only, HTTP redirect 301
- Google-managed SSL cert (auto-renew)
- Cloud Armor WAF: rate limit 60/min/IP, block SQLi/XSS/LFI/RCE
- JWT access 1h + refresh 30d rotating, revoke-able
- Bcrypt password hash cost 12
- Fernet AES-256-GCM cho secret vault
- Per-workspace schema isolation
- Image allowlist (chỉ Artifact Registry zeni-cloud-core, GCR Google samples)
- IAM least-privilege (SA chỉ có roles đúng cần)
- Audit log mọi action có actor + workspace + severity
- Uptime check 1 phút + alert email
