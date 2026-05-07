"""
ZeniCloud Router - Configuration
All secrets via env vars. Never hardcode.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Service ───
    APP_NAME: str = "zenicloud-router"
    APP_VERSION: str = "0.1.0"
    ENV: Literal["dev", "staging", "production"] = "dev"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # ─── Security ───
    JWT_SECRET: SecretStr = Field(default=SecretStr("CHANGE_ME_IN_PROD_USE_64CHAR_RANDOM"))
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60
    API_KEY_HEADER: str = "X-Zeni-API-Key"
    ALLOWED_ORIGINS: list[str] = [
        "https://zenicloud.io",
        "https://zenidigital.com",
        "https://zeni.holdings",
        "http://localhost:3000",
    ]
    RATE_LIMIT_PER_MINUTE: int = 100
    MAX_REQUEST_SIZE_MB: int = 10

    # ─── Database ───
    DATABASE_URL: str = "postgresql+asyncpg://zeni:zeni@localhost:5432/zenicloud_router"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10

    # ─── Cache ───
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_SECONDS: int = 300
    SEMANTIC_CACHE_ENABLED: bool = True

    # ─── Provider API Keys (loaded but NEVER logged) ───
    ANTHROPIC_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    AWS_ACCESS_KEY_ID: SecretStr | None = None
    AWS_SECRET_ACCESS_KEY: SecretStr | None = None
    AWS_REGION: str = "ap-southeast-1"
    GCP_PROJECT_ID: str | None = None
    GCP_LOCATION: str = "asia-southeast1"

    # ─── Routing Strategy (80/15/5) ───
    USE_MOCK_ADAPTERS: bool = True  # Switch to False when real keys are provided
    DEFAULT_TIER: Literal["fast", "balanced", "frontier"] = "balanced"
    QUALITY_THRESHOLD_FAST: float = 0.65
    QUALITY_THRESHOLD_BALANCED: float = 0.85
    COST_BUDGET_USD_PER_REQUEST: float = 0.50
    ENABLE_FAILOVER: bool = True
    FAILOVER_TIMEOUT_SECONDS: int = 8

    # ─── Telemetry ───
    PROMETHEUS_ENABLED: bool = True
    OTEL_ENABLED: bool = False
    OTEL_ENDPOINT: str = "http://localhost:4317"

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
