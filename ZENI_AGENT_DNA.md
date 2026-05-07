# 🧬 ZENI CLOUD · AGENT DNA — Workflow Standard

> **Đọc file này trước khi code bất cứ task nào trên Zeni Cloud.**
> Mọi agent (Stream A/B/C/D/E + future) tuân thủ cùng 1 workflow để output consistent.
> Bản: 2026-04-28 · Owner: chairman session (Zeni Cloud Core).

---

## 1. NGUYÊN TẮC CỐT LÕI (5 điều bất di bất dịch)

```
1. CODE → FIX BUG → SECURITY → TEST E2E → 10/10 → BÁO CÁO
   (KHÔNG bao giờ skip 2-3-4)

2. KHÔNG đụng code đã merge vào main (tránh regression)
   Thay đổi → tạo file mới hoặc Edit thêm function, không sửa logic existing

3. WORKSPACE ISOLATION luôn enforce
   Mọi endpoint nhận `ws=<workspace_id>` phải call require_workspace_access(ws, me)
   Mọi SQL phải filter `WHERE workspace_id = :ws`

4. NAMED PARAMS, không bao giờ string-format SQL
   text("SELECT ... WHERE id = :id"), {"id": value}
   KHÔNG: f"SELECT ... WHERE id = '{value}'"  → SQL injection

5. AUDIT LOG mọi action thay đổi state
   audit_push(db, actor, workspace_id, action, target, severity, metadata)
```

---

## 2. API CONTRACT STANDARD

### Request

```http
{METHOD} /api/v1/{layer}/{resource}?ws={workspace_id}
Authorization: Bearer {jwt OR zeni_pat_xxx}
Content-Type: application/json

{ ...body schema validated by Pydantic }
```

### Response (success)

```json
HTTP 200 / 201 / 202
{
  "ok": true,
  "...resource fields...",
  "cost_usd": 0.001,
  "timings_ms": {"phase1": 100}
}
```

### Response (error)

```json
HTTP 400/401/402/403/404/422/429/502
{ "detail": "Vietnamese error message" }
```

### Error code mapping

| Code | Meaning | Use case |
|------|---------|----------|
| 400 | Bad request | Pydantic validation pass nhưng business logic reject |
| 401 | Missing/invalid token | Auth header issue |
| 402 | Payment required | Wallet hết tiền hoặc quota hết |
| 403 | Forbidden | Token có nhưng thiếu scope/role |
| 404 | Not found | Resource ID không tồn tại trong workspace |
| 422 | Validation error | Pydantic schema fail |
| 429 | Rate limited | Quá threshold |
| 502 | Upstream error | Cloud SQL / Vertex AI / Imagen 3 fail |

---

## 3. NAMING CONVENTIONS

```
DB tables:           snake_case          (workspaces, wallet_balances, webhook_attempts)
DB columns:          snake_case          (created_at, workspace_id, balance_vnd)
Schema names:        ws_{workspace_id}   (ws_anima, ws_nexbuild)
API URLs:            kebab-case          (/api/v1/api-tokens/, /webhook-attempts)
JSON keys:           snake_case          ({"workspace_id": "anima"})
Python files/funcs:  snake_case          (cost_dashboard.py, charge_workspace())
Python classes:      PascalCase          (StructuredArchitectureBrief)
Frontend JS files:   kebab-case          (zeni-api.js, zeni-realdata.js)
Frontend JS funcs:   camelCase           (fetchWorkspaceData(), bootApp())
HTML/CSS classes:    kebab-case          (.price-card, .field, .btn-primary)
Branch names (Git):  stream-X-feature    (stream-B-frontend-dashboard)
Image tags:          v{int}              (zeni-backend:v37)
```

---

## 4. SECURITY CHECKLIST (mỗi PR phải pass)

```
[ ] Auth check (JWT or PAT scope)
[ ] Workspace isolation (require_workspace_access)
[ ] Role check (Viewer/Developer/Admin/Owner — đúng quyền)
[ ] Input validation (Pydantic + business rules)
[ ] SQL injection-safe (named params only)
[ ] Rate limit applied (where applicable)
[ ] Audit log + billing log (every state-changing action)
[ ] Error messages KHÔNG leak internal info (vd: stack trace, secret keys)
[ ] Sensitive fields masked in response (token, password, secret_token)
[ ] Internal endpoints (/internal/*) hidden from Swagger
[ ] Background tasks không leak DB session
```

---

## 5. DEPLOY WORKFLOW (chuẩn mực)

```bash
# 1. Code change → test local logic
# 2. Build new image (semver: vN+1)
gcloud builds submit \
  --account=caotuanphat581@gmail.com \
  --tag="us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/zeni-backend:v{N+1}" \
  --project=zeni-cloud-core --region=us-central1 --timeout=900s

# 3. Apply migration (nếu có)
gcloud storage cp migrations/{NNN}_*.sql gs://zeni-system/sql/
gcloud sql import sql zeni-cloud-db gs://zeni-system/sql/{NNN}_*.sql \
  --account=caotuanphat581@gmail.com \
  --database=zeni_cloud --user=postgres --project=zeni-cloud-core --quiet

# 4. Deploy Cloud Run
gcloud run deploy zeni-backend \
  --account=caotuanphat581@gmail.com \
  --image="us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/zeni-backend:v{N+1}" \
  --region=us-central1 --platform=managed \
  --service-account=zeni-cloud-core-sa@zeni-cloud-core.iam.gserviceaccount.com \
  --add-cloudsql-instances=zeni-cloud-core:us-central1:zeni-cloud-db \
  --allow-unauthenticated \
  --port=8080 --memory=2Gi --cpu=2 --min-instances=1 --max-instances=15 \
  --concurrency=40 --timeout=600 \
  --env-vars-file=envs.yaml \
  --set-secrets="DATABASE_URL=zeni-database-url:latest,JWT_SECRET=zeni-jwt-secret:latest,VAULT_KEY=zeni-vault-key:latest,ADMIN_PASSWORD=zeni-admin-password:latest,GMAIL_SMTP_USER=zeni-smtp-user:latest,GMAIL_SMTP_PASSWORD=zeni-smtp-password:latest,ZENI_CRON_SECRET=zeni-cron-secret:latest" \
  --project=zeni-cloud-core

# 5. Smoke test
curl https://zenicloud.io/health  # → {"status":"ok"}

# 6. E2E test cho features mới (mock + real)

# 7. Security test (rate limit, SQL injection, cross-WS access)

# 8. Báo cáo: # tests passed, # bugs fixed, deploy revision N
```

---

## 6. FRONTEND PATCHING PATTERN (không đụng code main)

```javascript
// File: frontend/{feature}-real.js (loaded after zeni-api.js)
(function () {
  'use strict';
  if (!window.ZeniAPI || !window.state) {
    console.warn('[feature-real] dependencies not ready, retrying');
    setTimeout(arguments.callee, 100); return;
  }

  // 1. Define new render function
  async function renderFeatureReal() {
    const data = await fetchAPI();
    document.getElementById('view-feature').innerHTML = htmlTemplate(data);
  }

  // 2. Hook into existing setView via wrapping
  const _origSetView = window.setView;
  window.setView = function(view) {
    if (view === 'feature' && window.ZeniAPI.isAuthed()) {
      _origSetView('feature');  // existing UI
      renderFeatureReal();      // overlay real data
    } else {
      _origSetView(view);
    }
  };

  // 3. Re-render on workspace change
  if (window.ZeniRealData) {
    const _origBootstrap = window.ZeniRealData.bootstrap;
    window.ZeniRealData.bootstrap = async function() {
      await _origBootstrap();
      if (window.state.currentView === 'feature') renderFeatureReal();
    };
  }
})();
```

---

## 7. DATABASE MIGRATION PATTERN

```sql
-- File: backend/migrations/NNN_<feature>.sql
-- Idempotent (CREATE IF NOT EXISTS), no destructive changes

-- 1. Schema changes
CREATE TABLE IF NOT EXISTS ...
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...

-- 2. Indexes for common query paths
CREATE INDEX IF NOT EXISTS idx_<table>_<cols> ON <table>(<cols>);

-- 3. Grant to app user
GRANT ALL PRIVILEGES ON <new_table> TO zeni_app;
GRANT USAGE, SELECT ON <new_table>_id_seq TO zeni_app;  -- if BIGSERIAL

-- 4. (Optional) Seed initial data
INSERT INTO ... VALUES ... ON CONFLICT DO NOTHING;
```

---

## 8. AUDIT LOG FORMAT

```python
await audit_push(
    db,
    actor=me.email,                              # ai gây ra (email user/PAT name)
    workspace_id=ws,                             # context workspace (None nếu global)
    action="<layer>.<resource>.<verb>",          # vd: "compute.deploy", "billing.charge"
    target="<resource_id_or_name>",              # short ref, max 80 chars
    severity="ok" | "info" | "warn" | "err",
    metadata={                                   # arbitrary JSON, < 4KB
        "ip_hash": "sha256:abc123",              # privacy-respecting IP hint
        "scope": "ai,data",                      # for PAT
        "...": "...",
    },
)
```

---

## 9. STREAMS (MULTI-AGENT TEAM)

```
Stream A — Backend Gaps             (chairman session)
Stream B — Frontend Dashboard       (agent #2)
Stream C — OAuth + Auth             (agent #3, blocked on Anh tạo OAuth Client)
Stream D — Docs + SDK               (agent #4, fully independent)
Stream E — Migration & Onboarding   (agent #5, depends on A+B+C done 50%)

Mỗi agent:
- Đọc file này 100%
- Đọc MULTI_AGENT_WORKFLOW.md scope của mình
- Daily merge vào main, mỗi merge → deploy v(N+1)
- Quality gate: pass 9 security checks ở section 4
- Báo daily standup: what done, what next, what blocker
```

---

## 10. COMMIT MESSAGE FORMAT

```
<type>(<stream>): <short description>

- detail 1
- detail 2

Tests: <#tests passed>
Deploy: v<N>

Type: feat | fix | refactor | docs | sec | perf | test
Stream: A | B | C | D | E
```

Vd:
```
feat(B): add cost dashboard tab fetching real billing API

- Hook setView('billing') to call /billing/dashboard/summary
- Render breakdown by layer
- Show wallet balance + subscription quota progress

Tests: 4/4 E2E pass
Deploy: v38
```

---

## 11. TESTING PATTERN

### Unit test (Python: pytest)
```python
async def test_signup_rejects_weak_password(client):
    r = await client.post("/api/v1/auth/signup", json={
        "full_name": "X", "email": "a@b.com", "password": "weak",
        "company_name": "Y", "workspace_id": "y"
    })
    assert r.status_code == 422
```

### E2E test (curl)
```bash
ACCESS=$(curl -s ... | jq -r .access_token)
curl -X POST "$BASE/api/v1/projects?ws=anima" \
  -H "Authorization: Bearer $ACCESS" \
  -d '{...}' | jq
```

### Security test (manual checklist trong PR)
```
[ ] /api/v1/auth/signup spam 5 lần/IP → request 6 phải HTTP 429
[ ] /api/v1/auth/signup với "password=12345678" → HTTP 400 weak
[ ] /api/v1/projects?ws=other_workspace → HTTP 403 cross-WS
[ ] /internal/cron/* không token → HTTP 401
[ ] curl -I /health → có 5 security headers
```

---

## 12. ROLLBACK PROCEDURE (khi deploy fail)

```bash
# Revert traffic về revision cũ
gcloud run services update-traffic zeni-backend \
  --account=caotuanphat581@gmail.com \
  --to-revisions=zeni-backend-{N-1}-xxx=100 \
  --region=us-central1 --project=zeni-cloud-core

# Migration rollback (nếu có) — DỰ PHÒNG, hạn chế chạy
# Mỗi migration phải có file <NNN>_rollback.sql kèm theo
gcloud sql import sql zeni-cloud-db gs://zeni-system/sql/<NNN>_rollback.sql ...
```

---

## 13. CONTACT

- **Chairman session:** caotuanphat581@gmail.com (anh CEO)
- **Production URL:** https://zenicloud.io
- **API base:** https://zenicloud.io/api/v1
- **GCP Project:** zeni-cloud-core (project number 1026010018600)
- **DB:** Cloud SQL `zeni-cloud-db` Postgres 16, schema `ws_<workspace>`
- **Image registry:** us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/

---

**Hết DNA. Đọc thuộc → code đúng ngay từ commit đầu tiên.**
