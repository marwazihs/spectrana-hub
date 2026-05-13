"""POST /internal/magic-link/consume (M6.1).

Server-to-server endpoint that lets the Next.js viewer hand a raw magic-link
token to Hub and receive the session data needed to issue `hub_session` on
the viewer origin. All failure modes collapse to 410 (PLAN.md §4.7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer, MagicLinkToken, Session as AuthSession
from app.auth.service import mint_magic_link
from app.config import settings
from app.db.session import get_session
from app.events.models import Event
from app.main import app
from app.reports.models import Report


INTERNAL_TOKEN = "test-internal-token-padded-to-at-least-32-bytes-aaaa"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "HUB_MAGIC_LINK_HASH_SECRET",
        SecretStr("test-magic-secret-padded-to-at-least-32-bytes-aaaaaaaa"),
    )
    monkeypatch.setattr(settings, "HUB_INTERNAL_TOKEN", SecretStr(INTERNAL_TOKEN))


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


async def _make_customer(session: AsyncSession, *, name: str = "Acme") -> Customer:
    c = Customer(
        name=name,
        allowlist_emails=["ops@acme.com"],
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


# --- Auth gate ----------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_without_internal_token_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    raw = await mint_magic_link(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/magic-link/consume",
        json={"report_id": str(r.id), "token": raw},
    )
    assert resp.status_code == 401
    assert resp.json()["type"].endswith("invalid-internal-token")


@pytest.mark.asyncio
async def test_consume_with_wrong_internal_token_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    raw = await mint_magic_link(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": "wrong"},
        json={"report_id": str(r.id), "token": raw},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_consume_with_empty_configured_token_always_401(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-footgun: if HUB_INTERNAL_TOKEN is unset, every call 401s. A
    forgotten env var must not silently open the internal surface."""
    monkeypatch.setattr(settings, "HUB_INTERNAL_TOKEN", SecretStr(""))
    c = await _make_customer(session)
    r = await _make_report(session, c)
    raw = await mint_magic_link(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": ""},  # same as configured
        json={"report_id": str(r.id), "token": raw},
    )
    assert resp.status_code == 401


# --- Happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_happy_path_returns_session_data_and_logs_event(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    raw = await mint_magic_link(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(r.id), "token": raw},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert UUID(body["session_id"])  # parses
    assert body["customer_id"] == str(c.id)
    assert body["email"] == "ops@acme.com"
    assert body["expires_at"]  # ISO datetime

    # Session row persisted
    auth = (
        await session.execute(
            select(AuthSession).where(AuthSession.id == UUID(body["session_id"]))
        )
    ).scalar_one()
    assert auth.customer_id == c.id

    # Magic-link row marked consumed
    tok = (await session.execute(select(MagicLinkToken))).scalar_one()
    assert tok.consumed_at is not None

    # Event logged with truncated jti, not raw token
    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "magic_link_consumed")
        )
    ).scalar_one()
    assert ev.customer_id == c.id
    assert "token_jti" in ev.payload
    assert raw not in str(ev.payload)  # anti-leak


# --- Failure modes — all collapse to 410 -------------------------------


@pytest.mark.asyncio
async def test_consume_unknown_report_returns_410(
    client: AsyncClient, session: AsyncSession
) -> None:
    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(uuid4()), "token": "anything"},
    )
    assert resp.status_code == 410
    assert resp.json()["type"].endswith("magic-link-expired-or-consumed")


@pytest.mark.asyncio
async def test_consume_bogus_token_returns_410(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)

    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(r.id), "token": "not-a-real-token"},
    )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_consume_is_single_use(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    raw = await mint_magic_link(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    headers = {"X-Hub-Internal-Token": INTERNAL_TOKEN}
    first = await client.post(
        "/internal/magic-link/consume",
        headers=headers,
        json={"report_id": str(r.id), "token": raw},
    )
    assert first.status_code == 200

    second = await client.post(
        "/internal/magic-link/consume",
        headers=headers,
        json={"report_id": str(r.id), "token": raw},
    )
    assert second.status_code == 410


@pytest.mark.asyncio
async def test_consume_cross_customer_token_returns_410(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Token minted for customer A, used against a report owned by customer B.
    Even with a valid token, must collapse to 410 — no info leak."""
    a = await _make_customer(session, name="A")
    b = await _make_customer(session, name="B")
    r_b = await _make_report(session, b)
    raw = await mint_magic_link(session, customer_id=a.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(r_b.id), "token": raw},
    )
    assert resp.status_code == 410


# --- Schema validation -------------------------------------------------


@pytest.mark.asyncio
async def test_consume_rejects_extra_fields(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(uuid4()), "token": "x", "extra": "no"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_consume_rejects_empty_token(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/magic-link/consume",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"report_id": str(uuid4()), "token": ""},
    )
    assert resp.status_code == 422
