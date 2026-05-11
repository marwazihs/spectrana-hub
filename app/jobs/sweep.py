"""Nightly sweep of expired rows (PLAN.md §3.2, §10).

Each ExpiringMixin subclass owns a `cleanup_expired(session)` classmethod.
This module composes them in a fixed order and commits once at the end.
Order doesn't matter for correctness — tables have no FK between them —
but is stable for log readability.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import MagicLinkToken, RateLimitHit, Session
from app.reports.models import IdempotencyKey


logger = logging.getLogger("hub.jobs.sweep")


SWEEP_TARGETS = [MagicLinkToken, Session, IdempotencyKey, RateLimitHit]


async def sweep_expired(session: AsyncSession) -> dict[str, int]:
    """Delete expired rows from every ExpiringMixin table. Returns per-table count."""
    counts: dict[str, int] = {}
    for cls in SWEEP_TARGETS:
        deleted = await cls.cleanup_expired(session)
        counts[cls.__tablename__] = deleted
    await session.commit()
    logger.info("sweep_expired complete: %s", counts)
    return counts
