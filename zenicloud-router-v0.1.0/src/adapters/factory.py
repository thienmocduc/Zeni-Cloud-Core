"""
ZeniCloud Router - Adapter Factory.
Returns mock or real adapter based on settings.USE_MOCK_ADAPTERS.
"""
from src.adapters.base import BaseAdapter
from src.adapters.mock import MockAdapter
from src.core.config import settings
from src.core.logging import get_logger
from src.core.registry import ModelEntry, Provider

logger = get_logger(__name__)


# Cache adapter instances (singleton per provider)
_adapter_cache: dict[str, BaseAdapter] = {}


def get_adapter(model: ModelEntry) -> BaseAdapter:
    """Return adapter for given model. Mock if USE_MOCK_ADAPTERS=True."""
    cache_key = f"{model.provider.value}:{settings.USE_MOCK_ADAPTERS}"
    if cache_key in _adapter_cache:
        return _adapter_cache[cache_key]

    if settings.USE_MOCK_ADAPTERS:
        adapter: BaseAdapter = MockAdapter(provider_name=model.provider.value)
        logger.info("adapter_init_mock", provider=model.provider.value)
    else:
        adapter = _create_real_adapter(model)

    _adapter_cache[cache_key] = adapter
    return adapter


def _create_real_adapter(model: ModelEntry) -> BaseAdapter:
    """Lazy import real adapters - only when keys are available."""
    if model.provider == Provider.ANTHROPIC:
        from src.adapters.anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter()

    # Stubs for now - same pattern as Anthropic
    if model.provider == Provider.OPENAI:
        # from src.adapters.openai_adapter import OpenAIAdapter
        # return OpenAIAdapter()
        raise NotImplementedError("OpenAI real adapter pending — set USE_MOCK_ADAPTERS=true")

    if model.provider == Provider.GOOGLE:
        raise NotImplementedError("Google real adapter pending — set USE_MOCK_ADAPTERS=true")

    if model.provider == Provider.AWS_BEDROCK:
        raise NotImplementedError("Bedrock real adapter pending — set USE_MOCK_ADAPTERS=true")

    if model.provider == Provider.GCP_VERTEX:
        raise NotImplementedError("Vertex real adapter pending — set USE_MOCK_ADAPTERS=true")

    raise ValueError(f"Unknown provider: {model.provider}")


def reset_cache() -> None:
    """For tests / config changes."""
    _adapter_cache.clear()
