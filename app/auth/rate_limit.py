"""Postgres-backed fixed-window rate limiter (PLAN.md §7).

Two limits for the magic-link request endpoint:
- 5 per IP per minute       (scope_key='ip:<addr>',       window=60 s)
- 10 per report per hour    (scope_key='report:<uuid>',   window=3600 s)

Uses INSERT ... ON CONFLICT to atomically increment a counter per
(scope_key, window_start). Eviction handled by ExpiringMixin sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitHit
from app.config import settings
from app.errors import rate_limit_exceeded


def _floor_to_window(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


async def _bump(
    session: AsyncSession,
    *,
    scope_key: str,
    window_seconds: int,
    limit: int,
) -> int:
    """Increment the bucket for this scope+window. Raise on overrun.

    Returns the post-increment count (useful for tests; handlers usually
    only care about the raise/no-raise distinction).
    """
    now = datetime.now(tz=UTC)
    window_start = _floor_to_window(now, window_seconds)
    expires_at = window_start + timedelta(seconds=window_seconds)

    stmt = (
        insert(RateLimitHit)
        .values(
            scope_key=scope_key,
            window_start=window_start,
            count=1,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["scope_key", "window_start"],
            set_={"count": RateLimitHit.count + 1},
        )
        .returning(RateLimitHit.count)
    )
    result = await session.execute(stmt)
    count = int(result.scalar_one())

    if count > limit:
        retry_after = max(1, int((expires_at - now).total_seconds()))
        raise rate_limit_exceeded(retry_after_seconds=retry_after)
    return count


async def check_magic_link_ip(session: AsyncSession, ip: str) -> None:
    await _bump(
        session,
        scope_key=f"ip:{ip}",
        window_seconds=60,
        limit=settings.RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN,
    )


async def check_magic_link_report(session: AsyncSession, report_id: UUID) -> None:
    await _bump(
        session,
        scope_key=f"report:{report_id}",
        window_seconds=3600,
        limit=settings.RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR,
    )


async def check_magic_link_api_key(session: AsyncSession, customer_id: UUID) -> None:
    """Agent-auth path uses a per-API-key (per-customer) hourly bucket.
    Skips the per-IP gate because an agent's IP serves many customers and
    isn't a meaningful unit of abuse for this surface."""
    await _bump(
        session,
        scope_key=f"apikey:{customer_id}",
        window_seconds=3600,
        limit=settings.RATE_LIMIT_MAGIC_LINK_PER_API_KEY_PER_HOUR,
    )
