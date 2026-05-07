"""
Pytest fixtures for Zeni Cloud Core backend.

Provides:
- `event_loop`           — session-scoped asyncio loop
- `db`                   — clean async SQLAlchemy session per test
- `app`                  — FastAPI app instance with test settings
- `client`               — httpx.AsyncClient bound to the FastAPI app
- `auth_token`           — short-lived JWT for the seeded admin user
- `auth_headers`         — Bearer-prefixed dict ready for client requests
- `workspace_id`         — id of a fresh workspace seeded for the test
- `auth_token_for_user`  — factory to mint JWT for any (user_id, workspace_id)

The fixtures load environment from CI secrets (DATABASE_URL, JWT_SECRET, etc.).
For local dev, copy `.env.example` to `.env` and source it before pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator, Callable

import pytest
import pytest_asyncio

# Make backend/ importable so `from app.main import app` works
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Sane test defaults — only set if not already provided by the runner.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://zeni_app:testpass@localhost:5432/zeni_cloud")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_ALG", "HS256")
os.environ.setdefault("VAULT_KEY", "ZmFrZS1mZXJuZXQta2V5LWZvci1jaS10ZXN0c19fX19fX19fXz0=")
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdmin123!")
os.environ.setdefault("ADMIN_NAME", "Test Admin")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:8080")
os.environ.setdefault("APP_BASE_URL", "http://localhost:8080")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("GCP_REGION", "us-central1")


# ─── Async event loop (session-scoped) ────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─── App & client ────────────────────────────────────────────────
@pytest_asyncio.fixture(scope="session")
async def app():
    """Import the FastAPI app once per session.

    We delay import so env vars are set before settings parse.
    """
    from app.main import app as fastapi_app

    yield fastapi_app


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator:
    """Async httpx client bound to the FastAPI ASGI app — no real network."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ─── DB session ──────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db():
    """Provide an async SQLAlchemy session.

    Tests should not share DB state — keep writes to throw-away rows
    (random emails, UUID workspace slugs, etc.).
    """
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        yield session
        # rollback any uncommitted state to avoid bleeding into next test
        await session.rollback()


# ─── Auth helpers ────────────────────────────────────────────────
def _mint_jwt(sub: str, workspace_id: str | None = None, role: str = "Owner") -> str:
    """Sign a short-lived HS256 JWT mirroring app.core.security."""
    import time

    from jose import jwt

    secret = os.environ["JWT_SECRET"]
    alg = os.environ.get("JWT_ALG", "HS256")
    now = int(time.time())
    payload = {
        "sub": str(sub),
        "iat": now,
        "exp": now + 600,  # 10 min
        "role": role,
    }
    if workspace_id:
        payload["workspace_id"] = str(workspace_id)
    return jwt.encode(payload, secret, algorithm=alg)


@pytest_asyncio.fixture
async def auth_token(db) -> str:
    """JWT for the seeded admin user (created by app.lifespan or migrations).

    Falls back to a synthetic UUID if the admin row is not present yet —
    routes that only verify signature will still accept it.
    """
    from sqlalchemy import select

    try:
        from app.db.models import User

        admin_email = os.environ["ADMIN_EMAIL"].lower()
        result = await db.execute(select(User).where(User.email == admin_email))
        user = result.scalar_one_or_none()
        sub = str(user.id) if user else str(uuid.uuid4())
    except Exception:
        sub = str(uuid.uuid4())

    return _mint_jwt(sub, role="Owner")


@pytest.fixture
def auth_headers(auth_token) -> dict[str, str]:
    """Ready-to-use Authorization header dict."""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def auth_token_for_user() -> Callable[[str, str | None, str], str]:
    """Factory: mint a JWT for arbitrary user_id / workspace_id / role."""

    def _make(user_id: str, workspace_id: str | None = None, role: str = "Member") -> str:
        return _mint_jwt(user_id, workspace_id, role)

    return _make


# ─── Workspace fixture ───────────────────────────────────────────
@pytest_asyncio.fixture
async def workspace_id(db) -> str:
    """Seed a throw-away workspace and return its id.

    Skips silently if Workspace model/table is not available
    (test_smoke does not need this).
    """
    try:
        from app.db.models import Workspace

        ws = Workspace(
            name=f"test-ws-{uuid.uuid4().hex[:8]}",
            slug=f"test-{uuid.uuid4().hex[:8]}",
        )
        db.add(ws)
        await db.commit()
        await db.refresh(ws)
        return str(ws.id)
    except Exception as e:
        pytest.skip(f"workspace fixture unavailable: {e}")
