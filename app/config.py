"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "fastapi-payment-gateway"
    app_env: str = Field(default="development", pattern="^(development|test|staging|production)$")
    log_level: str = "INFO"

    database_url: str = "sqlite:///./gateway.db"

    bkash_webhook_secret: str = "bkash-sandbox-secret"
    sslcommerz_webhook_secret: str = "sslcz-sandbox-secret"
    stripe_webhook_secret: str = "whsec_sandbox_secret"

    webhook_timeout_seconds: float = 5.0
    webhook_max_attempts: int = 3
    webhook_retry_backoff_seconds: float = 0.5
    webhook_signature_tolerance_seconds: int = 300

    idempotency_ttl_hours: int = 24
    # How long a request holds an Idempotency-Key while it runs. Keep it above the slowest
    # provider round trip; a crashed request releases the key when this expires.
    idempotency_lock_seconds: int = Field(default=60, ge=1, le=3600)

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Managed Postgres (Render, Heroku) hands out ``postgres://`` or ``postgresql://`` URLs;
        SQLAlchemy needs the installed psycopg 3 driver spelled out."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
