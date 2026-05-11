"""API-key bearer middleware (app.auth.api_key)."""

from __future__ import annotations

import secrets

import bcrypt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import CurrentCustomer
from app.auth.models import Customer
from app.db.session import get_session
from app.errors import HubError, hub_error_handler


def _mint_test_key() -> tuple[str, str]:
    raw = f"mvk_live_{secrets.token_urlsafe(32)}"
    # cost=4 keeps tests fast; production CLI uses 12.
    digest = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("utf-8")
    return raw, digest


async def _seed_customer(session: AsyncSession, *, active: bool = True) -> tuple[Customer, str]:
    raw, digest = _mint_test_key()
    c = Customer(
        name="Acme",
        allowlist_emails=["ops@acme.com"],
        api_key_hash=digest,
        api_key_prefix=raw[:8],
        is_active=active,
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c, raw


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(HubError, hub_error_handler)  # type: ignore[arg-type]

    async def _override() -> AsyncSession:
        return session

    app.dependency_overrides[get_session] = _override

    @app.get("/protected")
    async def _protected(customer: CurrentCustomer) -> dict[str, str]:
        return {"customer_id": str(customer.id)}

    return app


async def test_valid_key_returns_customer(session: AsyncSession) -> None:
    customer, raw = await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert r.status_code == 200
    assert r.json()["customer_id"] == str(customer.id)


async def test_missing_header_is_401(session: AsyncSession) -> None:
    await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/protected")

    assert r.status_code == 401
    body = r.json()
    assert body["status"] == 401
    assert "invalid-api-key" in body["type"]


async def test_malformed_header_is_401(session: AsyncSession) -> None:
    await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Wrong scheme
        r1 = await c.get("/protected", headers={"Authorization": "Basic abc"})
        assert r1.status_code == 401
        # Bearer with no key
        r2 = await c.get("/protected", headers={"Authorization": "Bearer "})
        assert r2.status_code == 401
        # Bearer with wrong prefix
        r3 = await c.get("/protected", headers={"Authorization": "Bearer sk_test_abcdefg"})
        assert r3.status_code == 401


async def test_unknown_key_is_401(session: AsyncSession) -> None:
    await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    fake = f"mvk_live_{secrets.token_urlsafe(32)}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/protected", headers={"Authorization": f"Bearer {fake}"})

    assert r.status_code == 401


async def test_deactivated_customer_is_401(session: AsyncSession) -> None:
    _, raw = await _seed_customer(session, active=False)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/protected", headers={"Authorization": f"Bearer {raw}"})

    assert r.status_code == 401


async def test_correct_customer_picked_when_multiple_active(session: AsyncSession) -> None:
    c1, raw1 = await _seed_customer(session)
    c2, raw2 = await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.get("/protected", headers={"Authorization": f"Bearer {raw1}"})
        r2 = await c.get("/protected", headers={"Authorization": f"Bearer {raw2}"})

    assert r1.json()["customer_id"] == str(c1.id)
    assert r2.json()["customer_id"] == str(c2.id)
    assert c1.id != c2.id


@pytest.mark.parametrize("bad", ["", "   ", "mvk_live_", "mvk_live"])
async def test_empty_or_truncated_key_is_401(session: AsyncSession, bad: str) -> None:
    await _seed_customer(session)
    await session.commit()
    app = _build_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/protected", headers={"Authorization": f"Bearer {bad}"})

    assert r.status_code == 401
