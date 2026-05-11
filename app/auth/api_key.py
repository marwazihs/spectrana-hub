"""API-key bearer middleware (PLAN.md §5.2).

Resolves `Authorization: Bearer <api_key>` to a `Customer` row. Used as a
FastAPI dependency by all `/v1/*` agent endpoints.

Lookup strategy: the locked key shape is `mvk_live_<43 chars>` and the
`api_key_prefix` column stores the first 8 chars (always `mvk_live` in
v1), so the prefix doesn't narrow the search. We bcrypt-verify against
every active customer. At v1 scale (O(10) customers) this is acceptable;
if customer count grows past ~50, swap to a wider prefix or a keyed-HMAC
index. PLAN.md §11 tracks the upgrade trigger.
"""

from __future__ import annotations

from typing import Annotated

import bcrypt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.db.session import get_session
from app.errors import invalid_api_key


_BEARER = "Bearer "
_KEY_PREFIX = "mvk_live_"


def _parse_authorization(header: str | None) -> str:
    if not header or not header.startswith(_BEARER):
        raise invalid_api_key()
    raw = header[len(_BEARER) :].strip()
    if not raw or not raw.startswith(_KEY_PREFIX):
        raise invalid_api_key()
    return raw


async def authenticate_api_key(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Customer:
    raw = _parse_authorization(request.headers.get("authorization"))
    raw_bytes = raw.encode("utf-8")

    stmt = select(Customer).where(Customer.is_active.is_(True))
    customers = (await session.execute(stmt)).scalars().all()

    for customer in customers:
        if bcrypt.checkpw(raw_bytes, customer.api_key_hash.encode("utf-8")):
            return customer
    raise invalid_api_key()


CurrentCustomer = Annotated[Customer, Depends(authenticate_api_key)]
