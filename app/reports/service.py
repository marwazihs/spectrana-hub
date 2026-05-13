"""Reports service — CRUD + idempotency (PLAN.md §4.1-4.4).

All queries scope by `customer_id` from the authenticated API key — there is
no path that lets a customer see another customer's reports.

`publish_report` enforces the §4.1 write order:
    1. validate (caller / Pydantic)
    2. idempotency lookup
    3. uuid7 generation
    4. S3 PUT html
    5. S3 PUT supplementary
    6. INSERT report row
    7. INSERT idempotency row
    8. log event (caller via app.events.service)
    9. return
If step 4/5 raises, no DB row exists yet — retry with same key replays.
If step 6 fails, the row never lands; the idempotency row is also absent, so
retry replays. S3 orphans get cleaned by the nightly sweep.

`list_reports` implements keyset pagination per §4.2. Default order is
(generated_at DESC, report_id DESC). With ?search=, the order becomes
(ts_rank DESC, generated_at DESC, report_id DESC) and the cursor carries
all three sortable fields. Cursors are opaque base64-encoded JSON.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import uuid_utils
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.config import settings
from app.errors import customer_not_authorized, report_not_found, validation_failed
from app.events.service import log_report_published, log_search_query
from app.reports.models import IdempotencyKey, Report
from app.reports.schemas import (
    PublishRequest,
    ReportDetail,
    ReportListItem,
    ReportListResponse,
    ReportResponse,
    SupplementaryFileIn,
)
from app.reports.storage import (
    S3ClientProtocol,
    delete_report_objects,
    get_html,
    put_html,
    put_supplementary,
)


logger = logging.getLogger("hub.reports")


DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# --- URL helpers ---------------------------------------------------------


def report_url(report_id: UUID) -> str:
    return f"https://{settings.HUB_PRIMARY_DOMAIN}/r/{report_id}"


# --- Cursor codec --------------------------------------------------------


@dataclass(frozen=True)
class Cursor:
    generated_at: datetime
    report_id: UUID
    ts_rank: float | None  # only present in search results

    def encode(self) -> str:
        payload = {
            "g": self.generated_at.isoformat(),
            "r": str(self.report_id),
        }
        if self.ts_rank is not None:
            payload["s"] = self.ts_rank  # type: ignore[assignment]
        return base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii")

    @classmethod
    def decode(cls, raw: str) -> Cursor:
        try:
            decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
            payload = json.loads(decoded)
            return cls(
                generated_at=datetime.fromisoformat(payload["g"]),
                report_id=UUID(payload["r"]),
                ts_rank=payload.get("s"),
            )
        except (binascii.Error, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise validation_failed(f"invalid cursor: {exc}") from exc


# --- Publish -------------------------------------------------------------


async def publish_report(
    session: AsyncSession,
    *,
    customer: Customer,
    idempotency_key: str,
    body: PublishRequest,
    s3: S3ClientProtocol,
    supplementary_payload: list[tuple[SupplementaryFileIn, bytes]],
) -> tuple[ReportResponse, int]:
    """Idempotent publish. Returns (response, status_code) where 201=new, 200=replay."""
    if body.customer_id != customer.id:
        raise customer_not_authorized()

    # Step 2: idempotency lookup
    existing = await session.execute(
        select(IdempotencyKey).where(
            and_(
                IdempotencyKey.customer_id == customer.id,
                IdempotencyKey.key == idempotency_key,
            )
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return (ReportResponse.model_validate(row.response_body), 200)

    # Step 3: uuid v7 (time-sortable)
    report_id = UUID(str(uuid_utils.uuid7()))

    # Step 4: S3 PUT html (raises storage_unavailable → 503 on failure)
    s3_key = await put_html(
        s3, customer_id=customer.id, report_id=report_id, html=body.html
    )

    # Step 5: S3 PUT supplementary
    for meta, raw_bytes in supplementary_payload:
        await put_supplementary(
            s3,
            customer_id=customer.id,
            report_id=report_id,
            filename=meta.filename,
            content_type=meta.content_type,
            body=raw_bytes,
        )

    size_bytes = len(body.html.encode("utf-8")) + sum(
        len(b) for _, b in supplementary_payload
    )

    # Step 6: INSERT report
    report = Report(
        id=report_id,
        customer_id=customer.id,
        title=body.title,
        description=body.description,
        tags=body.tags,
        generated_at=body.generated_at,
        s3_key=s3_key,
        supplementary_files=[
            {"filename": m.filename, "content_type": m.content_type}
            for m, _ in supplementary_payload
        ],
        size_bytes=size_bytes,
    )
    session.add(report)
    try:
        await session.flush()
    except IntegrityError as exc:
        # uuid7 collision is astronomically unlikely; treat as server error.
        logger.error("report insert failed: %s", exc)
        raise

    # Step 7: idempotency row (committed atomically with the report)
    response = ReportResponse(
        report_id=report_id,
        url=report_url(report_id),
        created_at=report.created_at or datetime.now(tz=body.generated_at.tzinfo),
    )
    session.add(
        IdempotencyKey(
            customer_id=customer.id,
            key=idempotency_key,
            response_body=json.loads(response.model_dump_json()),
            status_code=201,
            expires_at=datetime.now(tz=body.generated_at.tzinfo).replace(microsecond=0)
            + IdempotencyKey.__ttl__,
        )
    )

    # Step 8: log event
    await log_report_published(
        session,
        customer_id=customer.id,
        report_id=report_id,
        idempotency_key=idempotency_key,
        deduped=False,
    )

    await session.commit()
    await session.refresh(report)
    # created_at had a server_default; refresh hydrates the real value.
    response = ReportResponse(
        report_id=report_id,
        url=report_url(report_id),
        created_at=report.created_at,
    )
    return (response, 201)


# --- List / search -------------------------------------------------------


async def list_reports(
    session: AsyncSession,
    *,
    customer: Customer,
    search: str | None = None,
    tags: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    log_search: bool = True,
) -> ReportListResponse:
    if limit < 1 or limit > MAX_LIMIT:
        raise validation_failed(f"limit must be 1..{MAX_LIMIT}")

    decoded_cursor = Cursor.decode(cursor) if cursor else None
    stmt = select(Report).where(Report.customer_id == customer.id)

    if tags:
        stmt = stmt.where(Report.tags.contains(tags))
    if date_from is not None:
        stmt = stmt.where(Report.generated_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Report.generated_at <= date_to)

    rank_col = None
    if search:
        tsquery = func.plainto_tsquery("english", search)
        stmt = stmt.where(Report.search_vector.op("@@")(tsquery))
        rank_col = func.ts_rank(Report.search_vector, tsquery)
        stmt = stmt.add_columns(rank_col.label("rank"))

        if decoded_cursor and decoded_cursor.ts_rank is not None:
            stmt = stmt.where(
                or_(
                    rank_col < decoded_cursor.ts_rank,
                    and_(
                        rank_col == decoded_cursor.ts_rank,
                        Report.generated_at < decoded_cursor.generated_at,
                    ),
                    and_(
                        rank_col == decoded_cursor.ts_rank,
                        Report.generated_at == decoded_cursor.generated_at,
                        Report.id < decoded_cursor.report_id,
                    ),
                )
            )
        stmt = stmt.order_by(rank_col.desc(), Report.generated_at.desc(), Report.id.desc())
    else:
        if decoded_cursor:
            stmt = stmt.where(
                or_(
                    Report.generated_at < decoded_cursor.generated_at,
                    and_(
                        Report.generated_at == decoded_cursor.generated_at,
                        Report.id < decoded_cursor.report_id,
                    ),
                )
            )
        stmt = stmt.order_by(Report.generated_at.desc(), Report.id.desc())

    stmt = stmt.limit(limit + 1)  # fetch one extra to compute has_more
    rows = (await session.execute(stmt)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    items: list[ReportListItem] = []
    last_rank: float | None = None
    last_report: Report | None = None
    for row in rows:
        if search:
            report, rank = row  # tuple
            last_rank = float(rank)
        else:
            report = row[0]
        last_report = report
        items.append(
            ReportListItem(
                report_id=report.id,
                title=report.title,
                description=report.description,
                tags=list(report.tags),
                generated_at=report.generated_at,
                url=report_url(report.id),
                size_bytes=report.size_bytes,
            )
        )

    next_cursor = None
    if has_more and last_report is not None:
        next_cursor = Cursor(
            generated_at=last_report.generated_at,
            report_id=last_report.id,
            ts_rank=last_rank if search else None,
        ).encode()

    if log_search and search:
        await log_search_query(
            session,
            customer_id=customer.id,
            query=search,
            result_count=len(items),
        )
        await session.commit()

    return ReportListResponse(items=items, next_cursor=next_cursor, has_more=has_more)


# --- Get one -------------------------------------------------------------


async def get_report(
    session: AsyncSession,
    *,
    customer: Customer,
    report_id: UUID,
    s3: S3ClientProtocol,
) -> ReportDetail:
    stmt = select(Report).where(
        and_(Report.id == report_id, Report.customer_id == customer.id)
    )
    report = (await session.execute(stmt)).scalar_one_or_none()
    if report is None:
        raise report_not_found()

    html_bytes = await get_html(s3, customer_id=customer.id, report_id=report_id)
    return ReportDetail(
        report_id=report.id,
        title=report.title,
        description=report.description,
        tags=list(report.tags),
        generated_at=report.generated_at,
        url=report_url(report.id),
        size_bytes=report.size_bytes,
        html=html_bytes.decode("utf-8"),
    )


def metadata_etag(item: ReportListItem | ReportDetail) -> str:
    """ETag = sha256 of stable metadata JSON (PLAN.md §4.3). html is excluded."""
    payload = {
        "report_id": str(item.report_id),
        "title": item.title,
        "description": item.description,
        "tags": item.tags,
        "generated_at": item.generated_at.isoformat(),
        "size_bytes": item.size_bytes,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f'"{digest}"'


# --- Delete --------------------------------------------------------------


async def delete_report(
    session: AsyncSession,
    *,
    customer: Customer,
    report_id: UUID,
    s3: S3ClientProtocol,
) -> None:
    """Postgres delete first, then S3. S3 failure is logged but not raised."""
    result = await session.execute(
        delete(Report).where(
            and_(Report.id == report_id, Report.customer_id == customer.id)
        )
    )
    if (result.rowcount or 0) == 0:
        raise report_not_found()
    await session.commit()

    n = await delete_report_objects(s3, customer_id=customer.id, report_id=report_id)
    if n == -1:
        logger.warning(
            "S3 cleanup deferred for report %s; nightly sweep will reconcile",
            report_id,
        )


