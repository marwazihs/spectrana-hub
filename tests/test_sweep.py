"""Nightly sweep: deletes expired rows from all ExpiringMixin tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer, MagicLinkToken, RateLimitHit, Session
from app.jobs.scheduler import build_scheduler
from app.jobs.sweep import SWEEP_TARGETS, sweep_expired
from app.reports.models import IdempotencyKey


async def _make_customer(session: AsyncSession) -> Customer:
    c = Customer(
        name="Acme",
        allowlist_emails=["ops@acme.com"],
        api_key_hash=bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c


async def _make_magic_link(
    session: AsyncSession, customer_id, expires_at: datetime
) -> None:
    session.add(
        MagicLinkToken(
            token_hash=f"hash-{expires_at.timestamp()}",
            customer_id=customer_id,
            email="ops@acme.com",
            expires_at=expires_at,
        )
    )


async def _make_session_row(
    session: AsyncSession, customer_id, expires_at: datetime
) -> None:
    session.add(
        Session(
            customer_id=customer_id,
            email="ops@acme.com",
            expires_at=expires_at,
        )
    )


async def _make_idempotency_key(
    session: AsyncSession, customer_id, key: str, expires_at: datetime
) -> None:
    session.add(
        IdempotencyKey(
            customer_id=customer_id,
            key=key,
            response_body={"ok": True},
            status_code=201,
            expires_at=expires_at,
        )
    )


async def _make_rate_limit_hit(
    session: AsyncSession, scope_key: str, expires_at: datetime
) -> None:
    session.add(
        RateLimitHit(
            scope_key=scope_key,
            window_start=expires_at - timedelta(minutes=1),
            count=1,
            expires_at=expires_at,
        )
    )


async def test_sweep_deletes_expired_keeps_active(session: AsyncSession) -> None:
    c = await _make_customer(session)
    now = datetime.now(tz=UTC)
    past, future = now - timedelta(hours=1), now + timedelta(hours=1)

    await _make_magic_link(session, c.id, past)
    await _make_magic_link(session, c.id, future)
    await _make_session_row(session, c.id, past)
    await _make_session_row(session, c.id, future)
    await _make_idempotency_key(session, c.id, "k1-old", past)
    await _make_idempotency_key(session, c.id, "k2-new", future)
    await _make_rate_limit_hit(session, "ip:1.1.1.1:202605110301", past)
    await _make_rate_limit_hit(session, "ip:1.1.1.1:202605110401", future)
    await session.commit()

    counts = await sweep_expired(session)

    assert counts == {
        "magic_link_tokens": 1,
        "sessions": 1,
        "idempotency_keys": 1,
        "rate_limit_hits": 1,
    }

    for cls in (MagicLinkToken, Session, IdempotencyKey, RateLimitHit):
        remaining = (
            await session.execute(select(func.count()).select_from(cls))
        ).scalar_one()
        assert remaining == 1, f"{cls.__tablename__} should have 1 active row"


async def test_sweep_is_idempotent(session: AsyncSession) -> None:
    c = await _make_customer(session)
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    await _make_magic_link(session, c.id, past)
    await session.commit()

    first = await sweep_expired(session)
    second = await sweep_expired(session)

    assert first["magic_link_tokens"] == 1
    assert second["magic_link_tokens"] == 0


async def test_sweep_no_expired_returns_zeros(session: AsyncSession) -> None:
    counts = await sweep_expired(session)
    assert counts == {
        "magic_link_tokens": 0,
        "sessions": 0,
        "idempotency_keys": 0,
        "rate_limit_hits": 0,
    }


async def test_sweep_targets_match_plan(session: AsyncSession) -> None:
    """PLAN.md §3.2: only MagicLinkToken, Session, IdempotencyKey, RateLimitHit."""
    names = {cls.__tablename__ for cls in SWEEP_TARGETS}
    assert names == {
        "magic_link_tokens",
        "sessions",
        "idempotency_keys",
        "rate_limit_hits",
    }


def test_scheduler_registers_sweep_job_at_3_17_utc() -> None:
    s = build_scheduler()
    job = s.get_job("sweep_expired")
    assert job is not None
    trigger = job.trigger
    # CronTrigger stores fields by name; assert hour=3 minute=17
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "3"
    assert fields["minute"] == "17"
    assert str(trigger.timezone) == "UTC"
