"""S3 object storage for report HTML + supplementary files (PLAN.md §4.1, §4.8).

Layout under HUB_S3_BUCKET:
    <customer_id>/<report_id>/index.html
    <customer_id>/<report_id>/supplementary/<filename>

Single PUT per object — HTML cap is 10MB, supplementary cap is 100MB per file,
both well under the 5GB single-PUT ceiling. Multipart is deferred until limits
move.

Tests inject a fake client matching the `S3ClientProtocol` shape — keeps unit
tests off real AWS / moto.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import UUID

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.errors import storage_unavailable


logger = logging.getLogger("hub.storage")


class S3ClientProtocol(Protocol):
    """Subset of aioboto3 S3 client we depend on. Lets tests inject fakes."""

    async def put_object(self, **kwargs: Any) -> dict[str, Any]: ...
    async def get_object(self, **kwargs: Any) -> dict[str, Any]: ...
    async def delete_objects(self, **kwargs: Any) -> dict[str, Any]: ...
    async def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...


def html_key(customer_id: UUID, report_id: UUID) -> str:
    return f"{customer_id}/{report_id}/index.html"


def supplementary_key(customer_id: UUID, report_id: UUID, filename: str) -> str:
    return f"{customer_id}/{report_id}/supplementary/{filename}"


def report_prefix(customer_id: UUID, report_id: UUID) -> str:
    return f"{customer_id}/{report_id}/"


def _client_session() -> aioboto3.Session:
    return aioboto3.Session(
        aws_access_key_id=(
            settings.AWS_ACCESS_KEY_ID.get_secret_value()
            if settings.AWS_ACCESS_KEY_ID
            else None
        ),
        aws_secret_access_key=(
            settings.AWS_SECRET_ACCESS_KEY.get_secret_value()
            if settings.AWS_SECRET_ACCESS_KEY
            else None
        ),
        region_name=settings.AWS_REGION,
    )


async def s3_client() -> AsyncIterator[S3ClientProtocol]:
    """FastAPI-style dep yielding a live aioboto3 S3 client. Overridable in tests."""
    session = _client_session()
    async with session.client("s3", endpoint_url=settings.AWS_S3_ENDPOINT_URL) as c:
        yield c  # type: ignore[misc]


async def put_html(
    client: S3ClientProtocol,
    *,
    customer_id: UUID,
    report_id: UUID,
    html: str,
) -> str:
    """Upload index.html and return the S3 key."""
    key = html_key(customer_id, report_id)
    try:
        await client.put_object(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("put_html failed: key=%s err=%s", key, exc)
        raise storage_unavailable() from exc
    return key


async def put_supplementary(
    client: S3ClientProtocol,
    *,
    customer_id: UUID,
    report_id: UUID,
    filename: str,
    content_type: str,
    body: bytes,
) -> str:
    if len(body) > settings.MAX_SUPPLEMENTARY_BYTES:
        from app.errors import payload_too_large

        raise payload_too_large(
            f"supplementary file '{filename}' exceeds "
            f"{settings.MAX_SUPPLEMENTARY_BYTES} bytes"
        )
    key = supplementary_key(customer_id, report_id, filename)
    try:
        await client.put_object(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("put_supplementary failed: key=%s err=%s", key, exc)
        raise storage_unavailable() from exc
    return key


async def get_html(
    client: S3ClientProtocol,
    *,
    customer_id: UUID,
    report_id: UUID,
) -> bytes:
    key = html_key(customer_id, report_id)
    try:
        resp = await client.get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        body = resp["Body"]
        # aioboto3 StreamingBody.read returns bytes
        return await body.read()
    except (BotoCoreError, ClientError) as exc:
        logger.warning("get_html failed: key=%s err=%s", key, exc)
        raise storage_unavailable() from exc


async def delete_report_objects(
    client: S3ClientProtocol,
    *,
    customer_id: UUID,
    report_id: UUID,
) -> int:
    """Delete every object under the report prefix. Returns count deleted.

    Per PLAN.md §4.4, S3 delete failure is logged but not raised — callers
    treat it as "orphan, cleaned later." Returning -1 signals partial/no-op.
    """
    prefix = report_prefix(customer_id, report_id)
    try:
        listing = await client.list_objects_v2(
            Bucket=settings.AWS_S3_BUCKET, Prefix=prefix
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("delete_report_objects list failed: prefix=%s err=%s", prefix, exc)
        return -1
    keys = [{"Key": obj["Key"]} for obj in listing.get("Contents", [])]
    if not keys:
        return 0
    try:
        await client.delete_objects(
            Bucket=settings.AWS_S3_BUCKET,
            Delete={"Objects": keys, "Quiet": True},
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning(
            "delete_report_objects delete failed: prefix=%s err=%s", prefix, exc
        )
        return -1
    return len(keys)
