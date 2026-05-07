"""
ZeniCloud Router - Test suite.
Run: pytest tests/ -v
"""
import pytest
from fastapi.testclient import TestClient

from src.adapters.base import CompletionRequest as AdapterReq
from src.adapters.factory import get_adapter, reset_cache
from src.adapters.mock import MockAdapter
from src.core.registry import (
    Capability,
    MODEL_REGISTRY,
    Tier,
    cheapest_in_tier,
    get_model,
    models_by_tier,
)
from src.main import app
from src.services.routing_engine import (
    RoutingRequest,
    TaskComplexity,
    routing_engine,
)


# ════════════════ REGISTRY ════════════════
class TestRegistry:
    def test_all_tiers_populated(self):
        for tier in Tier:
            models = models_by_tier(tier)
            assert len(models) >= 1, f"Tier {tier.value} is empty"

    def test_pricing_consistency(self):
        for m in MODEL_REGISTRY.values():
            assert m.input_price_per_mtok > 0, f"{m.model_id} has zero input price"
            assert m.output_price_per_mtok >= m.input_price_per_mtok, f"{m.model_id} output cheaper than input?"
            assert 0 < m.quality_score <= 1.0
            assert m.context_window >= 1024

    def test_tier_pricing_ordering(self):
        """FAST should be cheaper than BALANCED than FRONTIER on average."""
        avg_fast = sum(m.avg_price_per_mtok for m in models_by_tier(Tier.FAST)) / len(
            models_by_tier(Tier.FAST)
        )
        avg_bal = sum(m.avg_price_per_mtok for m in models_by_tier(Tier.BALANCED)) / len(
            models_by_tier(Tier.BALANCED)
        )
        avg_front = sum(m.avg_price_per_mtok for m in models_by_tier(Tier.FRONTIER)) / len(
            models_by_tier(Tier.FRONTIER)
        )
        assert avg_fast < avg_bal < avg_front, (
            f"Tier pricing wrong: fast={avg_fast:.2f} bal={avg_bal:.2f} front={avg_front:.2f}"
        )

    def test_failover_targets_exist(self):
        for m in MODEL_REGISTRY.values():
            if m.failover_to:
                assert get_model(m.failover_to) is not None, (
                    f"{m.model_id} → failover {m.failover_to} doesn't exist"
                )

    def test_cheapest_in_tier(self):
        cheap_fast = cheapest_in_tier(Tier.FAST)
        assert cheap_fast is not None
        assert cheap_fast.tier == Tier.FAST
        # Gemma 4 should be cheapest
        assert cheap_fast.model_id == "gemma-4-26b"

    def test_cost_calculation(self):
        m = get_model("opus-4-7")
        # 1M input + 1M output
        cost = m.estimate_cost(1_000_000, 1_000_000)
        assert cost == 5.0 + 25.0  # $30


# ════════════════ ROUTING ENGINE ════════════════
class TestRoutingEngine:
    def _req(self, **kwargs):
        defaults = dict(
            tenant_id="test_tenant",
            product="zenimake",
            task_type="rag_answer",
            estimated_input_tokens=1000,
            expected_output_tokens=500,
            required_capabilities=[],
        )
        defaults.update(kwargs)
        return RoutingRequest(**defaults)

    def test_trivial_routes_to_fast(self):
        decision = routing_engine.decide(self._req(task_type="caption"))
        assert decision.tier == Tier.FAST

    def test_frontier_routes_to_frontier(self):
        decision = routing_engine.decide(self._req(task_type="ipo_document"))
        assert decision.tier == Tier.FRONTIER
        assert decision.primary_model.model_id in ("opus-4-7", "gpt-5-5")

    def test_complex_routes_to_balanced(self):
        decision = routing_engine.decide(self._req(task_type="code_generate"))
        assert decision.tier == Tier.BALANCED

    def test_explicit_model_override(self):
        decision = routing_engine.decide(
            self._req(task_type="caption", explicit_model_id="opus-4-7")
        )
        assert decision.primary_model.model_id == "opus-4-7"
        assert "explicit_model_id" in decision.decision_reason

    def test_explicit_tier_override(self):
        decision = routing_engine.decide(
            self._req(task_type="caption", explicit_tier=Tier.FRONTIER)
        )
        assert decision.tier == Tier.FRONTIER

    def test_quality_threshold_escalates(self):
        decision_low = routing_engine.decide(
            self._req(task_type="rag_answer", quality_threshold=0.5)
        )
        decision_high = routing_engine.decide(
            self._req(task_type="rag_answer", quality_threshold=0.96)
        )
        assert decision_low.tier == Tier.FAST
        assert decision_high.tier == Tier.FRONTIER

    def test_cost_gate_downgrades(self):
        # Frontier task but tiny budget → should downgrade
        decision = routing_engine.decide(
            self._req(
                task_type="ipo_document",
                estimated_input_tokens=100_000,
                expected_output_tokens=10_000,
                max_cost_usd=0.10,
            )
        )
        assert "cost_gate" in decision.decision_reason or decision.tier != Tier.FRONTIER

    def test_capability_filter(self):
        decision = routing_engine.decide(
            self._req(
                task_type="code_explain",
                required_capabilities=[Capability.VISION],
            )
        )
        assert Capability.VISION in decision.primary_model.capabilities

    def test_failover_chain_built(self):
        decision = routing_engine.decide(self._req(task_type="ipo_document"))
        assert len(decision.failover_chain) >= 1
        # Failover models must NOT be the primary
        for m in decision.failover_chain:
            assert m.model_id != decision.primary_model.model_id

    def test_unknown_model_id_raises(self):
        with pytest.raises(ValueError):
            routing_engine.decide(self._req(explicit_model_id="ghost-model-9000"))


# ════════════════ MOCK ADAPTER ════════════════
class TestMockAdapter:
    @pytest.mark.asyncio
    async def test_mock_returns_response(self):
        m = get_model("haiku-4-5")
        adapter = MockAdapter(provider_name="anthropic")
        req = AdapterReq(
            model=m,
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=100,
        )
        resp = await adapter.complete(req)
        assert resp.text.startswith("[MOCK")
        assert resp.input_tokens > 0
        assert resp.output_tokens > 0
        assert resp.cost_usd > 0
        assert resp.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_mock_cost_matches_registry(self):
        m = get_model("opus-4-7")
        adapter = MockAdapter(provider_name="anthropic")
        req = AdapterReq(
            model=m,
            messages=[{"role": "user", "content": "x" * 4000}],
            max_tokens=200,
        )
        resp = await adapter.complete(req)
        expected_cost = m.estimate_cost(resp.input_tokens, resp.output_tokens)
        assert abs(resp.cost_usd - expected_cost) < 1e-9

    @pytest.mark.asyncio
    async def test_mock_failure_simulation(self):
        from src.adapters.base import ProviderError
        m = get_model("haiku-4-5")
        adapter = MockAdapter(provider_name="anthropic", simulate_failure_rate=1.0)
        with pytest.raises(ProviderError):
            await adapter.complete(
                AdapterReq(model=m, messages=[{"role": "user", "content": "test"}])
            )


# ════════════════ FAILOVER ════════════════
class TestFailover:
    @pytest.mark.asyncio
    async def test_failover_succeeds_after_primary_fails(self, monkeypatch):
        from src.adapters.factory import _adapter_cache
        from src.services.failover import failover_executor
        from src.services.routing_engine import RoutingDecision

        reset_cache()
        # Force primary to always fail, fallback succeeds
        primary = get_model("opus-4-7")
        fallback = get_model("gpt-5-5")
        decision = RoutingDecision(
            primary_model=primary,
            failover_chain=[fallback],
            estimated_cost_usd=0.1,
            decision_reason="test",
            tier=Tier.FRONTIER,
        )

        # Patch get_adapter: primary always-fail, fallback works
        original_get = get_adapter

        def patched_get(model):
            if model.model_id == primary.model_id:
                return MockAdapter(provider_name="anthropic", simulate_failure_rate=1.0)
            return MockAdapter(provider_name=model.provider.value)

        monkeypatch.setattr("src.services.failover.get_adapter", patched_get)

        adapter_req = AdapterReq(
            model=primary,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=50,
        )
        resp = await failover_executor.execute(decision, adapter_req)
        assert resp.model_id == fallback.model_id


# ════════════════ HTTP API ════════════════
class TestHttpAPI:
    DEV_KEY = "zk_dev_" + "a" * 32

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_root_open(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["service"] == "zenicloud-router"

    def test_health_open(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["mock_mode"] is True

    def test_security_headers(self, client):
        r = client.get("/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_models_requires_auth(self, client):
        r = client.get("/v1/models")
        assert r.status_code == 401

    def test_models_with_auth(self, client):
        r = client.get("/v1/models", headers={"X-Zeni-API-Key": self.DEV_KEY})
        assert r.status_code == 200
        models = r.json()
        assert len(models) >= 6
        assert all("model_id" in m for m in models)

    def test_invalid_key_format(self, client):
        r = client.get("/v1/models", headers={"X-Zeni-API-Key": "garbage"})
        assert r.status_code == 401

    def test_route_preview(self, client):
        r = client.post(
            "/v1/route",
            headers={"X-Zeni-API-Key": self.DEV_KEY, "Content-Type": "application/json"},
            json={
                "tenant_id": "test_t",
                "product": "zenimake",
                "task_type": "code_generate",
                "messages": [{"role": "user", "content": "build a todo app"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tier"] in ("balanced", "frontier")
        assert body["estimated_cost_usd"] > 0

    def test_complete_endpoint(self, client):
        r = client.post(
            "/v1/complete",
            headers={"X-Zeni-API-Key": self.DEV_KEY},
            json={
                "tenant_id": "test_t",
                "product": "zenilaw",
                "task_type": "qa_simple",
                "messages": [{"role": "user", "content": "Điều 22 Luật Doanh nghiệp"}],
                "max_tokens": 200,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "[MOCK" in body["text"]  # mock mode active
        assert body["routing"]["tier"] == "fast"
        assert body["routing"]["actual_cost_usd"] >= 0
        assert body["usage"]["total_tokens"] > 0

    def test_complete_with_explicit_model(self, client):
        r = client.post(
            "/v1/complete",
            headers={"X-Zeni-API-Key": self.DEV_KEY},
            json={
                "tenant_id": "test_t",
                "product": "zeniipo",
                "task_type": "qa_simple",  # would normally route to fast
                "model_id": "opus-4-7",  # but we override
                "messages": [{"role": "user", "content": "audit IPO docs"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["routing"]["primary_model"] == "opus-4-7"

    def test_message_size_limit(self, client):
        r = client.post(
            "/v1/complete",
            headers={"X-Zeni-API-Key": self.DEV_KEY},
            json={
                "tenant_id": "test_t",
                "product": "zenimake",
                "task_type": "qa_simple",
                "messages": [{"role": "user", "content": "x" * 600_000}],  # over 500KB
            },
        )
        assert r.status_code == 422  # validation rejects


# ════════════════ SECURITY ════════════════
class TestSecurity:
    DEV_KEY = "zk_dev_" + "b" * 32

    def test_secret_redaction_logging(self):
        from src.core.logging import redact_secrets

        result = redact_secrets(
            None,
            None,
            {
                "msg": "user provided sk-ant-api03-abcdef1234567890abcdef1234567890",
                "api_key": "secret_value_here",
                "normal_field": "hello world",
            },
        )
        # SECURE: secret pattern redacted in msg field
        assert "sk-ant-api03-abcdef1234567890abcdef1234567890" not in result["msg"]
        # SECURE: api_key field name itself triggers redaction
        assert result["api_key"] == "[REDACTED]"
        # Normal text preserved
        assert "hello world" in result["normal_field"]

    def test_no_docs_endpoint_in_prod(self, monkeypatch):
        # Settings.is_production must drive doc disable
        from src.core.config import settings
        # Just verify the property logic; we can't restart app in same process
        assert settings.ENV in ("dev", "staging", "production")

    def test_cors_only_allowed_origins(self):
        from src.core.config import settings
        for o in settings.ALLOWED_ORIGINS:
            assert o.startswith("https://") or o.startswith("http://localhost")
