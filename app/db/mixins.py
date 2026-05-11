"""ExpiringMixin — sweep contract for tables with explicit TTL (PLAN.md §3.2)."""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession


class ExpiringMixin:
    """Subclasses must define `__ttl__` (used by callers to set `expires_at` on insert)
    and have an `expires_at: Mapped[datetime]` column (defined per model since it's a
    real DB column, not a mixin column — keeps Alembic autogenerate honest).

    Models inheriting: MagicLinkToken, IdempotencyKey, RateLimitHit, Session.
    Not: Event (no TTL in v1), Report, Customer.
    """

    __ttl__: ClassVar[timedelta]

    @classmethod
    async def cleanup_expired(cls, session: AsyncSession) -> int:
        result = await session.execute(
            delete(cls).where(cls.expires_at < datetime.now(tz=UTC))  # type: ignore[attr-defined]
        )
        return result.rowcount or 0
