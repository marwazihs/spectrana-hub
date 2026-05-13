"""HTTP routes for /v1/reports (PLAN.md §4.1-4.4).

Thin handlers — parse headers, dispatch to `app.reports.service`. The
service layer owns all business logic, the S3 client is a FastAPI dep
that tests override via `app.dependency_overrides`.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import CurrentCustomer
from app.db.session import get_session
from app.errors import (
    idempotency_key_invalid,
    idempotency_key_missing,
    validation_failed,
)
from app.reports import service
from app.reports.schemas import (
    PublishRequest,
    ReportDetail,
    ReportListResponse,
    ReportResponse,
    SupplementaryFileIn,
)
from app.reports.service import metadata_etag
from app.reports.storage import S3ClientProtocol, s3_client


router = APIRouter(prefix="/v1/reports", tags=["reports"])


# --- Helpers -------------------------------------------------------------


def _parse_idempotency_key(raw: str | None) -> str:
    if not raw:
        raise idempotency_key_missing()
    try:
        UUID(raw)
    except ValueError as exc:
        raise idempotency_key_invalid() from exc
    return raw


def _decode_supplementary(
    files: list[SupplementaryFileIn],
) -> list[tuple[SupplementaryFileIn, bytes]]:
    decoded: list[tuple[SupplementaryFileIn, bytes]] = []
    for meta in files:
        try:
            raw = base64.b64decode(meta.base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise validation_failed(
                f"supplementary file '{meta.filename}' has invalid base64"
            ) from exc
        decoded.append((meta, raw))
    return decoded


def _parse_iso(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError as exc:
        raise validation_failed(f"invalid ISO8601 timestamp: {ts}") from exc


def _parse_tags(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [t for t in (s.strip() for s in raw.split(",")) if t]


# --- Routes --------------------------------------------------------------


@router.post("", status_code=201)
async def publish(
    request: Request,
    response: Response,
    body: PublishRequest,
    customer: CurrentCustomer,
    session: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3ClientProtocol, Depends(s3_client)],
    idempotency_key_header: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> ReportResponse:
    key = _parse_idempotency_key(idempotency_key_header)
    supplementary = _decode_supplementary(body.supplementary_files)
    resp, status = await service.publish_report(
        session,
        customer=customer,
        idempotency_key=key,
        body=body,
        s3=s3,
        supplementary_payload=supplementary,
    )
    response.status_code = status
    return resp


@router.get("")
async def list_(
    customer: CurrentCustomer,
    session: Annotated[AsyncSession, Depends(get_session)],
    search: str | None = Query(default=None, max_length=500),
    tags: str | None = Query(default=None, max_length=2000),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ReportListResponse:
    return await service.list_reports(
        session,
        customer=customer,
        search=search,
        tags=_parse_tags(tags),
        date_from=_parse_iso(from_),
        date_to=_parse_iso(to),
        cursor=cursor,
        limit=limit,
    )


@router.get("/{report_id}")
async def get_one(
    report_id: UUID,
    request: Request,
    customer: CurrentCustomer,
    session: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3ClientProtocol, Depends(s3_client)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    detail: ReportDetail = await service.get_report(
        session, customer=customer, report_id=report_id, s3=s3
    )
    etag = metadata_etag(detail)
    if if_none_match == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=0, must-revalidate",
            },
        )
    return Response(
        content=detail.model_dump_json(),
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


@router.delete("/{report_id}", status_code=204)
async def delete_(
    report_id: UUID,
    customer: CurrentCustomer,
    session: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3ClientProtocol, Depends(s3_client)],
) -> Response:
    await service.delete_report(
        session, customer=customer, report_id=report_id, s3=s3
    )
    return Response(status_code=204)
