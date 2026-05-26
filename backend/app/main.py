from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api import admin_access, admin_access_callback, admin_platform, agents, agents_library, ai, ai_core, api_tokens, audit, auth, automation, backup_dr, benchmarks, billing, billing_v2, books, build_farm, cache, compliance, cost_dashboard, crons, cto_agent, cto_customer, customer_oauth, customer_oauth_flow, data, design, edge_cdn, edge_runtime, email_api, email_verify, gcp, github_integration, identity, internal_cron, legal_entities, login_2fa, lora_inference, mail as mail_hosting, members, messaging, mfa, mobile_certs, multi_region, oauth, observability, ocr, package_registry, password, payouts, phone_otp, pricing, privacy, projects, push_notifications, queue, quick_deploy, realtime, reseller, router as zeni_router_api, slack, sms, source_upload, storage as zeni_storage, training, translate, trial, vector, vector_premium, voice_ai, waitlist, wallet, web3, workspace_whitelist, workspaces, zeni_mail, zeni_pay, zeni_token, zeni_voice
# Note: zeni_studio, zeni_crm, zeni_workspace = SaaS apps (not cloud infra),
# code retained in apps/ for future independent deploy as Cloud Run services.
from app.middleware.metrics_middleware import MetricsMiddleware
from app.core.config import settings
from app.core.security import hash_password
from app.db.base import SessionLocal, engine
from app.db.models import User, UserWorkspace, Workspace

log = logging.getLogger("zeni.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def seed_admin() -> None:
    """On first boot, ensure admin user exists with Owner role & all-workspace access."""
    async with SessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == settings.admin_email.lower()))).scalar_one_or_none()
        if existing:
            log.info("admin user already exists: %s", existing.email)
            return

        ws_ids = (await db.execute(select(Workspace.id))).scalars().all()
        if not ws_ids:
            log.warning("no workspaces seeded yet — admin will be created without workspace links")

        admin = User(
            email=settings.admin_email.lower(),
            password_hash=hash_password(settings.admin_password),
            name=settings.admin_name,
            role="Owner",
        )
        db.add(admin)
        await db.flush()

        for ws_id in ws_ids:
            db.add(UserWorkspace(user_id=admin.id, workspace_id=ws_id, role="Owner"))

        await db.commit()
        log.info("seeded admin user: %s (password from ADMIN_PASSWORD env)", admin.email)


async def apply_pending_migrations() -> None:
    """
    Auto-apply migrations on boot using asyncpg raw connection (supports multi-statement).
    Idempotent — every migration uses CREATE IF NOT EXISTS / INSERT ON CONFLICT.
    """
    import asyncpg
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not migrations_dir.exists():
        log.warning("migrations dir not found: %s", migrations_dir)
        return
    # Apply migrations 018-030 + 040-099 (idempotent CREATE IF NOT EXISTS / INSERT ON CONFLICT)
    # BLOCKLIST — these migrations DELETE production data and must NEVER auto-run.
    # They are kept on disk for historical reference / one-off manual application only.
    # Re-running them on every boot wipes customer workspaces + cascades user_workspaces.
    DESTRUCTIVE_MIGRATIONS = {
        "031_reset_demo_data.sql",         # DELETE FROM workspaces (demo wipe)
        "042_cleanup_test_workspaces.sql", # DELETE FROM workspaces WHERE id NOT IN ('nexbuild')
    }
    pending = []
    for i in list(range(18, 31)) + list(range(40, 100)):
        for p in migrations_dir.glob(f"{i:03d}_*.sql"):
            if p.name in DESTRUCTIVE_MIGRATIONS:
                log.info("migration SKIPPED (destructive blocklist): %s", p.name)
                continue
            pending.append(p)
    pending = sorted(pending)
    if not pending:
        log.info("no pending migrations to apply")
        return

    # Get raw asyncpg connection params from settings
    db_url = str(settings.database_url) if hasattr(settings, "database_url") else None
    if not db_url:
        from app.core.config import settings as _s
        db_url = getattr(_s, "DATABASE_URL", None) or getattr(_s, "database_url", None)
    if not db_url:
        log.error("DATABASE_URL not found in settings")
        return
    # Convert postgresql+asyncpg://... to postgresql://... for raw asyncpg
    raw_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")

    try:
        # Parse for asyncpg.connect
        conn = await asyncpg.connect(dsn=raw_url, timeout=30)
    except Exception as e:
        log.exception("Cannot connect for migrations: %s", e)
        return

    try:
        for mig in pending:
            try:
                sql = mig.read_text(encoding="utf-8")
                if sql.startswith("﻿"):
                    sql = sql[1:]
                # asyncpg.execute() supports multi-statement SQL natively
                await conn.execute(sql)
                log.info("migration applied: %s", mig.name)
            except asyncpg.exceptions.PostgresError as e:
                # Idempotent — log nhưng không crash
                log.warning("migration %s partial-skip: %s", mig.name, str(e)[:200])
            except Exception as e:
                log.exception("migration %s failed: %s", mig.name, e)
    finally:
        await conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migration runner — chạy background (KHÔNG block startup probe).
    # Cloud Run startup probe có timeout ~4 phút; migrations 040-099 idempotent
    # nên chạy fire-and-forget sau khi app đã sẵn sàng nhận traffic.
    import asyncio as _asyncio
    try:
        _asyncio.create_task(apply_pending_migrations())
        log.info("apply_pending_migrations scheduled as background task")
    except Exception as e:
        log.exception("apply_pending_migrations schedule failed: %s", e)
    try:
        await seed_admin()
    except Exception as e:
        log.exception("seed_admin failed: %s", e)
    yield
    await engine.dispose()


app = FastAPI(
    title="Zeni Cloud · API",
    description=(
        "Zeni Cloud — Cloud OS thống nhất cho doanh nghiệp Việt Nam. "
        "100% Google Cloud Platform · 6 lớp hạ tầng (Compute · Data · AI · Automation · Identity · Web3). "
        "Production: https://zenicloud.io · Build by Zeni Holdings."
    ),
    version="1.0.0",
    lifespan=lifespan,
    contact={
        "name": "Zeni Cloud Support",
        "email": "caotuanphat581@gmail.com",
        "url": "https://zenicloud.io",
    },
    license_info={
        "name": "Proprietary",
        "url": "https://zenicloud.io/legal",
    },
    servers=[
        {"url": "https://zenicloud.io", "description": "Production"},
    ],
    openapi_tags=[
        {"name": "auth",        "description": "Authentication: JWT login, MFA TOTP, register"},
        {"name": "workspaces",  "description": "Workspace management"},
        {"name": "projects",    "description": "L1 Compute — deploy Cloud Run services + custom domain"},
        {"name": "data",        "description": "L2 Data — multi-tenant SQL on Cloud SQL"},
        {"name": "ai",          "description": "L3 AI — Vertex AI Gemini 2.5 + Imagen 3 + Embeddings"},
        {"name": "ai-core",     "description": "L3 AI Core — image generation, multi-modal, streaming"},
        {"name": "agents",      "description": "Specialized design agents (Architecture, Interior, Product, Fashion, Structural)"},
        {"name": "automation",  "description": "L4 Automation — webhooks, connectors, events"},
        {"name": "crons",       "description": "L4 Cron jobs (Cloud Scheduler integration)"},
        {"name": "identity",    "description": "L5 Identity — vault, secrets, OAuth"},
        {"name": "web3",        "description": "L6 Web3 — Polygon RPC reads, $ZENI Token, smart contracts"},
        {"name": "billing",     "description": "Wallet (VND), subscriptions, transactions, price book"},
        {"name": "dashboard",   "description": "Cost dashboard analytics per workspace"},
        {"name": "api-tokens",  "description": "Personal Access Tokens (PAT) cho service-to-service"},
        {"name": "members",     "description": "Workspace members + invites"},
        {"name": "audit",       "description": "Immutable audit log"},
        {"name": "waitlist",    "description": "Public landing waitlist signup"},
    ],
)

# ─── CORS ────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Zeni-Cron-Token", "X-Requested-With"],
    max_age=600,
)


# ─── Security headers middleware (HARDENED v104) ────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Core security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
        "magnetometer=(), gyroscope=(), fullscreen=(self)"
    )
    # Cross-Origin policies (prevent Spectre-class attacks)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-site"
    # Server signature obfuscation
    response.headers["Server"] = "Zeni Cloud"
    # Don't override CSP for static HTML pages (they have their own meta CSP)
    if not request.url.path.startswith("/static/") and request.url.path not in ("/", "/app", "/signup"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' https://*.googleapis.com https://*.run.app; "
            "frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'; object-src 'none'"
        )
    return response


# ─── Global error handler ────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "body": exc.body},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ─── Health ──────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "service": "zeni-cloud-core", "version": app.version}


# ─── API routers ─────────────────────────────────────
API_PREFIX = "/api/v1"
app.include_router(auth.router,        prefix=API_PREFIX)
app.include_router(workspaces.router,  prefix=API_PREFIX)
app.include_router(workspace_whitelist.router, prefix=API_PREFIX)  # per-workspace image registry whitelist
app.include_router(projects.router,    prefix=API_PREFIX)
app.include_router(data.router,        prefix=API_PREFIX)
app.include_router(ai.router,          prefix=API_PREFIX)
app.include_router(automation.router,  prefix=API_PREFIX)
app.include_router(identity.router,    prefix=API_PREFIX)
app.include_router(web3.router,        prefix=API_PREFIX)
app.include_router(members.router,     prefix=API_PREFIX)
app.include_router(audit.router,       prefix=API_PREFIX)
app.include_router(billing.router,     prefix=API_PREFIX)
app.include_router(gcp.router,         prefix=API_PREFIX)
app.include_router(waitlist.router,    prefix=API_PREFIX)
app.include_router(mfa.router,         prefix=API_PREFIX)
app.include_router(crons.router,       prefix=API_PREFIX)
app.include_router(api_tokens.router,  prefix=API_PREFIX)
app.include_router(build_farm.router,    prefix=API_PREFIX)  # Native build cloud (Tauri/Rust/Electron)
app.include_router(edge_runtime.router,   prefix=API_PREFIX)  # Sandboxed microVMs (Computer Use, Playwright, Python)
app.include_router(voice_ai.router,         prefix=API_PREFIX)  # Voice AI: STT (Whisper VN) + TTS (XTTS-v3 VN)
app.include_router(push_notifications.router, prefix=API_PREFIX)  # Push notifications: APNs (iOS) + FCM (Android)
app.include_router(benchmarks.router,        prefix=API_PREFIX)  # AI Benchmark tracker (SWE-bench, HumanEval, etc.)
app.include_router(payouts.router,           prefix=API_PREFIX)  # Outgoing Payouts (bank/$ZENI/USDT)
app.include_router(zeni_storage.router,      prefix=API_PREFIX)  # Zeni Storage (S3-compat backed by GCS)
app.include_router(realtime.router,          prefix=API_PREFIX)  # Zeni Realtime (WebSocket pub-sub)
app.include_router(mobile_certs.router,      prefix=API_PREFIX)  # Mobile Cert Manager (APNs/Apple/Android)
app.include_router(package_registry.router,  prefix=API_PREFIX)  # Package Registry (npm + pypi native API)
app.include_router(package_registry.npm_router, prefix=API_PREFIX)   # npm-compatible registry endpoints
app.include_router(package_registry.pypi_router, prefix=API_PREFIX)  # pypi-compatible registry endpoints
app.include_router(ai_core.router,     prefix=API_PREFIX)
app.include_router(agents.router,      prefix=API_PREFIX)
app.include_router(billing_v2.router,    prefix=API_PREFIX)
app.include_router(cost_dashboard.router, prefix=API_PREFIX)
app.include_router(internal_cron.router,  prefix=API_PREFIX)
app.include_router(email_api.router,     prefix=API_PREFIX)
app.include_router(oauth.router,         prefix=API_PREFIX)

# ─── Sprint A2 — Extension modules (v47) ────────────────
app.include_router(vector.router,         prefix=API_PREFIX)
app.include_router(cache.router,          prefix=API_PREFIX)
app.include_router(queue.router,          prefix=API_PREFIX)
app.include_router(ocr.router,            prefix=API_PREFIX)
app.include_router(translate.router,      prefix=API_PREFIX)
app.include_router(sms.router,            prefix=API_PREFIX)
app.include_router(slack.router,          prefix=API_PREFIX)
app.include_router(legal_entities.router, prefix=API_PREFIX)

# ─── Sprint A3 — Privacy + Auth Security + Smart Contract gateway (v51) ─
app.include_router(privacy.router,                prefix=API_PREFIX)
app.include_router(admin_access.router,           prefix=API_PREFIX)
app.include_router(admin_access_callback.router,  prefix=API_PREFIX)
app.include_router(email_verify.router,           prefix=API_PREFIX)
app.include_router(phone_otp.router,              prefix=API_PREFIX)
app.include_router(login_2fa.router,              prefix=API_PREFIX)

# ─── 3h Sprint — ZeniRouter + Pricing + Quota (v52) ─────
app.include_router(zeni_router_api.router,        prefix=API_PREFIX)
app.include_router(pricing.router,                prefix=API_PREFIX)

# ─── Sprint A4 — Phase 0+1 (v56): Observability + Messaging + Zeni Pay + Zeni Books ─
# Prometheus /metrics endpoint mounted at root (no prefix) for scrape compatibility
try:
    app.include_router(observability.prom_router)  # public /metrics
except AttributeError:
    pass
app.include_router(observability.router,          prefix=API_PREFIX)
app.include_router(messaging.router,              prefix=API_PREFIX)
app.include_router(zeni_pay.router,               prefix=API_PREFIX)
app.include_router(books.router,                  prefix=API_PREFIX)

# ─── Sprint A5 — Phase 2 (v57): Vector Premium + Compliance + AI Agents + Mail + Voice ─
app.include_router(vector_premium.router,         prefix=API_PREFIX)
app.include_router(compliance.router,             prefix=API_PREFIX)
app.include_router(agents_library.router,         prefix=API_PREFIX)
app.include_router(zeni_mail.router,              prefix=API_PREFIX)
app.include_router(zeni_voice.router,             prefix=API_PREFIX)

# ─── Sprint A6 — Phase 3 (v63): Web3 + Wallet + Platform Admin (infra-only) ─
# Note: Studio/Workspace/CRM = SaaS APPS, NOT cloud infra → moved to apps/ folder
app.include_router(zeni_token.router,             prefix=API_PREFIX)  # Web3 layer
app.include_router(wallet.router,                 prefix=API_PREFIX)  # Payment infra Cấp 2
app.include_router(admin_platform.router,         prefix=API_PREFIX)  # Platform admin

# ─── Sprint A7 — Phase 4 (v75): Edge CDN + Backup/DR + Multi-Region + Reseller ─
app.include_router(edge_cdn.router,               prefix=API_PREFIX)  # Edge CDN + SSL
app.include_router(backup_dr.router,              prefix=API_PREFIX)  # Backup + DR + PITR
app.include_router(multi_region.router,           prefix=API_PREFIX)  # Multi-region + auto-scale
app.include_router(reseller.router,               prefix=API_PREFIX)  # White-label reseller
app.include_router(github_integration.router,     prefix=API_PREFIX)  # GitHub Integration (Phase 1)
app.include_router(customer_oauth.router,         prefix=API_PREFIX)  # Customer OAuth (Zalo/Apple/Facebook/etc)
app.include_router(customer_oauth_flow.router)    # /auth/{provider}/{ws}/login + /callback (NO API_PREFIX)
app.include_router(trial.router,                  prefix=API_PREFIX)  # 14-day trial enforcement
app.include_router(source_upload.router,          prefix=API_PREFIX)  # ZIP upload deploy (NO GITHUB)
app.include_router(quick_deploy.router,           prefix=API_PREFIX)  # Quick Deploy (1-call API for AI agents)
app.include_router(design.router,                 prefix=API_PREFIX)  # Design Agents: 6 KTS AI (kiến trúc + nội thất + kết cấu + MEP + BOQ + QA)
app.include_router(mail_hosting.router,           prefix=API_PREFIX)  # L7 Mail Hosting · per-domain mailboxes (Phase 1 skeleton)
app.include_router(cto_agent.router,              prefix=API_PREFIX)  # CTO Chat Assistant · AI-driven deploy orchestrator (Phase 1 MVP)
app.include_router(cto_customer.router, prefix=API_PREFIX)  # CTO Customer Portal: customer-facing AI deploy assist (Charter LOCK + Watcher + AutoLock)
from app.api import registry as zeni_registry
app.include_router(zeni_registry.router,          prefix=API_PREFIX)  # Zeni Container Registry · per-workspace AR repo (replace Docker Hub Pro)

# ─── Overnight build 2026-05-26: Password flow + Training pipeline + LoRA inference ─
app.include_router(password.router,               prefix=API_PREFIX)  # /auth/password/{change,forgot/init,forgot/verify,forgot/status}
app.include_router(training.router,               prefix=API_PREFIX)  # /training/datasets + /training/jobs
app.include_router(lora_inference.router,         prefix=API_PREFIX)  # /design/render-vietcontech-style + /design/lora-models


# ─── Frontend SPA mount ──────────────────────────────
# In Docker, ./frontend is mounted as /app/static; fallback to project-local path for dev.
_static_dir = Path("/app/static")
if not _static_dir.exists():
    _static_dir = Path(__file__).resolve().parent.parent.parent / "frontend"

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    _landing_path = _static_dir / "landing.html"
    _app_path = _static_dir / "index.html"
    _signup_path = _static_dir / "signup.html"

    # ── Public marketing landing at "/" ─────────────────────
    @app.get("/", include_in_schema=False)
    async def serve_landing() -> FileResponse:
        if _landing_path.exists():
            return FileResponse(str(_landing_path))
        return FileResponse(str(_app_path))  # fallback if no landing yet

    # ── Public signup form at /signup ───────────────────────
    @app.get("/signup", include_in_schema=False)
    async def serve_signup() -> FileResponse:
        if _signup_path.exists():
            return FileResponse(str(_signup_path))
        return FileResponse(str(_landing_path) if _landing_path.exists() else str(_app_path))

    # ── 3h Sprint pages (v52): /pricing, /onboarding, /docs ─
    _pricing_path = _static_dir / "pricing.html"
    _onboarding_path = _static_dir / "onboarding.html"
    _docs_dir = _static_dir / "docs"

    @app.get("/pricing", include_in_schema=False)
    async def serve_pricing() -> FileResponse:
        if _pricing_path.exists():
            return FileResponse(str(_pricing_path))
        return FileResponse(str(_landing_path))

    @app.get("/onboarding", include_in_schema=False)
    async def serve_onboarding() -> FileResponse:
        if _onboarding_path.exists():
            return FileResponse(str(_onboarding_path))
        return FileResponse(str(_landing_path))

    @app.get("/docs", include_in_schema=False)
    @app.get("/docs/", include_in_schema=False)
    async def serve_docs_index() -> FileResponse:
        idx = _docs_dir / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return FileResponse(str(_landing_path))

    @app.get("/docs/{page:path}", include_in_schema=False)
    async def serve_docs_page(page: str) -> FileResponse:
        # Normalize: docs/ai-call → docs/ai-call.html
        if page and not page.endswith(".html"):
            page = page + ".html"
        target = _docs_dir / page
        # Security: prevent path traversal
        try:
            target_resolved = target.resolve()
            if _docs_dir.resolve() not in target_resolved.parents and target_resolved != _docs_dir.resolve():
                return FileResponse(str(_docs_dir / "index.html")) if (_docs_dir / "index.html").exists() else FileResponse(str(_landing_path))
        except Exception:
            return FileResponse(str(_landing_path))
        if target.exists() and target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(_docs_dir / "index.html")) if (_docs_dir / "index.html").exists() else FileResponse(str(_landing_path))

    # ── Legal pages /legal, /legal/{page} ────────────────────
    # Bug chairman 2026-05-16: signup link /legal/terms.html bị catch-all bắt
    # → serve landing thay vì terms. Fix: add explicit route TRƯỚC catch-all.
    _legal_dir = _static_dir / "legal"

    @app.get("/legal", include_in_schema=False)
    @app.get("/legal/", include_in_schema=False)
    async def serve_legal_index() -> FileResponse:
        idx = _legal_dir / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return FileResponse(str(_landing_path))

    @app.get("/legal/{page:path}", include_in_schema=False)
    async def serve_legal_page(page: str) -> FileResponse:
        # Normalize: legal/terms → legal/terms.html
        if page and not page.endswith(".html"):
            page = page + ".html"
        target = _legal_dir / page
        # Security: prevent path traversal
        try:
            target_resolved = target.resolve()
            if _legal_dir.resolve() not in target_resolved.parents and target_resolved != _legal_dir.resolve():
                return FileResponse(str(_legal_dir / "index.html")) if (_legal_dir / "index.html").exists() else FileResponse(str(_landing_path))
        except Exception:
            return FileResponse(str(_landing_path))
        if target.exists() and target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(_legal_dir / "index.html")) if (_legal_dir / "index.html").exists() else FileResponse(str(_landing_path))

    # ── Authenticated app dashboard at /app and /app/* ──────
    @app.get("/app", include_in_schema=False)
    @app.get("/app/", include_in_schema=False)
    async def serve_app_root() -> FileResponse:
        return FileResponse(str(_app_path))

    @app.get("/app/{full_path:path}", include_in_schema=False)
    async def serve_app_spa(full_path: str) -> FileResponse:
        # SPA fallback — let client-side router handle the path
        return FileResponse(str(_app_path))

    # ── CTO Customer Portal pages (NEW) - customer-facing AI ────
    _public_pages = {
        "cto-customer": "cto-customer.html",
        "deploy-help": "cto-customer.html",
        # Overnight build 2026-05-26: password + 2FA self-service pages.
        # Bug: /forgot-password.html previously fell through to landing via catch-all.
        "forgot-password": "forgot-password.html",
        "change-password": "change-password.html",
        "setup-2fa": "setup-2fa.html",
    }
    for _route, _filename in _public_pages.items():
        _page_path = _static_dir / _filename
        if _page_path.exists():
            def _make_handler(target_path: Path = _page_path):
                async def handler() -> FileResponse:
                    return FileResponse(str(target_path))
                return handler
            _handler = _make_handler()
            app.add_api_route(f"/{_route}", _handler, methods=["GET"], include_in_schema=False)
            app.add_api_route(f"/{_route}.html", _handler, methods=["GET"], include_in_schema=False)

    # ── Catch-all for landing-style anchors (#pricing etc.) ─
    # Only handle paths that aren't reserved; otherwise let FastAPI 404
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_landi