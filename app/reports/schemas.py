"""Pydantic request/response schemas for /v1/reports (PLAN.md §4.1-4.3)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings


MAX_TITLE = 500
MAX_DESCRIPTION = 5000
MAX_TAGS = 50
MAX_TAG_LEN = 64
MAX_SUPPLEMENTARY_FILES = 20
MAX_FILENAME = 255


class SupplementaryFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., min_length=1, max_length=MAX_FILENAME)
    content_type: str = Field(..., min_length=1, max_length=255)
    base64: str = Field(..., min_length=1)


class PublishRequest(BaseModel):
    """POST /v1/reports body (PLAN.md §4.1)."""

    model_config = ConfigDict(extra="forbid")

    customer_id: UUID
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    generated_at: datetime
    html: str = Field(..., min_length=1)
    supplementary_files: list[SupplementaryFileIn] = Field(
        default_factory=list, max_length=MAX_SUPPLEMENTARY_FILES
    )

    @field_validator("tags")
    @classmethod
    def _validate_tag_lengths(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not tag or len(tag) > MAX_TAG_LEN:
                raise ValueError(
                    f"tag must be 1..{MAX_TAG_LEN} chars, got {len(tag)}"
                )
        return v

    @field_validator("html")
    @classmethod
    def _validate_html_size(cls, v: str) -> str:
        # Byte size, not char count — multibyte content must respect MAX_HTML_BYTES.
        size = len(v.encode("utf-8"))
        if size > settings.MAX_HTML_BYTES:
            raise ValueError(
                f"html exceeds {settings.MAX_HTML_BYTES} bytes (got {size})"
            )
        return v


class ReportResponse(BaseModel):
    """POST/GET-one response shape (PLAN.md §4.1, §4.3)."""

    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    url: str
    created_at: datetime


class ReportListItem(BaseModel):
    """GET list response item (PLAN.md §4.2)."""

    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    title: str
    description: str
    tags: list[str]
    generated_at: datetime
    url: str
    size_bytes: int


class ReportListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReportListItem]
    next_cursor: str | None = None
    has_more: bool


class ReportDetail(ReportListItem):
    """GET-one response (list item + html)."""

    html: str
