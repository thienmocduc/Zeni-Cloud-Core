# Option B — Data Warehouse trên 100% Zeni Cloud (GCP), $0 tiền tươi

Mục tiêu: 10M ảnh kiến trúc/nội thất → curated 1-2M → train 5 LoRA styles → cấp service cho Viet Contech và các khách thiết kế.

**Tổng cost ước tính: ~$55-60 GCP credits** (từ $300 pool có sẵn) → **$0 tiền tươi**.

---

## Pipeline tổng quan

```
┌──────────────────────────────────────────────────────────────┐
│  STAGE 1-4  (Cloud Run Job, CPU, 24h max)                    │
│  zeni-data-warehouse                                         │
│  ─────────────────────────────────────────────────────────   │
│  ├─ 1. LAION-Aesthetics V2 filter → 10M URLs                 │
│  ├─ 2. Open Images V7 filter      → 1M URLs                  │
│  ├─ 3. Common Crawl WARC extract  → 2M URLs                  │
│  └─ 4. img2dataset batch download → ~3TB raw .tar shards    │
│       output: gs://zeni-data-warehouse/v1/raw/               │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 5  (Vertex AI Custom Training, GPU L4 spot, 6h)       │
│  zeni-curation                                               │
│  ─────────────────────────────────────────────────────────   │
│  ├─ pHash dedup                                              │
│  ├─ CLIP scoring (positive/negative concept)                 │
│  ├─ Resolution + size filter                                 │
│  └─ output: gs://zeni-data-warehouse/v1/curated/             │
│       ~1-2M curated images, ~600GB                           │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  STAGE 6  (Vertex AI Custom Training × 5 jobs parallel)      │
│  zeni-lora-{style}                                           │
│  ─────────────────────────────────────────────────────────   │
│  ├─ indochine    (200K ảnh, L4 spot 12h, $3.6)               │
│  ├─ japandi      (150K ảnh, L4 spot 10h, $3.0)               │
│  ├─ tropical     (100K ảnh, L4 spot  8h, $2.4)               │
│  ├─ luxury       ( 80K ảnh, L4 spot  7h, $2.1)               │
│  └─ industrial   (100K ảnh, L4 spot  8h, $2.4)               │
│       output: gs://zeni-data-warehouse/v1/models/            │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  PRODUCTION SERVE                                            │
│  Cloud Run inference service (GPU L4 on-demand)              │
│  POST /api/v1/design/render                                  │
│  ─────────────────────────────────────────────────────────   │
│  SDXL + zeni-{style}-lora → image output                     │
└──────────────────────────────────────────────────────────────┘
```

---

## Cost breakdown

| Resource | Spec | Duration | Rate | Cost |
|---|---|---|---|---|
| Cloud Run Job (Stages 1-4) | 8 vCPU × 32GB | 24h | $0.30/h | $7 |
| Cloud Build (image build) | E2_HIGHCPU_8 | 15 min | $0.16/h | $0.04 |
| Artifact Registry | Docker image storage | 1 GB | $0.10/GB/mo | $0.10/mo |
| GCS Standard (raw tạm) | 3 TB × 7 ngày | — | $0.02/GB/day | $4 |
| Vertex AI L4 (curation) | g2-standard-8, 1× L4 spot | 6h | $0.30/h | $2 |
| Vertex AI L4 × 5 (LoRA) | 5 jobs parallel | 45h total | $0.30/h | $14 |
| GCS Coldline (curated final) | 600 GB × 12 mo | — | $0.004/GB/mo | $29/yr |
| GCS Coldline (LoRA models) | 50 GB × 12 mo | — | $0.004/GB/mo | $2/yr |
| Egress (mostly intra-region) | — | — | — | $0 |
| **TỔNG one-time** | | | | **~$28** |
| **TỔNG monthly** | | | | **~$3** |

Vs Option A (Flux/Meshy/D5/Maket $300/tháng) → **tiết kiệm $272/tháng**.

Còn dư $272 credits sau setup → đủ chạy thêm 4 vòng retrain hoặc 30 LoRA pilot khác.

---

## File mới (commit 46e1370 cộng dồn)

```
backend/scripts/data_warehouse/
├── README.md                      # Pipeline overview (đã commit)
├── OPTION_B_DEPLOY.md             # File này
├── laion_downloader.py            # Stage 1 (đã commit)
├── openimages_filter.py           # Stage 2 (đã commit)
├── commoncrawl_image.py           # Stage 3 (đã commit)
├── curation_pipeline.py           # Stage 5 (đã commit)
├── Dockerfile                     # Cloud Run Job image
├── requirements-warehouse.txt     # Python deps
├── run_pipeline.sh                # Entry orchestrate 4 stages
├── cloudbuild.yaml                # Build & push image
├── deploy_job.sh                  # One-click deploy + execute
└── vertex_train_lora.py           # Stage 6: LoRA training trên Vertex AI
```

---

## Quickstart deploy (1 chairman approve → 5 lệnh)

```bash
# 1. Auth + set project
gcloud auth login
gcloud config set project zeni-cloud-core

# 2. Build image + push Artifact Registry
cd backend/scripts/data_warehouse
chmod +x deploy_job.sh
./deploy_job.sh build

# 3. Create Cloud Run Job + GCS bucket + SA (idempotent)
./deploy_job.sh create

# 4. Execute (run pipeline 1 lần, background)
./deploy_job.sh run

# 5. Stream logs
gcloud beta run jobs logs tail zeni-data-warehouse \
    --region=us-central1 --project=zeni-cloud-core
```

Sau ~24h (Stage 1-4 done):

```bash
# Stage 5 (curation) — Vertex AI L4 spot, 6h
gcloud ai custom-jobs create \
    --region=us-central1 \
    --display-name=zeni-curation \
    --config=vertex_curation_config.yaml

# Stage 6 (LoRA train all 5 styles) — 45h parallel
python vertex_train_lora.py --style=all
```

---

## Quota cần check trước khi deploy

| Quota | Region | Cần | Default | Cách xin |
|---|---|---|---|---|
| Cloud Run Jobs concurrent | us-central1 | 1 | 10 | OK |
| Cloud Run Job memory | per task | 32 GB | 32 GB | OK |
| Vertex AI L4 GPU | us-central1 | 5 (parallel) | 1 (default) | Request quota |
| Vertex AI A100 80GB | us-central1 | 0 (dùng L4 thay) | 0 | Không cần |
| GCS Standard storage | us-central1 | 3 TB tạm | unlimited | OK |
| Cloud Build minutes | global | 120/day | 120 free | OK |

→ **Action item:** Request L4 GPU quota tăng từ 1 → 5 (5 phút auto-approve).

---

## Rollback plan

Nếu pipeline fail:
1. **Stage 1-4 fail:** Cloud Run Job retry max 1 lần auto. Sau đó manual debug logs.
2. **Stage 5 fail:** GCS raw vẫn còn, chỉ retry curation. Cost retry: $2.
3. **Stage 6 fail (1 LoRA):** Re-submit job cho style đó. Cost: $2-4.
4. **Toàn bộ scrap:** Delete bucket `gs://zeni-data-warehouse`, repeat. Cost retry: ~$28.

Storage lifecycle policy đã set:
- STANDARD → COLDLINE@30d (auto)
- COLDLINE → ARCHIVE@365d (auto)

→ Nếu không truy cập 1 năm → archive tự động, cost 90% rẻ hơn Standard.

---

## So với Option A (3rd party APIs $300)

| Tiêu chí | Option A (Flux/Meshy/D5/Maket) | Option B (Zeni Cloud) |
|---|---|---|
| Tiền tươi | $300/tháng | $0 |
| Credits đốt | 0 | ~$28 one-time + $3/tháng |
| Tự chủ data | KHÔNG (data ở 3rd party) | CÓ (data trên GCS chính chủ) |
| Tự chủ model | KHÔNG (API key có thể bị revoke) | CÓ (LoRA weights lưu GCS) |
| Margin VC khi serve | thấp (phải pay 3rd party) | cao (zero marginal cost) |
| Risk deplatform | CAO | THẤP |
| Scale | cap theo quota 3rd party | scale theo GCP quota tự xin |
| Brand đầu ra | watermark Flux/Meshy có thể | 100% Zeni-branded |
| **Tuân Rule 9 (Zeni Cloud only)** | **❌ vi phạm** | **✅ đúng rule** |

---

## Timeline thực tế

- **Hôm nay (chairman approve)** → em deploy `./deploy_job.sh all` → Cloud Run Job running
- **+24h** → Stage 1-4 done, raw data trên GCS
- **+30h** → Stage 5 (curation) done
- **+72h** → Stage 6 done, 5 LoRA models trên GCS
- **+96h** → Wire vào `/api/v1/design/render` endpoint, serve VC demo

---

## Trạng thái hiện tại

- [x] Scripts code xong (commit `46e1370`)
- [x] Dockerfile + cloudbuild.yaml + deploy_job.sh sẵn sàng
- [x] Vertex AI training script sẵn sàng
- [ ] **Chờ chairman approve deploy** → em chạy `./deploy_job.sh all`
- [ ] Request L4 GPU quota tăng lên 5
- [ ] Verify GCS bucket gs://zeni-data-warehouse tạo OK
- [ ] Smoke test 1 stage (laion only) trước khi chạy full

---

**Author:** Zeni CTO (Claude Opus 4.7)
**File:** `backend/scripts/data_warehouse/OPTION_B_DEPLOY.md`
**Updated:** 2026-05-16
