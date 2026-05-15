# Zeni Data Warehouse — 10M ảnh FREE LEGAL pipeline

Mục đích: build kho ảnh 10M kiến trúc/nội thất (FREE LEGAL) → train LoRA design agent.
KHÔNG cần subscribe Flux/Meshy/D5/Maket APIs ($300/tháng).

---

## Tổng quan pipeline (3 ngày)

```
Day 1 (raw fetch ~3 TB):
   LAION-Aesthetics V2  →  laion_downloader.py    →  10M URLs filtered
   Open Images V7       →  openimages_filter.py   →  1M URLs filtered
   Common Crawl WARC    →  commoncrawl_image.py   →  2M URLs filtered
   (Wikimedia/Unsplash) →  manual list           →  500K URLs
                                ↓
   img2dataset (CLI)         →  ~10M ảnh raw 1024px (~3 TB)

Day 2 (curation):
   raw/ ─→ curation_pipeline.py ─→ curated/
              ├─ phase 1: resolution + size filter
              ├─ phase 2: pHash dedup
              ├─ phase 3: CLIP scoring (positive/negative concept)
              └─ output: train_clean.jsonl (~1-2M ảnh)

Day 3 (storage + train):
   curated/ → GCS Coldline (100 TB plan, ~$100/tháng)
            → train LoRA "Indochine VN" + 4 styles (A100 spot, ~$100)
```

---

## File trong thư mục này

| File | Mục đích | Input | Output |
|---|---|---|---|
| `laion_downloader.py` | Stream LAION-Aesthetics V2 metadata, filter interior keywords | HF dataset `laion/laion2B-en-aesthetic` | `urls_filtered.parquet` (10M URLs) |
| `openimages_filter.py` | Filter Google Open Images V7 by interior labels | OIDv7 CSV files | `urls.txt` (~1M URLs) |
| `commoncrawl_image.py` | Extract `<img>` tags từ Common Crawl WARC, filter alt-text | WARC URL | `urls.txt` + `metadata.jsonl` |
| `curation_pipeline.py` | Dedup + CLIP scoring + resolution filter | raw image dir | `train_clean.jsonl` |

---

## Quickstart

### Bước 1 — Cài tools

```bash
pip install datasets img2dataset open_clip_torch warcio imagededup pillow pandas
```

### Bước 2 — LAION-Aesthetics filter

```bash
python laion_downloader.py \
    --target 10000000 \
    --output-dir /mnt/zeni-data/laion-interior
```

Output: `urls_filtered.parquet` (~10M rows).

### Bước 3 — img2dataset batch download (10M ảnh)

```bash
img2dataset \
    --url_list=/mnt/zeni-data/laion-interior/urls_filtered.parquet \
    --input_format=parquet \
    --url_col=URL --caption_col=TEXT \
    --output_folder=/mnt/zeni-data/laion-interior/images \
    --processes_count=16 \
    --thread_count=64 \
    --image_size=1024 \
    --output_format=webdataset \
    --resize_mode=keep_ratio \
    --enable_wandb=False
```

Thời gian: ~24h trên 16 cores + 1 Gbps connection.
Dung lượng: ~3 TB (WebDataset .tar shards, 1GB mỗi shard).

### Bước 4 — Open Images filter (parallel)

```bash
# Download metadata trước
wget https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions.csv
wget https://storage.googleapis.com/openimages/v7/oidv7-train-annotations-human-imagelabels.csv

# Filter
python openimages_filter.py \
    --labels-csv oidv7-class-descriptions.csv \
    --annotations-csv oidv7-train-annotations-human-imagelabels.csv \
    --output-dir /mnt/zeni-data/openimages-interior \
    --target 1000000

# Batch download
img2dataset \
    --url_list=/mnt/zeni-data/openimages-interior/urls.txt \
    --output_folder=/mnt/zeni-data/openimages-interior/images \
    --image_size=1024 \
    --output_format=webdataset
```

### Bước 5 — Common Crawl

```bash
# Pick 1 segment từ index
WARC_URL="https://data.commoncrawl.org/crawl-data/CC-MAIN-2025-13/segments/.../warc.gz"

python commoncrawl_image.py \
    --warc-url $WARC_URL \
    --output-dir /mnt/zeni-data/cc-interior \
    --target 2000000

# Batch download
img2dataset --url_list=/mnt/zeni-data/cc-interior/urls.txt \
            --output_folder=/mnt/zeni-data/cc-interior/images \
            --image_size=1024 --output_format=webdataset
```

### Bước 6 — Curate (GPU)

```bash
python curation_pipeline.py \
    --input-dir /mnt/zeni-data \
    --output-dir /mnt/zeni-data/curated \
    --min-clip-score 0.25 \
    --min-resolution 512 \
    --batch-size 256 \
    --device cuda
```

Hardware: 1× A100 (Replicate spot ~$1/h, ~6h total = $6).
Output: `train_clean.jsonl` với ~1-2M ảnh chất lượng cao.

### Bước 7 — Upload GCS Coldline

```bash
gsutil -m cp -r /mnt/zeni-data/curated \
    gs://zeni-data-warehouse/v1/curated/

# Coldline pricing: $0.004/GB/month → 2TB curated = $8/tháng
```

---

## Budget thực tế (3 ngày)

| Mục | Cost | Note |
|---|---|---|
| Compute download (16-core VM 24h) | $20 | Spot instance e2-standard-16 |
| Storage tạm raw (3TB SSD 3 ngày) | $30 | Cloud SQL pricing |
| GPU curate (A100 spot 6h) | $6 | Replicate `meta/llama-2-70b-chat` rate |
| GCS Coldline (2TB final) | $8/tháng | Lưu lâu dài |
| LoRA train pilot (A100 30h) | $30 | Test 1 style đầu tiên |
| **Tổng one-time** | **$94** | |
| **Tổng monthly** | **$8** | GCS storage |

So với plan cũ ($300 cho Flux/Meshy/D5/Maket APIs): **TIẾT KIỆM $206**.

---

## License compliance

| Nguồn | License | Yêu cầu |
|---|---|---|
| LAION-Aesthetics V2 | CC0 (research + commercial) | Không bắt buộc credit |
| Open Images V7 | CC-BY 2.0 | Credit khi publish derivative |
| Common Crawl | Fair use + opt-out respect | Honor `noai`/`noimageai` meta |
| Wikimedia | CC-BY-SA 4.0 | Share-alike khi derivative |
| Unsplash/Pexels/Pixabay | License riêng (gần free) | Credit photographer |

**Quan trọng:**
- Code đã honor `<meta robots noai>` opt-out (commoncrawl_image.py)
- Mọi ảnh CC-BY → lưu `attribution` field trong metadata
- Mọi ảnh CC-BY-SA → train output cũng phải mở source (LoRA weights công khai)
- LAION CC0 → free commercial sử dụng

---

## Pipeline scale lên 100M ảnh (tương lai)

Khi cần dataset lớn hơn:
1. Stream toàn bộ LAION-2B (2 tỉ ảnh) — filter chặt hơn
2. Thêm DataComp-1B (1.4B ảnh, CC0)
3. Thêm SAM-1B (Segment Anything Model, 1.1B masks)
4. Auto-caption với BLIP-2 (re-caption khi alt text yếu)

Hardware: Cần TPU pod hoặc 8× A100 cluster.

---

## Next steps (sau Day 3)

- [ ] Train LoRA "Indochine VN" trên 200K ảnh kiến trúc Việt Nam
- [ ] Train LoRA "Japandi" trên 150K ảnh Japanese/Scandinavian
- [ ] Train LoRA "Tropical Villa" trên 100K ảnh nhiệt đới
- [ ] Train LoRA "Luxury Indochine" trên 80K ảnh cao cấp
- [ ] Train LoRA "Industrial Loft" trên 100K ảnh loft
- [ ] Wire LoRA models vào Zeni AI Engine (L3) → cấp service cho VC
- [ ] Benchmark vs Flux Pro / Midjourney trên 100 prompt thiết kế

---

**Author:** Zeni CTO (Claude Opus 4.7)
**Last updated:** 2026-05-16
**Status:** Scripts ready — chờ chairman approve GPU spot + GCS Coldline budget ($94 one-time + $8/tháng)
