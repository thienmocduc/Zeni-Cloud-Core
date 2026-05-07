# ZeniCloud Router

> Smart multi-model router for **Zeni Holdings** — 11+ products, 3 clouds, one endpoint.
> Endpoint: `router.zenicloud.io`

```
   ZeniMake │ ZeniOS │ ZeniERP │ ZeniPay │ ZeniLaw │ ZeniMedia │ ZeniClaw
   Zeniipo  │ ANIMA Care │ WellKOC │ LegalRadar
                          ▼
              ┌───────────────────────┐
              │   ZeniCloud Router    │  ← this repo
              │   80 / 15 / 5         │
              └───────────────────────┘
                ▼          ▼          ▼
           Anthropic    Bedrock     Vertex
           (Claude)    (GPT-5.5)   (Gemini)
```

## What this does

Picks the **cheapest model that meets quality threshold** for every prompt sent by Zeni products, with automatic failover if a provider is down.

Target: **12–18× cost reduction** vs always-Opus, while shielding products from provider-specific SDK quirks.

## 80 / 15 / 5 strategy

| Tier | Traffic share | Models | $/MTok output |
|---|---|---|---|
| **Fast** | 80% | Gemma 4 / Haiku 4.5 / Gemini 3.1 Flash | $0.40 – $5 |
| **Balanced** | 15% | Sonnet 4.6 / Gemini 3.1 Pro / GPT-5.4 | $15 |
| **Frontier** | 5% | Opus 4.7 / GPT-5.5 / Mythos | $25 – $30 |

## Quick start (local dev)

```bash
# 1. Install
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (copy .env.example → .env)
cp .env.example .env
# Default: USE_MOCK_ADAPTERS=true (no real API keys needed)

# 3. Run
uvicorn src.main:app --reload --port 8080

# 4. Test
curl http://localhost:8080/health
```

## Test the routing

```bash
# Use any dev key matching pattern zk_dev_<32hex>
KEY="zk_dev_$(openssl rand -hex 16)"

# Preview which model would be selected
curl -X POST http://localhost:8080/v1/route \
  -H "X-Zeni-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "acme",
    "product": "zenimake",
    "task_type": "code_generate",
    "messages": [{"role": "user", "content": "build a todo app"}],
    "max_tokens": 800
  }'

# Expected: tier=balanced, model=gpt-5-4 or sonnet-4-6 (cheapest balanced)
```

## API surface

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | none | Service info |
| `/health` | GET | none | Liveness probe |
| `/v1/models` | GET | key | List models, filter by tier |
| `/v1/route` | POST | key | Preview routing decision (no LLM call) |
| `/v1/complete` | POST | key | Route + execute completion |

## Architecture

```
src/
├── main.py              # FastAPI entrypoint + middleware stack
├── core/
│   ├── config.py        # All settings (12-factor, env-based)
│   ├── logging.py       # Structlog with secret redaction
│   └── registry.py      # Model registry — source of truth
├── adapters/
│   ├── base.py          # CompletionRequest/Response interface
│   ├── factory.py       # Mock vs real dispatcher
│   ├── mock.py          # Deterministic fake (for tests + dev)
│   └── anthropic_adapter.py  # Real Anthropic implementation
├── services/
│   ├── routing_engine.py     # 80/15/5 decision logic
│   └── failover.py           # Try chain on errors
├── routers/api.py            # HTTP routes
├── middleware/auth.py        # API key verification
└── schemas/api.py            # Pydantic request/response models

tests/test_router.py          # 33 tests covering everything
```

## Switching to real providers

When sếp cấp keys, two changes:

```bash
# .env
USE_MOCK_ADAPTERS=false
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
GCP_PROJECT_ID=zeni-cloud-prod
```

Restart. Routing logic untouched.

## Tests

```bash
pytest tests/ -v
# 33 passed
```

Coverage:
- Registry integrity (pricing, failover targets, tier ordering)
- Routing engine (all task types, capability filters, cost gates, quality thresholds)
- Mock adapter behavior + cost calculation
- Failover orchestration (primary fails → fallback succeeds)
- HTTP API (auth, validation, headers, size limits)
- Security (secret redaction, CORS allowlist)

## Production deployment

- **Hosting:** Cloud Run (GCP `asia-southeast1`)
- **Domain:** `router.zenicloud.io` via Cloudflare proxy
- **Secrets:** GCP Secret Manager (synced from 1Password Business)
- **Observability:** Cloud Logging + Cloud Monitoring + OTel → Tempo
- **CI/CD:** GitHub Actions → Cloud Run deploy on `main` push
- **Cost:** ~$50–$100/mo at 1M reqs/mo (mostly egress)

## License

Internal — Zeni Holdings · CONFIDENTIAL.

## Owners

- **Author:** CTO Em
- **Reviewed:** Chairman Thiên Mộc Đức
- **Locked:** 2026-04-30
- **Doc lineage:** `zeni_digital_ai_infra_strategy_v1.html` § 04
