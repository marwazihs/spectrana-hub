"""Pydantic schemas for /v1/reports."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import settings
from app.reports.schemas import (
    MAX_DESCRIPTION,
    MAX_SUPPLEMENTARY_FILES,
    MAX_TAGS,
    MAX_TAG_LEN,
    MAX_TITLE,
    PublishRequest,
    SupplementaryFileIn,
)


def _valid_body(**overrides) -> dict:
    body = {
        "customer_id": str(uuid4()),
        "title": "Q3 earnings",
        "description": "Auto-generated from Spectra run",
        "tags": ["earnings", "q3"],
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "html": "<html><body>hi</body></html>",
        "supplementary_files": [],
    }
    body.update(overrides)
    return body


def test_publish_request_accepts_canonical_payload() -> None:
    req = PublishRequest.model_validate(_valid_body())
    assert req.title == "Q3 earnings"
    assert req.tags == ["earnings", "q3"]


def test_publish_request_rejects_oversize_html() -> None:
    big = "a" * (settings.MAX_HTML_BYTES + 1)
    with pytest.raises(ValidationError, match="html exceeds"):
        PublishRequest.model_validate(_valid_body(html=big))


def test_publish_request_rejects_empty_html() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(html=""))


def test_publish_request_rejects_oversize_title() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(title="x" * (MAX_TITLE + 1)))


def test_publish_request_rejects_oversize_description() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(
            _valid_body(description="x" * (MAX_DESCRIPTION + 1))
        )


def test_publish_request_rejects_too_many_tags() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(tags=["t"] * (MAX_TAGS + 1)))


def test_publish_request_rejects_long_tag() -> None:
    with pytest.raises(ValidationError, match="tag must be"):
        PublishRequest.model_validate(_valid_body(tags=["x" * (MAX_TAG_LEN + 1)]))


def test_publish_request_rejects_empty_tag() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(tags=[""]))


def test_publish_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(unknown_field="hi"))


def test_publish_request_rejects_too_many_supplementary_files() -> None:
    files = [
        {
            "filename": f"f{i}.txt",
            "content_type": "text/plain",
            "base64": base64.b64encode(b"hello").decode(),
        }
        for i in range(MAX_SUPPLEMENTARY_FILES + 1)
    ]
    with pytest.raises(ValidationError):
        PublishRequest.model_validate(_valid_body(supplementary_files=files))


def test_publish_request_defaults_empty_description_and_tags() -> None:
    body = _valid_body()
    body.pop("description")
    body.pop("tags")
    req = PublishRequest.model_validate(body)
    assert req.description == ""
    assert req.tags == []


def test_supplementary_file_rejects_extras() -> None:
    with pytest.raises(ValidationError):
        SupplementaryFileIn.model_validate(
            {
                "filename": "f.txt",
                "content_type": "text/plain",
                "base64": base64.b64encode(b"hi").decode(),
                "extra": "no",
            }
        )


def test_publish_request_html_byte_size_uses_utf8() -> None:
    """Title is char-bounded; html cap is byte-based (multibyte counts more)."""
    # A 2-byte char × (MAX/2 + 1) exceeds MAX_HTML_BYTES even though char count
    # is half.
    multibyte = "é" * (settings.MAX_HTML_BYTES // 2 + 1)
    with pytest.raises(ValidationError, match="html exceeds"):
        PublishRequest.model_validate(_valid_body(html=multibyte))
