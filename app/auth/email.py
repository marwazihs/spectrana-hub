"""Magic-link email send (PLAN.md §5.1).

Templates: app/auth/templates/magic_link.{html,txt}.j2
Transport: aiosmtplib with STARTTLS (settings.SMTP_*).

`build_magic_link` produces the canonical link URL. `render_magic_link_email`
is pure and unit-testable. `send_magic_link_email` does the SMTP I/O —
integration-tested separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings


_TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    keep_trailing_newline=True,
)


def build_magic_link(report_id: UUID, raw_token: str) -> str:
    """Canonical consume URL. PLAN.md §4.7."""
    return (
        f"https://{settings.HUB_PRIMARY_DOMAIN}/r/{report_id}?token={raw_token}"
    )


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def render_magic_link_email(
    *,
    report_id: UUID,
    raw_token: str,
    brand: str | None = None,
    ttl_minutes: int | None = None,
) -> RenderedEmail:
    brand = brand or settings.HUB_EMAIL_BRAND_NAME
    ttl_minutes = ttl_minutes if ttl_minutes is not None else settings.MAGIC_LINK_TTL_MINUTES
    link = build_magic_link(report_id, raw_token)
    ctx = {"brand": brand, "link": link, "ttl_minutes": ttl_minutes}
    text = _env.get_template("magic_link.txt.j2").render(**ctx)
    html = _env.get_template("magic_link.html.j2").render(**ctx)
    subject = f"Your {brand} report link"
    return RenderedEmail(subject=subject, text=text, html=html)


def _build_message(*, to_email: str, rendered: RenderedEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.HUB_EMAIL_FROM
    msg["To"] = to_email
    msg["Subject"] = rendered.subject
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    return msg


async def send_magic_link_email(
    *,
    to_email: str,
    report_id: UUID,
    raw_token: str,
) -> None:
    rendered = render_magic_link_email(report_id=report_id, raw_token=raw_token)
    msg = _build_message(to_email=to_email, rendered=rendered)
    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER or None,
        password=settings.SMTP_PASS.get_secret_value() or None,
        start_tls=settings.SMTP_STARTTLS,
    )
