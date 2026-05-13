"""Shared fixtures — session-scoped testcontainers postgres + alembic upgrade.

Strategy:
- Spin up a postgres:16 container once per pytest session
- Run `alembic upgrade head` once against it to materialize schema
- Per-test: open an async session, truncate all tables in teardown
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Session-scoped event loop so the testcontainers session matches."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """One postgres:16 container per pytest session."""
    with PostgresContainer(
        image="postgres:16",
        username="hub",
        password="hub",
        dbname="hub",
        driver=None,  # raw URL; we'll rewrite to asyncpg below
    ) as pg:
        yield pg


@pytest.fixture(scope="session")
def database_url(pg_container: PostgresContainer) -> str:
    raw = pg_container.get_connection_url()
    # testcontainers gives psycopg2 URL; normalize to asyncpg for app code.
    return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session", autouse=True)
def _alembic_upgrade(database_url: str) -> None:
    """Materialize schema once per session via alembic upgrade head."""
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = database_url
    cfg = Config("alembic.ini")
    # Inject explicitly so env.py uses the testcontainers URL even if
    # app.config.settings was imported earlier and cached the dev URL.
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Per-test async session. Truncates all tables on teardown."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    # Truncate everything except alembic_version. RESTART IDENTITY resets BIGSERIAL.
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE customers, reports, magic_link_tokens, "
            "idempotency_keys, rate_limit_hits, events, sessions "
            "RESTART IDENTITY CASCADE;"
        )
    await engine.dispose()
