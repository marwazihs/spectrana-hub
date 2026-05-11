"""Iframe content origin: GET /render/{id}?t=<jwt> (PLAN.md §4.8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.auth.service import mint_iframe_jwt
from app.config import settings
from app.db.session import get_session
from app.main import app
from app.reports.html_injector import SHIM_MARKER, RESIZE_MARKER
from app.reports.models import Report
from app.reports.storage import s3_client
from tests.test_reports_storage import FakeS3


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _iframe_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "HUB_IFRAME_JWT_SECRET",
        SecretStr("test-iframe-secret-padded-to-at-least-32-bytes-cccccccc"),
    )
    monkeypatch.setattr(settings, "HUB_IFRAME_JWT_SECRET_PREVIOUS", None)
    # M8 Host-header guard: /render only responds when the request arrives on
    # the reports origin. AsyncClient sets Host from base_url ("reports.test"
    # below), so configure the setting to match.
    monkeypatch.setattr(settings, "HUB_REPORTS_DOMAIN", "reports.test")


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
        async with AsyncClient(transport=transport, base_url="http://reports.test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


async def _seed_customer_and_report(
    session: AsyncSession, fake_s3: FakeS3, *, html: bytes
) -> tuple[Customer, UUID]:
    from uuid_utils import uuid7

    c = Customer(
        name="Acme",
        allowlist_emails=["ops@acme.com"],
        api_key_hash=bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode(),
        api_key_prefix="mvk_live",
    )
    session.add(c)
    await session.flush()
    await session.refresh(c)
    rid = UUID(str(uuid7()))
    r = Report(
        id=rid,
        customer_id=c.id,
        title="t",
        description="",
        tags=[],
        generated_at=datetime.now(tz=UTC),
        s3_key=f"{c.id}/{rid}/index.html",
        supplementary_files=[],
        size_bytes=len(html),
    )
    session.add(r)
    await session.commit()
    fake_s3.store[f"{c.id}/{rid}/index.html"] = {
        "Body": html,
        "ContentType": "text/html",
    }
    return c, rid


# --- Happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_render_returns_injected_html_with_security_headers(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    c, rid = await _seed_customer_and_report(
        session,
        fake_s3,
        html=b"<html><head></head><body><h1>hi</h1></body></html>",
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)

    r = await client.get(f"/render/{rid}", params={"t": token})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert SHIM_MARKER in r.content
    assert RESIZE_MARKER in r.content

    # Security headers per §4.8
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    # frame-ancestors authorizes the viewer origin (HUB_PRIMARY_DOMAIN), not
    # 'self' — the viewer and iframe live on different origins by design.
    assert f"frame-ancestors {settings.HUB_PRIMARY_DOMAIN}" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "img-src data: https:" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-cache, must-revalidate"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["etag"].startswith('"') and r.headers["etag"].endswith('"')
    assert "v2" in r.headers["etag"]


# --- ETag / 304 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_render_returns_304_when_if_none_match_matches(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body><p>x</p></body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    first = await client.get(f"/render/{rid}", params={"t": token})
    etag = first.headers["etag"]

    second = await client.get(
        f"/render/{rid}", params={"t": token}, headers={"If-None-Match": etag}
    )
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


@pytest.mark.asyncio
async def test_render_etag_changes_when_html_changes(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body><p>v1</p></body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    first = await client.get(f"/render/{rid}", params={"t": token})

    # Spectra overwrites the same S3 object (PLAN.md §4.8 rewrite rationale)
    fake_s3.store[f"{c.id}/{rid}/index.html"]["Body"] = (
        b"<html><body><p>v2</p></body></html>"
    )
    second = await client.get(f"/render/{rid}", params={"t": token})

    assert first.headers["etag"] != second.headers["etag"]


# --- Auth failure modes --------------------------------------------------


@pytest.mark.asyncio
async def test_render_401_when_jwt_missing(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    _, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    r = await client.get(f"/render/{rid}")
    # FastAPI returns 422 for a missing required query param; that's its own
    # validation, not our iframe-token-invalid contract. Our contract kicks in
    # for present-but-bad tokens (next test).
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_render_401_when_jwt_signed_with_wrong_secret(
    client: AsyncClient,
    session: AsyncSession,
    fake_s3: FakeS3,
) -> None:
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    bogus = jwt.encode(
        {
            "report_id": str(rid),
            "customer_id": str(c.id),
            "iss": settings.HUB_PRIMARY_DOMAIN,
            "iat": int(datetime.now(tz=UTC).timestamp()),
            "exp": int((datetime.now(tz=UTC) + timedelta(seconds=60)).timestamp()),
        },
        "wrong-secret-padded-to-at-least-32-bytes-xxxxxxxxx",
        algorithm="HS256",
    )
    r = await client.get(f"/render/{rid}", params={"t": bogus})
    assert r.status_code == 401
    assert r.json()["type"].endswith("iframe-token-invalid")


@pytest.mark.asyncio
async def test_render_401_when_jwt_for_different_report(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    other_token = mint_iframe_jwt(report_id=uuid4(), customer_id=c.id)
    r = await client.get(f"/render/{rid}", params={"t": other_token})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_render_401_when_jwt_for_different_customer(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    _, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    # Mint a JWT whose customer_id doesn't own this report.
    bad_token = mint_iframe_jwt(report_id=rid, customer_id=uuid4())
    r = await client.get(f"/render/{rid}", params={"t": bad_token})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_render_401_when_jwt_expired(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    now = datetime.now(tz=UTC)
    expired = jwt.encode(
        {
            "report_id": str(rid),
            "customer_id": str(c.id),
            "iss": settings.HUB_PRIMARY_DOMAIN,
            "iat": int((now - timedelta(seconds=120)).timestamp()),
            "exp": int((now - timedelta(seconds=60)).timestamp()),
        },
        settings.HUB_IFRAME_JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )
    r = await client.get(f"/render/{rid}", params={"t": expired})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_render_previous_secret_still_verifies_during_rotation_overlap(
    client: AsyncClient,
    session: AsyncSession,
    fake_s3: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JWT signed with the about-to-rotate _PREVIOUS secret still works."""
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    old_secret = settings.HUB_IFRAME_JWT_SECRET.get_secret_value()
    # Issue a token under the OLD secret, then "rotate": new primary, old as previous.
    old_token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    monkeypatch.setattr(
        settings,
        "HUB_IFRAME_JWT_SECRET",
        SecretStr("rotated-new-iframe-secret-padded-to-32-bytes-ddddddd"),
    )
    monkeypatch.setattr(
        settings, "HUB_IFRAME_JWT_SECRET_PREVIOUS", SecretStr(old_secret)
    )
    r = await client.get(f"/render/{rid}", params={"t": old_token})
    assert r.status_code == 200


# --- Host-header guard (M8) ---------------------------------------------


@pytest.mark.asyncio
async def test_render_404_when_host_does_not_match_reports_domain(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    """A request that arrives on HUB_PRIMARY_DOMAIN (or any host other than
    HUB_REPORTS_DOMAIN) must look like the route does not exist. Anti-enum
    posture: same shape as report-not-found."""
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    r = await client.get(
        f"/render/{rid}",
        params={"t": token},
        headers={"Host": "hub.test"},  # primary domain, not reports
    )
    assert r.status_code == 404
    payload = r.json()
    assert "report-not-found" in payload.get("type", payload.get("slug", ""))


@pytest.mark.asyncio
async def test_render_accepts_x_forwarded_host_for_reports_domain(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    """Behind a TLS-terminating LB, Host is the internal name and the public
    hostname lands in X-Forwarded-Host. The guard must prefer XFH when set."""
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    r = await client.get(
        f"/render/{rid}",
        params={"t": token},
        headers={
            "Host": "hub-internal-name",
            "X-Forwarded-Host": "reports.test",
        },
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_render_404_when_x_forwarded_host_does_not_match(
    client: AsyncClient, session: AsyncSession, fake_s3: FakeS3
) -> None:
    """XFH is preferred when present. If it says hub.<domain>, reject even
    if the inner Host header would have matched."""
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    r = await client.get(
        f"/render/{rid}",
        params={"t": token},
        headers={
            "Host": "reports.test",
            "X-Forwarded-Host": "hub.test",
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_render_404_when_reports_domain_unconfigured(
    client: AsyncClient,
    session: AsyncSession,
    fake_s3: FakeS3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: an empty HUB_REPORTS_DOMAIN must reject every request,
    not match every request (an empty-string compare would otherwise be
    satisfied by any missing Host header)."""
    monkeypatch.setattr(settings, "HUB_REPORTS_DOMAIN", "")
    c, rid = await _seed_customer_and_report(
        session, fake_s3, html=b"<html><body>x</body></html>"
    )
    token = mint_iframe_jwt(report_id=rid, customer_id=c.id)
    r = await client.get(f"/render/{rid}", params={"t": token})
    assert r.status_code == 404
