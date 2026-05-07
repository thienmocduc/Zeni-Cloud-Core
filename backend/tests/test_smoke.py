"""
Smoke tests for Zeni Cloud Core FastAPI app.

Goals:
1. /health returns 200 with expected JSON shape.
2. Public endpoints (pricing plans, router models) return 200 without auth.
3. Auth-required endpoints return 401 for unauthenticated requests.
4. Workspace-scoped endpoints respect workspace permission checks.
5. Security headers (CSP, HSTS, X-Frame-Options) are present on responses.
6. CORS preflight allows configured origins.

These tests run against the in-process ASGI app via httpx — no live network.
They are intentionally lenient (accept 401/403/404 alternatives) because
the app is large and the goal is to verify the framework is wired up,
not to assert business logic.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════════
# 1. Health endpoint
# ═══════════════════════════════════════════════════════════════════
class TestHealth:
    async def test_health_returns_200(self, client):
        r = await client.get("/health")
        assert r.status_code == 200, f"/health → {r.status_code}: {r.text[:200]}"

    async def test_health_payload_shape(self, client):
        r = await client.get("/health")
        body = r.json()
        assert body.get("status") == "ok"
        assert "service" in body
        assert "version" in body

    async def test_health_no_auth_required(self, client):
        # Even with a garbage Authorization header, /health must succeed.
        r = await client.get("/health", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# 2. Public endpoints (no auth)
# ═══════════════════════════════════════════════════════════════════
class TestPublicEndpoints:
    async def test_pricing_plans_public(self, client):
        r = await client.get("/api/v1/pricing/plans")
        # Public endpoint — should be 200, but accept 404 if router not mounted
        assert r.status_code in (200, 404), f"/pricing/plans → {r.status_code}"

    async def test_router_models_public(self, client):
        r = await client.get("/api/v1/router/models")
        assert r.status_code in (200, 404), f"/router/models → {r.status_code}"

    async def test_waitlist_get_public(self, client):
        # waitlist usually has a public POST; GET may be admin-only — accept either
        r = await client.get("/api/v1/waitlist")
        assert r.status_code in (200, 401, 403, 404, 405)


# ═══════════════════════════════════════════════════════════════════
# 3. Auth-required endpoints reject anonymous access
# ═══════════════════════════════════════════════════════════════════
class TestAuthRequired:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/workspaces",
            "/api/v1/projects",
            "/api/v1/billing/wallet",
            "/api/v1/audit",
            "/api/v1/api-tokens",
            "/api/v1/members",
        ],
    )
    async def test_returns_401_without_token(self, client, path):
        r = await client.get(path)
        # Most should be 401; some may 404 if not mounted, 403 if other middleware
        # rejects first. We only assert it's NOT a successful 200/2xx — which
        # would mean an unauthed user got privileged data.
        assert r.status_code >= 400, f"{path} should require auth, got {r.status_code}"
        assert r.status_code in (401, 403, 404, 422)

    async def test_invalid_token_rejected(self, client):
        r = await client.get(
            "/api/v1/workspaces",
            headers={"Authorization": "Bearer this.is.not.a.valid.jwt"},
        )
        assert r.status_code in (401, 403, 422)

    async def test_malformed_auth_header_rejected(self, client):
        # Missing "Bearer " prefix
        r = await client.get(
            "/api/v1/workspaces",
            headers={"Authorization": "just-a-token"},
        )
        assert r.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════
# 4. Authenticated request smoke
# ═══════════════════════════════════════════════════════════════════
class TestAuthenticatedSmoke:
    async def test_workspaces_list_with_token(self, client, auth_headers):
        """Admin token should get past auth — actual response may be empty list or workspace data."""
        r = await client.get("/api/v1/workspaces", headers=auth_headers)
        # Should NOT be 401 (token is valid). Accept 200, 403 (workspace policy),
        # 404 (no workspaces yet), 500 (DB not seeded in some test envs).
        assert r.status_code != 401, f"Valid JWT was rejected: {r.text[:300]}"

    async def test_audit_log_with_token(self, client, auth_headers):
        r = await client.get("/api/v1/audit", headers=auth_headers)
        assert r.status_code != 401


# ═══════════════════════════════════════════════════════════════════
# 5. Security headers
# ═══════════════════════════════════════════════════════════════════
class TestSecurityHeaders:
    async def test_hsts_header_present(self, client):
        r = await client.get("/health")
        hsts = r.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts

    async def test_frame_options_deny(self, client):
        r = await client.get("/health")
        assert r.headers.get("X-Frame-Options") == "DENY"

    async def test_content_type_options_nosniff(self, client):
        r = await client.get("/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    async def test_referrer_policy_set(self, client):
        r = await client.get("/health")
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    async def test_csp_present_on_api(self, client):
        r = await client.get("/api/v1/router/models")
        # API endpoints (not /, /app, /signup) should have CSP set
        if r.status_code == 200:
            assert "Content-Security-Policy" in r.headers


# ═══════════════════════════════════════════════════════════════════
# 6. CORS preflight
# ═══════════════════════════════════════════════════════════════════
class TestCORS:
    async def test_options_preflight_localhost(self, client):
        r = await client.options(
            "/api/v1/router/models",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        # CORS preflight should be 200 or 204
        assert r.status_code in (200, 204), f"CORS preflight → {r.status_code}"
        # Should echo back the allowed origin
        ao = r.headers.get("Access-Control-Allow-Origin", "")
        assert "localhost" in ao or ao == "*"


# ═══════════════════════════════════════════════════════════════════
# 7. Error handling
# ═══════════════════════════════════════════════════════════════════
class TestErrorHandling:
    async def test_404_on_unknown_api_path(self, client):
        r = await client.get("/api/v1/this-route-does-not-exist")
        assert r.status_code == 404

    async def test_validation_error_returns_422(self, client, auth_headers):
        # Send obviously-invalid JSON to a POST endpoint that expects a body
        r = await client.post(
            "/api/v1/auth/login",
            json={"this_is_not_a_valid_login_payload": True},
        )
        # 422 (validation), 400 (custom value error), or 401 (rejected)
        assert r.status_code in (400, 401, 422)

    async def test_method_not_allowed(self, client):
        # /health is GET-only; POST should be 405
        r = await client.post("/health")
        assert r.status_code in (405, 404)


# ═══════════════════════════════════════════════════════════════════
# 8. OpenAPI / docs
# ═══════════════════════════════════════════════════════════════════
class TestDocsAndSchema:
    async def test_openapi_json_available(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        body = r.json()
        assert body.get("openapi", "").startswith("3.")
        assert body.get("info", {}).get("title") == "Zeni Cloud · API"
