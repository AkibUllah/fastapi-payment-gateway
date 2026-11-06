"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.backoff import parse_schedule


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
    # Seconds between scheduled retries, after the in-request attempts. Empty disables them.
    webhook_retry_schedule: str = "60,300,1800,7200,43200"
    webhook_retry_jitter: float = Field(default=0.1, ge=0, le=1)
    # How long a worker owns a delivery. A worker that dies mid-send frees it when this expires.
    webhook_lease_seconds: int = Field(default=60, ge=1, le=3600)
    webhook_dispatch_poll_seconds: float = Field(default=5.0, gt=0)
    webhook_dispatch_batch_size: int = Field(default=50, ge=1, le=1000)

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

    @field_validator("webhook_retry_schedule")
    @classmethod
    def _validate_retry_schedule(cls, value: str) -> str:
        parse_schedule(value)
        return value

    @model_validator(mode="after")
    def _lease_outlives_an_attempt(self) -> Settings:
        # httpx applies the timeout per phase: pool, connect, write and read.
        slowest_attempt = 4 * self.webhook_timeout_seconds
        if self.webhook_lease_seconds < slowest_attempt:
            raise ValueError(
                f"WEBHOOK_LEASE_SECONDS must be at least {slowest_attempt:g} "
                "(4 x WEBHOOK_TIMEOUT_SECONDS)"
            )
        return self

    @property
    def webhook_retry_steps(self) -> tuple[int, ...]:
        return parse_schedule(self.webhook_retry_schedule)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
