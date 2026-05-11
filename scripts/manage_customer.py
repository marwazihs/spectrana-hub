"""Customer provisioning CLI — PLAN.md §5.4.

Single operator surface for Majeve ops to create, list, update, and rotate
customers. The raw API key is printed exactly once on `create` and `rotate-key`;
it is never logged, never re-printable, never stored anywhere except as a bcrypt
hash on the customers row.

Usage:
    uv run python -m scripts.manage_customer create --name "Acme" --emails a@b.com
    uv run python -m scripts.manage_customer list [--active-only] [--format table|json]
    uv run python -m scripts.manage_customer get --email a@b.com
    uv run python -m scripts.manage_customer get --id <uuid>
    uv run python -m scripts.manage_customer add-email <id> <email>
    uv run python -m scripts.manage_customer remove-email <id> <email>
    uv run python -m scripts.manage_customer rotate-key <id>
    uv run python -m scripts.manage_customer deactivate <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from datetime import datetime
from uuid import UUID

import bcrypt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Customer
from app.config import settings
from app.db.session import SessionFactory, engine


# --- key minting -----------------------------------------------------------


KEY_PREFIX = "mvk_live_"
KEY_PREFIX_LEN = 8  # first 8 chars of the prefix-suffix concat, for log debugging
BCRYPT_ROUNDS = 12  # matches app middleware (§5.2 verify path)


def _mint_key() -> tuple[str, str, str]:
    """Return (raw_key, prefix, bcrypt_hash). Raw key shape: mvk_live_<43 chars>."""
    suffix = secrets.token_urlsafe(32)
    raw = f"{KEY_PREFIX}{suffix}"
    # Prefix-for-logs is the first 8 chars of the raw key (e.g. "mvk_live")
    prefix = raw[:KEY_PREFIX_LEN]
    digest = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    return raw, prefix, digest.decode("utf-8")


# --- DB-URL foot-gun guard -------------------------------------------------


def _assert_safe_db_url() -> None:
    """Refuse to run against a DB whose URL doesn't look like ours. Foot-gun
    guard against pointing at the wrong DB (e.g. someone's prod Spectra)."""
    url = settings.DATABASE_URL
    if "hub" not in url:
        sys.stderr.write(
            f"refusing to run: DATABASE_URL does not contain 'hub' → {url!r}\n"
            "Set DATABASE_URL to a hub database before running this CLI.\n"
        )
        sys.exit(2)


# --- output helpers --------------------------------------------------------


def _customer_row(c: Customer) -> dict[str, object]:
    return {
        "id": str(c.id),
        "name": c.name,
        "api_key_prefix": c.api_key_prefix,
        "allowlist_email_count": len(c.allowlist_emails),
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _print_rows(rows: list[dict[str, object]], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("(no customers)")
        return
    # Stable column order, simple table.
    cols = ["id", "name", "api_key_prefix", "allowlist_email_count", "is_active", "created_at"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


# --- commands --------------------------------------------------------------


async def cmd_create(session: AsyncSession, name: str, emails: list[str]) -> int:
    raw, prefix, digest = _mint_key()
    customer = Customer(
        name=name,
        allowlist_emails=emails,
        api_key_hash=digest,
        api_key_prefix=prefix,
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)

    # ONE-SHOT print. After this returns, the raw key is unrecoverable.
    print(f"customer_id: {customer.id}")
    print(f"api_key:     {raw}")
    print(f"prefix:      {prefix}")
    print()
    print("Deliver the api_key to the customer via 1Password share or equivalent.")
    print("Never email plaintext. This is the only time you'll see it.")
    return 0


async def cmd_list(session: AsyncSession, active_only: bool, fmt: str) -> int:
    stmt = select(Customer).order_by(Customer.created_at.desc())
    if active_only:
        stmt = stmt.where(Customer.is_active.is_(True))
    result = await session.execute(stmt)
    rows = [_customer_row(c) for c in result.scalars()]
    _print_rows(rows, fmt)
    return 0


async def cmd_get_by_email(session: AsyncSession, email: str, fmt: str) -> int:
    # allowlist_emails is text[], use array-contains operator @>.
    result = await session.execute(
        select(Customer).where(Customer.allowlist_emails.contains([email]))
    )
    rows = [_customer_row(c) for c in result.scalars()]
    if not rows:
        sys.stderr.write(f"no customer found with allowlist email {email!r}\n")
        return 1
    _print_rows(rows, fmt)
    return 0


async def cmd_get_by_id(session: AsyncSession, customer_id: UUID, fmt: str) -> int:
    result = await session.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if customer is None:
        sys.stderr.write(f"no customer with id {customer_id}\n")
        return 1
    _print_rows([_customer_row(customer)], fmt)
    return 0


async def cmd_add_email(session: AsyncSession, customer_id: UUID, email: str) -> int:
    result = await session.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if customer is None:
        sys.stderr.write(f"no customer with id {customer_id}\n")
        return 1
    if email in customer.allowlist_emails:
        print(f"already present: {email}")
        return 0
    customer.allowlist_emails = [*customer.allowlist_emails, email]
    await session.commit()
    print(f"added {email} ({len(customer.allowlist_emails)} total)")
    return 0


async def cmd_remove_email(session: AsyncSession, customer_id: UUID, email: str) -> int:
    result = await session.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if customer is None:
        sys.stderr.write(f"no customer with id {customer_id}\n")
        return 1
    if email not in customer.allowlist_emails:
        print(f"not present: {email}")
        return 0
    customer.allowlist_emails = [e for e in customer.allowlist_emails if e != email]
    await session.commit()
    print(f"removed {email} ({len(customer.allowlist_emails)} total)")
    return 0


async def cmd_rotate_key(session: AsyncSession, customer_id: UUID) -> int:
    raw, prefix, digest = _mint_key()
    result = await session.execute(
        update(Customer)
        .where(Customer.id == customer_id)
        .values(api_key_hash=digest, api_key_prefix=prefix)
        .returning(Customer.id)
    )
    if result.scalar_one_or_none() is None:
        sys.stderr.write(f"no customer with id {customer_id}\n")
        await session.rollback()
        return 1
    await session.commit()
    print(f"customer_id: {customer_id}")
    print(f"new_api_key: {raw}")
    print(f"new_prefix:  {prefix}")
    print()
    print("Old key is now invalid. Spectra must restart with the new key.")
    print("This is the only time you'll see the new key.")
    return 0


async def cmd_deactivate(session: AsyncSession, customer_id: UUID) -> int:
    result = await session.execute(
        update(Customer)
        .where(Customer.id == customer_id)
        .values(is_active=False)
        .returning(Customer.id)
    )
    if result.scalar_one_or_none() is None:
        sys.stderr.write(f"no customer with id {customer_id}\n")
        await session.rollback()
        return 1
    await session.commit()
    print(f"deactivated {customer_id}")
    print("Reports are NOT deleted. Existing magic-link sessions remain valid until expiry.")
    return 0


# --- argparse --------------------------------------------------------------


def _parse_emails(value: str) -> list[str]:
    return [e.strip() for e in value.split(",") if e.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manage_customer",
        description="Hub customer provisioning CLI (PLAN.md §5.4)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("create", help="create a new customer")
    pc.add_argument("--name", required=True)
    pc.add_argument(
        "--emails",
        required=True,
        type=_parse_emails,
        help="comma-separated allowlist emails",
    )

    pl = sub.add_parser("list", help="list customers")
    pl.add_argument("--active-only", action="store_true")
    pl.add_argument("--format", choices=["table", "json"], default="table")

    pg = sub.add_parser("get", help="look up a customer")
    pg_grp = pg.add_mutually_exclusive_group(required=True)
    pg_grp.add_argument("--email")
    pg_grp.add_argument("--id", dest="customer_id", type=UUID)
    pg.add_argument("--format", choices=["table", "json"], default="table")

    pae = sub.add_parser("add-email", help="add an email to a customer's allowlist")
    pae.add_argument("customer_id", type=UUID)
    pae.add_argument("email")

    pre = sub.add_parser("remove-email", help="remove an email from allowlist")
    pre.add_argument("customer_id", type=UUID)
    pre.add_argument("email")

    pr = sub.add_parser("rotate-key", help="generate a new api key, invalidating the old")
    pr.add_argument("customer_id", type=UUID)

    pd = sub.add_parser("deactivate", help="set is_active=false (does not delete reports)")
    pd.add_argument("customer_id", type=UUID)

    return p


async def _dispatch(args: argparse.Namespace) -> int:
    try:
        async with SessionFactory() as session:
            if args.command == "create":
                return await cmd_create(session, args.name, args.emails)
            if args.command == "list":
                return await cmd_list(session, args.active_only, args.format)
            if args.command == "get":
                if args.email is not None:
                    return await cmd_get_by_email(session, args.email, args.format)
                return await cmd_get_by_id(session, args.customer_id, args.format)
            if args.command == "add-email":
                return await cmd_add_email(session, args.customer_id, args.email)
            if args.command == "remove-email":
                return await cmd_remove_email(session, args.customer_id, args.email)
            if args.command == "rotate-key":
                return await cmd_rotate_key(session, args.customer_id)
            if args.command == "deactivate":
                return await cmd_deactivate(session, args.customer_id)
        return 2
    finally:
        # Dispose engine inside the same event loop so asyncpg can close its
        # loop-bound connections cleanly. Running dispose() in a second
        # asyncio.run() call binds it to a fresh loop and triggers
        # "Event loop is closed" on connection teardown.
        await engine.dispose()


def main() -> int:
    _assert_safe_db_url()
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    sys.exit(main())
