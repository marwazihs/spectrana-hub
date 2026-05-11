"""Internal endpoints consumed by the Next.js frontend (M6).

Server-to-server only. Auth is a shared secret in the `X-Hub-Internal-Token`
header (constant-time compare). These routes are NOT meant to be exposed at
the public LB; production restricts them by network policy as well.

M6.1 — POST /internal/magic-link/consume
Next.js calls this from its `/r/[id]/consume?token=...` route. Hub verifies
the magic-link token, marks it consumed atomically, mints a session row, and
returns the session id + email + customer_id so Next.js can issue the
`hub_session` cookie itself (host-only on the viewer origin).
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.auth.service import (
    consume_magic_link,
    mint_iframe_jwt,
    mint_session,
    verify_and_refresh_session,
)
from app.config import settings
from app.db.session import get_session
from app.errors import HubError, magic_link_expired_or_consumed
from app.events.service import log_magic_link_consumed
from app.reports.models import Report


router = APIRouter(prefix="/internal", tags=["internal"])


def _invalid_internal_token() -> HubError:
    return HubError(
        slug="invalid-internal-token",
        status=401,
        title="Invalid internal token",
        detail="The X-Hub-Internal-Token header is missing or does not match.",
    )


async def require_internal_token(
    x_hub_internal_token: Annotated[str | None, Header(alias="X-Hub-Internal-Token")] = None,
) -> None:
    """Constant-time compare against HUB_INTERNAL_TOKEN. An empty configured
    token disables internal endpoints (everything 401s) so a forgotten env var
    can't silently expose them."""
    configured = settings.HUB_INTERNAL_TOKEN.get_secret_value()
    if not configured:
        raise _invalid_internal_token()
    if x_hub_internal_token is None or not hmac.compare_digest(
        x_hub_internal_token, configured
    ):
        raise _invalid_internal_token()


class ConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    token: str = Field(min_length=1, max_length=512)


class ConsumeResponse(BaseModel):
    session_id: UUID
    customer_id: UUID
    email: str
    expires_at: datetime


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


@router.post(
    "/magic-link/consume",
    response_model=ConsumeResponse,
    dependencies=[Depends(require_internal_token)],
)
async def consume_magic_link_internal(
    body: ConsumeRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConsumeResponse:
    """Consume a magic-link token. Returns session data Next.js needs to set
    the cookie. Any failure (unknown report, bad token, expired, replay,
    cross-customer) → 410 magic-link-expired-or-consumed, same as the public
    consume path. The "same error class" rule (PLAN.md §4.7) lets Next.js
    render the expired-link page without branching on root cause."""
    report = await session.get(Report, body.report_id)
    if report is None:
        raise magic_link_expired_or_consumed()

    row = await consume_magic_link(
        session, body.token, expected_customer_id=report.customer_id
    )
    auth_session = await mint_session(
        session, customer_id=row.customer_id, email=row.email
    )
    await log_magic_link_consumed(
        session,
        customer_id=row.customer_id,
        token_jti=row.token_hash[:16],
        ip=_client_ip(request),
    )
    await session.commit()

    return ConsumeResponse(
        session_id=auth_session.id,
        customer_id=auth_session.customer_id,
        email=auth_session.email,
        expires_at=auth_session.expires_at,
    )


# === M6.2: iframe JWT mint =============================================


class IframeJwtRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    session_id: UUID


class IframeJwtResponse(BaseModel):
    token: str
    ttl_seconds: int
    # Report metadata for the chrome (DESIGN.md S2 metadata row). Bundled
    # here so Next.js renders the chrome in one round-trip; a separate
    # /internal/report-metadata endpoint would add a hop with no benefit
    # since both calls happen in the same server render anyway.
    report_title: str
    customer_name: str
    generated_at: datetime


def _iframe_unauthorized() -> HubError:
    """Single failure class for iframe-jwt mint: bad/expired session, missing
    report, or cross-customer. Next.js maps this to "send the user back to
    /r/[id] email-entry" — same anti-enum posture as the GET /r/{id} flow."""
    return HubError(
        slug="iframe-jwt-unauthorized",
        status=401,
        title="Iframe token not authorized",
        detail="The session does not authorize iframe access for this report.",
    )


@router.post(
    "/iframe-jwt",
    response_model=IframeJwtResponse,
    dependencies=[Depends(require_internal_token)],
)
async def mint_iframe_jwt_internal(
    body: IframeJwtRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IframeJwtResponse:
    """Mint a 60-second iframe JWT bound to (report_id, customer_id).

    Next.js calls this server-side every time it renders /r/[id] chrome.
    The JWT itself is what the browser sees in the iframe `src`; the
    session_id never leaves the server.

    Verification chain:
      1. session_id resolves to an unexpired session row → customer_id
      2. report_id exists and belongs to that customer
    Any failure → 401 iframe-jwt-unauthorized (no info leak; Next.js renders
    the email-entry page on this response, matching the M4 anti-enum rule)."""
    auth_session = await verify_and_refresh_session(session, body.session_id)
    if auth_session is None:
        raise _iframe_unauthorized()

    report = await session.get(Report, body.report_id)
    if report is None or report.customer_id != auth_session.customer_id:
        raise _iframe_unauthorized()

    customer = await session.get(Customer, auth_session.customer_id)
    if customer is None:  # pragma: no cover — FK guarantees this
        raise _iframe_unauthorized()

    await session.commit()  # persist the sliding-window refresh on session row

    token = mint_iframe_jwt(
        report_id=body.report_id, customer_id=auth_session.customer_id
    )
    return IframeJwtResponse(
        token=token,
        ttl_seconds=settings.IFRAME_JWT_TTL_SECONDS,
        report_title=report.title,
        customer_name=customer.name,
        generated_at=report.generated_at,
    )
