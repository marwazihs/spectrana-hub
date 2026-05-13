"""Settings — single source of truth for env-var contract (PLAN.md §7)."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="forbid",
    )

    # App identity & domain (Pattern 1 / Level A)
    HUB_PRIMARY_DOMAIN: str = "hub.majeve.com"
    HUB_REPORTS_DOMAIN: str = "reports.hub.majeve.com"
    HUB_EMAIL_FROM: str = "Majeve Reports <reports@majeve.com>"
    HUB_EMAIL_BRAND_NAME: str = "Majeve"

    # Secrets (three distinct values)
    HUB_SESSION_SECRET: SecretStr = SecretStr("")
    HUB_IFRAME_JWT_SECRET: SecretStr = SecretStr("")
    HUB_IFRAME_JWT_SECRET_PREVIOUS: SecretStr | None = None
    HUB_MAGIC_LINK_HASH_SECRET: SecretStr = SecretStr("")
    # Shared secret for /internal/* routes consumed by the Next.js frontend
    # (server-to-server only; never exposed to the browser). Must be ≥32 bytes
    # in production; an empty value disables /internal/* (401 on every call).
    HUB_INTERNAL_TOKEN: SecretStr = SecretStr("")

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://hub:hub@localhost:5432/hub"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_POOL_MAX_OVERFLOW: int = 10
    DATABASE_POOL_RECYCLE_SECONDS: int = 1800

    # AWS S3
    AWS_REGION: str = "us-east-1"
    AWS_S3_BUCKET: str = "hub-reports-prod"
    AWS_S3_ENDPOINT_URL: str | None = None
    AWS_ACCESS_KEY_ID: SecretStr | None = None
    AWS_SECRET_ACCESS_KEY: SecretStr | None = None

    # URL scheme for outbound magic links (email body + API response).
    # Defaults to https for prod; set HUB_URL_SCHEME=http in local compose
    # so SMTP-delivered links and the agent's return URL both work over
    # http://localhost.
    HUB_URL_SCHEME: str = "https"

    # SMTP
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: SecretStr = SecretStr("")
    SMTP_STARTTLS: bool = True

    # TTLs and tunables
    SESSION_TTL_DAYS: int = 30
    MAGIC_LINK_TTL_MINUTES: int = 15
    IFRAME_JWT_TTL_SECONDS: int = 60
    IDEMPOTENCY_TTL_HOURS: int = 24

    # Rate limits
    RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN: int = 5
    RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR: int = 10
    # Generous bucket for agent-authenticated mint (per API key). Agents
    # need to mint many links per hour (one per recipient on a broadcast);
    # the per-IP and per-report buckets that protect the public form aren't
    # the right shape here.
    RATE_LIMIT_MAGIC_LINK_PER_API_KEY_PER_HOUR: int = 100

    # Payload limits
    MAX_HTML_BYTES: int = 10 * 1024 * 1024  # 10 MB
    MAX_SUPPLEMENTARY_BYTES: int = 100 * 1024 * 1024  # 100 MB

    # Operational
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str | None = None


settings = Settings()
