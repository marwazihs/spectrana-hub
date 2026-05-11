"""Iframe content origin + magic-link request endpoint.

After M6 (Next.js viewer), the Jinja-served viewer chrome (GET /r/{id},
GET /r/{id}?token=, the email-entry and chrome templates) is retired —
Next.js owns those routes end-to-end. Hub keeps:

  - `iframe_router`: GET /render/{report_id}?t=<jwt>. Served from the
    `reports.<domain>` origin in production. Unchanged from M4.2.
  - `viewer_router`: POST /r/{id}/request-link only. Dual personality:
      * Unauth → anti-enum + IP rate-limited + email-only (Next.js
        server-action target for the email-entry form).
      * Agent (Authorization: Bearer ...) → real errors + per-API-key
        rate limit + `delivery: "email"|"return"` so an agent can mint
        a link and hand it off through chat/SMS/whatever channel.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import authenticate_api_key
from app.auth.email import send_magic_link_email
from app.auth.models import Customer
from app.auth.rate_limit import (
    check_magic_link_api_key,
    check_magic_link_ip,
    check_magic_link_report,
)
from app.auth.service import mint_iframe_jwt, mint_magic_link, verify_iframe_jwt
from app.config import settings
from app.errors import (
    HubError,
    iframe_token_invalid,
    report_not_found,
    validation_failed,
)
from app.events.service import log_magic_link_issued
from app.reports.html_injector import INJECTOR_VERSION, inject
from app.reports.models import Report
from app.reports.storage import S3ClientProtocol, get_html, s3_client
from app.db.session import get_session


iframe_router = APIRouter(tags=["iframe"])
viewer_router = APIRouter(tags=["viewer"])


# === Iframe content origin (unchanged from M4.2) ========================


def _render_etag(s3_etag_like: str) -> str:
    """ETag = sha256(html bytes) suffixed with the injector version so a
    shim/poster bump invalidates cached entries (PLAN.md §4.8)."""
    return f'"{s3_etag_like}-{INJECTOR_VERSION}"'


def _render_headers(etag: str) -> dict[str, str]:
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "img-src data: https:; "
            "style-src 'unsafe-inline' 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'self';"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-cache, must-revalidate",
        "ETag": etag,
        "Referrer-Policy": "no-referrer",
    }


@iframe_router.get("/render/{report_id}")
async def render_iframe(
    report_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3ClientProtocol, Depends(s3_client)],
    t: str = Query(..., description="Iframe JWT (60s, HS256)"),
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    payload = verify_iframe_jwt(t, expected_report_id=report_id)
    claim_customer_id = UUID(payload["customer_id"])

    stmt = select(Report).where(
        and_(Report.id == report_id, Report.customer_id == claim_customer_id)
    )
    report = (await session.execute(stmt)).scalar_one_or_none()
    if report is None:
        raise iframe_token_invalid()

    html_bytes = await get_html(s3, customer_id=claim_customer_id, report_id=report_id)
    body = inject(html_bytes)
    digest = hashlib.sha256(body).hexdigest()
    etag = _render_etag(digest)
    headers = _render_headers(etag)

    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, headers=headers, media_type="text/html")


# === Magic-link request — dual-personality endpoint =====================


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


async def _load_report(session: AsyncSession, report_id: UUID) -> Report | None:
    return await session.get(Report, report_id)


async def _customer_allowlist(session: AsyncSession, customer_id: UUID) -> list[str]:
    row = await session.get(Customer, customer_id)
    return list(row.allowlist_emails) if row is not None else []


def _viewer_consume_url(report_id: UUID, raw_token: str) -> str:
    """The URL the recipient clicks. Next.js owns /r/[id]/consume after M6.4;
    until then, the existing GET /r/{id}?token= consume path also works
    (legacy behavior preserved). The host is the viewer origin, not the
    reports iframe origin."""
    return (
        f"https://{settings.HUB_PRIMARY_DOMAIN}/r/{report_id}/consume"
        f"?token={raw_token}"
    )


class RequestLinkBody(BaseModel):
    """Request body. Both paths post JSON; only the unauth path also accepts
    bare form-encoded email for backward compat with anything still hitting
    it that way (the M4 Jinja form is retired; nothing should be in v1)."""

    model_config = ConfigDict(extra="forbid")

    # Plain str, not EmailStr — Hub doesn't deliver to addresses outside the
    # allowlist (allowlist membership check is the gate), and pulling in
    # email-validator just to reject malformed forms is overkill. RFC-shape
    # is bounded by min/max length.
    email: str = Field(min_length=3, max_length=320)
    # Ignored on the unauth path (always "email"). Honored only when an
    # API key authenticates the call.
    delivery: Literal["email", "return"] = "email"
    # Free-form audit label for the agent path (e.g. "slack", "sms").
    # Capped to keep events compact.
    channel_hint: str | None = Field(default=None, max_length=64)


class RequestLinkAgentResponse(BaseModel):
    """Agent-path response. Always 200 if email is on the allowlist.

    With `delivery: "return"`, `url` is the bearer credential — treat like
    a password (no logs, no transcripts, no telemetry). Single-use, expires
    in MAGIC_LINK_TTL_MINUTES.
    """

    status: Literal["sent", "returned"]
    url: str | None = None
    expires_in_minutes: int


class RequestLinkPublicResponse(BaseModel):
    """Unauth path response. Identical shape regardless of success/failure
    (anti-enumeration). Body text mirrors PLAN.md §4.6."""

    status: Literal["accepted"] = "accepted"
    message: str = "If this email is on file, a link has been sent."


def _email_not_allowlisted() -> HubError:
    """Distinct slug used ONLY on the agent path. Public callers never see
    this — they get the generic anti-enum 200."""
    return HubError(
        slug="email-not-allowlisted",
        status=422,
        title="Email not allowlisted",
        detail="The recipient email is not on this report's customer allowlist.",
    )


async def _public_request_link(
    request: Request,
    session: AsyncSession,
    report_id: UUID,
    body: RequestLinkBody,
) -> RequestLinkPublicResponse:
    """Anti-enum path. Always returns the same 200 regardless of report
    existence, allowlist membership, or SMTP outcome."""
    ip = _client_ip(request)

    # Rate-limit gates fire BEFORE any branch on report existence so timing
    # doesn't leak.
    await check_magic_link_ip(session, ip)
    await check_magic_link_report(session, report_id)

    report = await _load_report(session, report_id)
    normalized = body.email.strip().lower()

    if report is not None:
        allowlist = await _customer_allowlist(session, report.customer_id)
        if normalized in {e.lower() for e in allowlist}:
            raw = await mint_magic_link(
                session, customer_id=report.customer_id, email=normalized
            )
            await log_magic_link_issued(
                session,
                customer_id=report.customer_id,
                email=normalized,
                ip=ip,
                delivery="email",
            )
            await session.commit()
            try:
                await send_magic_link_email(
                    to_email=normalized, report_id=report_id, raw_token=raw
                )
            except Exception:  # pragma: no cover — SMTP failures are operational
                pass

    return RequestLinkPublicResponse()


async def _agent_request_link(
    request: Request,
    session: AsyncSession,
    report_id: UUID,
    body: RequestLinkBody,
    customer: Customer,
) -> RequestLinkAgentResponse:
    """Authenticated path. Returns real errors and may return the raw URL.

    The agent's API key scopes mint to its own reports — cross-customer
    `report_id` is a 404, identical to other /v1 routes. Allowlist
    membership is still enforced (the link is bound to an email recipient
    even when channel is chat/SMS); off-allowlist → 422 with a distinct
    slug since the caller is authenticated and gains nothing from the
    anti-enum shape."""
    ip = _client_ip(request)
    await check_magic_link_api_key(session, customer.id)

    report = await _load_report(session, report_id)
    if report is None or report.customer_id != customer.id:
        raise report_not_found()

    normalized = body.email.strip().lower()
    allowlist = await _customer_allowlist(session, customer.id)
    if normalized not in {e.lower() for e in allowlist}:
        raise _email_not_allowlisted()

    if body.delivery == "return" and body.channel_hint is None:
        # Soft requirement: when the agent takes the URL out of band, audit
        # is the only record of where it went. Force a label.
        raise validation_failed(
            "channel_hint is required when delivery is 'return'."
        )

    raw = await mint_magic_link(session, customer_id=customer.id, email=normalized)
    await log_magic_link_issued(
        session,
        customer_id=customer.id,
        email=normalized,
        ip=ip,
        delivery=body.delivery,
        channel_hint=body.channel_hint,
    )
    await session.commit()

    if body.delivery == "email":
        try:
            await send_magic_link_email(
                to_email=normalized, report_id=report_id, raw_token=raw
            )
        except Exception:  # pragma: no cover
            pass
        return RequestLinkAgentResponse(
            status="sent",
            url=None,
            expires_in_minutes=settings.MAGIC_LINK_TTL_MINUTES,
        )

    return RequestLinkAgentResponse(
        status="returned",
        url=_viewer_consume_url(report_id, raw),
        expires_in_minutes=settings.MAGIC_LINK_TTL_MINUTES,
    )


@viewer_router.post("/r/{report_id}/request-link")
async def request_link(
    report_id: UUID,
    request: Request,
    body: RequestLinkBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Mint a magic-link token bound to (report_id, email).

    Without `Authorization`: anti-enum 200 every time, email-only delivery,
    IP + per-report rate limits. This is the public surface Next.js proxies
    the email-entry form to.

    With `Authorization: Bearer <api_key>`: real success/failure (404 for
    unknown/cross-customer report, 422 for off-allowlist email), per-API-key
    rate limit, `delivery: "return"` returns the URL so the agent can hand
    it off through chat. `channel_hint` is required when delivery is
    "return" so audit can answer "how did this token get to the recipient?"
    """
    if authorization is None:
        public = await _public_request_link(request, session, report_id, body)
        return Response(
            content=public.model_dump_json(),
            media_type="application/json",
            status_code=200,
        )

    # Authenticate; reuse the same path /v1/* routes use.
    customer = await authenticate_api_key(request, session)
    agent = await _agent_request_link(request, session, report_id, body, customer)
    return Response(
        content=agent.model_dump_json(),
        media_type="application/json",
        status_code=200,
    )
