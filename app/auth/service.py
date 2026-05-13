"""Auth services: magic-link tokens, iframe JWTs, session cookies.

PLAN.md §2.3 — this module owns:
- mint/consume magic-link tokens (HMAC-hashed at rest, 15 min TTL, single-use)
- mint/verify iframe JWTs (60 s TTL, HS256, rotation overlap via _PREVIOUS)
- mint/verify session rows (30 d sliding, opaque random id stored in cookie)

Routes that invoke these services live in app/reports/api.py (M3/M4).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import MagicLinkToken, Session as AuthSession
from app.config import settings
from app.errors import iframe_token_invalid, magic_link_expired_or_consumed


SESSION_COOKIE_NAME = "hub_session"
_JWT_ALG = "HS256"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


# --- Magic-link tokens ---------------------------------------------------


def _hash_magic_link(raw: str) -> str:
    """HMAC-SHA256 of the raw token. Storing the hash means a DB leak
    doesn't yield usable links."""
    key = settings.HUB_MAGIC_LINK_HASH_SECRET.get_secret_value().encode("utf-8")
    return hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()


async def mint_magic_link(
    session: AsyncSession,
    *,
    customer_id: UUID,
    email: str,
) -> str:
    """Return the raw token (caller emails it). DB stores only the hash."""
    raw = secrets.token_urlsafe(32)
    now = _utc_now()
    row = MagicLinkToken(
        token_hash=_hash_magic_link(raw),
        customer_id=customer_id,
        email=email,
        expires_at=now + timedelta(minutes=settings.MAGIC_LINK_TTL_MINUTES),
    )
    session.add(row)
    await session.flush()
    return raw


async def consume_magic_link(
    session: AsyncSession,
    raw_token: str,
    *,
    expected_customer_id: UUID,
) -> MagicLinkToken:
    """Verify + mark consumed in one atomic step. Raises HubError on any failure.

    PLAN.md §4.7: all failure modes return the same error class (no info leak).
    """
    token_hash = _hash_magic_link(raw_token)
    now = _utc_now()
    stmt = select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise magic_link_expired_or_consumed()
    if (
        row.consumed_at is not None
        or row.expires_at <= now
        or row.customer_id != expected_customer_id
    ):
        raise magic_link_expired_or_consumed()
    row.consumed_at = now
    await session.flush()
    return row


# --- Iframe JWTs ---------------------------------------------------------


def mint_iframe_jwt(*, report_id: UUID, customer_id: UUID) -> str:
    now = _utc_now()
    payload: dict[str, Any] = {
        "report_id": str(report_id),
        "customer_id": str(customer_id),
        "iss": settings.HUB_PRIMARY_DOMAIN,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(seconds=settings.IFRAME_JWT_TTL_SECONDS)).timestamp()
        ),
    }
    secret = settings.HUB_IFRAME_JWT_SECRET.get_secret_value()
    return jwt.encode(payload, secret, algorithm=_JWT_ALG)


def _iframe_secrets_in_order() -> list[str]:
    """Current secret first; previous (if configured and non-empty) second.
    Rotation overlap: a token signed with the now-_PREVIOUS key still verifies."""
    out = [settings.HUB_IFRAME_JWT_SECRET.get_secret_value()]
    prev = settings.HUB_IFRAME_JWT_SECRET_PREVIOUS
    if prev is not None:
        prev_value = prev.get_secret_value()
        if prev_value:
            out.append(prev_value)
    return out


def verify_iframe_jwt(token: str, *, expected_report_id: UUID) -> dict[str, Any]:
    last_error: Exception | None = None
    for secret in _iframe_secrets_in_order():
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[_JWT_ALG],
                issuer=settings.HUB_PRIMARY_DOMAIN,
                options={"require": ["exp", "iat", "iss"]},
            )
        except jwt.InvalidTokenError as e:
            last_error = e
            continue
        if payload.get("report_id") != str(expected_report_id):
            raise iframe_token_invalid()
        return payload
    # If we got here every secret rejected the signature/exp/iss.
    _ = last_error
    raise iframe_token_invalid()


# --- Session cookies -----------------------------------------------------


async def mint_session(
    session: AsyncSession, *, customer_id: UUID, email: str
) -> AuthSession:
    """Insert a session row. Caller sets the cookie to row.id."""
    now = _utc_now()
    row = AuthSession(
        customer_id=customer_id,
        email=email,
        expires_at=now + timedelta(days=settings.SESSION_TTL_DAYS),
        last_accessed_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def verify_and_refresh_session(
    session: AsyncSession, session_id: UUID
) -> AuthSession | None:
    """Look up by opaque id, check expiry, slide the window. Returns None if
    not found or expired — caller decides the response (render email-entry page,
    not error)."""
    row = await session.get(AuthSession, session_id)
    now = _utc_now()
    if row is None or row.expires_at <= now:
        return None
    row.last_accessed_at = now
    row.expires_at = now + timedelta(days=settings.SESSION_TTL_DAYS)
    await session.flush()
    return row


async def revoke_session(session: AsyncSession, session_id: UUID) -> bool:
    """Delete a session row. Idempotent: returns True if a row was deleted,
    False if none existed. Caller is responsible for clearing the cookie on
    the browser side regardless of the return value (anti-enum: don't branch
    response on revoke outcome)."""
    row = await session.get(AuthSession, session_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
