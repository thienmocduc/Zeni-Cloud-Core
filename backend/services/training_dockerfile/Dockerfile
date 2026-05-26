# ============================================================================
# Zeni Cloud · LoRA training container (Vertex AI Custom Training)
#
# Target: a2-highgpu-1g (1× A100 40GB) on Vertex AI Custom Training.
# Build:
#   gcloud builds submit --tag us-central1-docker.pkg.dev/zeni-cloud-core/zeni-train/lora-train:v1 .
# Push image to Artifact Registry, then reference in Vertex CustomJob spec.
#
# ⚠ Chairman approval required: build cost + Vertex training cost (~$3-5/hr A100).
# ============================================================================

FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/hf \
    TORCH_HOME=/cache/torch \
    HF_HUB_ENABLE_HF_TRANSFER=1

# ─── System deps ────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        git curl ca-certificates \
        libgl1 libglib2.0-0 \
        build-essential \
        && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python && \
    python -m pip install --upgrade pip setuptools wheel

# ─── Python deps ────────────────────────────────────────────────────────────
# Install torch first (large wheel, CUDA-matched) to maximize layer cache
RUN pip install --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.3.1 torchvision==0.18.1

RUN pip install \
    diffusers==0.30.3 \
    accelerate==0.34.2 \
    transformers==4.45.2 \
    peft==0.13.2 \
    datasets==3.0.1 \
    webdataset==0.2.100 \
    gcsfs==2024.9.0 \
    google-cloud-storage==2.18.2 \
    safetensors==0.4.5 \
    Pillow==10.4.0 \
    hf_transfer==0.1.8 \
    sentencepiece==0.2.0 \
    bitsandbytes==0.43.3 \
    xformers==0.0.27 \
    einops==0.8.0

# ─── App ─────────────────────────────────────────────────────────────────────
# IMPORTANT: build context must be backend/services/ so this COPY finds lora_train.py.
# Recommended build invocation from repo root:
#   gcloud builds submit backend/services/ \
#     --tag us-central1-docker.pkg.dev/zeni-cloud-core/zeni-train/lora-train:v1 \
#     --config=- <<EOF
#   steps:
#     - name: gcr.io/cloud-builders/docker
#       args: ['build','-f','training_dockerfile/Dockerfile','-t','\$_IMG','.']
#   EOF
# Or simpler: `docker build -f backend/services/training_dockerfile/Dockerfile backend/services/`
WORKDIR /workspace
COPY lora_train.py /workspace/lora_train.py

# Sanity check imports at build time (cheap — fails fast on dep mismatch)
RUN python -c "import torch, diffusers, accelerate, transformers, peft, webdataset, gcsfs; \
print('torch=', torch.__version__, 'diffusers=', diffusers.__version__, \
'accelerate=', accelerate.__version__, 'transformers=', transformers.__version__)"

# Vertex AI passes args after ENTRYPOINT
ENTRYPOINT ["python", "/workspace/lora_train.py"]
