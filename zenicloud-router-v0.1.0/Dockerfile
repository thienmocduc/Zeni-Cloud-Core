FROM python:3.11-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ─── Runtime ───
FROM python:3.11-slim

# Non-root user
RUN groupadd -r zeni && useradd -r -g zeni -d /app -s /sbin/nologin zeni

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /home/zeni/.local
ENV PATH=/home/zeni/.local/bin:$PATH

# Copy source
COPY src/ ./src/
COPY pytest.ini ./

# Permissions
RUN chown -R zeni:zeni /app

USER zeni

EXPOSE 8080

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()" || exit 1

# Production: 4 workers, no reload, no docs
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "4", \
     "--access-log", \
     "--log-level", "info", \
     "--no-server-header", \
     "--proxy-headers"]
