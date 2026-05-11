"""POST /internal/session/revoke (M6.9).

Server-to-server sign-out endpoint. Idempotent: any valid call returns 200,
regardless of whether the session row existed. Pairs with the Next.js
/r/[id]/signout POST handler that clears the hub_session cookie on the
browser side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer, Session as AuthSession
from app.auth.service import mint_session
from app.config import settings
from app.db.session import get_session
from app.main import app


INTERNAL_TOKEN = "test-internal-token-padded-to-at-least-32-bytes-aaaa"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.asyncio
async def test_revoke_without_internal_token_returns_401(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/internal/session/revoke",
        json={"session_id": str(uuid4())},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_with_wrong_internal_token_returns_401(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/internal/session/revoke",
        headers={"X-Hub-Internal-Token": "wrong"},
        json={"session_id": str(uuid4())},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_deletes_session_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    c = await _make_customer(session)
    row = await mint_session(session, customer_id=c.id, email="ops@acme.com")
    sid = row.id
    await session.commit()

    resp = await client.post(
        "/internal/session/revoke",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"session_id": str(sid)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    fetched = (
        await session.execute(select(AuthSession).where(AuthSession.id == sid))
    ).scalar_one_or_none()
    assert fetched is None


@pytest.mark.asyncio
async def test_revoke_missing_session_is_idempotent(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/session/revoke",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"session_id": str(uuid4())},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_revoke_rejects_extra_fields(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/session/revoke",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"session_id": str(uuid4()), "extra": "x"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revoke_rejects_malformed_uuid(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/session/revoke",
        headers={"X-Hub-Internal-Token": INTERNAL_TOKEN},
        json={"session_id": "not-a-uuid"},
    )
    assert resp.status_code == 422
