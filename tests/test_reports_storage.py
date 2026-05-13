"""S3 storage service: PUT/GET/DELETE via injected fake client."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from app.config import settings
from app.errors import HubError
from app.reports.storage import (
    delete_report_objects,
    get_html,
    html_key,
    put_html,
    put_supplementary,
    report_prefix,
    supplementary_key,
)


class FakeS3:
    """In-memory S3 surface — bucket → key → {Body, ContentType}."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        if "put_object" in self.fail_on:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "down"}},
                "PutObject",
            )
        self.store[kwargs["Key"]] = {
            "Body": kwargs["Body"],
            "ContentType": kwargs.get("ContentType"),
        }
        return {}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        if "get_object" in self.fail_on:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
            )
        item = self.store[kwargs["Key"]]

        class _Body:
            def __init__(self, data: bytes) -> None:
                self.data = data

            async def read(self) -> bytes:
                return self.data

        return {"Body": _Body(item["Body"])}

    async def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        if "list_objects_v2" in self.fail_on:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "down"}},
                "ListObjectsV2",
            )
        prefix = kwargs["Prefix"]
        contents = [{"Key": k} for k in self.store if k.startswith(prefix)]
        return {"Contents": contents}

    async def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_objects", kwargs))
        if "delete_objects" in self.fail_on:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "down"}},
                "DeleteObjects",
            )
        for obj in kwargs["Delete"]["Objects"]:
            self.store.pop(obj["Key"], None)
        return {}


def _ids() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()


def test_key_helpers_match_plan_layout() -> None:
    c, r = _ids()
    assert html_key(c, r) == f"{c}/{r}/index.html"
    assert supplementary_key(c, r, "data.csv") == f"{c}/{r}/supplementary/data.csv"
    assert report_prefix(c, r) == f"{c}/{r}/"


async def test_put_html_writes_utf8_text_html() -> None:
    s3 = FakeS3()
    c, r = _ids()
    key = await put_html(s3, customer_id=c, report_id=r, html="<h1>hi</h1>")
    assert key == html_key(c, r)
    stored = s3.store[key]
    assert stored["Body"] == b"<h1>hi</h1>"
    assert stored["ContentType"] == "text/html; charset=utf-8"


async def test_put_html_503_on_s3_failure() -> None:
    s3 = FakeS3(fail_on={"put_object"})
    c, r = _ids()
    with pytest.raises(HubError) as exc:
        await put_html(s3, customer_id=c, report_id=r, html="x")
    assert exc.value.status == 503
    assert exc.value.slug == "storage-unavailable"


async def test_put_supplementary_writes_and_rejects_oversize() -> None:
    s3 = FakeS3()
    c, r = _ids()
    key = await put_supplementary(
        s3,
        customer_id=c,
        report_id=r,
        filename="data.csv",
        content_type="text/csv",
        body=b"a,b,c",
    )
    assert key == supplementary_key(c, r, "data.csv")
    assert s3.store[key]["ContentType"] == "text/csv"

    too_big = b"x" * (settings.MAX_SUPPLEMENTARY_BYTES + 1)
    with pytest.raises(HubError) as exc:
        await put_supplementary(
            s3,
            customer_id=c,
            report_id=r,
            filename="big.bin",
            content_type="application/octet-stream",
            body=too_big,
        )
    assert exc.value.slug == "payload-too-large"


async def test_get_html_returns_bytes() -> None:
    s3 = FakeS3()
    c, r = _ids()
    await put_html(s3, customer_id=c, report_id=r, html="<p>hello</p>")
    body = await get_html(s3, customer_id=c, report_id=r)
    assert body == b"<p>hello</p>"


async def test_get_html_503_on_s3_failure() -> None:
    s3 = FakeS3(fail_on={"get_object"})
    c, r = _ids()
    with pytest.raises(HubError) as exc:
        await get_html(s3, customer_id=c, report_id=r)
    assert exc.value.status == 503


async def test_delete_report_objects_clears_prefix() -> None:
    s3 = FakeS3()
    c, r = _ids()
    await put_html(s3, customer_id=c, report_id=r, html="x")
    await put_supplementary(
        s3,
        customer_id=c,
        report_id=r,
        filename="a.txt",
        content_type="text/plain",
        body=b"hi",
    )
    # Drop in an unrelated key to confirm prefix scoping.
    other_c, other_r = _ids()
    await put_html(s3, customer_id=other_c, report_id=other_r, html="other")

    n = await delete_report_objects(s3, customer_id=c, report_id=r)
    assert n == 2
    assert html_key(other_c, other_r) in s3.store
    assert html_key(c, r) not in s3.store


async def test_delete_report_objects_returns_zero_when_no_keys() -> None:
    s3 = FakeS3()
    c, r = _ids()
    n = await delete_report_objects(s3, customer_id=c, report_id=r)
    assert n == 0


async def test_delete_report_objects_swallows_s3_errors_returning_minus_one() -> None:
    s3 = FakeS3(fail_on={"list_objects_v2"})
    c, r = _ids()
    n = await delete_report_objects(s3, customer_id=c, report_id=r)
    assert n == -1
