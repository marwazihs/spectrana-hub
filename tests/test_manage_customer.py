"""manage_customer.py — PLAN.md §5.4 test plan.

- Round-trip: create → get-by-email → add-email → rotate-key → deactivate
- Property (Hypothesis): add-email + remove-email returns allowlist to baseline
- Negative: get unknown email → exit 1; rotate-key unknown id → exit 1;
            duplicate name allowed
"""

from __future__ import annotations

import io
import uuid
from contextlib import redirect_stdout

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.auth.models import Customer
from scripts import manage_customer as mc


# --- happy-path round-trip -------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip(session) -> None:
    # create
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mc.cmd_create(session, "Acme Corp", ["ops@acme.com", "cfo@acme.com"])
    assert rc == 0
    out = buf.getvalue()
    assert "api_key:     mvk_live_" in out
    # raw key visible once — extract it for verification
    raw_key = next(
        line.split("api_key:", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("api_key:")
    )
    assert raw_key.startswith("mvk_live_")
    assert len(raw_key) > 8 + 32  # prefix + token_urlsafe(32) suffix

    customer = (await session.execute(select(Customer))).scalar_one()
    customer_id = customer.id
    # bcrypt hash, not the raw key
    assert customer.api_key_hash.startswith("$2b$") or customer.api_key_hash.startswith("$2a$")
    assert customer.api_key_prefix == "mvk_live"  # first 8 chars
    assert customer.is_active is True
    assert sorted(customer.allowlist_emails) == ["cfo@acme.com", "ops@acme.com"]

    # get by email
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mc.cmd_get_by_email(session, "ops@acme.com", "json")
    assert rc == 0
    assert str(customer_id) in buf.getvalue()

    # get by id
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mc.cmd_get_by_id(session, customer_id, "json")
    assert rc == 0
    assert "Acme Corp" in buf.getvalue()

    # add email (idempotent on duplicate)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mc.cmd_add_email(session, customer_id, "audit@acme.com")
    assert rc == 0
    await session.refresh(customer)
    assert "audit@acme.com" in customer.allowlist_emails

    # adding the same email twice is a no-op
    rc = await mc.cmd_add_email(session, customer_id, "audit@acme.com")
    assert rc == 0
    await session.refresh(customer)
    assert customer.allowlist_emails.count("audit@acme.com") == 1

    # rotate key — prefix stays "mvk_live" but hash changes
    old_hash = customer.api_key_hash
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mc.cmd_rotate_key(session, customer_id)
    assert rc == 0
    assert "new_api_key: mvk_live_" in buf.getvalue()
    await session.refresh(customer)
    assert customer.api_key_hash != old_hash
    assert customer.api_key_prefix == "mvk_live"

    # deactivate
    rc = await mc.cmd_deactivate(session, customer_id)
    assert rc == 0
    await session.refresh(customer)
    assert customer.is_active is False


# --- property: add then remove returns to baseline -------------------------


@pytest.mark.asyncio
@hyp_settings(
    max_examples=15,
    deadline=None,
    # Test creates+deletes the customer per example, so the function-scoped
    # session fixture being shared across examples is intentional.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    email=st.from_regex(
        r"^[a-z][a-z0-9._-]{0,20}@[a-z][a-z0-9-]{0,20}\.[a-z]{2,5}$", fullmatch=True
    )
)
async def test_add_then_remove_email_is_identity(session, email: str) -> None:
    # Fresh customer per example. (Hypothesis re-runs the fn many times; each
    # run is its own transaction inside `session` so state is independent.)
    raw, prefix, digest = mc._mint_key()
    customer = Customer(
        name="HypoTest",
        allowlist_emails=["baseline@example.com"],
        api_key_hash=digest,
        api_key_prefix=prefix,
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)

    baseline = sorted(customer.allowlist_emails)
    buf = io.StringIO()
    with redirect_stdout(buf):
        await mc.cmd_add_email(session, customer.id, email)
        await mc.cmd_remove_email(session, customer.id, email)
    await session.refresh(customer)
    assert sorted(customer.allowlist_emails) == baseline

    # Cleanup so the next Hypothesis example starts clean within this session.
    await session.delete(customer)
    await session.commit()


# --- negative cases --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unknown_email_exits_1(session, capsys) -> None:
    rc = await mc.cmd_get_by_email(session, "nobody@nowhere.invalid", "json")
    assert rc == 1
    assert "no customer found" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_rotate_key_unknown_id_exits_1(session, capsys) -> None:
    unknown = uuid.uuid4()
    rc = await mc.cmd_rotate_key(session, unknown)
    assert rc == 1
    assert "no customer with id" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_deactivate_unknown_id_exits_1(session, capsys) -> None:
    unknown = uuid.uuid4()
    rc = await mc.cmd_deactivate(session, unknown)
    assert rc == 1
    assert "no customer with id" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_duplicate_name_allowed(session) -> None:
    # Per §5.4: uniqueness is by id, not name. Two customers with the same
    # name must both succeed.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc1 = await mc.cmd_create(session, "Same Name LLC", ["a@a.com"])
        rc2 = await mc.cmd_create(session, "Same Name LLC", ["b@b.com"])
    assert rc1 == rc2 == 0
    customers = (await session.execute(select(Customer))).scalars().all()
    assert len(customers) == 2
    assert customers[0].id != customers[1].id
