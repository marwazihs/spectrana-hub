"""Auth-domain tables: customers, magic-link tokens, sessions, rate-limit hits.

RateLimitHit lives here because the dominant usage is magic-link issuance
throttling (§3.1 rate_limit_hits). If a later milestone makes report-scoped
limits dominant, it can move without contract impact — table name is stable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.config import settings
from app.db.base import Base
from app.db.mixins import ExpiringMixin


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    allowlist_emails: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    api_key_hash: Mapped[str] = mapped_column(String, nullable=False)
    api_key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("customers_api_key_prefix_idx", "api_key_prefix"),)


class MagicLinkToken(Base, ExpiringMixin):
    __tablename__ = "magic_link_tokens"
    __ttl__ = timedelta(minutes=settings.MAGIC_LINK_TTL_MINUTES)

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("magic_link_tokens_expires_idx", "expires_at"),)


class Session(Base, ExpiringMixin):
    """Session cookies. Sliding 30-day expiry (PLAN.md §3.1, §5)."""

    __tablename__ = "sessions"
    __ttl__ = timedelta(days=settings.SESSION_TTL_DAYS)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("sessions_expires_idx", "expires_at"),)


class RateLimitHit(Base, ExpiringMixin):
    """Token-bucket scope counters. scope_key shapes: 'ip:1.2.3.4' or 'report:<uuid>'."""

    __tablename__ = "rate_limit_hits"
    # Window granularity is per-caller (minute for IP, hour for report). __ttl__ here
    # is the conservative max — actual rows get expires_at set by the caller.
    __ttl__ = timedelta(hours=1)

    scope_key: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("rate_limit_hits_expires_idx", "expires_at"),)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
