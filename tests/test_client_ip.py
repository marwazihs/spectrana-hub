"""client_ip() — rightmost X-Forwarded-For, validated, peer fallback."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.client_ip import client_ip


def _request(xff: str | None, peer: str | None = "10.0.0.9") -> Request:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode())]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 12345) if peer else None,
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        (None, "10.0.0.9"),  # no header → socket peer
        ("203.0.113.7", "203.0.113.7"),  # single entry (Traefik overwrite)
        ("1.1.1.1, 203.0.113.7", "203.0.113.7"),  # spoofed leftmost ignored
        ("203.0.113.7, ", "203.0.113.7"),  # trailing empty entry skipped
        ("2001:db8::1", "2001:db8::1"),  # IPv6
        ("not-an-ip", "10.0.0.9"),  # garbage → peer
        ("1.1.1.1, garbage", "10.0.0.9"),  # garbage rightmost → peer, not leftmost
        ("", "10.0.0.9"),  # empty header → peer
        (" , ", "10.0.0.9"),  # only separators → peer
    ],
)
def test_client_ip(xff: str | None, expected: str) -> None:
    assert client_ip(_request(xff)) == expected


def test_client_ip_no_peer_no_header() -> None:
    assert client_ip(_request(None, peer=None)) == "0.0.0.0"
