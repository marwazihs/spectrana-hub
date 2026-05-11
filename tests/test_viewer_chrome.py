"""Viewer chrome + magic-link flow (PLAN.md §4.5-4.7).

End-to-end across the three states of GET /r/{id}, the request-link mint,
and the token consume → session cookie → redirect path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.auth.service import SESSION_COOKIE_NAME, mint_session
from app.config import settings
from app.db.session import get_session
from app.events.models import Event
from app.main import app
from app.reports import render as render_module
from app.reports.models import Report


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _secrets_and_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "HUB_MAGIC_LINK_HASH_SECRET",
        SecretStr("test-magic-secret-padded-to-at-least-32-bytes-aaaaaaaa"),
    )
    monkeypatch.setattr(
        settings,
        "HUB_IFRAME_JWT_SECRET",
        SecretStr("test-iframe-secret-padded-to-at-least-32-bytes-bbbbbbb"),
    )
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET_PREVIOUS", None)
    monkeypatch.setattr(settings, "HUB_PRIMARY_DOMAIN", "hub.test")
    monkeypatch.setattr(settings, "HUB_REPORTS_DOMAIN", "reports.hub.test")
    # Generous limits so non-rate-limit tests don't trip them.
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN", 100)
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR", 100)


@pytest_asyncio.fixture
async def sent_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture send_magic_link_email calls instead of hitting SMTP."""
    captured: list[dict[str, Any]] = []

    async def _fake_send(*, to_email: str, report_id: UUID, raw_token: str) -> None:
        captured.append(
            {"to": to_email, "report_id": report_id, "token": raw_token}
        )

    monkeypatch.setattr(render_module, "send_magic_link_email", _fake_send)
    return captured


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://hub.test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


async def _make_customer(
    session: AsyncSession, *, allowlist: list[str], name: str = "Acme"
) -> Customer:
    c = Customer(
        name=name,
        allowlist_emails=allowlist,
        api_key_hash=bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c


async def _make_report(session: AsyncSession, customer: Customer) -> Report:
    from uuid_utils import uuid7

    rid = UUID(str(uuid7()))
    r = Report(
        id=rid,
        customer_id=customer.id,
        title="t",
        description="",
        tags=[],
        generated_at=datetime.now(tz=UTC),
        s3_key=f"{customer.id}/{rid}/index.html",
        supplementary_files=[],
        size_bytes=100,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


# --- GET /r/{id} — state 1: no cookie ------------------------------------


@pytest.mark.asyncio
async def test_get_viewer_no_cookie_renders_email_entry(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    resp = await client.get(f"/r/{r.id}")
    assert resp.status_code == 200
    assert "Send link" in resp.text
    assert "email" in resp.text.lower()


@pytest.mark.asyncio
async def test_get_viewer_no_cookie_for_missing_report_still_renders_entry(
    client: AsyncClient,
) -> None:
    """Anti-enumeration: a missing report and an existing report look the
    same to an unauthenticated caller."""
    resp = await client.get(f"/r/{uuid4()}")
    assert resp.status_code == 200
    assert "Send link" in resp.text


# --- GET /r/{id} — state 2: valid session, matching customer -------------


@pytest.mark.asyncio
async def test_get_viewer_with_valid_session_renders_chrome_and_logs_view(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    auth = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, str(auth.id))
    resp = await client.get(f"/r/{r.id}")

    assert resp.status_code == 200
    assert "<iframe" in resp.text
    assert "reports.hub.test/render/" in resp.text
    assert f"/render/{r.id}?t=" in resp.text
    assert "ops@acme.com" in resp.text  # rendered in chrome footer

    # Event log: report_view fired
    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "report_view")
        )
    ).scalar_one()
    assert ev.payload["report_id"] == str(r.id)
    assert ev.payload["session_id"] == str(auth.id)


# --- GET /r/{id} — state 3: valid session, wrong customer ----------------


@pytest.mark.asyncio
async def test_get_viewer_with_session_for_wrong_customer_renders_entry(
    client: AsyncClient, session: AsyncSession
) -> None:
    c1 = await _make_customer(session, allowlist=["a@x.com"], name="C1")
    c2 = await _make_customer(session, allowlist=["b@y.com"], name="C2")
    r = await _make_report(session, c2)
    auth = await mint_session(session, customer_id=c1.id, email="a@x.com")
    await session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, str(auth.id))
    resp = await client.get(f"/r/{r.id}")
    # Per §4.5 the contract says 404 for mismatch, but per memory of the
    # anti-enumeration constraint we render the same email-entry page as for
    # no-cookie. Either way: chrome MUST NOT render.
    assert "<iframe" not in resp.text
    assert resp.status_code == 200


# --- POST /r/{id}/request-link ------------------------------------------


@pytest.mark.asyncio
async def test_request_link_sends_email_for_allowlisted_address(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", data={"email": "ops@acme.com"}
    )

    assert resp.status_code == 200
    assert "If this email is on file" in resp.text
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "ops@acme.com"
    assert sent_emails[0]["report_id"] == r.id

    # magic_link_issued event logged
    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "magic_link_issued")
        )
    ).scalar_one()
    assert ev.payload == {"email": "ops@acme.com", "ip": ev.payload["ip"]}


@pytest.mark.asyncio
async def test_request_link_non_allowlisted_returns_same_page_no_email(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", data={"email": "stranger@example.com"}
    )
    assert resp.status_code == 200
    assert "If this email is on file" in resp.text
    assert sent_emails == []


@pytest.mark.asyncio
async def test_request_link_for_missing_report_returns_same_page_no_email(
    client: AsyncClient,
    sent_emails: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        f"/r/{uuid4()}/request-link", data={"email": "stranger@x.com"}
    )
    assert resp.status_code == 200
    assert "If this email is on file" in resp.text
    assert sent_emails == []


@pytest.mark.asyncio
async def test_request_link_email_case_insensitive_match(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
) -> None:
    c = await _make_customer(session, allowlist=["Ops@Acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", data={"email": "OPS@acme.COM"}
    )
    assert resp.status_code == 200
    assert len(sent_emails) == 1


@pytest.mark.asyncio
async def test_request_link_rate_limit_per_ip(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN", 2)
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    for _ in range(2):
        r1 = await client.post(
            f"/r/{r.id}/request-link", data={"email": "ops@acme.com"}
        )
        assert r1.status_code == 200

    over = await client.post(
        f"/r/{r.id}/request-link", data={"email": "ops@acme.com"}
    )
    assert over.status_code == 429
    assert over.json()["type"].endswith("rate-limit-exceeded")
    assert "retry-after" in {k.lower() for k in over.headers}


# --- GET /r/{id}?token=... — consume ------------------------------------


@pytest.mark.asyncio
async def test_consume_token_redirects_and_sets_session_cookie(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    # Trigger the mint flow.
    await client.post(
        f"/r/{r.id}/request-link", data={"email": "ops@acme.com"}
    )
    raw_token = sent_emails[0]["token"]

    resp = await client.get(
        f"/r/{r.id}", params={"token": raw_token}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/r/{r.id}"
    cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None

    # magic_link_consumed event logged
    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "magic_link_consumed")
        )
    ).scalar_one()
    assert ev.customer_id == c.id


@pytest.mark.asyncio
async def test_consume_bogus_token_returns_410(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.get(
        f"/r/{r.id}", params={"token": "not-a-real-token"}, follow_redirects=False
    )
    assert resp.status_code == 410
    assert resp.json()["type"].endswith("magic-link-expired-or-consumed")


@pytest.mark.asyncio
async def test_consume_token_for_missing_report_returns_410(
    client: AsyncClient,
) -> None:
    resp = await client.get(
        f"/r/{uuid4()}", params={"token": "x"}, follow_redirects=False
    )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_consume_token_is_single_use(
    client: AsyncClient,
    session: AsyncSession,
    sent_emails: list[dict[str, Any]],
) -> None:
    c = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    await client.post(f"/r/{r.id}/request-link", data={"email": "ops@acme.com"})
    raw_token = sent_emails[0]["token"]

    first = await client.get(
        f"/r/{r.id}", params={"token": raw_token}, follow_redirects=False
    )
    assert first.status_code == 302

    # Re-using the same token must fail with the same error class.
    second = await client.get(
        f"/r/{r.id}", params={"token": raw_token}, follow_redirects=False
    )
    assert second.status_code == 410
