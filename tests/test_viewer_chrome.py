"""POST /r/{id}/request-link — dual-personality magic-link mint.

After M6, this is the only viewer-side route Hub serves directly. The
GET /r/{id} and GET /r/{id}?token= flows moved to Next.js (it owns the
chrome and the consume route, calling /internal/* server-to-server).

Two personalities, one URL:
  - Unauth → anti-enum, IP rate-limited, email-only.
  - Authorization: Bearer <api_key> → real errors, per-API-key bucket,
    `delivery: "email"|"return"` for chat/SMS hand-off.
"""

from __future__ import annotations

import secrets
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
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN", 100)
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR", 100)
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_API_KEY_PER_HOUR", 100)


@pytest_asyncio.fixture
async def sent_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _fake_send(*, to_email: str, report_id: UUID, raw_token: str) -> None:
        captured.append({"to": to_email, "report_id": report_id, "token": raw_token})

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
    session: AsyncSession,
    *,
    allowlist: list[str],
    name: str = "Acme",
    raw_api_key: str | None = None,
) -> tuple[Customer, str]:
    raw = raw_api_key or f"mvk_live_{secrets.token_urlsafe(32)}"
    c = Customer(
        name=name,
        allowlist_emails=allowlist,
        api_key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c, raw


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


# === Unauth (public) path — anti-enum =================================


@pytest.mark.asyncio
async def test_public_allowlisted_sends_email_and_logs_event(
    client: AsyncClient, session: AsyncSession, sent_emails: list[dict[str, Any]]
) -> None:
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", json={"email": "ops@acme.com"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert "If this email is on file" in body["message"]
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "ops@acme.com"

    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "magic_link_issued")
        )
    ).scalar_one()
    assert ev.payload["email"] == "ops@acme.com"
    assert ev.payload["delivery"] == "email"


@pytest.mark.asyncio
async def test_public_non_allowlisted_identical_200_no_email(
    client: AsyncClient, session: AsyncSession, sent_emails: list[dict[str, Any]]
) -> None:
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", json={"email": "stranger@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert sent_emails == []


@pytest.mark.asyncio
async def test_public_missing_report_identical_200_no_email(
    client: AsyncClient, sent_emails: list[dict[str, Any]]
) -> None:
    resp = await client.post(
        f"/r/{uuid4()}/request-link", json={"email": "stranger@x.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert sent_emails == []


@pytest.mark.asyncio
async def test_public_email_case_insensitive_allowlist_match(
    client: AsyncClient, session: AsyncSession, sent_emails: list[dict[str, Any]]
) -> None:
    c, _ = await _make_customer(session, allowlist=["Ops@Acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link", json={"email": "OPS@acme.COM"}
    )
    assert resp.status_code == 200
    assert len(sent_emails) == 1


@pytest.mark.asyncio
async def test_public_ip_rate_limit_returns_429(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN", 2)
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    for _ in range(2):
        ok = await client.post(
            f"/r/{r.id}/request-link", json={"email": "ops@acme.com"}
        )
        assert ok.status_code == 200

    over = await client.post(
        f"/r/{r.id}/request-link", json={"email": "ops@acme.com"}
    )
    assert over.status_code == 429
    assert "retry-after" in {k.lower() for k in over.headers}


# === Agent (API-key) path — real errors + delivery options =============


@pytest.mark.asyncio
async def test_agent_delivery_email_returns_sent_and_emails(
    client: AsyncClient, session: AsyncSession, sent_emails: list[dict[str, Any]]
) -> None:
    c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": "ops@acme.com", "delivery": "email"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["url"] is None
    assert body["expires_in_minutes"] == settings.MAGIC_LINK_TTL_MINUTES
    assert len(sent_emails) == 1


@pytest.mark.asyncio
async def test_agent_delivery_return_returns_url_no_email(
    client: AsyncClient, session: AsyncSession, sent_emails: list[dict[str, Any]]
) -> None:
    """Returned URL is a bearer credential — agent hands off via chat/SMS.
    Hub doesn't send email when delivery is 'return'."""
    c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "email": "ops@acme.com",
            "delivery": "return",
            "channel_hint": "slack",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "returned"
    assert body["url"] is not None
    assert body["url"].startswith("https://hub.test/r/")
    assert "/consume?token=" in body["url"]
    assert sent_emails == []  # critically: no email sent on delivery=return

    # Event captured the audit fields.
    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "magic_link_issued")
        )
    ).scalar_one()
    assert ev.payload["delivery"] == "return"
    assert ev.payload["channel_hint"] == "slack"
    # Anti-leak: token is in the URL, must not be in the event payload.
    assert "token" not in ev.payload
    raw_token = body["url"].split("token=")[1]
    assert raw_token not in str(ev.payload)


@pytest.mark.asyncio
async def test_agent_delivery_return_requires_channel_hint(
    client: AsyncClient, session: AsyncSession
) -> None:
    c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": "ops@acme.com", "delivery": "return"},
    )
    assert resp.status_code == 422
    assert resp.json()["type"].endswith("validation-failed")


@pytest.mark.asyncio
async def test_agent_off_allowlist_returns_422(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Authenticated caller: real error, distinct slug. No anti-enum
    shape — the agent already knows the report exists."""
    c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    resp = await client.post(
        f"/r/{r.id}/request-link",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": "stranger@example.com", "delivery": "email"},
    )
    assert resp.status_code == 422
    assert resp.json()["type"].endswith("email-not-allowlisted")


@pytest.mark.asyncio
async def test_agent_cross_customer_report_returns_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    a, key_a = await _make_customer(session, allowlist=["a@x.com"], name="A")
    b, _ = await _make_customer(session, allowlist=["b@x.com"], name="B")
    r_b = await _make_report(session, b)

    resp = await client.post(
        f"/r/{r_b.id}/request-link",
        headers={"Authorization": f"Bearer {key_a}"},
        json={"email": "b@x.com", "delivery": "email"},
    )
    assert resp.status_code == 404
    assert resp.json()["type"].endswith("report-not-found")


@pytest.mark.asyncio
async def test_agent_unknown_report_returns_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    _c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    resp = await client.post(
        f"/r/{uuid4()}/request-link",
        headers={"Authorization": f"Bearer {key}"},
        json={"email": "ops@acme.com", "delivery": "email"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_invalid_api_key_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    resp = await client.post(
        f"/r/{r.id}/request-link",
        headers={"Authorization": "Bearer mvk_live_definitelynotvalid"},
        json={"email": "ops@acme.com"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agent_uses_api_key_rate_limit_not_ip(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting IP limit to 1 and API-key limit to 5 → agent gets 5 mints
    on the same IP without hitting the IP gate. This verifies the buckets
    are independent (no IP gate on the auth path)."""
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_MAGIC_LINK_PER_API_KEY_PER_HOUR", 3)
    c, key = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)

    headers = {"Authorization": f"Bearer {key}"}
    body = {"email": "ops@acme.com", "delivery": "email"}
    for _ in range(3):
        ok = await client.post(f"/r/{r.id}/request-link", headers=headers, json=body)
        assert ok.status_code == 200

    over = await client.post(f"/r/{r.id}/request-link", headers=headers, json=body)
    assert over.status_code == 429


# === Schema validation =================================================


@pytest.mark.asyncio
async def test_request_link_rejects_extra_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    resp = await client.post(
        f"/r/{r.id}/request-link",
        json={"email": "ops@acme.com", "extra": "no"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_request_link_rejects_invalid_delivery(
    client: AsyncClient, session: AsyncSession
) -> None:
    c, _ = await _make_customer(session, allowlist=["ops@acme.com"])
    r = await _make_report(session, c)
    resp = await client.post(
        f"/r/{r.id}/request-link",
        json={"email": "ops@acme.com", "delivery": "carrier-pigeon"},
    )
    assert resp.status_code == 422
