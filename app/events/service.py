"""Event logging service (PLAN.md §10).

Single low-level `log_event` writes a row to `events`. Typed helpers wrap the
five v1 event types so callers can't typo `event_type` or skip required payload
fields. Caller controls commit lifecycle — service only `add()`s, never commits.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.models import Event


EventType = Literal[
    "report_published",
    "report_view",
    "search_query",
    "magic_link_issued",
    "magic_link_consumed",
]


async def log_event(
    session: AsyncSession,
    *,
    event_type: EventType,
    customer_id: UUID | None,
    payload: dict[str, Any],
) -> Event:
    event = Event(
        event_type=event_type,
        customer_id=customer_id,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def log_report_published(
    session: AsyncSession,
    *,
    customer_id: UUID,
    report_id: UUID,
    idempotency_key: str,
    deduped: bool,
) -> Event:
    return await log_event(
        session,
        event_type="report_published",
        customer_id=customer_id,
        payload={
            "report_id": str(report_id),
            "idempotency_key": idempotency_key,
            "deduped": deduped,
        },
    )


async def log_report_view(
    session: AsyncSession,
    *,
    customer_id: UUID,
    report_id: UUID,
    session_id: UUID,
    ip: str,
    user_agent: str,
    viewport_width: int | None = None,
) -> Event:
    payload: dict[str, Any] = {
        "report_id": str(report_id),
        "session_id": str(session_id),
        "ip": ip,
        "user_agent": user_agent,
    }
    if viewport_width is not None:
        payload["viewport_width"] = viewport_width
    return await log_event(
        session,
        event_type="report_view",
        customer_id=customer_id,
        payload=payload,
    )


async def log_search_query(
    session: AsyncSession,
    *,
    customer_id: UUID,
    query: str,
    result_count: int,
) -> Event:
    return await log_event(
        session,
        event_type="search_query",
        customer_id=customer_id,
        payload={"query": query, "result_count": result_count},
    )


async def log_magic_link_issued(
    session: AsyncSession,
    *,
    customer_id: UUID,
    email: str,
    ip: str,
    delivery: str = "email",
    channel_hint: str | None = None,
) -> Event:
    """`delivery` is "email" (Hub sends) or "return" (raw URL handed to a
    caller, e.g. an agent passing it via chat). `channel_hint` is a free-form
    label the agent supplies for audit ("slack", "sms"). Token itself is
    never in the payload — see anti-leak tests in test_events_service."""
    payload: dict[str, str] = {"email": email, "ip": ip, "delivery": delivery}
    if channel_hint is not None:
        payload["channel_hint"] = channel_hint
    return await log_event(
        session,
        event_type="magic_link_issued",
        customer_id=customer_id,
        payload=payload,
    )


async def log_magic_link_consumed(
    session: AsyncSession,
    *,
    customer_id: UUID,
    token_jti: str,
    ip: str,
) -> Event:
    return await log_event(
        session,
        event_type="magic_link_consumed",
        customer_id=customer_id,
        payload={"token_jti": token_jti, "ip": ip},
    )
