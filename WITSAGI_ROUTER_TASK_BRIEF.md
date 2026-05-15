# TASK BRIEF — WitsAGI Router (Layer-0 AI Gateway)

**From:** Zeni Cloud CTO (Claude Opus 4.7)
**To:** WitsAGI Coding Agent
**Priority:** P0 — Launch trong 72h
**Reference architecture:** Zeni Router (đã built ở `Zeni-Cloud-Core/backend/app/services/router/`)

---

## 📋 CONTEXT

Build **WitsAGI Router** — single AI gateway tổng hợp 8 LLM models (4 frontier API + 4 self-host) cho WitsAGI platform. Pattern lấy cảm hứng từ **Zeni Router** (đã production v170 trên `zenicloud.io`).

WitsAGI Router làm gì:
- **Adaptive routing:** mỗi customer query → chọn model phù hợp (simple→Haiku, complex→Opus, code→DeepSeek)
- **Multi-Agent Council:** 4 LLM parallel vote consensus cho critical tasks
- **Cost optimization:** auto-route 80% queries sang self-host (cost $0.05/M) thay $15/M
- **Failover chain:** primary down → tự switch fallback
- **Billing tracking:** mỗi call ghi cost USD cho audit

---

## 🏗 ARCHITECTURE

```
WitsAGI customer query
        ↓
┌──────────────────────────────────┐
│  WitsAGI Router /v1/router/route │
│   1. Detect complexity           │
│   2. Select model tier           │
│   3. Apply failover chain        │
│   4. Track cost                  │
└──────────┬───────────────────────┘
           │
   ┌───────┴────────┬──────────────┬─────────────┐
   ↓                ↓              ↓             ↓
Frontier API   Self-host LLM   Specialized   Council
(critical)     (cheap)         (VN LoRA)     (consensus)
- Claude       - DeepSeek V3   - WitsAGI-    - 4 LLM
- GPT-4o       - Llama 3.3       Coder-VN     parallel
- Gemini       - Qwen Coder    - Architect-  vote
- DeepSeek     - Phi-4           VN
```

---

## 📂 FILES TO CREATE

```
witsagi-platform/
├── backend/
│   ├── app/
│   │   ├── services/
│   │   │   ├── router/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── registry.py         # MODEL_REGISTRY (10+ models)
│   │   │   │   ├── orchestrator.py     # detect_complexity + select_model
│   │   │   │   ├── council.py          # Multi-agent council pattern
│   │   │   │   ├── llm_gateway.py      # call_provider (Anthropic/OpenAI/Gemini/DeepSeek)
│   │   │   │   ├── selfhost.py         # vLLM inference for self-host models
│   │   │   │   └── billing.py          # cost tracking per call
│   │   │   └── inference/
│   │   │       └── vllm_server.py      # vLLM deployment script
│   │   └── api/
│   │       └── router.py               # POST /v1/router/route endpoint
│   └── tests/
│       └── router/
│           ├── test_registry.py
│           ├── test_complexity.py
│           └── test_council.py
└── deploy/
    └── witsagi-router-Dockerfile
```

---

## 📜 REUSE FROM ZENI (chairman forward em chia sẻ source code)

Em đã build sẵn các pattern này trên Zeni-Cloud-Core, WitsAGI agent COPY+ADAPT:

| Zeni file (reference) | Pattern lấy |
|---|---|
| `backend/app/services/router/registry.py` | MODEL_REGISTRY dataclass + adaptive tier mapping |
| `backend/app/services/coder/orchestrator.py` | `detect_complexity()` + `_select_model()` + `call_persona()` |
| `backend/app/services/coder/council.py` | 6-vai parallel council pattern (em đề xuất WitsAGI dùng 4-vai) |
| `backend/app/services/llm_gateway.py` | Provider abstraction (Anthropic/Gemini/DeepSeek/OpenAI) |

WitsAGI clone Zeni-Cloud-Core repo → copy reference → adapt cho WitsAGI workspace.

---

## 🔌 API CONTRACT

### POST `/v1/router/route`

**Request:**
```json
{
  "prompt": "Phân tích cấu trúc móng nhà 3 tầng",
  "system": "You are WitsAGI architect agent",
  "complexity_hint": "complex",  // optional: simple|medium|complex|critical
  "capabilities": ["text", "reasoning"],  // required capabilities
  "max_tokens": 2048,
  "temperature": 0.7,
  "mode": "single"  // single | council | failover
}
```

**Response:**
```json
{
  "output": "Phân tích cấu trúc móng...",
  "model_used": "deepseek-v3",
  "provider": "deepseek-selfhost",
  "input_tokens": 234,
  "output_tokens": 1024,
  "cost_usd": 0.000156,
  "latency_ms": 1234,
  "routing_decision": {
    "complexity_detected": "complex",
    "tier": "frontier",
    "model_chosen": "deepseek-v3",
    "failover_chain": ["llama-3.3-70b", "claude-sonnet-4-5"],
    "reason": "complex code task → DeepSeek V3 (best code OS)"
  },
  "council_votes": null  // populated nếu mode=council
}
```

### POST `/v1/router/council`

Council mode — 4 LLM parallel vote, return consensus:

**Request:** same as `/route` + `mode: "council"`

**Response:** thêm `council_votes` array với 4 votes from Claude/GPT/Gemini/DeepSeek.

---

## 📊 MODEL REGISTRY (10+ models cần register)

| model_id | Provider | Tier | Real model | Cost/M in | Cost/M out |
|---|---|---|---|---|---|
| `claude-opus-4-7` | Anthropic API | frontier | claude-opus-4-1-20250805 | $15 | $75 |
| `claude-sonnet-4-5` | Anthropic | balanced | claude-sonnet-4-5-20250929 | $3 | $15 |
| `claude-haiku-4-5` | Anthropic | fast | claude-haiku-4-5-20251001 | $0.80 | $4 |
| `gpt-4o` | OpenAI | balanced | gpt-4o | $5 | $15 |
| `gemini-2.5-pro` | Google | balanced | gemini-2.5-pro | $1.25 | $5 |
| `gemini-2.5-flash` | Google | fast | gemini-2.5-flash | $0.075 | $0.30 |
| `deepseek-v3` | Self-host vLLM | frontier-cheap | DeepSeek-V3 | $0.05 | $0.05 |
| `llama-3.3-70b` | Self-host | balanced | Llama-3.3-70B | $0.05 | $0.05 |
| `qwen-2.5-coder` | Self-host | code | Qwen2.5-Coder-32B | $0.03 | $0.03 |
| `phi-4` | Self-host | fast | phi-4 | $0.01 | $0.01 |

---

## 🧠 COMPLEXITY DETECTION RULES

Em đã viết logic này trong Zeni (`backend/app/services/coder/orchestrator.py:detect_complexity`). WitsAGI copy + adapt:

```python
def detect_complexity(prompt: str, context: dict = None) -> str:
    """Return: simple | medium | complex | critical"""
    word_count = len(prompt.split())
    text_lower = prompt.lower()

    # CRITICAL signals (route → Opus/Council)
    critical_kw = ["deploy production", "structural calculation", "legal",
                   "compliance TCVN", "audit", "deletion", "migration"]
    if any(kw in text_lower for kw in critical_kw):
        return "critical"

    # COMPLEX signals
    complex_kw = ["architecture", "refactor", "multi-step", "design system"]
    if any(kw in text_lower for kw in complex_kw) or word_count > 200:
        return "complex"

    # MEDIUM
    if word_count > 20 or any(w in text_lower for w in ["deploy", "build", "test"]):
        return "medium"

    return "simple"
```

---

## ✅ ACCEPTANCE CRITERIA

WitsAGI Router PHẢI pass 10 tests này:

1. ✅ POST `/v1/router/route` simple query → trả Haiku response < 2s
2. ✅ POST `/v1/router/route` complex query → trả Sonnet/Opus response
3. ✅ POST `/v1/router/council` → 4 votes parallel + consensus
4. ✅ Failover: nếu Anthropic 429 rate-limit → tự switch DeepSeek
5. ✅ Cost tracking: mỗi call ghi `cost_usd` đúng đến 6 chữ số thập phân
6. ✅ Latency p50 < 3s cho simple queries
7. ✅ Self-host DeepSeek V3 inference qua vLLM live
8. ✅ Audit log mỗi call (workspace_id, user, model, cost)
9. ✅ Rate limit per workspace (1000 req/min default)
10. ✅ Health check `/v1/router/health` returns 200 + active models list

---

## 🧪 TESTING PLAN

```bash
# Unit tests
pytest tests/router/ -v

# Integration tests
curl -X POST http://localhost:8000/v1/router/route \
  -H "Authorization: Bearer $WITSAGI_TOKEN" \
  -d '{"prompt": "Hello", "complexity_hint": "simple"}'

# Load test
ab -n 1000 -c 10 -p test-payload.json \
  -T application/json \
  -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/v1/router/route

# Benchmark vs Claude API
python benchmark.py --models witsagi,claude,gpt4o --tasks humaneval,mmlu,vn-context
```

---

## ⏱ TIMELINE 72H

| Hour | Task |
|---|---|
| **0-12** | Download model weights + setup vLLM serving |
| **12-24** | Build registry.py + orchestrator.py + complexity detection |
| **24-36** | Wire 4 frontier providers (Anthropic/OpenAI/Gemini/DeepSeek API) |
| **36-48** | Wire 4 self-host models qua vLLM + Council pattern |
| **48-60** | Build billing + rate limit + audit log |
| **60-66** | 10 acceptance tests + benchmark |
| **66-72** | Deploy staging + handoff doc |

---

## 🚦 HANDOFF NOTES

**WitsAGI agent cần:**
1. Clone Zeni-Cloud-Core repo: `git clone https://github.com/thienmocduc/Zeni-Cloud-Core`
2. Reference files (xem code mẫu):
   - `backend/app/services/router/registry.py` (lines 1-300)
   - `backend/app/services/coder/orchestrator.py` (`detect_complexity` function)
   - `backend/app/services/coder/council.py` (Multi-agent pattern)
3. Adapt cho WitsAGI namespace (rename `zeni_*` → `witsagi_*`)
4. Deploy lên WitsAGI Cloud Run service riêng (không đụng Zeni production)
5. Wire vào Zeni Cloud workspace `witsagi_flatform` qua PAT

**Em standby** — nếu WitsAGI agent stuck, anh forward question cho em qua chairman, em hỗ trợ.

---

## 📊 SUCCESS METRICS (sau 72h)

- ✅ WitsAGI Router live on staging URL
- ✅ Route 80% queries → self-host (cost 300× rẻ hơn Anthropic)
- ✅ Council mode: 4 LLM parallel consensus working
- ✅ Benchmark HumanEval ≥ 90% (ngang Claude)
- ✅ VN context benchmark ≥ 95% (vượt Claude 70%)
- ✅ Cost serve: $0.05/M tokens (300× rẻ frontier)
- ✅ Audit log + billing track 100% requests

---

**File:** `C:\Users\Admin\Documents\Zeni-Cloud-Core\WITSAGI_ROUTER_TASK_BRIEF.md`
**Send to:** WitsAGI Coding Agent
**Approve:** Chairman Thien Moc Duc
**Generated:** 2026-05-12 by Zeni CTO Claude
