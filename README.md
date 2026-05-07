# Zeni Cloud Core

Nền tảng cloud thống nhất cho hệ sinh thái Zeni — thay thế chi phí và phân mảnh từ Vercel / Supabase / Auth0 / OpenAI / Zapier bằng **1 database · 1 auth · 6 lớp hạ tầng**.

## Kiến trúc

```
┌─────────────────────────────────────────────────────────┐
│  Frontend · HTML SPA (cosmic theme, 6-layer dashboards) │
└─────────────────────┬───────────────────────────────────┘
                      │ /api/v1/*
┌─────────────────────┴───────────────────────────────────┐
│  FastAPI backend (Python 3.12) — 11 entities, 6 layers  │
│  Auth JWT · Vault Fernet · LLM Gateway · Audit log      │
└─────┬─────────────┬─────────────┬────────────┬──────────┘
      │             │             │            │
   PostgreSQL    Redis       GCP services    Anthropic/OpenAI
   (primary)   (cache/     (Vertex AI +      (Claude/GPT)
                queue)      Secret Mgr +
                            Cloud Storage)
```

### 6 lớp (từ HTML spec)

| Layer | Tên | Vai trò | Endpoint mẫu |
|---|---|---|---|
| L1 | Compute | Deploy service / Cloud Run / container | `POST /projects` |
| L2 | Data | SQL + Vector + Object storage | `POST /data/query` |
| L3 | AI | Model Garden · Agent runtime · LLM Gateway | `POST /ai/complete` |
| L4 | Automation | Event bus · 120+ connectors · workflows | `POST /automation/events/fire` |
| L5 | Identity | SSO · MFA · Vault (Fernet + GCP Secret Manager) | `POST /identity/secrets` |
| L6 | Web3 | Wallet-as-a-Service · contract deploy | `POST /web3/execute` |

### 8 workspaces (entities)

`holdings` (HQ) · `anima` (wellness) · `zeniipo` (IPO SaaS) · `digital` (horizontal SaaS) · `wellkoc` (social commerce) · `nexbuild` (constructech) · `bthome` (design) · `capital` (finance)

### 4 roles

`Owner` (all) · `Admin` (single WS) · `Developer` · `Viewer`

## Tech stack

- **Backend:** FastAPI 0.115 · SQLAlchemy 2.0 async · asyncpg · Pydantic v2
- **Auth:** JWT (access 1h + refresh 30d, rotating) · bcrypt password
- **Vault:** cryptography.Fernet (local) / GCP Secret Manager (prod)
- **DB:** PostgreSQL 16 (+ ready for pgvector)
- **Cache:** Redis 7
- **LLM:** Anthropic Claude · OpenAI GPT · Google Gemini 2.5 qua Vertex AI
- **GCP:** Vertex AI · Secret Manager · Cloud Storage
- **Frontend:** HTML + CSS + vanilla JS (giữ từ design spec, wired qua `/static/zeni-api.js`)

## Cấu trúc thư mục

```
Zeni-Cloud-Core/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app + lifespan + seed admin
│   │   ├── core/
│   │   │   ├── config.py            # Settings via pydantic-settings
│   │   │   ├── security.py          # JWT, bcrypt
│   │   │   ├── vault.py             # Fernet encryption
│   │   │   └── deps.py              # get_current_user, RBAC
│   │   ├── db/
│   │   │   ├── base.py              # Async engine, session
│   │   │   └── models.py            # 11 tables
│   │   ├── schemas/                 # Pydantic request/response
│   │   ├── api/                     # 11 routers (auth, workspaces, projects, data, ai, automation, identity, web3, members, audit, billing, gcp)
│   │   └── services/
│   │       ├── llm_gateway.py       # Unified Claude/OpenAI/Gemini (Vertex AI)
│   │       ├── gcp.py               # Secret Manager + Cloud Storage wrappers
│   │       └── audit.py             # Audit + billing writers
│   ├── migrations/001_init.sql      # Schema + seed 8 workspaces
│   ├── requirements.txt
│   ├── Dockerfile
│   └── gcp-sa-key.json              # Service account (.gitignored)
├── frontend/
│   ├── index.html                   # Spec UI (cosmic theme, 4726 lines)
│   └── zeni-api.js                  # API client + patch doLogin/doLogout
├── docker-compose.yml
├── .env.example
└── .gitignore
```

## Chạy local

### 1. Chuẩn bị `.env`

```bash
cp .env.example .env
# Edit .env: set JWT_SECRET, VAULT_KEY, ADMIN_PASSWORD
```

### 2. Generate secrets

```bash
# VAULT_KEY (Fernet 32-byte)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# JWT_SECRET (hex 32)
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Boot full stack

```bash
docker compose up -d
```

→ http://localhost:8080 — đăng nhập bằng `ADMIN_EMAIL` / `ADMIN_PASSWORD` trong `.env`.
→ http://localhost:8080/docs — OpenAPI docs (Swagger UI).

### 4. Dev không Docker (chỉ Postgres qua Docker)

```bash
docker compose up -d postgres redis
cd backend
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate    # Linux/Mac
pip install -r requirements.txt
export DATABASE_URL="postgresql+asyncpg://zeni:zeni_dev_pass@localhost:5432/zeni_cloud"
uvicorn app.main:app --reload --port 8080 --host 0.0.0.0
```

Mount folder `frontend/` sẽ auto serve qua `/` và `/static/`.

## Google Cloud (GCP) integration

Project mặc định: **`zeni-cloud-core`** (tùy chỉnh qua `GCP_PROJECT_ID`).

### APIs đã enable

- `generativelanguage.googleapis.com` — Gemini API (fallback path qua API key)
- `aiplatform.googleapis.com` — Vertex AI (path chính, service account auth)
- `secretmanager.googleapis.com` — Secret Manager
- `storage.googleapis.com` — Cloud Storage

### Service Account

- Email: `zeni-cloud-core-sa@zeni-cloud-core.iam.gserviceaccount.com`
- Roles: `Vertex AI User` · `Secret Manager Admin` · `Storage Object Admin`
- Key file: `backend/gcp-sa-key.json` (đã gitignore — KHÔNG commit)

### Models Gemini đã verify qua Vertex AI

| Model | Trạng thái | Giá (USD/1M) | Dùng khi |
|---|---|---|---|
| `gemini-2.5-pro` | ✅ GA | $1.25 / $10.00 | Reasoning, IPO report, pháp lý |
| `gemini-2.5-flash` | ✅ GA | $0.30 / $2.50 | Default chat, copy, support |
| `gemini-2.5-flash-lite` | ✅ GA | $0.10 / $0.40 | Bulk caption, simple task |
| `gemini-3.x` | ❌ Chưa GA | — | (preview sẽ thêm khi có) |

> **Lưu ý Gemini 2.5**: thinking tokens ngầm ăn vào `max_tokens`. Default đặt 2048 để output không bị cắt. Muốn tắt thinking dùng `gemini-2.5-flash-lite`.

### Rotate / xóa SA key

```bash
gcloud iam service-accounts keys delete KEY_ID \
  --iam-account=zeni-cloud-core-sa@zeni-cloud-core.iam.gserviceaccount.com \
  --project=zeni-cloud-core
```

Hoặc vào Console: IAM → Service Accounts → `zeni-cloud-core-sa` → Keys.

## API chính (dry list)

```
POST   /api/v1/auth/login              email + password → {access, refresh}
POST   /api/v1/auth/refresh            rotate session
GET    /api/v1/auth/me                 current user + workspaces
GET    /api/v1/workspaces
POST   /api/v1/projects?ws=X           deploy service L1
POST   /api/v1/data/query?ws=X         SQL/Vector/Object query L2
POST   /api/v1/ai/complete?ws=X        Gemini/Claude/GPT inference L3
POST   /api/v1/automation/events/fire  event → action L4
POST   /api/v1/identity/secrets?ws=X   vault create/rotate L5
POST   /api/v1/web3/execute?ws=X       deploy contract / mint / transfer L6
GET    /api/v1/members
POST   /api/v1/members/invite
GET    /api/v1/audit                   immutable log
GET    /api/v1/billing/summary         cost aggregation
GET    /api/v1/gcp/status              GCP readiness check
GET    /api/v1/gcp/storage/buckets
```

Đầy đủ ở `/docs` (Swagger UI) và `/redoc`.

## Admin bootstrap

Khi backend boot lần đầu, nó tạo sẵn user `Owner` từ env:

```
ADMIN_EMAIL=ceo@zeni-holdings.vn
ADMIN_PASSWORD=ChangeMeImmediately123!
ADMIN_NAME=CEO Zeni
```

Admin này có quyền Owner → truy cập tất cả 8 workspaces. **Đổi password ngay sau khi boot lần đầu.**

## Bảo mật

- JWT access token 1h · refresh 30d rotating, revoke-able
- Password bcrypt (`passlib`)
- Secret vault: Fernet AES-256-GCM (dev) / GCP Secret Manager (prod)
- CORS allowlist trong `.env`
- CSP strict (`connect-src 'self'`)
- Audit log immutable, mọi action có actor + severity
- SA JSON key và `.env` trong `.gitignore`
- Rate limit: sẽ thêm ở bước hardening (Phase 2)

## Status (tới commit này)

- ✅ Schema + seed 8 workspaces + 5 DB + 5 agents + 8 connectors + 5 contracts
- ✅ Auth JWT full (login, refresh, logout, me, register)
- ✅ Projects CRUD (L1)
- ✅ Data query mock + DB list (L2)
- ✅ AI inference thật qua Vertex AI Gemini 2.5 + Claude/OpenAI
- ✅ Automation connectors + fire event (L4)
- ✅ Secrets CRUD + rotate + reveal (L5, Fernet)
- ✅ Web3 deploy/execute mock + contracts CRUD (L6)
- ✅ Members invite/accept
- ✅ Audit log + billing summary
- ✅ GCP: Secret Manager + Cloud Storage routers
- ✅ Frontend HTML + `zeni-api.js` patch login
- ⏳ Rate limit, CSRF, email SMTP cho invite
- ⏳ Real Cloud Run deploy (L1 hiện là mock)
- ⏳ Real Web3 RPC (L6 hiện là mock)

## License

Proprietary · © Zeni Holdings
