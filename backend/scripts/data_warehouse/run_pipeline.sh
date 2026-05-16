#!/usr/bin/env bash
# Entrypoint cho Cloud Run Job — orchestrate 4 stages của data warehouse pipeline.
# Mỗi stage chạy độc lập, có thể skip qua env var để retry phần lỗi.
#
# ENV VARS (set qua gcloud run jobs update):
#   STAGE             - all|laion|openimages|commoncrawl|curate (default: all)
#   GCS_BUCKET        - target GCS bucket (default: zeni-data-warehouse)
#   TARGET            - max images per source (default: 1000000)
#   HUGGINGFACE_TOKEN - optional, faster HF dataset stream
#   WARC_URL          - Common Crawl WARC URL (1 segment)
#   GCP_PROJECT       - injected by Cloud Run

# NOTE: KHÔNG dùng -e — nếu 1 stage fail (vd HF gated dataset), pipeline phải
# continue sang stage tiếp theo (Open Images / Common Crawl) thay vì stop hoàn toàn.
set -uo pipefail

STAGE="${STAGE:-all}"
GCS_BUCKET="${GCS_BUCKET:-zeni-data-warehouse}"
TARGET="${TARGET:-1000000}"
WORK_DIR="${WORK_DIR:-/tmp/zeni-data}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

mkdir -p "$WORK_DIR"
cd /workspace

log "===== Stage config: STAGE=$STAGE TARGET=$TARGET BUCKET=gs://$GCS_BUCKET ====="

# ── Stage 1: HF Hub dataset filter (PD12M / COYO / Conceptual-12M) ──
if [[ "$STAGE" == "all" || "$STAGE" == "laion" ]]; then
    log "Stage 1/4: HF Hub public dataset filter (PD12M/COYO/Conceptual)..."
    python laion_downloader.py \
        --target "$TARGET" \
        --output-dir "$WORK_DIR/laion-interior" \
        || log "WARN: Stage 1 failed (HF Hub unreachable or all datasets gated), continuing to Stage 2"

    if [[ -f "$WORK_DIR/laion-interior/urls_filtered.parquet" ]]; then
        log "Upload Stage 1 URL parquet to GCS..."
        gsutil -m cp "$WORK_DIR/laion-interior/urls_filtered.parquet" \
            "gs://$GCS_BUCKET/v1/laion/urls_filtered.parquet" || \
            log "WARN: gsutil upload failed"
    fi

    log "Stage 1 done (or skipped)."
fi

# ── Stage 2: Open Images filter ───────────────────────────────
if [[ "$STAGE" == "all" || "$STAGE" == "openimages" ]]; then
    log "Stage 2/4: Open Images V7 filter..."
    if [[ ! -f /tmp/oidv7-class-descriptions.csv ]]; then
        wget -q -O /tmp/oidv7-class-descriptions.csv \
            https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions.csv
    fi
    if [[ ! -f /tmp/oidv7-annotations.csv ]]; then
        wget -q -O /tmp/oidv7-annotations.csv \
            https://storage.googleapis.com/openimages/v7/oidv7-train-annotations-human-imagelabels.csv
    fi

    python openimages_filter.py \
        --labels-csv /tmp/oidv7-class-descriptions.csv \
        --annotations-csv /tmp/oidv7-annotations.csv \
        --output-dir "$WORK_DIR/openimages-interior" \
        --target "$TARGET" \
        || log "WARN: Stage 2 failed, continuing"

    if [[ -f "$WORK_DIR/openimages-interior/urls.txt" ]]; then
        gsutil -m cp "$WORK_DIR/openimages-interior/urls.txt" \
            "gs://$GCS_BUCKET/v1/openimages/urls.txt" || log "WARN: gsutil failed"
    fi

    log "Stage 2 done (or skipped)."
fi

# ── Stage 3: Common Crawl image extract ───────────────────────
if [[ "$STAGE" == "all" || "$STAGE" == "commoncrawl" ]]; then
    log "Stage 3/4: Common Crawl image extract..."
    if [[ -z "${WARC_URL:-}" ]]; then
        log "INFO: WARC_URL not set, skipping Stage 3 (you can add WARC_URL env later)"
    else
        python commoncrawl_image.py \
            --warc-url "$WARC_URL" \
            --output-dir "$WORK_DIR/cc-interior" \
            --target "$TARGET" \
            || log "WARN: Stage 3 failed, continuing"

        if [[ -f "$WORK_DIR/cc-interior/urls.txt" ]]; then
            gsutil -m cp "$WORK_DIR/cc-interior/urls.txt" \
                "gs://$GCS_BUCKET/v1/cc/urls.txt" || log "WARN: gsutil failed"
        fi
    fi
    log "Stage 3 done (or skipped)."
fi

# ── Stage 4: img2dataset batch download ───────────────────────
if [[ "$STAGE" == "all" || "$STAGE" == "download" ]]; then
    log "Stage 4/4: img2dataset batch download..."
    for source in laion-interior openimages-interior cc-interior; do
        src_dir="$WORK_DIR/$source"
        urls_file=""
        [[ -f "$src_dir/urls_filtered.parquet" ]] && urls_file="$src_dir/urls_filtered.parquet"
        [[ -f "$src_dir/urls.txt" ]] && urls_file="$src_dir/urls.txt"

        if [[ -z "$urls_file" ]]; then
            log "Skip $source — no URLs file found"
            continue
        fi

        log "Downloading $source from $urls_file..."
        img2dataset \
            --url_list="$urls_file" \
            --output_folder="$src_dir/images" \
            --processes_count=8 \
            --thread_count=32 \
            --image_size=1024 \
            --output_format=webdataset \
            --resize_mode=keep_ratio \
            --enable_wandb=False \
            --retries=2 \
            --timeout=20 || log "img2dataset failed for $source, continuing..."

        # Upload .tar shards to GCS
        log "Upload $source shards to GCS..."
        gsutil -m cp -r "$src_dir/images" \
            "gs://$GCS_BUCKET/v1/$source/" || log "gsutil upload failed, skip"
    done
    log "Stage 4 done."
fi

# ── Stage 5 (separate Vertex AI Job, NOT here): CLIP curation
# Stage 5 runs on GPU L4 via Vertex AI Custom Training — see vertex_train_lora.py

log "===== Pipeline complete ====="
log "Output: gs://$GCS_BUCKET/v1/"
log "Next: trigger Vertex AI curation job (Stage 5)"
