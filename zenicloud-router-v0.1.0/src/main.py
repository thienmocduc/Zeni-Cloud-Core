"""
ZeniCloud Router - FastAPI entrypoint.
Endpoint: router.zenicloud.io

Security stack:
  - CORS allowlist
  - Trusted host
  - Rate limiting (slowapi)
  - Request size limit
  - Security headers
  - API key auth on /v1/*
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.config import settings
from src.core.logging import configure_logging, get_logger
from src.routers.api import router as api_router

configure_logging()
logger = get_logger(__name__)


# ─── Lifespan ───
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "startup",
        version=settings.APP_VERSION,
        env=settings.ENV,
        mock_mode=settings.USE_MOCK_ADAPTERS,
    )
    yield
    logger.info("shutdown")


# ─── App ───
app = FastAPI(
    title="ZeniCloud Router",
    version=settings.APP_VERSION,
    description="Smart multi-model router for Zeni Holdings (11+ products). 80/15/5 cost optimization.",
    docs_url="/docs" if not settings.is_production else None,  # disable docs in prod
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)


# ─── Rate limiting ───
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ─── Security headers ───
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        response.headers["X-Zeni-Service"] = "router"
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ─── Request size guard ───
class RequestSizeLimit(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > settings.MAX_REQUEST_SIZE_MB * 1024 * 1024:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Payload exceeds {settings.MAX_REQUEST_SIZE_MB}MB"},
            )
        return await call_next(request)


app.add_middleware(RequestSizeLimit)


# ─── Trusted hosts (only in prod) ───
if settings.is_production:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["router.zenicloud.io", "*.zenicloud.io"],
    )


# ─── CORS ───
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", settings.API_KEY_HEADER],
    max_age=600,
)


# ─── Global error handler — never leak internals ───
@app.exception_handler(Exception)
async def unhandled_exception(_request: Request, exc: Exception):
    logger.exception("unhandled_exception", error=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal error", "trace_id": "[REDACTED]"},
    )


# ─── Routes ───
app.include_router(api_router)


# ─── Root ───
@app.get("/", tags=["system"])
async def root() -> dict:
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs" if not settings.is_production else "disabled in production",
        "endpoint": "router.zenicloud.io",
        "philosophy": "80/15/5 — cheapest model that meets quality threshold wins",
    }
