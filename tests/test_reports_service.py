"""Reports service: publish (idempotent), list (cursor + tsquery), get, delete."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.errors import HubError
from app.events.models import Event
from app.reports.models import IdempotencyKey, Report
from app.reports.schemas import PublishRequest, SupplementaryFileIn
from app.reports.service import (
    Cursor,
    delete_report,
    get_report,
    list_reports,
    metadata_etag,
    publish_report,
    report_url,
)
from tests.test_reports_storage import FakeS3


# --- Fixtures ------------------------------------------------------------


async def _make_customer(session: AsyncSession, name: str = "Acme") -> Customer:
    c = Customer(
        name=name,
        allowlist_emails=["ops@acme.com"],
        api_key_hash=bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    return c


def _publish_body(
    customer_id: UUID,
    *,
    title: str = "Q3 earnings",
    description: str = "",
    tags: list[str] | None = None,
    html: str = "<h1>hi</h1>",
    generated_at: datetime | None = None,
) -> PublishRequest:
    return PublishRequest.model_validate(
        {
            "customer_id": str(customer_id),
            "title": title,
            "description": description,
            "tags": tags or [],
            "generated_at": (generated_at or datetime.now(tz=UTC)).isoformat(),
            "html": html,
            "supplementary_files": [],
        }
    )


# --- Publish -------------------------------------------------------------


async def test_publish_writes_report_idempotency_event_and_returns_201(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id, tags=["q3", "earnings"])

    response, status = await publish_report(
        session,
        customer=c,
        idempotency_key=str(uuid4()),
        body=body,
        s3=s3,
        supplementary_payload=[],
    )

    assert status == 201
    assert response.url == report_url(response.report_id)

    # Report row
    report = (
        await session.execute(select(Report).where(Report.id == response.report_id))
    ).scalar_one()
    assert report.customer_id == c.id
    assert report.title == "Q3 earnings"
    assert report.tags == ["q3", "earnings"]
    assert report.size_bytes == len(body.html.encode("utf-8"))
    assert report.s3_key == f"{c.id}/{report.id}/index.html"

    # Idempotency row
    idem = (
        await session.execute(
            select(IdempotencyKey).where(IdempotencyKey.customer_id == c.id)
        )
    ).scalar_one()
    assert idem.status_code == 201
    assert idem.response_body["report_id"] == str(response.report_id)

    # Event row
    ev = (
        await session.execute(select(Event).where(Event.event_type == "report_published"))
    ).scalar_one()
    assert ev.payload["deduped"] is False
    assert ev.payload["report_id"] == str(response.report_id)

    # S3 has the html
    assert (
        f"{c.id}/{report.id}/index.html" in s3.store
    ), "html should be uploaded to S3"


async def test_publish_replays_on_idempotency_key_reuse(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id)
    key = str(uuid4())

    first, first_status = await publish_report(
        session,
        customer=c,
        idempotency_key=key,
        body=body,
        s3=s3,
        supplementary_payload=[],
    )
    second, second_status = await publish_report(
        session,
        customer=c,
        idempotency_key=key,
        body=body,
        s3=s3,
        supplementary_payload=[],
    )

    assert first_status == 201
    assert second_status == 200
    assert first.report_id == second.report_id
    # Only one row in reports, one in idempotency_keys, one published event.
    n_reports = (
        await session.execute(select(func.count()).select_from(Report))
    ).scalar_one()
    n_events = (
        await session.execute(
            select(func.count()).select_from(Event).where(
                Event.event_type == "report_published"
            )
        )
    ).scalar_one()
    assert n_reports == 1
    assert n_events == 1


async def test_publish_rejects_mismatched_customer(session: AsyncSession) -> None:
    c = await _make_customer(session)
    other = await _make_customer(session, "Other")
    body = _publish_body(other.id)

    with pytest.raises(HubError) as exc:
        await publish_report(
            session,
            customer=c,
            idempotency_key=str(uuid4()),
            body=body,
            s3=FakeS3(),
            supplementary_payload=[],
        )
    assert exc.value.slug == "customer-not-authorized"


async def test_publish_uploads_supplementary_files(session: AsyncSession) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id)
    meta = SupplementaryFileIn(
        filename="data.csv",
        content_type="text/csv",
        base64=base64.b64encode(b"a,b,c").decode(),
    )

    resp, _ = await publish_report(
        session,
        customer=c,
        idempotency_key=str(uuid4()),
        body=body,
        s3=s3,
        supplementary_payload=[(meta, b"a,b,c")],
    )

    assert f"{c.id}/{resp.report_id}/supplementary/data.csv" in s3.store
    report = (
        await session.execute(select(Report).where(Report.id == resp.report_id))
    ).scalar_one()
    assert report.supplementary_files == [
        {"filename": "data.csv", "content_type": "text/csv"}
    ]
    # size_bytes counts html + supplementary bytes.
    assert report.size_bytes == len(body.html.encode("utf-8")) + len(b"a,b,c")


async def test_publish_503_when_s3_down_leaves_no_db_rows(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    s3 = FakeS3(fail_on={"put_object"})
    body = _publish_body(c.id)

    with pytest.raises(HubError) as exc:
        await publish_report(
            session,
            customer=c,
            idempotency_key=str(uuid4()),
            body=body,
            s3=s3,
            supplementary_payload=[],
        )
    assert exc.value.status == 503

    # Nothing committed — retry with same key replays.
    n = (await session.execute(select(func.count()).select_from(Report))).scalar_one()
    assert n == 0


# --- List / search -------------------------------------------------------


async def _seed_report(
    session: AsyncSession,
    c: Customer,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    generated_at: datetime | None = None,
    html: str = "<p>x</p>",
) -> Report:
    """Insert a report directly (bypasses S3) for list tests."""
    from uuid_utils import uuid7

    rid = UUID(str(uuid7()))
    r = Report(
        id=rid,
        customer_id=c.id,
        title=title,
        description=description,
        tags=tags or [],
        generated_at=generated_at or datetime.now(tz=UTC),
        s3_key=f"{c.id}/{rid}/index.html",
        supplementary_files=[],
        size_bytes=len(html.encode("utf-8")),
    )
    session.add(r)
    await session.flush()
    await session.refresh(r)
    return r


async def test_list_scoped_to_customer(session: AsyncSession) -> None:
    c1 = await _make_customer(session, "C1")
    c2 = await _make_customer(session, "C2")
    await _seed_report(session, c1, title="alpha")
    await _seed_report(session, c2, title="beta")
    await session.commit()

    resp = await list_reports(session, customer=c1)
    assert [i.title for i in resp.items] == ["alpha"]
    assert resp.has_more is False
    assert resp.next_cursor is None


async def test_list_default_order_generated_at_desc(session: AsyncSession) -> None:
    c = await _make_customer(session)
    now = datetime.now(tz=UTC)
    await _seed_report(session, c, title="old", generated_at=now - timedelta(hours=2))
    await _seed_report(session, c, title="new", generated_at=now)
    await _seed_report(session, c, title="mid", generated_at=now - timedelta(hours=1))
    await session.commit()

    resp = await list_reports(session, customer=c)
    assert [i.title for i in resp.items] == ["new", "mid", "old"]


async def test_list_pagination_cursor_walks_all_pages(session: AsyncSession) -> None:
    c = await _make_customer(session)
    now = datetime.now(tz=UTC)
    for i in range(5):
        await _seed_report(
            session,
            c,
            title=f"t{i}",
            generated_at=now - timedelta(minutes=i),
        )
    await session.commit()

    page1 = await list_reports(session, customer=c, limit=2)
    assert len(page1.items) == 2
    assert page1.has_more is True
    page2 = await list_reports(session, customer=c, limit=2, cursor=page1.next_cursor)
    assert len(page2.items) == 2
    page3 = await list_reports(session, customer=c, limit=2, cursor=page2.next_cursor)
    assert len(page3.items) == 1
    assert page3.has_more is False

    seen = [i.report_id for i in page1.items + page2.items + page3.items]
    assert len(set(seen)) == 5  # no dupes across pages


async def test_list_tag_filter(session: AsyncSession) -> None:
    c = await _make_customer(session)
    await _seed_report(session, c, title="a", tags=["earnings"])
    await _seed_report(session, c, title="b", tags=["pipeline"])
    await _seed_report(session, c, title="c", tags=["earnings", "pipeline"])
    await session.commit()

    resp = await list_reports(session, customer=c, tags=["earnings"])
    assert sorted(i.title for i in resp.items) == ["a", "c"]


async def test_list_date_range_filter(session: AsyncSession) -> None:
    c = await _make_customer(session)
    now = datetime.now(tz=UTC)
    await _seed_report(session, c, title="old", generated_at=now - timedelta(days=3))
    await _seed_report(session, c, title="mid", generated_at=now - timedelta(days=1))
    await _seed_report(session, c, title="new", generated_at=now)
    await session.commit()

    resp = await list_reports(
        session,
        customer=c,
        date_from=now - timedelta(days=2),
        date_to=now - timedelta(hours=1),
    )
    assert [i.title for i in resp.items] == ["mid"]


async def test_list_search_uses_tsquery_and_logs_event(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    await _seed_report(session, c, title="Quarterly earnings", description="rev up")
    await _seed_report(session, c, title="Pipeline review", description="leads")
    await session.commit()

    resp = await list_reports(session, customer=c, search="earnings")
    assert [i.title for i in resp.items] == ["Quarterly earnings"]

    ev = (
        await session.execute(
            select(Event).where(Event.event_type == "search_query")
        )
    ).scalar_one()
    assert ev.payload == {"query": "earnings", "result_count": 1}


async def test_list_rejects_bad_limit(session: AsyncSession) -> None:
    c = await _make_customer(session)
    with pytest.raises(HubError) as exc:
        await list_reports(session, customer=c, limit=0)
    assert exc.value.slug == "validation-failed"
    with pytest.raises(HubError):
        await list_reports(session, customer=c, limit=10_000)


async def test_list_rejects_invalid_cursor(session: AsyncSession) -> None:
    c = await _make_customer(session)
    with pytest.raises(HubError) as exc:
        await list_reports(session, customer=c, cursor="not-base64-json")
    assert exc.value.slug == "validation-failed"


# --- Get / Delete --------------------------------------------------------


async def test_get_returns_html_and_metadata(session: AsyncSession) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id, html="<p>full body</p>")
    resp, _ = await publish_report(
        session,
        customer=c,
        idempotency_key=str(uuid4()),
        body=body,
        s3=s3,
        supplementary_payload=[],
    )

    detail = await get_report(session, customer=c, report_id=resp.report_id, s3=s3)
    assert detail.html == "<p>full body</p>"
    assert detail.title == "Q3 earnings"


async def test_get_404_when_report_belongs_to_other_customer(
    session: AsyncSession,
) -> None:
    c1 = await _make_customer(session, "C1")
    c2 = await _make_customer(session, "C2")
    r = await _seed_report(session, c2, title="confidential")
    await session.commit()

    with pytest.raises(HubError) as exc:
        await get_report(session, customer=c1, report_id=r.id, s3=FakeS3())
    assert exc.value.slug == "report-not-found"


async def test_get_404_when_report_missing(session: AsyncSession) -> None:
    c = await _make_customer(session)
    with pytest.raises(HubError) as exc:
        await get_report(session, customer=c, report_id=uuid4(), s3=FakeS3())
    assert exc.value.slug == "report-not-found"


async def test_delete_removes_row_and_s3_then_404s(session: AsyncSession) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id)
    resp, _ = await publish_report(
        session,
        customer=c,
        idempotency_key=str(uuid4()),
        body=body,
        s3=s3,
        supplementary_payload=[],
    )

    await delete_report(session, customer=c, report_id=resp.report_id, s3=s3)

    gone = (
        await session.execute(select(Report).where(Report.id == resp.report_id))
    ).scalar_one_or_none()
    assert gone is None
    assert f"{c.id}/{resp.report_id}/index.html" not in s3.store


async def test_delete_404_when_cross_customer(session: AsyncSession) -> None:
    c1 = await _make_customer(session, "C1")
    c2 = await _make_customer(session, "C2")
    r = await _seed_report(session, c2, title="x")
    await session.commit()

    with pytest.raises(HubError) as exc:
        await delete_report(session, customer=c1, report_id=r.id, s3=FakeS3())
    assert exc.value.slug == "report-not-found"

    # c2's report is intact.
    still_there = (
        await session.execute(select(Report).where(Report.id == r.id))
    ).scalar_one_or_none()
    assert still_there is not None


async def test_delete_swallows_s3_failure_but_drops_db_row(
    session: AsyncSession,
) -> None:
    c = await _make_customer(session)
    s3 = FakeS3()
    body = _publish_body(c.id)
    resp, _ = await publish_report(
        session,
        customer=c,
        idempotency_key=str(uuid4()),
        body=body,
        s3=s3,
        supplementary_payload=[],
    )
    s3.fail_on = {"list_objects_v2"}

    await delete_report(session, customer=c, report_id=resp.report_id, s3=s3)

    gone = (
        await session.execute(select(Report).where(Report.id == resp.report_id))
    ).scalar_one_or_none()
    assert gone is None


# --- Cursor + ETag -------------------------------------------------------


def test_cursor_roundtrip_with_and_without_rank() -> None:
    now = datetime.now(tz=UTC)
    rid = uuid4()
    c1 = Cursor(generated_at=now, report_id=rid, ts_rank=None)
    assert Cursor.decode(c1.encode()) == c1
    c2 = Cursor(generated_at=now, report_id=rid, ts_rank=0.42)
    assert Cursor.decode(c2.encode()) == c2


def test_metadata_etag_is_stable_and_excludes_html() -> None:
    from app.reports.schemas import ReportDetail

    base = {
        "report_id": uuid4(),
        "title": "t",
        "description": "d",
        "tags": ["a", "b"],
        "generated_at": datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        "url": "https://hub.majeve.com/r/x",
        "size_bytes": 100,
        "html": "<p>1</p>",
    }
    d1 = ReportDetail(**base)
    d2 = ReportDetail(**{**base, "html": "<p>different body</p>"})
    assert metadata_etag(d1) == metadata_etag(d2)
    d3 = ReportDetail(**{**base, "title": "different"})
    assert metadata_etag(d1) != metadata_etag(d3)
