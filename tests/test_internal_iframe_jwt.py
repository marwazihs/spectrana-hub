"""POST /internal/iframe-jwt (M6.2).

Next.js calls this server-side every time it renders /r/[id] chrome. Takes
a session_id (from the hub_session cookie it issued), returns a 60s iframe
JWT bound to (report_id, customer_id). All auth/authz failures collapse to
401 iframe-jwt-unauthorized — Next.js maps that to the email-entry page,
keeping anti-enum parity with M4's GET /r/{id} behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer, Session as AuthSession
from app.auth.service import mint_session
from app.config import settings
from app.db.session import get_session
from app.main import app
from app.reports.models import Report


INTERNAL_TOKEN = "test-internal-token-padded-to-at-least-32-bytes-aaaa"
IFRAME_SECRET = "test-iframe-secret-padded-to-at-least-32-bytes-bbbbbbb"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HUB_INTERNAL_TOKEN", SecretStr(INTERNAL_TOKEN))
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET", SecretStr(IFRAME_SECRET))
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET_PREVIOUS", None)
    monkeypatch.setattr(settings, "HUB_PRIMARY_DOMAIN", "hub.test")
    monkeypatch.setattr(settings, "IFRAME_JWT_TTL_SECONDS", 60)


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


HEADERS = {"X-Hub-Internal-Token": INTERNAL_TOKEN}


# --- Auth gate ----------------------------------------------------------


@pytest.mark.asyncio
async def test_iframe_jwt_without_internal_token_returns_401(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/internal/iframe-jwt",
        json={"report_id": str(uuid4()), "session_id": str(uuid4())},
    )
    assert resp.status_code == 401
    assert resp.json()["type"].endswith("invalid-internal-token")


# --- Happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_iframe_jwt_happy_path_returns_decodable_jwt(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    auth = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(r.id), "session_id": str(auth.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ttl_seconds"] == 60

    payload = pyjwt.decode(
        body["token"],
        IFRAME_SECRET,
        algorithms=["HS256"],
        issuer="hub.test",
        options={"require": ["exp", "iat", "iss"]},
    )
    assert payload["report_id"] == str(r.id)
    assert payload["customer_id"] == str(c.id)


@pytest.mark.asyncio
async def test_iframe_jwt_refreshes_session_sliding_window(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A successful mint slides the session expiry — same contract as
    verify_and_refresh_session in the M4 viewer chrome path."""
    c = await _make_customer(session)
    r = await _make_report(session, c)
    auth = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    # Backdate to make the slide observable.
    auth.expires_at = datetime.now(tz=UTC) + timedelta(days=1)
    auth.last_accessed_at = datetime.now(tz=UTC) - timedelta(hours=1)
    await session.commit()
    before = auth.expires_at

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(r.id), "session_id": str(auth.id)},
    )
    assert resp.status_code == 200

    refreshed = await session.get(AuthSession, auth.id)
    assert refreshed is not None
    assert refreshed.expires_at > before


# --- Failure modes — all collapse to 401 -------------------------------


@pytest.mark.asyncio
async def test_iframe_jwt_unknown_session_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(r.id), "session_id": str(uuid4())},
    )
    assert resp.status_code == 401
    assert resp.json()["type"].endswith("iframe-jwt-unauthorized")


@pytest.mark.asyncio
async def test_iframe_jwt_expired_session_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    r = await _make_report(session, c)
    auth = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    auth.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    await session.commit()

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(r.id), "session_id": str(auth.id)},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_iframe_jwt_unknown_report_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    auth = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(uuid4()), "session_id": str(auth.id)},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_iframe_jwt_cross_customer_returns_401(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Session for customer A, report owned by customer B → 401, not a JWT
    for B's content. This is the protection that makes Next.js safe to call
    /internal/iframe-jwt with whatever (report, session) pair the URL+cookie
    supply — Hub enforces the binding."""
    a = await _make_customer(session, name="A")
    b = await _make_customer(session, name="B")
    r_b = await _make_report(session, b)
    auth_a = await mint_session(session, customer_id=a.id, email="ops@acme.com")
    await session.commit()

    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={"report_id": str(r_b.id), "session_id": str(auth_a.id)},
    )
    assert resp.status_code == 401


# --- Schema validation -------------------------------------------------


@pytest.mark.asyncio
async def test_iframe_jwt_rejects_extra_fields(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/iframe-jwt",
        headers=HEADERS,
        json={
            "report_id": str(uuid4()),
            "session_id": str(uuid4()),
            "extra": "no",
        },
    )
    assert resp.status_code == 422
