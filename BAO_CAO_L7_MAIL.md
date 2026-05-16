# BÁO CÁO L7 — Zeni Mail Hosting (per-domain mail service)

**Mục tiêu:** Cấp dịch vụ mail @customdomain.com cho khách Zeni Cloud thu phí, đồng thời chairman dùng nội bộ cho 10+ domain mà KHÔNG phải trả $10-100/tháng cho Google Workspace/Zoho.

**Cost Zeni: $0.27/domain/tháng → thu khách $2-25 → margin 86%+**

**File:** `BAO_CAO_L7_MAIL.md`
**Author:** Zeni CTO (Claude Opus 4.7)
**Date:** 2026-05-16

---

## 1. Vấn đề hiện tại

| Provider | Cost cho 10 domain × 5 mailbox |
|---|---|
| Google Workspace | $300/tháng ($6/user × 50 user) |
| Microsoft 365 | $250/tháng |
| Zoho Mail | $50/tháng ($1/user × 50) |
| Cloudflare Email Routing | Free (chỉ FORWARD, không phải mail box thật) |
| **Chairman hiện trả** | **~$10-30/tháng** (1-3$/domain qua provider thô) |

**Pain:** Mỗi mới mở 1 domain mất thêm $1-30/tháng → lãng phí. Cộng dồn 10+ domain → tốn nhiều.

**Cơ hội:** Build feature này → vừa giảm chi nội bộ, vừa cấp thành sản phẩm thu khách.

---

## 2. Architecture L7 Mail

```
┌──────────────────────────────────────────────────────────────┐
│ Customer DNS Records (1 lần setup):                          │
│   MX     vietcontech.com  →  10 mx.zenicloud.io              │
│   SPF    "v=spf1 include:zenicloud.io ~all"                  │
│   DKIM   zeni._domainkey.vietcontech.com → (Zeni gen keypair)│
│   DMARC  _dmarc.vietcontech.com → "v=DMARC1; p=quarantine"   │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ Inbound mail (port 25 SMTP)
┌──────────────────────────────────────────────────────────────┐
│ Cloud Run service: zeni-mail-mx                              │
│   ├─ Postfix listen :25                                      │
│   ├─ rspamd anti-spam + DKIM/SPF/DMARC verify                │
│   ├─ Reject if spam score > 7.0                              │
│   └─ Accept → POST /api/v1/mail/inbox/receive (webhook)      │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ Parse + store
┌──────────────────────────────────────────────────────────────┐
│ Cloud SQL Postgres (existing zenicloud-prod-db):             │
│   ├─ mail_domains       (domain registration per workspace)  │
│   ├─ mail_mailboxes     (hello@vietcontech.com etc.)         │
│   ├─ mail_messages      (parsed MIME, body, headers JSONB)   │
│   ├─ mail_folders       (Inbox/Sent/Drafts/Trash/Custom)     │
│   ├─ mail_attachments   (FK to GCS object)                   │
│   └─ mail_filters       (rules: from → folder)               │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ Attachments
┌──────────────────────────────────────────────────────────────┐
│ GCS bucket zeni-mail-attachments/                            │
│   {ws_id}/{message_id}/{attachment_filename}                 │
│   Lifecycle: Standard 90d → Coldline 1y → Archive 3y         │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ Webmail UI
┌──────────────────────────────────────────────────────────────┐
│ Zeni Frontend Webmail (React component)                      │
│   /mail UI: Inbox/Sent/Drafts/Trash + compose + search       │
│   IMAP-style API: GET /api/v1/mail/messages?folder=inbox     │
│   SMTP send:      POST /api/v1/mail/messages/send            │
└────────────────────┬─────────────────────────────────────────┘
                     ↓ Outbound
┌──────────────────────────────────────────────────────────────┐
│ Send relay:                                                  │
│  Phase 1 (launch):  Amazon SES SMTP relay $0.10/1K           │
│                     ses-smtp-user qua API key Zeni master    │
│  Phase 2 (matured): Self-host Postfix outbound + IP warming │
│                     (30 ngày warm IP → deliverability tốt)   │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Database schema (migration 070-074)

```sql
-- 070_mail_domains.sql
CREATE TABLE mail_domains (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    VARCHAR(64) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    domain          VARCHAR(255) NOT NULL,
    status          VARCHAR(32) DEFAULT 'pending_dns',  -- pending_dns|active|suspended
    dkim_selector   VARCHAR(32) DEFAULT 'zeni',
    dkim_private_key TEXT NOT NULL,        -- encrypted via Vault
    dkim_public_key TEXT NOT NULL,
    spf_verified    BOOLEAN DEFAULT FALSE,
    mx_verified     BOOLEAN DEFAULT FALSE,
    dmarc_policy    VARCHAR(16) DEFAULT 'quarantine',
    plan            VARCHAR(32) DEFAULT 'starter',  -- starter|pro|business|enterprise
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (domain)
);
CREATE INDEX idx_mail_domains_ws ON mail_domains(workspace_id);

-- 071_mail_mailboxes.sql
CREATE TABLE mail_mailboxes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id       UUID NOT NULL REFERENCES mail_domains(id) ON DELETE CASCADE,
    username        VARCHAR(64) NOT NULL,       -- "hello" cho hello@vietcontech.com
    password_hash   TEXT NOT NULL,              -- bcrypt
    display_name    VARCHAR(128),
    quota_mb        INT DEFAULT 5120,           -- 5GB default
    used_mb         INT DEFAULT 0,
    is_catchall     BOOLEAN DEFAULT FALSE,
    aliases         TEXT[],                     -- VD: ["info", "support"]
    forward_to      TEXT,                       -- forward all mail to external
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (domain_id, username)
);
CREATE INDEX idx_mail_mailboxes_domain ON mail_mailboxes(domain_id);

-- 072_mail_messages.sql
CREATE TABLE mail_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id      UUID NOT NULL REFERENCES mail_mailboxes(id) ON DELETE CASCADE,
    folder          VARCHAR(32) DEFAULT 'inbox',  -- inbox|sent|drafts|trash|<custom>
    message_id      VARCHAR(255) UNIQUE,           -- RFC 5322 Message-ID
    thread_id       VARCHAR(64),                   -- conversation grouping
    from_addr       VARCHAR(255) NOT NULL,
    to_addrs        TEXT[] NOT NULL,
    cc_addrs        TEXT[],
    bcc_addrs       TEXT[],
    subject         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    headers         JSONB,                         -- raw headers parsed
    raw_mime_gcs    TEXT,                          -- GCS path to raw .eml
    is_read         BOOLEAN DEFAULT FALSE,
    is_starred      BOOLEAN DEFAULT FALSE,
    spam_score      FLOAT,
    received_at     TIMESTAMPTZ DEFAULT NOW(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX idx_mail_messages_mailbox_folder ON mail_messages(mailbox_id, folder, received_at DESC);
CREATE INDEX idx_mail_messages_thread ON mail_messages(thread_id);

-- 073_mail_attachments.sql
CREATE TABLE mail_attachments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      UUID NOT NULL REFERENCES mail_messages(id) ON DELETE CASCADE,
    filename        VARCHAR(255),
    content_type    VARCHAR(128),
    size_bytes      BIGINT,
    gcs_path        TEXT NOT NULL,                -- gs://zeni-mail-attachments/...
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_mail_attachments_msg ON mail_attachments(message_id);

-- 074_mail_filters.sql
CREATE TABLE mail_filters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id      UUID NOT NULL REFERENCES mail_mailboxes(id) ON DELETE CASCADE,
    name            VARCHAR(64),
    priority        INT DEFAULT 100,
    conditions      JSONB NOT NULL,    -- {"from": "*@spam.com"}
    actions         JSONB NOT NULL,    -- {"move_to": "trash"} hoặc {"star": true}
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_mail_filters_mailbox ON mail_filters(mailbox_id);
```

**Multi-tenant isolation:** Mọi query đều JOIN qua `mail_domains.workspace_id` → RLS bằng workspace_id, không cross-tenant.

---

## 4. API contract `/api/v1/mail/...`

### 4.1 Domain management

```
POST   /api/v1/mail/domains?ws={ws}
       body: {"domain": "vietcontech.com", "plan": "starter"}
       → 201 {dkim_record, spf_record, mx_record, dmarc_record}

GET    /api/v1/mail/domains?ws={ws}
       → list domains in workspace

POST   /api/v1/mail/domains/{id}/verify-dns
       → check MX/SPF/DKIM/DMARC, update verified flags

DELETE /api/v1/mail/domains/{id}?ws={ws}
       → soft delete (suspend MX)
```

### 4.2 Mailbox CRUD

```
POST   /api/v1/mail/mailboxes?ws={ws}
       body: {"domain_id": "...", "username": "hello", "password": "...",
              "display_name": "Viet Contech", "quota_mb": 5120}

GET    /api/v1/mail/mailboxes?ws={ws}&domain={domain}
PATCH  /api/v1/mail/mailboxes/{id}
DELETE /api/v1/mail/mailboxes/{id}
```

### 4.3 Messages (IMAP-style)

```
GET    /api/v1/mail/messages?ws={ws}&mailbox={id}&folder=inbox&limit=50
GET    /api/v1/mail/messages/{id}                  → full message + attachments
PATCH  /api/v1/mail/messages/{id}                  → mark read/starred/move
DELETE /api/v1/mail/messages/{id}                  → soft delete (→ trash)
POST   /api/v1/mail/messages/send                  → compose + send via SES
       body: {"from_mailbox_id":"...", "to":["..."], "subject":"...", "body":"..."}
```

### 4.4 Inbox webhook (internal, called by Postfix container)

```
POST   /api/v1/mail/inbox/receive                  (internal Auth: SA JWT)
       body: {"recipient":"hello@vietcontech.com",
              "raw_mime_base64":"...",
              "spam_score": 1.2}
       → parse MIME → store DB + GCS attachments
```

---

## 5. Pricing model

| Plan | Giá/domain/tháng | Mailbox | Email/tháng | Storage | Cost Zeni | Margin |
|---|---|---|---|---|---|---|
| **Starter** | $2 | 5 | 1K | 5GB | $0.27 | 86% |
| **Pro** | $5 | 20 | 10K | 50GB | $0.70 | 86% |
| **Business** | $10 | unlimited | 50K | 200GB | $1.40 | 86% |
| **Enterprise** | $25 | unlimited | 250K | 1TB + archive + calendar | $3.50 | 86% |

**Add-on:**
- Extra storage: $0.50/10GB/tháng
- Extra emails: $0.10/1K
- Catchall mailbox: $1/domain/tháng
- Email archive 7 năm: $5/domain/tháng

### So sánh competitors

| Provider | 10 domain × 5 mailbox/tháng | 1 năm |
|---|---|---|
| Google Workspace ($6/user) | $300 | $3,600 |
| Microsoft 365 ($5/user) | $250 | $3,000 |
| Zoho Mail ($1/user) | $50 | $600 |
| **Zeni Mail Starter** ($2/domain) | **$20** | **$240** |
| Tiết kiệm vs Google | **$280/tháng (93%)** | **$3,360/năm** |

---

## 6. Cost breakdown chi tiết (Starter $2/domain/tháng)

| Resource | Spec | Cost/tháng | Note |
|---|---|---|---|
| Cloud Run zeni-mail-mx (shared) | 2 vCPU × 4GB, autoscale 0-10 | $0.05 | Cost share across 100+ domains |
| Cloud SQL row + storage | 5GB Postgres rows | $0.10 | $0.02/GB pgvector tier |
| GCS attachments | 5GB Standard | $0.02 | $0.004/GB Coldline auto |
| SES outbound | 1K emails × $0.10/1K | $0.10 | Or self-host Phase 2 → $0.01 |
| Bandwidth egress | ~500MB | ~$0 | Intra-region free |
| **Tổng cost Zeni** | | **$0.27** | |
| **Revenue Zeni** | | **$2.00** | |
| **Margin** | | **$1.73 (86%)** | |

---

## 7. Implementation timeline (6 tuần)

### Tuần 1: Backend foundation
- Migration SQL 070-074 (5 tables)
- API endpoint `/api/v1/mail/domains` + `/api/v1/mail/mailboxes` (CRUD)
- Pydantic schemas + tests
- **Output:** API REST ready, no MX yet

### Tuần 2: MX receive infrastructure
- Postfix Cloud Run image (Dockerfile + cloudbuild)
- rspamd integration (anti-spam)
- DKIM keypair auto-generation (per domain)
- POST /api/v1/mail/inbox/receive webhook
- **Output:** Mail từ Gmail gửi đến hello@test.zenicloud.io → store DB OK

### Tuần 3: Send outbound
- Amazon SES SMTP relay integration (1 account master)
- POST /api/v1/mail/messages/send endpoint
- SPF/DKIM signing on outbound
- **Output:** Send từ hello@test.zenicloud.io → Gmail receive OK

### Tuần 4: Webmail UI
- React component `<ZeniWebmail />` trong Zeni frontend
- Inbox list + read + compose + reply
- Search + folder management
- **Output:** /mail UI usable trên zenicloud.io

### Tuần 5: Billing + onboarding
- DNS auto-setup wizard (show records cho khách copy)
- Verify DNS button → check MX/SPF/DKIM/DMARC live
- Billing wire (layer="mail", action="message.send" mỗi email)
- Pricing live trên zenicloud.io/pricing
- **Output:** Khách self-service đăng ký được

### Tuần 6: Beta test + launch
- 3 domain chairman test (clawwits.com, nexbuild.holdings, makewits.com)
- 2 khách beta (Viet Contech + 1 khách khác)
- Fix bugs từ beta feedback
- Production launch + announce
- **Output:** Mail layer LIVE, có revenue

---

## 8. Risk + mitigation

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| IP reputation bị block (outbound spam) | Cao | Cao (deliverability) | Phase 1 dùng SES (Amazon đã warm IP), Phase 2 mới self-host |
| Customer abuse spam | Trung | Cao (block toàn IP Zeni) | Rate limit 1K mail/giờ/mailbox, AUP review |
| DMARC reject hợp lệ | Trung | Trung | DMARC `p=quarantine` thay vì `reject` ban đầu |
| Postfix Cloud Run cold start | Thấp | Thấp | min-instances=1 (chi phí thêm $5/tháng) |
| Cloud SQL row limit (millions emails) | Thấp | Trung | Partitioning by mail_messages.received_at YEAR_MONTH |

---

## 9. Compliance + privacy

- **GDPR/CCPA:** Email content lưu workspace_id isolated; delete domain → cascade delete tất cả messages
- **Data residency:** GCS bucket region asia-southeast1 cho khách VN, us-central1 cho khách US
- **Encryption at rest:** Cloud SQL + GCS đều mã hóa default
- **Encryption in transit:** STARTTLS bắt buộc trên port 25, TLS 1.2+ trên webmail
- **Backup:** Daily snapshot Cloud SQL, 30 ngày retention
- **DSAR:** Khách export tất cả mail của mình qua GET /api/v1/mail/export?ws={ws}

---

## 10. Em đề xuất

### Phase 1 (ngay sau khi data warehouse stable, ~T+24h):
- Em viết migration SQL 070-074 + API endpoints CRUD
- Deploy staging mail-staging.zenicloud.io (Cloud Run)
- Test smoke với 1 domain test.zenicloud.io
- **Effort:** 1 tuần em làm part-time

### Phase 2 (sau Phase 1 OK):
- Postfix container + rspamd
- SES integration
- DNS auto-setup
- **Effort:** 2 tuần

### Phase 3 (sau Phase 2 OK):
- Webmail UI
- Billing + pricing live
- Beta test
- **Effort:** 3 tuần

**Tổng:** 6 tuần từ approve → public launch.

**Cost build:** ~$50 credits từ $300 GCP pool (development) + $5/tháng vận hành staging.

---

## 11. Câu hỏi cần chairman quyết

1. **Approve build L7 Mail?** → Em start Phase 1 sau khi data warehouse stable
2. **Phase 1 outbound dùng SES hay tự host từ đầu?** → SES cost thêm $1-5/tháng nhưng deliverability tốt
3. **Pricing tier có OK không?** → $2 Starter / $5 Pro / $10 Business / $25 Enterprise
4. **Domain MX root: `mx.zenicloud.io`?** → Hay tách `mx.zenimail.io` brand riêng?

---

**Status:** Spec ready — chờ chairman approve để start build.
