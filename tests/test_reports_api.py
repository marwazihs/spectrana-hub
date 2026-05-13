"""End-to-end HTTP tests for /v1/reports.

Approach:
- Override `get_session` to yield the per-test AsyncSession used by other tests
  (matches schema setup; rolls back via the conftest TRUNCATE).
- Override `s3_client` to yield a FakeS3 instance from test_reports_storage.
- Use httpx.AsyncClient with ASGITransport against the FastAPI app.

Lifespan is bypassed (we don't want the APScheduler started during tests),
which httpx ASGITransport supports by default unless lifespan="on" is passed.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import bcrypt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.db.session import get_session
from app.main import app
from app.reports.storage import s3_client
from tests.test_reports_storage import FakeS3


# --- Fixtures ------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_s3() -> FakeS3:
    return FakeS3()


@pytest_asyncio.fixture
async def client(
    session: AsyncSession, fake_s3: FakeS3
) -> AsyncIterator[AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _override_s3() -> AsyncIterator[FakeS3]:
        yield fake_s3

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[s3_client] = _override_s3
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def customer_and_key(session: AsyncSession) -> tuple[Customer, str]:
    raw = "mvk_live_" + "x" * 43
    key_hash = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=4)).decode()
    c = Customer(
        name="Acme",
        allowlist_emails=["ops@acme.com"],
        api_key_hash=key_hash,
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c, raw


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _body(customer_id, **overrides) -> dict:
    body = {
        "customer_id": str(customer_id),
        "title": "Q3 earnings",
        "description": "",
        "tags": ["q3"],
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "html": "<h1>hi</h1>",
        "supplementary_files": [],
    }
    body.update(overrides)
    return body


# --- POST /v1/reports ----------------------------------------------------


@pytest.mark.asyncio
async def test_post_publish_returns_201_with_url(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    body = _body(customer.id)
    r = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=body,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["url"].endswith(f"/r/{data['report_id']}")
    assert "created_at" in data


@pytest.mark.asyncio
async def test_post_replay_returns_200_same_body(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    idem = str(uuid4())
    body = _body(customer.id)
    first = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": idem},
        json=body,
    )
    second = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": idem},
        json=body,
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["report_id"] == second.json()["report_id"]


@pytest.mark.asyncio
async def test_post_requires_idempotency_key(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    r = await client.post(
        "/v1/reports", headers=_auth(key), json=_body(customer.id)
    )
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"].endswith("idempotency-key-missing")


@pytest.mark.asyncio
async def test_post_rejects_non_uuid_idempotency_key(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    r = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": "not-a-uuid"},
        json=_body(customer.id),
    )
    assert r.status_code == 422
    assert r.json()["type"].endswith("idempotency-key-invalid")


@pytest.mark.asyncio
async def test_post_401_without_bearer(client: AsyncClient, customer_and_key) -> None:
    customer, _ = customer_and_key
    r = await client.post(
        "/v1/reports",
        headers={"Idempotency-Key": str(uuid4())},
        json=_body(customer.id),
    )
    assert r.status_code == 401
    assert r.json()["type"].endswith("invalid-api-key")


@pytest.mark.asyncio
async def test_post_403_when_customer_id_mismatches(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    r = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=_body(uuid4()),  # different customer
    )
    assert r.status_code == 403
    assert r.json()["type"].endswith("customer-not-authorized")


@pytest.mark.asyncio
async def test_post_with_supplementary_files(
    client: AsyncClient, customer_and_key, fake_s3: FakeS3
) -> None:
    customer, key = customer_and_key
    body = _body(
        customer.id,
        supplementary_files=[
            {
                "filename": "data.csv",
                "content_type": "text/csv",
                "base64": base64.b64encode(b"a,b,c").decode(),
            }
        ],
    )
    r = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=body,
    )
    assert r.status_code == 201
    rid = r.json()["report_id"]
    assert f"{customer.id}/{rid}/supplementary/data.csv" in fake_s3.store


# --- GET /v1/reports -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_list_returns_customer_reports(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    # Publish 2 reports
    for title in ["alpha", "beta"]:
        await client.post(
            "/v1/reports",
            headers={**_auth(key), "Idempotency-Key": str(uuid4())},
            json=_body(customer.id, title=title),
        )

    r = await client.get("/v1/reports", headers=_auth(key))
    assert r.status_code == 200
    items = r.json()["items"]
    assert sorted(i["title"] for i in items) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_get_list_with_search(client: AsyncClient, customer_and_key) -> None:
    customer, key = customer_and_key
    await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=_body(customer.id, title="Quarterly earnings"),
    )
    await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=_body(customer.id, title="Pipeline review"),
    )
    r = await client.get("/v1/reports?search=earnings", headers=_auth(key))
    assert r.status_code == 200
    titles = [i["title"] for i in r.json()["items"]]
    assert titles == ["Quarterly earnings"]


@pytest.mark.asyncio
async def test_get_list_pagination(client: AsyncClient, customer_and_key) -> None:
    customer, key = customer_and_key
    for i in range(3):
        await client.post(
            "/v1/reports",
            headers={**_auth(key), "Idempotency-Key": str(uuid4())},
            json=_body(customer.id, title=f"t{i}"),
        )
    p1 = (await client.get("/v1/reports?limit=2", headers=_auth(key))).json()
    assert p1["has_more"] is True
    assert len(p1["items"]) == 2
    p2 = (
        await client.get(
            f"/v1/reports?limit=2&cursor={p1['next_cursor']}", headers=_auth(key)
        )
    ).json()
    assert p2["has_more"] is False
    assert len(p2["items"]) == 1


# --- GET /v1/reports/{id} ------------------------------------------------


@pytest.mark.asyncio
async def test_get_one_returns_html_and_etag(
    client: AsyncClient, customer_and_key
) -> None:
    customer, key = customer_and_key
    pub = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=_body(customer.id, html="<p>body</p>"),
    )
    rid = pub.json()["report_id"]
    r = await client.get(f"/v1/reports/{rid}", headers=_auth(key))
    assert r.status_code == 200
    assert r.json()["html"] == "<p>body</p>"
    etag = r.headers["etag"]
    assert etag

    # If-None-Match → 304 with no body
    r304 = await client.get(
        f"/v1/reports/{rid}",
        headers={**_auth(key), "If-None-Match": etag},
    )
    assert r304.status_code == 304
    assert r304.content == b""
    assert r304.headers["etag"] == etag


@pytest.mark.asyncio
async def test_get_one_404_when_not_owned(
    client: AsyncClient, customer_and_key, session: AsyncSession
) -> None:
    customer, key = customer_and_key
    # Create another customer and publish a report under it directly.
    other = Customer(
        name="Other",
        allowlist_emails=["x@y.com"],
        api_key_hash=bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(other)
    await session.commit()
    await session.refresh(other)

    other_raw = "mvk_live_" + "y" * 43
    other.api_key_hash = bcrypt.hashpw(
        other_raw.encode(), bcrypt.gensalt(rounds=4)
    ).decode()
    await session.commit()

    pub = await client.post(
        "/v1/reports",
        headers=_auth(other_raw) | {"Idempotency-Key": str(uuid4())},
        json=_body(other.id),
    )
    other_rid = pub.json()["report_id"]

    # Try to fetch as customer (different key)
    r = await client.get(f"/v1/reports/{other_rid}", headers=_auth(key))
    assert r.status_code == 404
    assert r.json()["type"].endswith("report-not-found")


# --- DELETE --------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_204_then_404(
    client: AsyncClient, customer_and_key, fake_s3: FakeS3
) -> None:
    customer, key = customer_and_key
    pub = await client.post(
        "/v1/reports",
        headers={**_auth(key), "Idempotency-Key": str(uuid4())},
        json=_body(customer.id),
    )
    rid = pub.json()["report_id"]
    d = await client.delete(f"/v1/reports/{rid}", headers=_auth(key))
    assert d.status_code == 204
    assert f"{customer.id}/{rid}/index.html" not in fake_s3.store
    g = await client.get(f"/v1/reports/{rid}", headers=_auth(key))
    assert g.status_code == 404
