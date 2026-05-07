# ══════════════════════════════════════════════════════════════════
#  Zeni Cloud Core — Root Dockerfile (production, Cloud Run)
#  Build context: project root (includes backend/ + frontend/)
#  Bundles frontend into /app/static, FastAPI serves SPA + API
# ══════════════════════════════════════════════════════════════════

# ─── Stage 1: build deps ───────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ─── Stage 2: runtime ──────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.12/site-packages"

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r zeni && useradd -r -g zeni -u 1001 -m -d /home/zeni zeni

WORKDIR /app

COPY --from=builder /install /install
COPY --chown=zeni:zeni backend/ /app/
COPY --chown=zeni:zeni frontend/ /app/static/

USER zeni

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2 --proxy-headers --forwarded-allow-ips='*'"]
