# Architecture Wellnexus ↔ Zeni Cloud API Bridge

**Mục đích:** Wellnexus chứa kho 10M ảnh + LoRA models (data layer). Zeni Cloud expose API cho khách hàng bên thứ 3 (service layer). Chairman own cả 2.

**File:** `WELLNEXUS_ZENI_API_BRIDGE.md`
**Author:** Zeni CTO (Claude Opus 4.7)
**Date:** 2026-05-16

---

## 1. Vì sao 2 project tách biệt?

| Layer | Project | Account | Lý do tách |
|---|---|---|---|
| **Data** | `WELLNEXUS-VIETCONTECH` | doanhnhancaotuan@gmail.com (Wellnexus entity) | Free $313 trial mới, không đụng production |
| **Service** | `zeni-cloud-core` | caotuanphat581@gmail.com (Zeni entity) | Production zenicloud.io, 14 workspace khách trả tiền |

**Lợi ích:**
- Data ăn vào free trial $313 (storage rẻ + lâu dài)
- Service production isolated khỏi data layer (downtime data ≠ downtime API)
- Mỗi entity có pháp nhân riêng → giảm legal risk
- Scale data + scale service độc lập

---

## 2. Architecture tổng thể

```
┌──────────────────────────────────────────────────────────────────┐
│ 3rd party customer (khách thiết kế / Viet Contech / agencies)    │
│  Auth: API token Zeni Cloud (Bearer xxx)                         │
└──────────────────────┬───────────────────────────────────────────┘
                       │ POST /api/v1/design/render
                       │ Body: { prompt, style, ... }
                       ↓
┌──────────────────────────────────────────────────────────────────┐
│ Zeni Cloud (zenicloud.io)                                        │
│ Project: zeni-cloud-core                                          │
│  ├─ FastAPI /design router (backend/app/api/design.py)            │
│  ├─ Auth: workspace_id + role + quota check                       │
│  ├─ Billing: charge khách qua wallet_transactions                 │
│  ├─ Cache: hash(prompt) → GCS check trước                         │
│  └─ Internal call → Wellnexus storage qua SA cross-project        │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Internal HTTPS (signed JWT)
                       │ GET /v1/wellnexus/lora/{style}
                       │ GET /v1/wellnexus/cache/{hash}.jpg
                       ↓
┌──────────────────────────────────────────────────────────────────┐
│ Wellnexus Data Storage                                           │
│ Project: project-630d0936-0066-4385-9a3                          │
│  ├─ GCS bucket gs://wellnexus-data-warehouse/                     │
│  │   ├─ v1/raw/      → 10M ảnh raw training data                  │
│  │   ├─ v1/curated/  → 1-2M ảnh curated (CLIP filter)             │
│  │   └─ v1/models/   → 5 LoRA weights {indochine, japandi, ...}   │
│  ├─ VM wellnexus-data-builder (chỉ download/curation)             │
│  └─ Cloud Run inference (sau khi có LoRA): /v1/wellnexus/render   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. API contract — Zeni Cloud (public-facing)

### `POST /api/v1/design/render`

Khách hàng gọi → Zeni Cloud orchestrate → trả ảnh result.

**Request:**
```http
POST /api/v1/design/render?ws=<workspace_id>
Authorization: Bearer <zeni_api_token>
Content-Type: application/json

{
  "prompt": "Phòng khách phong cách Indochine, ánh sáng vàng ấm, gỗ tự nhiên",
  "style": "indochine",
  "mode": "preview" | "final" | "ultra",
  "negative_prompt": "blurry, low quality",
  "seed": 42,
  "num_images": 4,
  "size": "1024x1024"
}
```

**Response:**
```json
{
  "request_id": "req_abc123",
  "status": "completed",
  "images": [
    {
      "url": "https://zenicloud.io/render/req_abc123/img_0.jpg",
      "signed_url_expires_in": 3600,
      "size_bytes": 524288,
      "width": 1024,
      "height": 1024
    },
    ...
  ],
  "metrics": {
    "latency_ms": 7234,
    "cost_usd": 0.012,
    "model": "flux-1.1-pro + zeni-indochine-lora",
    "cache_hit": false
  }
}
```

### `GET /api/v1/design/styles`

List 5 LoRA styles available (indochine, japandi, tropical, luxury, industrial).

### `GET /api/v1/design/quota?ws=<workspace>`

Trả workspace plan + remaining quota (renders/tháng).

---

## 4. API contract — Wellnexus internal (chỉ Zeni Cloud gọi)

### `GET /v1/wellnexus/lora/{style}` (SA-auth)

Trả LoRA weights file URL từ GCS Wellnexus.

```
Auth: SA token zeni-cloud-core@... với quyền objectViewer trên gs://wellnexus-data-warehouse/v1/models/
Response: signed URL (1 hour TTL) → download .safetensors file
```

### `GET /v1/wellnexus/cache/{hash}.jpg` (SA-auth)

Cache layer: check trước nếu khách query trùng prompt+style trước đó.

### `POST /v1/wellnexus/inference` (SA-auth, FUTURE — sau khi deploy Cloud Run GPU)

Wellnexus host Cloud Run GPU L4 inference. Zeni Cloud forward request → Wellnexus generate → return ảnh.

```
Request: { prompt, style, lora_path, ... }
Response: { image_bytes (base64), latency_ms }
```

---

## 5. Auth — Cross-project access

### Option A: Service Account cross-project (RECOMMENDED)
```bash
# Tạo SA trong zeni-cloud-core
gcloud iam service-accounts create zeni-wellnexus-reader \
    --project=zeni-cloud-core

# Grant SA quyền read trên Wellnexus bucket
gsutil iam ch \
    serviceAccount:zeni-wellnexus-reader@zeni-cloud-core.iam.gserviceaccount.com:objectViewer \
    gs://wellnexus-data-warehouse
```

Zeni Cloud backend dùng SA này gọi GCS Wellnexus → no API key needed.

### Option B: Internal API token
```
Wellnexus expose internal endpoint /v1/wellnexus/* với header `X-Internal-Token: <secret>`
Token chỉ Zeni Cloud biết. Stored in Secret Manager.
```

Em recommend **Option A** vì:
- No secrets to rotate
- IAM audit log per request
- Cross-project SA là pattern Google chính thức

---

## 6. Billing flow

```
┌─────────────────────────────────────────────────────────────────┐
│ Khách trả: $0.50/render (Basic plan)                            │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────────┐
│ Zeni Cloud billing (wallet_transactions table)                  │
│  ├─ charge khách: -$0.50                                        │
│  ├─ cost compute Zeni Cloud Run: $0.002 (GPU L4 spot, ~5s)     │
│  ├─ cost Wellnexus storage egress: ~$0.001 (1MB ảnh)            │
│  ├─ cost Flux API (nếu chưa có LoRA self-host): $0.04           │
│  └─ Margin Zeni: $0.50 - $0.043 = $0.457 (91%)                  │
└─────────────────────────────────────────────────────────────────┘

Khi có LoRA self-host (sau khi train):
  Cost compute: $0.002 (GPU L4 spot)
  Cost Wellnexus: $0.001
  Cost Flux API: $0 (self-host)
  Margin Zeni: $0.497 (99%)
```

---

## 7. Cache strategy (giảm cost 30-40%)

```python
# Zeni Cloud /design/render handler
async def render(req):
    hash_key = hashlib.sha256(f"{req.prompt}|{req.style}|{req.seed}".encode()).hexdigest()
    
    # Step 1: check Wellnexus cache
    cached_url = await wellnexus_cache_check(hash_key)
    if cached_url:
        # Cache hit (~35% rate VN context recurring prompts)
        await audit_push(action="design.cache_hit", cost_usd=0.0001)
        return {"images": [cached_url], "cache_hit": True}
    
    # Step 2: gen ảnh (Flux API hoặc self-host)
    images = await flux_generate(req.prompt, req.style, req.seed)
    
    # Step 3: write to Wellnexus cache
    cache_url = await wellnexus_cache_write(hash_key, images[0])
    
    return {"images": [cache_url], "cache_hit": False}
```

---

## 8. Implementation roadmap

### Phase 1 — Hiện tại (đang chạy)
- ✅ VM Wellnexus chạy pipeline filter PD12M
- 🟡 GCS bucket nhận ảnh raw
- 🟡 Pipeline scale 100K → 1M → 10M (cần LAION HF token)

### Phase 2 — Sau khi có 1M ảnh (T+~2-3 ngày)
- [ ] CLIP curation script chạy trên Wellnexus VM (filter chất lượng)
- [ ] Upload 500K ảnh curated lên `gs://wellnexus-data-warehouse/v1/curated/`

### Phase 3 — Sau khi có curated dataset (T+~5 ngày)
- [ ] Train LoRA "Indochine" trên Vertex AI L4 spot (T+1 ngày, ~$15)
- [ ] Train 4 LoRA khác parallel (Japandi, Tropical, Luxury, Industrial)
- [ ] Upload LoRA weights `gs://wellnexus-data-warehouse/v1/models/`

### Phase 4 — Wire API bridge
- [ ] Tạo SA `zeni-wellnexus-reader@zeni-cloud-core` 
- [ ] Grant SA cross-project access vào bucket Wellnexus
- [ ] Code endpoint `/api/v1/design/render` trong `backend/app/api/design.py`
- [ ] Wire Flux API call (Phase 4a) HOẶC Wellnexus self-host inference (Phase 4b)
- [ ] Test với 1 workspace (Viet Contech)

### Phase 5 — Production launch
- [ ] Pricing live trên zenicloud.io/pricing (Basic $0.50/render, Pro $0.30, Enterprise $0.15)
- [ ] Onboard 3 khách architect agencies pilot
- [ ] Revenue tracking dashboard

---

## 9. Cost projection production

### Year 1 — 1M renders/tháng
| Resource | Cost/tháng |
|---|---|
| GCS Wellnexus storage 10M ảnh + LoRA + cache | $50 |
| GCS Zeni Cloud cache 100K ảnh | $5 |
| Cloud Run GPU L4 spot inference (1M × 5s) | $400 |
| Cross-region egress (Wellnexus → Zeni) | $30 |
| **Total cost** | **$485/tháng** |
| **Revenue (1M × avg $0.30)** | **$300,000/tháng** |
| **Margin** | **99.84%** |

### Năm 2 — 10M renders/tháng
| Resource | Cost/tháng |
|---|---|
| Storage + cache 100M ảnh | $200 |
| Cloud Run GPU L4 spot inference 10M | $4,000 |
| Egress | $300 |
| **Total cost** | **$4,500/tháng** |
| **Revenue (10M × avg $0.20)** | **$2,000,000/tháng** |
| **Margin** | **99.78%** |

---

## 10. Tuân Rule chairman

| Rule | Compliance |
|---|---|
| Rule 9 — Chỉ dùng Zeni Cloud (GCP) | ✅ Cả 2 project đều GCP, no 3rd party platform |
| Rule 0 — Giữ ổn định 100% | ✅ Wellnexus tách biệt khỏi production zenicloud.io |
| Rule 1 — Không sửa direct production | ✅ Mọi code thay đổi qua branch + PR + canary |
| Rule 2 — Chỉ nhận lệnh chairman | ✅ Khách qua API chính thức, không tự fix |
| Rule 6 — Không reset password user khác | ✅ Cross-project SA, không touch user accounts |

---

## 11. Câu hỏi cần chairman quyết

1. **Pricing tier:** $0.50 Basic / $0.30 Pro / $0.15 Enterprise (em đã đề xuất) — OK chưa?
2. **SA cross-project name:** `zeni-wellnexus-reader@zeni-cloud-core` — OK hay tên khác?
3. **Cache strategy:** Hash by `prompt+style+seed` (giảm 35%) hay `prompt+style` only (giảm 60%, mất unique seed)?
4. **Flux API tier:** Default Flux 1.1 Pro ($0.04) hay Schnell preview ($0.003)?
5. **LoRA train budget:** $15/LoRA × 5 = $75 từ Wellnexus credits (em đã đề xuất), OK?

---

**Status:** Architecture doc xong, chờ chairman approve để Phase 4 wire API.
