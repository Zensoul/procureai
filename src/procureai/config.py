# ==============================================================================
# PROCUREAI — config.py
# Central configuration management using pydantic-settings.
#
# WHY THIS FILE EXISTS:
# Every piece of configuration — database URL, API keys, environment name —
# must come from environment variables in production. Hard-coding these values
# is a security vulnerability and makes deployment impossible.
#
# pydantic-settings reads environment variables, validates their types, and
# makes them available as a typed Python object throughout the application.
# If a required variable is missing, the app refuses to start — immediately,
# with a clear error — not 3 hours later when the first request hits a
# missing API key.
# ==============================================================================

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application settings loaded from environment variables.

    Pydantic-settings automatically reads from:
    1. Environment variables (highest priority — used in production)
    2. .env file (used in development)

    The order matters: production environment variables always override
    anything in .env. This means you can safely have a .env file locally
    without it affecting production deployments on Railway.
    """

    # ── Model configuration ────────────────────────────────────────────────────
    # Tell pydantic-settings where to find the .env file and how to handle it.
    model_config = SettingsConfigDict(
        # Look for .env in the project root directory
        env_file=".env",
        # If .env doesn't exist (production), don't raise an error
        env_file_encoding="utf-8",
        # Ignore extra variables in .env that aren't defined here
        extra="ignore",
        # Variable names are case-insensitive: DATABASE_URL == database_url
        case_sensitive=False,
    )

    # ── Application Settings ───────────────────────────────────────────────────
    # Literal type restricts to exactly these three values.
    # If APP_ENV=staging is set, pydantic raises a validation error immediately.
    app_env: Literal["development", "production", "testing"] = Field(
        default="development",
        description="Current environment. Controls logging, Sentry, debug mode."
    )

    app_name: str = Field(
        default="ProcureAI",
        description="Application name used in API responses and logs."
    )

    # Used to sign JWT tokens. Must be at least 32 characters.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    secret_key: str = Field(
        ...,  # ... means required — app won't start without this
        min_length=32,
        description="Secret key for JWT token signing. Never expose this."
    )

    # JWT tokens expire after this many minutes.
    # 60 minutes for regular users. n8n service tokens are long-lived (separate).
    access_token_expire_minutes: int = Field(
        default=60,
        gt=0,  # Must be greater than 0
        description="JWT access token expiry in minutes."
    )

    # ── Database Settings ──────────────────────────────────────────────────────
    # Full async PostgreSQL connection URL.
    # Format: postgresql+asyncpg://user:password@host:port/dbname
    # The +asyncpg part tells SQLAlchemy to use the async driver.
    database_url: str = Field(
        ...,
        description="Async PostgreSQL connection URL. Must use postgresql+asyncpg:// scheme."
    )

    # Connection pool settings — how many database connections to maintain.
    # pool_size: connections kept open permanently (ready to use instantly)
    # max_overflow: extra connections allowed during traffic spikes
    # Total max connections = pool_size + max_overflow
    # Keep this below your PostgreSQL max_connections limit (usually 100)
    db_pool_size: int = Field(default=5, gt=0, le=20)
    db_max_overflow: int = Field(default=10, gt=0, le=30)

    # ── GST Integration — Sandbox.co.in ───────────────────────────────────────
    # Sandbox.co.in is the Technical Service Provider (TSP) that abstracts
    # GSTN's complex API (encryption, OTP sessions, digital signing) into
    # simple REST calls.
    sandbox_api_key: str = Field(
        ...,
        description="Sandbox.co.in API key for GST data access."
    )

    sandbox_base_url: str = Field(
        default="https://api.sandbox.co.in",
        description="Sandbox.co.in base URL. Override for sandbox/test environment."
    )

    # How long to wait for GSTN API responses before timing out.
    # GSTN can be slow — 30 seconds is realistic for production.
    sandbox_timeout_seconds: int = Field(default=30, gt=0, le=120)

    # ── Messaging — MSG91 ─────────────────────────────────────────────────────
    # MSG91 is the Indian messaging provider for RCS and SMS delivery.
    # DLT Sender ID must be pre-registered with TRAI.
    msg91_api_key: str = Field(
        ...,
        description="MSG91 API key for RCS and SMS delivery."
    )

    msg91_sender_id: str = Field(
        default="PRCRAI",
        max_length=6,  # TRAI mandates 6-character sender IDs
        description="DLT-registered sender ID. Exactly 6 characters."
    )

    msg91_base_url: str = Field(
        default="https://api.msg91.com/api/v5",
        description="MSG91 API base URL."
    )

    # ── AI — Anthropic ────────────────────────────────────────────────────────
    # Used only for PDF invoice parsing (Feature 1 fallback).
    # Claude Sonnet 4.6 is the correct model for this use case —
    # accurate enough for structured data extraction, cost-effective.
    openai_api_key: str = Field(
    ...,
    description="OpenAI API key for invoice PDF parsing."
    )

    openai_model: str = Field(
    gdefault="gpt-4o",
    description="OpenAI model for PDF extraction."
    )

    # ── Observability — Sentry ────────────────────────────────────────────────
    # Sentry captures unhandled exceptions in production and sends alerts.
    # Optional in development — if not set, errors only appear in logs.
    sentry_dsn: str | None = Field(
        default=None,
        description="Sentry DSN for error tracking. Optional in development."
    )

    # Fraction of transactions to trace for performance monitoring.
    # 0.1 = 10% of requests. Enough data without excessive cost.
    sentry_traces_sample_rate: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Sentry performance traces sample rate. 0.0 to 1.0."
    )

    # ── Security — Encryption ─────────────────────────────────────────────────
    # Fernet symmetric encryption key for sensitive database fields.
    # Used to encrypt GSTIN credentials stored in the database.
    # Generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = Field(
        ...,
        description="Fernet encryption key for sensitive data at rest."
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for Celery broker and result backend."
    )

    telegram_bot_token: str = Field(
        ...,
       description="Telegram bot token from BotFather"
    )

    # ── Computed Properties ───────────────────────────────────────────────────
    # These are derived from other settings — not read from environment.
    # @computed_field makes them available as regular attributes.

    @computed_field
    @property
    def is_production(self) -> bool:
        """
        True when running in production.
        Used to enable Sentry, disable debug endpoints, enforce HTTPS.
        """
        return self.app_env == "production"

    @computed_field
    @property
    def is_development(self) -> bool:
        """
        True in local development.
        Enables auto-reload, verbose logging, relaxed CORS.
        """
        return self.app_env == "development"

    @computed_field
    @property
    def is_testing(self) -> bool:
        """
        True when running pytest.
        Uses test database, disables external API calls.
        """
        return self.app_env == "testing"

    @computed_field
    @property
    def log_level(self) -> str:
        """
        Log level based on environment.
        Development: DEBUG (see everything)
        Production: INFO (see important events, not noise)
        Testing: WARNING (only see problems)
        """
        levels = {
            "development": "DEBUG",
            "production": "INFO",
            "testing": "WARNING",
        }
        return levels[self.app_env]


# ==============================================================================
# SINGLETON PATTERN — get_settings()
#
# WHY lru_cache?
# Settings reads from environment variables and validates them.
# We don't want this to happen on every function call — once is enough.
# lru_cache(maxsize=1) runs get_settings() exactly once and returns
# the same Settings object for every subsequent call.
#
# HOW TO USE throughout the application:
#   from procureai.config import get_settings
#   settings = get_settings()
#   print(settings.database_url)
#
# HOW TO OVERRIDE IN TESTS:
#   from procureai.config import get_settings
#   app.dependency_overrides[get_settings] = lambda: Settings(
#       app_env="testing",
#       database_url="postgresql+asyncpg://test:test@localhost/test_procureai",
#       ...
#   )
# ==============================================================================

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns the application settings singleton.
    Reads from environment variables and .env file on first call only.
    Subsequent calls return the cached instance.
    """
    return Settings()  # type: ignore[call-arg]