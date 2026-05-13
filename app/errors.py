"""RFC 7807 problem-detail responses (PLAN.md §4.9).

Single source of truth for the registered error slugs. Helpers raise
HubError, which the FastAPI exception handler in main.py turns into a
Content-Type: application/problem+json response.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings


_ERRORS_BASE = "https://{domain}/errors/"


class HubError(Exception):
    """RFC 7807 problem-detail. Carries everything the handler needs."""

    def __init__(
        self,
        *,
        slug: str,
        status: int,
        title: str,
        detail: str,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.slug = slug
        self.status = status
        self.title = title
        self.detail = detail
        self.extensions = extensions or {}

    def to_dict(self, instance: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": _ERRORS_BASE.format(domain=settings.HUB_PRIMARY_DOMAIN) + self.slug,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance is not None:
            body["instance"] = instance
        body.update(self.extensions)
        return body


def invalid_api_key() -> HubError:
    return HubError(
        slug="invalid-api-key",
        status=401,
        title="Invalid API key",
        detail="The provided API key is missing, malformed, or unrecognized.",
    )


def customer_not_authorized() -> HubError:
    return HubError(
        slug="customer-not-authorized",
        status=403,
        title="Customer not authorized",
        detail="The authenticated customer is not permitted to perform this action.",
    )


def report_not_found() -> HubError:
    return HubError(
        slug="report-not-found",
        status=404,
        title="Report not found",
        detail="No report exists with the given identifier, or it is not visible to you.",
    )


def magic_link_expired_or_consumed() -> HubError:
    return HubError(
        slug="magic-link-expired-or-consumed",
        status=410,
        title="Magic link no longer valid",
        detail="This link has expired or has already been used. Request a new one.",
    )


def iframe_token_invalid() -> HubError:
    return HubError(
        slug="iframe-token-invalid",
        status=401,
        title="Iframe token invalid",
        detail="The iframe content token is missing, expired, or signed with an unknown key.",
    )


def validation_failed(detail: str, extensions: dict[str, Any] | None = None) -> HubError:
    return HubError(
        slug="validation-failed",
        status=422,
        title="Validation failed",
        detail=detail,
        extensions=extensions,
    )


def payload_too_large(detail: str) -> HubError:
    return HubError(
        slug="payload-too-large",
        status=422,
        title="Payload too large",
        detail=detail,
    )


def storage_unavailable() -> HubError:
    return HubError(
        slug="storage-unavailable",
        status=503,
        title="Storage temporarily unavailable",
        detail="Object storage is unavailable. Retry with the same Idempotency-Key.",
    )


def idempotency_key_missing() -> HubError:
    return HubError(
        slug="idempotency-key-missing",
        status=422,
        title="Idempotency-Key header required",
        detail="POST /v1/reports requires an Idempotency-Key header containing a UUID.",
    )


def idempotency_key_invalid() -> HubError:
    return HubError(
        slug="idempotency-key-invalid",
        status=422,
        title="Idempotency-Key must be a UUID",
        detail="The Idempotency-Key header must be a valid UUID (RFC 4122).",
    )


def rate_limit_exceeded(retry_after_seconds: int) -> HubError:
    return HubError(
        slug="rate-limit-exceeded",
        status=429,
        title="Rate limit exceeded",
        detail="Too many requests. Please slow down.",
        extensions={"retry_after_seconds": retry_after_seconds},
    )


async def hub_error_handler(request: Request, exc: HubError) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status,
        content=exc.to_dict(instance=request.url.path),
        media_type="application/problem+json",
    )
    if exc.slug == "rate-limit-exceeded":
        retry = exc.extensions.get("retry_after_seconds")
        if isinstance(retry, int):
            response.headers["Retry-After"] = str(retry)
    return response
