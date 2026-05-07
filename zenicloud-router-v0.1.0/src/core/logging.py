"""
ZeniCloud Router - Structured logging with secret redaction.
NEVER log: API keys, JWT, prompts (PII), responses (PII).
"""
import logging
import re
import sys
from typing import Any

import structlog

from src.core.config import settings

# Patterns to redact in any log output
SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_-]{20,})"),  # OpenAI/Anthropic (allow hyphens/underscores)
    re.compile(r"(AKIA[0-9A-Z]{16})"),  # AWS access key
    re.compile(r"(Bearer\s+[A-Za-z0-9._-]+)"),  # Bearer tokens
    re.compile(r"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"),  # JWT
    re.compile(r"(\"api_key\"\s*:\s*\"[^\"]+\")"),  # JSON api_key field
    re.compile(r"(password=\"[^\"]+\")"),
    re.compile(r"(zk_(dev|stg|prod)_[a-f0-9]{32})"),  # Zeni internal API keys
]


def redact_secrets(_logger, _method, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact secrets from any field."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            redacted = value
            for pattern in SECRET_PATTERNS:
                redacted = pattern.sub("[REDACTED]", redacted)
            # Always redact known secret keys by name
            if any(k in key.lower() for k in ["password", "secret", "key", "token", "auth"]):
                if isinstance(value, str) and len(value) > 8:
                    event_dict[key] = "[REDACTED]"
                    continue
            event_dict[key] = redacted
    return event_dict


def configure_logging() -> None:
    """Configure structured JSON logging."""
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_production:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> Any:
    return structlog.get_logger(name)
