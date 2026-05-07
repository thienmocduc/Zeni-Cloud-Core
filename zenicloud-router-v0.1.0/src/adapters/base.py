"""
ZeniCloud Router - Provider adapter base.
Common interface for all providers (Anthropic, OpenAI, Google, Bedrock, Vertex).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.core.registry import ModelEntry


@dataclass
class CompletionRequest:
    """Provider-agnostic completion input."""
    model: ModelEntry
    messages: list[dict]  # [{"role": "user|assistant|system", "content": "..."}]
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    tools: list[dict] | None = None
    system: str | None = None


@dataclass
class CompletionResponse:
    """Provider-agnostic completion output."""
    text: str
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    finish_reason: str = "stop"
    raw_response: dict | None = None


class ProviderError(Exception):
    """Base for provider failures."""
    def __init__(self, message: str, provider: str, retriable: bool = True):
        super().__init__(message)
        self.provider = provider
        self.retriable = retriable


class RateLimitError(ProviderError):
    pass


class AuthError(ProviderError):
    def __init__(self, message: str, provider: str):
        super().__init__(message, provider, retriable=False)


class BaseAdapter(ABC):
    """All provider adapters implement this."""

    provider_name: str = "base"

    @abstractmethod
    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        """Execute a completion request."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify provider is reachable + auth works."""
        ...
