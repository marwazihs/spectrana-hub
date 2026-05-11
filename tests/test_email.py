"""Email render + send (app.auth.email)."""

from __future__ import annotations

from email.message import EmailMessage
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.auth.email import (
    _build_message,
    build_magic_link,
    render_magic_link_email,
    send_magic_link_email,
)
from app.config import settings


def test_build_magic_link_shape() -> None:
    rid = uuid4()
    token = "tokenvalue123"
    link = build_magic_link(rid, token)
    assert link == f"https://{settings.HUB_PRIMARY_DOMAIN}/r/{rid}?token={token}"


def test_render_includes_link_and_brand() -> None:
    rid = uuid4()
    rendered = render_magic_link_email(report_id=rid, raw_token="abc123", brand="Majeve")
    assert "Majeve" in rendered.subject
    assert "Majeve" in rendered.text
    assert "Majeve" in rendered.html
    assert f"/r/{rid}?token=abc123" in rendered.text
    assert f"/r/{rid}?token=abc123" in rendered.html


def test_render_uses_settings_defaults() -> None:
    rendered = render_magic_link_email(report_id=uuid4(), raw_token="t")
    assert settings.HUB_EMAIL_BRAND_NAME in rendered.subject
    assert str(settings.MAGIC_LINK_TTL_MINUTES) in rendered.text


def test_render_html_escapes_token() -> None:
    # Tokens are token_urlsafe so won't contain HTML, but the rendering path
    # must still autoescape — guard against future changes that pass user data.
    rendered = render_magic_link_email(report_id=uuid4(), raw_token="<script>")
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


def test_build_message_multipart_alternative() -> None:
    rendered = render_magic_link_email(report_id=uuid4(), raw_token="t")
    msg = _build_message(to_email="ops@acme.com", rendered=rendered)
    assert isinstance(msg, EmailMessage)
    assert msg["To"] == "ops@acme.com"
    assert msg["From"] == settings.HUB_EMAIL_FROM
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    subtypes = {p.get_content_subtype() for p in parts}
    assert subtypes == {"plain", "html"}


async def test_send_dispatches_to_aiosmtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test.example")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "user")
    monkeypatch.setattr(settings, "SMTP_PASS", SecretStr("pw"))
    monkeypatch.setattr(settings, "SMTP_STARTTLS", True)

    captured: dict[str, object] = {}

    async def _fake_send(msg: EmailMessage, **kwargs: object) -> tuple[dict[str, object], str]:
        captured["msg"] = msg
        captured["kwargs"] = kwargs
        return ({}, "ok")

    monkeypatch.setattr("app.auth.email.aiosmtplib.send", _fake_send)

    rid = uuid4()
    await send_magic_link_email(to_email="ops@acme.com", report_id=rid, raw_token="rawtok")

    msg = captured["msg"]
    assert isinstance(msg, EmailMessage)
    assert msg["To"] == "ops@acme.com"
    body = msg.get_body(("plain",))
    assert body is not None
    assert f"/r/{rid}?token=rawtok" in body.get_content()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["hostname"] == "smtp.test.example"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "user"
    assert kwargs["password"] == "pw"
    assert kwargs["start_tls"] is True
