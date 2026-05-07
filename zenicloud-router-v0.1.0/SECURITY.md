# ZeniCloud Router — Security Model

## Threat Model

ZeniRouter sits in front of expensive AI providers and handles tenant credentials. The threats we defend against:

1. **API key leakage** — adversary obtains tenant key → calls expensive frontier models on our dime
2. **Provider key leakage** — our Anthropic/OpenAI/GCP master keys exposed → catastrophic billing event
3. **Prompt injection** — tenant content tries to extract system prompts, other tenants' data
4. **PII in logs** — accidentally persisting customer prompts/responses
5. **DoS / cost-bomb** — adversary floods router with large requests
6. **CSRF / XSS** — browser-side attacks on the dashboard
7. **Supply chain** — compromised dependency

## Defenses (implemented)

### Authentication
- Every `/v1/*` endpoint requires `X-Zeni-API-Key` header
- Keys follow pattern `zk_<env>_<32hex>` — environment-scoped
- In production, keys are looked up in DB by SHA-256 hash (not plaintext)
- Key prefix logged for audit, never the full key

### Provider credentials
- All API keys loaded via `pydantic.SecretStr` from env vars
- **Never logged** — `redact_secrets` processor strips them from any log output
- Master keys stored in **1Password Business** (Zeni vault)
- Per-environment keys: dev / staging / production never share

### Rate limiting
- 100 requests/minute per IP via `slowapi`
- Per-tenant quota enforced in `complete()` (TODO: DB-backed)

### Request hardening
- Max body size: 10MB (configurable via `MAX_REQUEST_SIZE_MB`)
- Per-message content cap: 500KB
- Max messages per request: 200
- Pydantic validates every field — bad input → 422, never reaches engine

### Network
- CORS allowlist: only `zenicloud.io`, `zenidigital.com`, `zeni.holdings`, `localhost:3000`
- TrustedHostMiddleware in production limits to `*.zenicloud.io`
- HSTS enabled in production (2-year preload)

### Response hygiene
- Security headers on every response: X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy
- `/docs`, `/redoc`, `/openapi.json` **disabled in production**
- Global exception handler returns `{"detail": "Internal error"}` — never leaks tracebacks

### Logging
- Structured JSON in production (parseable by Datadog/CloudWatch)
- Auto-redacts: `sk-*`, `AKIA*`, JWT, Bearer tokens, `zk_*` keys, anything in field names matching `*key*|*secret*|*token*|*auth*|*password*`
- Request prompts and responses **NOT logged** by default — only metadata (tenant, product, task, cost, latency)

### Observability
- Prometheus `/metrics` endpoint (TODO)
- OpenTelemetry traces (configurable)
- Audit log of every routing decision (tenant + model + cost)

## Defenses (roadmap)

| Item | Priority | Owner |
|---|---|---|
| DB-backed tenant table with hashed keys | P1 | next sprint |
| Per-tenant cost ceiling (hard cutoff) | P1 | next sprint |
| Semantic prompt-injection detector | P2 | Q3/2026 |
| WAF in front (Cloudflare) | P1 | deploy phase |
| Zero-trust mTLS between Zeni services | P2 | Q3/2026 |
| Encrypted-at-rest audit log → S3 with 7-year retention | P2 | Q3/2026 |
| Bug bounty program | P3 | post-GA |

## Incident response

1. **Suspected key leak** → rotate immediately via 1Password, revoke at provider, audit billing
2. **Cost spike** → set `USE_MOCK_ADAPTERS=true` to halt all real spend, investigate
3. **Provider outage** → failover chain auto-handles; if all 3 down, return 503 cleanly
4. **Tenant abuse** → revoke key in DB, return 403 on all subsequent requests

## Compliance posture

- GDPR: no PII persisted by default; tenant prompts pass through, never stored
- SOC 2 Type II: in scope for 2027 audit (alongside Zeniipo platform)
- VN Decree 13/2023 (personal data): tenants are data controllers; ZeniCloud is processor

---

**Reviewed by:** CTO Em · **Date:** 2026-04-30 · **Lock:** v0.1.0
