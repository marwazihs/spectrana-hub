"""RFC 7807 error helpers (app.errors)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.errors import (
    HubError,
    customer_not_authorized,
    hub_error_handler,
    iframe_token_invalid,
    invalid_api_key,
    magic_link_expired_or_consumed,
    rate_limit_exceeded,
    report_not_found,
)


def test_invalid_api_key_shape() -> None:
    err = invalid_api_key()
    body = err.to_dict(instance="/v1/reports")
    assert body["status"] == 401
    assert body["type"].endswith("/errors/invalid-api-key")
    assert body["type"].startswith(f"https://{settings.HUB_PRIMARY_DOMAIN}/")
    assert body["instance"] == "/v1/reports"
    assert "title" in body and "detail" in body


def test_all_registered_slugs_have_correct_status() -> None:
    cases = [
        (invalid_api_key(), 401, "invalid-api-key"),
        (customer_not_authorized(), 403, "customer-not-authorized"),
        (report_not_found(), 404, "report-not-found"),
        (magic_link_expired_or_consumed(), 410, "magic-link-expired-or-consumed"),
        (iframe_token_invalid(), 401, "iframe-token-invalid"),
        (rate_limit_exceeded(30), 429, "rate-limit-exceeded"),
    ]
    for err, status, slug in cases:
        assert err.status == status
        assert err.slug == slug
        body = err.to_dict()
        assert body["status"] == status
        assert slug in body["type"]


def test_rate_limit_includes_retry_after_extension() -> None:
    err = rate_limit_exceeded(retry_after_seconds=42)
    body = err.to_dict()
    assert body["retry_after_seconds"] == 42


@pytest.mark.asyncio
async def test_handler_returns_problem_json_media_type() -> None:
    app = FastAPI()
    app.add_exception_handler(HubError, hub_error_handler)  # type: ignore[arg-type]

    @app.get("/boom")
    async def _boom() -> None:
        raise invalid_api_key()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/boom")

    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 401
    assert body["instance"] == "/boom"


@pytest.mark.asyncio
async def test_rate_limit_sets_retry_after_header() -> None:
    app = FastAPI()
    app.add_exception_handler(HubError, hub_error_handler)  # type: ignore[arg-type]

    @app.get("/burst")
    async def _burst() -> None:
        raise rate_limit_exceeded(retry_after_seconds=15)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/burst")

    assert r.status_code == 429
    assert r.headers["retry-after"] == "15"
