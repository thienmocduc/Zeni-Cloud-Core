# 🚀 SPRINT A2 — 5h FULL CYCLE (2026-04-29)

> **Mục tiêu**: Bổ sung 6 modules còn thiếu để ZeniCloud cover 100% LegalRadar use case + multi-entity billing + UI thân thiện.
> **Strategy**: 6 streams song song, mỗi stream code 1 module độc lập, chairman integrate + deploy.

---

## 📐 SCOPE & API CONTRACT (ràng buộc cứng — agents code dựa vào đây)

### Stream A1 — Vector DB (pgvector)
**Files** (CHỈ tạo, không sửa file existing):
- `backend/migrations/015_vector_search.sql`
- `backend/app/services/vector_search.py`
- `backend/app/api/vector.py`

**Endpoints**:
- `POST /api/v1/vector/collections?ws=` body `{name, dim, metric}` → tạo collection
- `GET /api/v1/vector/collections?ws=` → list
- `POST /api/v1/vector/{name}/upsert?ws=` body `{points: [{id, vector, metadata}]}` → bulk upsert
- `POST /api/v1/vector/{name}/search?ws=` body `{vector, k, filter?}` → top-k với cosine
- `DELETE /api/v1/vector/{name}?ws=` → drop collection

**DB schema** (mỗi workspace có schema riêng, vector trong schema của ws):
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE public.vector_collections (
  id BIGSERIAL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  dim INT NOT NULL,
  metric TEXT NOT NULL DEFAULT 'cosine',
  row_count BIGINT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (workspace_id, name)
);

-- Per collection table created dynamically: ws_<workspace_id>.vec_<name>(id, vector, metadata, created_at)
```

**Pricing**: $0.10/1K vectors stored/tháng + $0.05/1K search ops.
**Audit**: `vector.upsert`, `vector.search`, `vector.delete`.

---

### Stream A2 — Cache + Queue (Postgres-based)
**Files**:
- `backend/migrations/016_cache_queue.sql`
- `backend/app/services/cache.py`
- `backend/app/services/queue.py`
- `backend/app/api/cache.py`
- `backend/app/api/queue.py`

**Endpoints (Cache)**:
- `POST /api/v1/cache/{key}?ws=` body `{value, ttl_seconds?}` → set
- `GET /api/v1/cache/{key}?ws=` → get value (404 if expired/missing)
- `DELETE /api/v1/cache/{key}?ws=` → delete
- `GET /api/v1/cache?ws=&prefix=` → list keys

**Endpoints (Queue)**:
- `POST /api/v1/queue/{name}/push?ws=` body `{payload, delay_seconds?}` → enqueue
- `POST /api/v1/queue/{name}/pull?ws=` body `{lease_seconds?: 60}` → pull next + lease
- `POST /api/v1/queue/{name}/ack?ws=` body `{job_id, success: bool, error?}` → ack
- `GET /api/v1/queue/{name}/stats?ws=` → pending, in_flight, dead_letter, completed

**DB schema**:
```sql
CREATE UNLOGGED TABLE public.kv_cache (
  workspace_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value JSONB NOT NULL,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (workspace_id, key)
);
CREATE INDEX idx_kv_cache_expires ON public.kv_cache(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE public.queue_jobs (
  id BIGSERIAL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  queue_name TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|leased|completed|failed|dead_letter
  attempts INT DEFAULT 0,
  max_attempts INT DEFAULT 3,
  available_at TIMESTAMPTZ DEFAULT NOW(),
  leased_until TIMESTAMPTZ,
  lease_token UUID,
  last_error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX idx_queue_pull ON public.queue_jobs(workspace_id, queue_name, status, available_at);
```

**Pull pattern**: `SELECT ... WHERE status='pending' AND available_at <= NOW() ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1`.

**Pricing**: free up to 10K ops/tháng.
**Audit**: chỉ log queue operations (cache không log để giảm noise).

---

### Stream A3 — OCR + Translation
**Files**:
- `backend/app/services/ocr.py`
- `backend/app/services/translate.py`
- `backend/app/api/ocr.py`
- `backend/app/api/translate.py`

**Endpoints**:
- `POST /api/v1/ocr/image?ws=` body `{gcs_uri | image_url | image_base64}` → text
- `POST /api/v1/ocr/pdf?ws=` body `{gcs_uri}` → list of page texts (sync, max 5 pages; nếu nhiều hơn → 400)
- `POST /api/v1/translate?ws=` body `{text, target_lang: "vi"|"en"|..., source_lang?}` → `{translated_text, source_lang_detected, char_count}`

**Backend**: Cloud Vision API + Cloud Translation API qua REST (httpx + SA token).
**Pricing**:
- OCR image: $1.50/1K page
- Translation: $20/1M chars

**Audit**: `ai.ocr`, `ai.translate`.
**Billing**: layer="L3", action="ai.ocr" hoặc "ai.translate".

---

### Stream A4 — SMS + Slack
**Files**:
- `backend/app/services/sms.py`
- `backend/app/services/slack.py`
- `backend/app/api/sms.py`
- `backend/app/api/slack.py`

**Endpoints**:
- `POST /api/v1/sms/send?ws=` body `{to, text}` → routes:
  - `to` bắt đầu `+84` hoặc `0` → Stringee (VN, $0.005)
  - Khác → Twilio (international, $0.05)
  - Nếu credentials thiếu → 503 "SMS provider not configured"
- `POST /api/v1/slack/webhook?ws=` body `{webhook_url, text, blocks?, attachments?}` → fire and forget
- `POST /api/v1/slack/post?ws=` body `{token, channel, text, blocks?}` → Slack Bot API

**Env required** (sẽ stub if missing):
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`
- `STRINGEE_API_KEY`, `STRINGEE_FROM`

**Audit**: `notify.sms`, `notify.slack`.

---

### Stream A5 — Multi-Entity Billing
**Files**:
- `backend/migrations/017_multi_entity_billing.sql`
- `backend/app/services/multi_entity_billing.py`
- `backend/app/api/legal_entities.py`

**Endpoints**:
- `POST /api/v1/legal-entities` (admin only) body `{id, name, parent_id?, bank_account?, tax_id?, is_master?}` → create
- `GET /api/v1/legal-entities` → list (admin or member)
- `PATCH /api/v1/legal-entities/{id}` → update
- `POST /api/v1/billing/charge-tagged?ws=` body `{amount_vnd, legal_entity_id, action, metadata?}` → charge wallet + tag revenue
- `GET /api/v1/billing/revenue-by-entity?period=YYYY-MM` → breakdown
- `POST /api/v1/billing/intercompany/run` (admin) body `{period_start, period_end}` → tổng hợp + tạo transfer records

**DB schema**:
```sql
CREATE TABLE public.legal_entities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_id TEXT REFERENCES public.legal_entities(id),
  bank_account TEXT,
  tax_id TEXT,
  is_master BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.billing_transactions
  ADD COLUMN IF NOT EXISTS legal_entity_id TEXT REFERENCES public.legal_entities(id);

CREATE TABLE public.intercompany_transfers (
  id BIGSERIAL PRIMARY KEY,
  from_entity TEXT NOT NULL REFERENCES public.legal_entities(id),
  to_entity TEXT NOT NULL REFERENCES public.legal_entities(id),
  amount_vnd NUMERIC(18,2) NOT NULL,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  external_ref TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  processed_at TIMESTAMPTZ
);

INSERT INTO public.legal_entities(id, name, is_master) VALUES
  ('zeni_holdings', 'Zeni Holdings', TRUE),
  ('anima_care', 'ANIMA Care Co.', FALSE),
  ('zeni_cloud', 'Zeni Cloud Co.', FALSE),
  ('ios_portal', 'IOS Portal Co.', FALSE),
  ('zeni_chain', 'Zeni Chain Co.', FALSE),
  ('zeniipo', 'Zeniipo Co.', FALSE)
ON CONFLICT DO NOTHING;
```

**Audit**: `billing.legal_entity.*`, `billing.intercompany.*`.

---

### Stream A6 — UI/UX (Frontend Tabs Mới + Polish)
**Files** (CHỈ tạo file mới hoặc Edit thêm function, KHÔNG sửa logic existing):
- `frontend/zeni-app-views.js` (Edit thêm — render functions cho 6 tabs mới)
- `frontend/zeni-realdata.js` (Edit thêm — fetch APIs mới)
- `frontend/zeni-ext-modules.js` (NEW — wire up vector/cache/queue/ocr/translate/sms/slack/legal-entities tabs)

**Tabs mới** (tích hợp vào sidebar `index.html`):
1. **Vector DB** (icon database+sparkle): list collections, create, search playground
2. **Queue & Cache**: queue stats, push job UI, cache key-value editor
3. **OCR & Translate**: upload image/PDF → OCR; text input → translate
4. **SMS & Slack**: send SMS form (preview cost), Slack webhook tester
5. **Multi-Entity** (admin only): list legal entities, revenue breakdown chart, intercompany transfers
6. **Billing nâng cấp**: thêm tab "Revenue by Entity" trong existing Billing dashboard

**Design principles**:
- Kế thừa style hiện có (dark theme, rounded cards, kebab-case CSS)
- Mobile-first responsive (375px base)
- Form input có placeholder + helper text Vietnamese
- Cost preview trước khi gọi API (vd: SMS "Sẽ tốn 100đ — [Gửi]")
- Loading state + error toast Vietnamese

---

## 🔒 SECURITY CHECKLIST (mỗi stream phải pass)
1. `require_workspace_access(ws, me)` đầu mỗi endpoint
2. Pydantic validation cho mọi body
3. Named SQL params (`text(...)` + dict)
4. `audit_push` cho mọi state-changing action
5. `billing_push` cho mọi action có cost
6. PAT scope check (vd: `vector` scope cho /vector/*, `notify` cho /sms /slack)
7. Rate limit qua `@rate_limit` (nếu có decorator) hoặc skip cho MVP
8. Error messages tiếng Việt, không leak stack trace

---

## 🚧 INTEGRATION RULES (chairman làm)
- KHÔNG agent nào sửa `main.py` — chairman thêm router include sau
- KHÔNG agent nào sửa `models.py` — schema mới qua raw SQL migration
- KHÔNG agent nào sửa migrations existing — chỉ tạo mới
- Mỗi agent đọc DNA + scope của mình + report code path đã tạo

---

## ⏰ TIMELINE
```
0:00-0:15  Plan + APIs enable (chairman)             [DONE]
0:15-3:00  Spawn 6 agents code song song (2h45)
3:00-3:30  Chairman integrate routers + verify
3:30-4:00  Build image v47 + apply migrations
4:00-4:30  Deploy v47 + smoke test
4:30-5:00  E2E + bug fix + report
```

---

**Hết. Mỗi agent đọc scope của mình + ZENI_AGENT_DNA.md, code đúng từ commit đầu tiên.**
