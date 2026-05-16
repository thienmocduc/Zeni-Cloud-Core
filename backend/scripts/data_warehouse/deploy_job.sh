#!/usr/bin/env bash
# One-click deploy Cloud Run Job + execute pipeline.
#
# Pre-req:
#   gcloud auth login
#   gcloud config set project zeni-cloud-core
#   GCS bucket exists: gs://zeni-data-warehouse (create if missing)
#   Artifact Registry exists: us-central1-docker.pkg.dev/zeni-cloud-core/zeni
#
# Usage:
#   ./deploy_job.sh build      # Cloud Build → push image
#   ./deploy_job.sh create     # Create/update Cloud Run Job
#   ./deploy_job.sh run        # Execute job
#   ./deploy_job.sh all        # build + create + run

set -euo pipefail

PROJECT="${PROJECT:-zeni-cloud-core}"
REGION="${REGION:-us-central1}"
JOB_NAME="${JOB_NAME:-zeni-data-warehouse}"
BUCKET="${BUCKET:-zeni-data-warehouse}"
IMAGE="us-central1-docker.pkg.dev/$PROJECT/zeni/data-warehouse:latest"
TARGET="${TARGET:-1000000}"
STAGE="${STAGE:-all}"
WARC_URL="${WARC_URL:-}"  # optional

ensure_bucket() {
    if ! gsutil ls "gs://$BUCKET" >/dev/null 2>&1; then
        echo "Creating GCS bucket gs://$BUCKET (Standard → lifecycle Coldline after 30d)..."
        gsutil mb -p "$PROJECT" -l "$REGION" -c STANDARD "gs://$BUCKET"
        cat > /tmp/lifecycle.json <<'EOF'
{
  "rule": [
    {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
     "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}},
    {"action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
     "condition": {"age": 365, "matchesStorageClass": ["COLDLINE"]}}
  ]
}
EOF
        gsutil lifecycle set /tmp/lifecycle.json "gs://$BUCKET"
        echo "Bucket lifecycle: STANDARD → COLDLINE@30d → ARCHIVE@1yr"
    fi
}

ensure_artifact_registry() {
    if ! gcloud artifacts repositories describe zeni --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
        echo "Creating Artifact Registry zeni..."
        gcloud artifacts repositories create zeni \
            --repository-format=docker \
            --location="$REGION" \
            --project="$PROJECT" \
            --description="Zeni Cloud images"
    fi
}

cmd_build() {
    ensure_artifact_registry
    echo "Submitting Cloud Build (this image: $IMAGE)..."
    gcloud builds submit \
        --config=cloudbuild.yaml \
        --region="$REGION" \
        --project="$PROJECT" \
        --substitutions=SHORT_SHA=$(date +%Y%m%d-%H%M%S)
    echo "Build done."
}

cmd_create() {
    ensure_bucket

    # Cloud Run Job specs: 16 vCPU × 32GB RAM × 24h max → ~$25 spot
    # NOTE: --execution-environment=gen2 required for >8 CPU
    echo "Creating/updating Cloud Run Job $JOB_NAME..."

    if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
        ACTION=update
    else
        ACTION=create
    fi

    gcloud run jobs "$ACTION" "$JOB_NAME" \
        --image="$IMAGE" \
        --region="$REGION" \
        --project="$PROJECT" \
        --task-timeout=86400 \
        --max-retries=1 \
        --parallelism=1 \
        --tasks=1 \
        --cpu=8 \
        --memory=32Gi \
        --set-env-vars="STAGE=$STAGE,TARGET=$TARGET,GCS_BUCKET=$BUCKET,WARC_URL=$WARC_URL" \
        --service-account="zeni-warehouse-runner@$PROJECT.iam.gserviceaccount.com" \
        --execution-environment=gen2 \
        || true

    echo "Job $ACTION done."
}

ensure_sa() {
    SA="zeni-warehouse-runner@$PROJECT.iam.gserviceaccount.com"
    if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1; then
        echo "Creating SA $SA..."
        gcloud iam service-accounts create zeni-warehouse-runner \
            --project="$PROJECT" \
            --display-name="Zeni Data Warehouse Runner"
        gcloud projects add-iam-policy-binding "$PROJECT" \
            --member="serviceAccount:$SA" \
            --role="roles/storage.objectAdmin" \
            --condition=None
        gcloud projects add-iam-policy-binding "$PROJECT" \
            --member="serviceAccount:$SA" \
            --role="roles/logging.logWriter" \
            --condition=None
    fi
}

cmd_run() {
    echo "Executing job $JOB_NAME (this triggers a single run)..."
    gcloud run jobs execute "$JOB_NAME" \
        --region="$REGION" \
        --project="$PROJECT" \
        --wait=false
    echo
    echo "Stream logs:"
    echo "  gcloud beta run jobs logs tail $JOB_NAME --region=$REGION --project=$PROJECT"
    echo
    echo "Or via Cloud Console:"
    echo "  https://console.cloud.google.com/run/jobs/details/$REGION/$JOB_NAME?project=$PROJECT"
}

cmd_status() {
    gcloud run jobs executions list \
        --job="$JOB_NAME" \
        --region="$REGION" \
        --project="$PROJECT" \
        --limit=5
}

cmd_all() {
    ensure_sa
    cmd_build
    cmd_create
    cmd_run
}

action="${1:-help}"
case "$action" in
    build)   cmd_build ;;
    create)  ensure_sa; cmd_create ;;
    run)     cmd_run ;;
    status)  cmd_status ;;
    all)     cmd_all ;;
    *)
        cat <<EOF
Usage: $0 {build|create|run|status|all}

Env vars:
  PROJECT  = $PROJECT
  REGION   = $REGION
  JOB_NAME = $JOB_NAME
  BUCKET   = $BUCKET
  TARGET   = $TARGET (default 1000000)
  STAGE    = $STAGE  (all|laion|openimages|commoncrawl|download)
  WARC_URL = (set for commoncrawl stage)

Examples:
  # First-time setup + run
  ./deploy_job.sh all

  # Re-run with bigger target
  TARGET=10000000 ./deploy_job.sh run

  # Just rebuild image after script edit
  ./deploy_job.sh build && ./deploy_job.sh run
EOF
        exit 1 ;;
esac
