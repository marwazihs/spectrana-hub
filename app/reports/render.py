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
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.errors import iframe_token_invalid, report_not_found
from app.reports.html_injector import INJECTOR_VERSION, inject
from app.reports.models import Report
from app.reports.storage import S3ClientProtocol, get_html, s3_client
from app.auth.service import verify_iframe_jwt


iframe_router = APIRouter(tags=["iframe"])


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
