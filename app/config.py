"""Application settings, read once from the environment via get_settings()."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration mirroring the table in docs/architecture.md."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    redis_url: str
    qg_admin_token: str = Field(min_length=1)
    database_url: str = "sqlite:///./data/quotaguard.db"
    qg_service_token: str | None = None
    redis_timeout_ms: int = Field(default=100, ge=1, le=60_000)
    key_cache_ttl_seconds: int = Field(default=5, ge=0, le=3600)
    rollup_interval_seconds: int = Field(default=300, ge=0, le=86_400)
    webhook_timeout_seconds: int = Field(default=5, ge=1, le=120)
    webhook_max_attempts: int = Field(default=5, ge=1, le=50)
    qg_webhook_secret: str | None = None
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance; the cache is cleared by tests that swap the environment."""
    return Settings()
