"""Events service: log_event + typed helpers (PLAN.md §10)."""

from __future__ import annotations

from uuid import uuid4

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.events.models import Event
from app.events.service import (
    log_event,
    log_magic_link_consumed,
    log_magic_link_issued,
    log_report_published,
    log_report_view,
    log_search_query,
)


async def _make_customer(session: AsyncSession) -> Customer:
    key_hash = bcrypt.hashpw(b"unused", bcrypt.gensalt(rounds=4)).decode()
    c = Customer(
        name="Acme",
        allowlist_emails=["ops@acme.com"],
        api_key_hash=key_hash,
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c


async def test_log_event_writes_row_and_sets_occurred_at(session: AsyncSession) -> None:
    c = await _make_customer(session)
    ev = await log_event(
        session,
        event_type="report_published",
        customer_id=c.id,
        payload={"k": "v"},
    )
    await session.commit()

    assert ev.id is not None
    assert ev.occurred_at is not None
    row = (await session.execute(select(Event).where(Event.id == ev.id))).scalar_one()
    assert row.event_type == "report_published"
    assert row.customer_id == c.id
    assert row.payload == {"k": "v"}


async def test_log_event_allows_null_customer(session: AsyncSession) -> None:
    ev = await log_event(
        session,
        event_type="magic_link_issued",
        customer_id=None,
        payload={"email": "stranger@nowhere.com", "ip": "1.2.3.4"},
    )
    await session.commit()
    assert ev.customer_id is None


async def test_log_report_published_payload_shape(session: AsyncSession) -> None:
    c = await _make_customer(session)
    report_id = uuid4()
    ev = await log_report_published(
        session,
        customer_id=c.id,
        report_id=report_id,
        idempotency_key="abc-123",
        deduped=True,
    )
    assert ev.event_type == "report_published"
    assert ev.payload == {
        "report_id": str(report_id),
        "idempotency_key": "abc-123",
        "deduped": True,
    }


async def test_log_report_view_omits_viewport_width_when_absent(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    rid, sid = uuid4(), uuid4()
    ev = await log_report_view(
        session,
        customer_id=c.id,
        report_id=rid,
        session_id=sid,
        ip="10.0.0.1",
        user_agent="Mozilla/5.0",
    )
    assert "viewport_width" not in ev.payload
    assert ev.payload["ip"] == "10.0.0.1"


async def test_log_report_view_includes_viewport_width_when_set(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    ev = await log_report_view(
        session,
        customer_id=c.id,
        report_id=uuid4(),
        session_id=uuid4(),
        ip="10.0.0.1",
        user_agent="Mozilla/5.0",
        viewport_width=414,
    )
    assert ev.payload["viewport_width"] == 414


async def test_log_search_query_payload(session: AsyncSession) -> None:
    c = await _make_customer(session)
    ev = await log_search_query(
        session, customer_id=c.id, query="growth", result_count=7
    )
    assert ev.event_type == "search_query"
    assert ev.payload == {"query": "growth", "result_count": 7}


async def test_log_magic_link_issued_never_carries_token(session: AsyncSession) -> None:
    c = await _make_customer(session)
    ev = await log_magic_link_issued(
        session, customer_id=c.id, email="ops@acme.com", ip="1.1.1.1"
    )
    assert ev.event_type == "magic_link_issued"
    assert "token" not in ev.payload
    assert "token_hash" not in ev.payload
    assert ev.payload == {"email": "ops@acme.com", "ip": "1.1.1.1"}


async def test_log_magic_link_consumed_carries_jti_not_token(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    ev = await log_magic_link_consumed(
        session, customer_id=c.id, token_jti="jti-abc", ip="1.1.1.1"
    )
    assert ev.payload == {"token_jti": "jti-abc", "ip": "1.1.1.1"}
    assert "token" not in str(ev.payload).replace("token_jti", "")
