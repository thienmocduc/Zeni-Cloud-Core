from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql+asyncpg://zeni:zeni_dev_pass@localhost:5432/zeni_cloud"
    redis_url: str = ""  # optional — empty = in-memory fallback for MVP on Cloud Run

    jwt_secret: str = "dev_jwt_secret_change_me_in_prod_32chars_min"
    jwt_alg: str = "HS256"
    jwt_access_ttl: int = 3600
    jwt_refresh_ttl: int = 2592000

    vault_key: str = ""

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # ─── Google Cloud ──────────────────────────────
    gcp_project_id: str = "zeni-cloud-core"
    gcp_region: str = "us-central1"
    google_application_credentials: str = ""   # path to service account JSON
    gcs_bucket_prefix: str = "zeni-"

    allowed_origins: str = "http://localhost:8080"
    app_base_url: str = "http://localhost:8080"

    admin_email: str = "ceo@zeni-holdings.vn"
    admin_password: str = "ChangeMeImmediately123!"
    admin_name: str = "CEO Zeni"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
