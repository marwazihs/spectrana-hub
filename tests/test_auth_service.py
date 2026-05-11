"""Auth services: magic-link, iframe JWT, session cookie.

Covers PLAN.md §6 non-negotiables:
- test_magic_link_token_storage_is_hashed
- test_iframe_jwt_rotation_overlap
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer, MagicLinkToken
from app.auth.service import (
    _hash_magic_link,
    consume_magic_link,
    mint_iframe_jwt,
    mint_magic_link,
    mint_session,
    verify_and_refresh_session,
    verify_iframe_jwt,
)
from app.config import settings
from app.errors import HubError

# asyncio_mode = "auto" in pyproject auto-marks async tests; no explicit mark needed.


# --- Helpers -------------------------------------------------------------


async def _make_customer(session: AsyncSession, email: str = "ops@acme.com") -> Customer:
    key_hash = bcrypt.hashpw(b"unused", bcrypt.gensalt(rounds=4)).decode()
    c = Customer(
        name="Acme Corp",
        allowlist_emails=[email],
        api_key_hash=key_hash,
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c


@pytest.fixture(autouse=True)
def _real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default test secrets. Individual tests can override via monkeypatch."""
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


# --- Magic link ----------------------------------------------------------


async def test_magic_link_token_storage_is_hashed(session: AsyncSession) -> None:
    """Non-negotiable v1 test. PLAN.md §6."""
    customer = await _make_customer(session)
    raw = await mint_magic_link(session, customer_id=customer.id, email="ops@acme.com")
    await session.commit()

    rows = (await session.execute(select(MagicLinkToken))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    # The raw token must never appear in the DB.
    assert row.token_hash != raw
    assert raw not in row.token_hash
    # The stored value must match the HMAC of the raw token.
    assert row.token_hash == _hash_magic_link(raw)
    # Hex digest of SHA-256 is 64 chars.
    assert len(row.token_hash) == 64


async def test_consume_magic_link_success(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    raw = await mint_magic_link(session, customer_id=customer.id, email="ops@acme.com")

    row = await consume_magic_link(session, raw, expected_customer_id=customer.id)
    assert row.consumed_at is not None


async def test_consume_magic_link_replay_raises(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    raw = await mint_magic_link(session, customer_id=customer.id, email="ops@acme.com")
    await consume_magic_link(session, raw, expected_customer_id=customer.id)

    with pytest.raises(HubError) as exc_info:
        await consume_magic_link(session, raw, expected_customer_id=customer.id)
    assert exc_info.value.slug == "magic-link-expired-or-consumed"


async def test_consume_magic_link_expired_raises(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    raw = await mint_magic_link(session, customer_id=customer.id, email="ops@acme.com")
    # Force expiry by mutating the stored row.
    stored = (
        await session.execute(select(MagicLinkToken))
    ).scalar_one()
    stored.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    await session.flush()

    with pytest.raises(HubError) as exc_info:
        await consume_magic_link(session, raw, expected_customer_id=customer.id)
    assert exc_info.value.slug == "magic-link-expired-or-consumed"


async def test_consume_magic_link_wrong_customer_raises(session: AsyncSession) -> None:
    c1 = await _make_customer(session, email="ops@acme.com")
    raw = await mint_magic_link(session, customer_id=c1.id, email="ops@acme.com")

    other_customer_id = uuid4()
    with pytest.raises(HubError) as exc_info:
        await consume_magic_link(session, raw, expected_customer_id=other_customer_id)
    assert exc_info.value.slug == "magic-link-expired-or-consumed"


async def test_consume_unknown_token_raises(session: AsyncSession) -> None:
    with pytest.raises(HubError) as exc_info:
        await consume_magic_link(session, "not-a-real-token", expected_customer_id=uuid4())
    assert exc_info.value.slug == "magic-link-expired-or-consumed"


# --- Iframe JWT ----------------------------------------------------------


def test_mint_and_verify_iframe_jwt_round_trip() -> None:
    report_id = uuid4()
    customer_id = uuid4()
    token = mint_iframe_jwt(report_id=report_id, customer_id=customer_id)
    payload = verify_iframe_jwt(token, expected_report_id=report_id)
    assert payload["report_id"] == str(report_id)
    assert payload["customer_id"] == str(customer_id)
    assert payload["iss"] == settings.HUB_PRIMARY_DOMAIN


def test_iframe_jwt_rejects_wrong_report_id() -> None:
    token = mint_iframe_jwt(report_id=uuid4(), customer_id=uuid4())
    with pytest.raises(HubError) as exc_info:
        verify_iframe_jwt(token, expected_report_id=uuid4())
    assert exc_info.value.slug == "iframe-token-invalid"


def test_iframe_jwt_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    report_id = uuid4()
    token = mint_iframe_jwt(report_id=report_id, customer_id=uuid4())
    monkeypatch.setattr(
        settings,
        "HUB_IFRAME_JWT_SECRET",
        SecretStr("different-secret-padded-to-at-least-32-bytes-cccccccc"),
    )
    with pytest.raises(HubError) as exc_info:
        verify_iframe_jwt(token, expected_report_id=report_id)
    assert exc_info.value.slug == "iframe-token-invalid"


def test_iframe_jwt_rotation_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-negotiable v1 test. PLAN.md §6.

    A token signed with key A still verifies after rotation puts A in
    HUB_IFRAME_JWT_SECRET_PREVIOUS and a new key in HUB_IFRAME_JWT_SECRET.
    """
    report_id = uuid4()
    customer_id = uuid4()

    # 1. Mint a token with secret A (the current at mint time).
    secret_a = "secret-A-pre-rotation-padded-to-at-least-32-bytes-dddd"
    secret_b = "secret-B-post-rotation-padded-to-at-least-32-bytes-eee"
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET", SecretStr(secret_a))
    token = mint_iframe_jwt(report_id=report_id, customer_id=customer_id)

    # 2. Rotate: A becomes PREVIOUS, new B becomes current.
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET", SecretStr(secret_b))
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET_PREVIOUS", SecretStr(secret_a))

    # 3. Token still verifies under the previous secret.
    payload = verify_iframe_jwt(token, expected_report_id=report_id)
    assert payload["report_id"] == str(report_id)


def test_iframe_jwt_rejects_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IFRAME_JWT_TTL_SECONDS", -1)
    token = mint_iframe_jwt(report_id=uuid4(), customer_id=uuid4())
    with pytest.raises(HubError) as exc_info:
        verify_iframe_jwt(token, expected_report_id=UUID(jwt.decode(token, options={"verify_signature": False})["report_id"]))
    assert exc_info.value.slug == "iframe-token-invalid"


# --- Session cookie ------------------------------------------------------


async def test_mint_and_verify_session_round_trip(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    row = await mint_session(session, customer_id=customer.id, email="ops@acme.com")
    assert row.id is not None

    fetched = await verify_and_refresh_session(session, row.id)
    assert fetched is not None
    assert fetched.customer_id == customer.id


async def test_verify_session_unknown_returns_none(session: AsyncSession) -> None:
    result = await verify_and_refresh_session(session, uuid4())
    assert result is None


async def test_verify_session_slides_expiry(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    row = await mint_session(session, customer_id=customer.id, email="ops@acme.com")

    # Pull expiry forward; verify should push it back to now+30d.
    near_expiry = datetime.now(tz=UTC) + timedelta(minutes=1)
    row.expires_at = near_expiry
    await session.flush()

    refreshed = await verify_and_refresh_session(session, row.id)
    assert refreshed is not None
    assert refreshed.expires_at > near_expiry + timedelta(days=1)


async def test_verify_session_expired_returns_none(session: AsyncSession) -> None:
    customer = await _make_customer(session)
    row = await mint_session(session, customer_id=customer.id, email="ops@acme.com")
    row.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    await session.flush()

    result = await verify_and_refresh_session(session, row.id)
    assert result is None


async def test_revoke_session_deletes_row(session: AsyncSession) -> None:
    from app.auth.service import revoke_session

    customer = await _make_customer(session)
    row = await mint_session(session, customer_id=customer.id, email="ops@acme.com")
    sid = row.id

    deleted = await revoke_session(session, sid)
    assert deleted is True
    assert await verify_and_refresh_session(session, sid) is None


async def test_revoke_session_missing_is_idempotent(session: AsyncSession) -> None:
    from app.auth.service import revoke_session

    deleted = await revoke_session(session, uuid4())
    assert deleted is False
