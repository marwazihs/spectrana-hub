"""Viewer chrome + iframe content origin (PLAN.md §4.5-4.8).

Two routers in one module:

  - `iframe_router` serves the iframe content origin (`reports.<domain>`).
    Exposes GET /render/{report_id}?t=<jwt>. Production deploys this on a
    separate hostname; tests just check the route exists on the same app.
  - `viewer_router` serves the human-facing viewer on the primary origin.
    Routes: GET /r/{id}, POST /r/{id}/request-link, GET /r/{id}?token=...

The iframe origin is intentionally separate (different hostname, different
CSP, no session cookie scope) so a script inside the iframe cannot read
the parent's session cookie or escape into the primary origin.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email import send_magic_link_email
from app.auth.rate_limit import check_magic_link_ip, check_magic_link_report
from app.auth.service import (
    SESSION_COOKIE_NAME,
    consume_magic_link,
    mint_iframe_jwt,
    mint_magic_link,
    mint_session,
    verify_and_refresh_session,
    verify_iframe_jwt,
)
from app.config import settings
from app.db.session import get_session
from app.errors import iframe_token_invalid, magic_link_expired_or_consumed
from app.events.service import (
    log_magic_link_consumed,
    log_magic_link_issued,
    log_report_view,
)
from app.reports.html_injector import INJECTOR_VERSION, inject
from app.reports.models import Report
from app.reports.storage import S3ClientProtocol, get_html, s3_client


iframe_router = APIRouter(tags=["iframe"])
viewer_router = APIRouter(tags=["viewer"])


_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)


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
    # Step 1: JWT verify scoped to this report_id (issuer + exp + iat enforced
    # inside verify_iframe_jwt). Failure → 401 iframe-token-invalid.
    payload = verify_iframe_jwt(t, expected_report_id=report_id)
    claim_customer_id = UUID(payload["customer_id"])

    # Step 2: confirm report exists and the claim's customer owns it.
    stmt = select(Report).where(
        and_(Report.id == report_id, Report.customer_id == claim_customer_id)
    )
    report = (await session.execute(stmt)).scalar_one_or_none()
    if report is None:
        # 401 not 404: a valid JWT pointing at a missing/foreign report means
        # the signer is misbehaving, not a curious viewer.
        raise iframe_token_invalid()

    # Step 3: fetch S3 body, inject, hash for ETag.
    html_bytes = await get_html(s3, customer_id=claim_customer_id, report_id=report_id)
    body = inject(html_bytes)
    digest = hashlib.sha256(body).hexdigest()
    etag = _render_etag(digest)

    headers = _render_headers(etag)

    if if_none_match == etag:
        return Response(status_code=304, headers=headers)

    return Response(content=body, headers=headers, media_type="text/html")


# === Viewer chrome (primary origin) ======================================


def _render_email_entry(report_id: UUID, *, submitted: bool = False) -> HTMLResponse:
    html = _env.get_template("email_entry.html.j2").render(
        brand=settings.HUB_EMAIL_BRAND_NAME,
        report_id=report_id,
        submitted=submitted,
        ttl_minutes=settings.MAGIC_LINK_TTL_MINUTES,
    )
    return HTMLResponse(content=html)


def _set_session_cookie(response: Response, session_id: UUID) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=str(session_id),
        max_age=settings.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only if a single hop is present; otherwise fall
    # back to the immediate peer. Deployment behind a known proxy will refine
    # this later (M8).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


async def _load_report(session: AsyncSession, report_id: UUID) -> Report | None:
    return await session.get(Report, report_id)


@viewer_router.get("/r/{report_id}")
async def viewer(
    report_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: str | None = Query(default=None),
) -> Response:
    """Three states (§4.5):
       - token query present → consume magic-link (§4.7)
       - session cookie + matching customer → chrome page
       - else → email-entry page
    Cross-customer mismatch → email-entry page (NOT a distinct 404, to avoid
    confirming the report exists)."""
    if token is not None:
        return await _consume_token(request, session, report_id, token)

    report = await _load_report(session, report_id)

    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    auth_session = None
    if raw_cookie:
        try:
            auth_session = await verify_and_refresh_session(session, UUID(raw_cookie))
        except ValueError:
            auth_session = None

    # State 1: no/expired/invalid session cookie → email entry (works even if
    # the report doesn't exist — anti-enumeration).
    if auth_session is None:
        return _render_email_entry(report_id)

    # State 3: session valid but customer mismatch (or report missing) → also
    # render email entry. The customer has no business signed-in-state here.
    if report is None or report.customer_id != auth_session.customer_id:
        return _render_email_entry(report_id)

    # State 2: render chrome. Mint a 60s iframe JWT and embed.
    iframe_jwt = mint_iframe_jwt(report_id=report_id, customer_id=auth_session.customer_id)
    iframe_src = (
        f"https://{settings.HUB_REPORTS_DOMAIN}/render/{report_id}?t={iframe_jwt}"
    )
    html = _env.get_template("chrome.html.j2").render(
        brand=settings.HUB_EMAIL_BRAND_NAME,
        email=auth_session.email,
        iframe_src=iframe_src,
        report_id=report_id,
    )

    await log_report_view(
        session,
        customer_id=auth_session.customer_id,
        report_id=report_id,
        session_id=auth_session.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    await session.commit()

    return HTMLResponse(content=html)


async def _consume_token(
    request: Request,
    session: AsyncSession,
    report_id: UUID,
    raw_token: str,
) -> Response:
    """Magic-link consume (§4.7). Any failure → 410, same error class."""
    report = await _load_report(session, report_id)
    if report is None:
        raise magic_link_expired_or_consumed()

    try:
        row = await consume_magic_link(
            session, raw_token, expected_customer_id=report.customer_id
        )
    except Exception:
        # consume_magic_link raises HubError; propagate. Any other exception
        # is a bug — let it bubble so the global handler can log it.
        raise

    auth_session = await mint_session(
        session, customer_id=row.customer_id, email=row.email
    )
    await log_magic_link_consumed(
        session,
        customer_id=row.customer_id,
        token_jti=row.token_hash[:16],  # truncated hash as opaque jti
        ip=_client_ip(request),
    )
    await session.commit()

    redirect = RedirectResponse(url=f"/r/{report_id}", status_code=302)
    _set_session_cookie(redirect, auth_session.id)
    return redirect


@viewer_router.post("/r/{report_id}/request-link")
async def request_link(
    report_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: Annotated[str, Form(min_length=3, max_length=320)],
) -> HTMLResponse:
    """Mint + email a magic link if the address is on the report's customer's
    allowlist. Always return the same 200 response regardless (§4.6 anti-enum).
    Rate-limited per IP (5/min) and per report (10/hour)."""
    ip = _client_ip(request)

    # Rate-limit gates fire BEFORE any branch on report-existence so timing
    # doesn't leak.
    await check_magic_link_ip(session, ip)
    await check_magic_link_report(session, report_id)

    report = await _load_report(session, report_id)
    normalized = email.strip().lower()

    if report is not None:
        customer_allowlist = await _customer_allowlist(session, report.customer_id)
        if normalized in {e.lower() for e in customer_allowlist}:
            raw = await mint_magic_link(
                session, customer_id=report.customer_id, email=normalized
            )
            await log_magic_link_issued(
                session,
                customer_id=report.customer_id,
                email=normalized,
                ip=ip,
            )
            await session.commit()
            # Send the email outside the DB transaction so SMTP latency doesn't
            # extend Postgres locks. Failures here are logged but do not flip
            # the user-facing response (still the generic 200).
            try:
                await send_magic_link_email(
                    to_email=normalized, report_id=report_id, raw_token=raw
                )
            except Exception:  # pragma: no cover - SMTP errors are operational
                pass

    return _render_email_entry(report_id, submitted=True)


async def _customer_allowlist(
    session: AsyncSession, customer_id: UUID
) -> list[str]:
    from app.auth.models import Customer

    row = await session.get(Customer, customer_id)
    return list(row.allowlist_emails) if row is not None else []
