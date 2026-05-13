"""Rate-limit service (app.auth.rate_limit)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import RateLimitHit
from app.auth.rate_limit import (
    _bump,
    _floor_to_window,
    check_magic_link_ip,
    check_magic_link_report,
)
from app.config import settings
from app.errors import HubError
from datetime import UTC, datetime


async def test_ip_limit_allows_up_to_threshold(session: AsyncSession) -> None:
    ip = "1.2.3.4"
    for _ in range(settings.RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN):
        await check_magic_link_ip(session, ip)
    # The (N+1)th hit in the same window should raise.
    with pytest.raises(HubError) as exc_info:
        await check_magic_link_ip(session, ip)
    err = exc_info.value
    assert err.slug == "rate-limit-exceeded"
    assert err.extensions["retry_after_seconds"] >= 1
    assert err.extensions["retry_after_seconds"] <= 60


async def test_report_limit_independent_from_ip(session: AsyncSession) -> None:
    rid = uuid4()
    # Burn through the IP limit on one address.
    for _ in range(settings.RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN):
        await check_magic_link_ip(session, "9.9.9.9")
    # Report-scope counter is untouched.
    for _ in range(settings.RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR):
        await check_magic_link_report(session, rid)
    with pytest.raises(HubError):
        await check_magic_link_report(session, rid)


async def test_different_scopes_have_independent_buckets(session: AsyncSession) -> None:
    await check_magic_link_ip(session, "1.1.1.1")
    await check_magic_link_ip(session, "2.2.2.2")
    rows = (await session.execute(select(RateLimitHit))).scalars().all()
    assert len(rows) == 2
    assert all(r.count == 1 for r in rows)


async def test_atomic_increment_returns_new_count(session: AsyncSession) -> None:
    counts: list[int] = []
    for _ in range(3):
        c = await _bump(
            session, scope_key="ip:test", window_seconds=60, limit=10
        )
        counts.append(c)
    assert counts == [1, 2, 3]


def test_floor_to_window_minute_boundary() -> None:
    # 2026-05-11 12:34:56 UTC → floor to minute → 12:34:00
    t = datetime(2026, 5, 11, 12, 34, 56, tzinfo=UTC)
    assert _floor_to_window(t, 60) == datetime(2026, 5, 11, 12, 34, 0, tzinfo=UTC)


def test_floor_to_window_hour_boundary() -> None:
    t = datetime(2026, 5, 11, 12, 34, 56, tzinfo=UTC)
    assert _floor_to_window(t, 3600) == datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)


@hyp_settings(max_examples=25, deadline=None)
@given(
    epoch=st.integers(min_value=1_700_000_000, max_value=2_000_000_000),
    window=st.sampled_from([60, 300, 3600]),
)
def test_floor_to_window_property(epoch: int, window: int) -> None:
    """Floored timestamp is always ≤ original and the gap is < window seconds."""
    t = datetime.fromtimestamp(epoch, tz=UTC)
    floored = _floor_to_window(t, window)
    assert floored <= t
    assert (t - floored).total_seconds() < window
    # And it's a multiple of `window` seconds from the epoch.
    assert int(floored.timestamp()) % window == 0
