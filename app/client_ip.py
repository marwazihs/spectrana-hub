"""Client IP resolution for rate limiting and audit events.

Deploy topology: browser → Traefik → (Next.js →) Hub. Hub has no host port
in production, so every request arrives from a proxy we control:

  - Traefik (untrusted entrypoints) drops client-supplied X-Forwarded-*
    and writes the real peer into X-Forwarded-For.
  - Next.js forwards the rightmost entry it received (lib/hub-client.ts).

We take the RIGHTMOST non-empty entry — the one written by the hop directly
in front of us — never the leftmost, which is client-controlled whenever a
proxy appends instead of overwriting. Anything that doesn't parse as an IP
falls back to the socket peer. See docs/operations/dokploy-deploy.md.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        entries = [e.strip() for e in fwd.split(",") if e.strip()]
        if entries:
            try:
                return str(ipaddress.ip_address(entries[-1]))
            except ValueError:
                pass
    return request.client.host if request.client else "0.0.0.0"
