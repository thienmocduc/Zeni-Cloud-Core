# Cloud Run Job image — data warehouse pipeline
# Build: gcloud builds submit --config cloudbuild.yaml
# Run:   gcloud run jobs execute zeni-data-warehouse --region us-central1
FROM python:3.11-slim

# System deps for img2dataset, pillow, opencv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    wget \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Python deps — pinned for reproducibility
COPY requirements-warehouse.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy pipeline scripts
COPY laion_downloader.py /workspace/
COPY openimages_filter.py /workspace/
COPY commoncrawl_image.py /workspace/
COPY curation_pipeline.py /workspace/
COPY run_pipeline.sh /workspace/
RUN chmod +x /workspace/run_pipeline.sh

# Default: print help — Cloud Run Job override via --args
CMD ["bash", "/workspace/run_pipeline.sh"]
