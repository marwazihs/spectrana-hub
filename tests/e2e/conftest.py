"""E2E fixtures: Postgres + MinIO + Hub + Next.js + seeded data.

The unit/integration suite uses the parent `tests/conftest.py` for an in-process
ASGI client. E2E tests need a *real* HTTP surface — a browser drives the Next.js
viewer, which fetches from Hub over HTTP. So we spawn:

  - Postgres (testcontainers, reused from parent `pg_container`).
  - MinIO (testcontainers; provides the S3 endpoint Hub writes report HTML to).
  - Hub  (uvicorn subprocess) — fixed port 18001.
  - Next.js (`next start` subprocess) — fixed port 13001.

Fixed ports keep the Next.js build cacheable: `NEXT_PUBLIC_HUB_REPORTS_DOMAIN`
is baked into the bundle at build time, so it can't depend on a runtime-allocated
port. Trade-off: if a port is busy the suite fails loudly — fine for a single-
machine dev workflow + CI.

The frontend build is reused if `frontend/.next` already exists and the env vars
match. Building from scratch is ~30s; rebuild only when the toolchain or env
changes.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncio
import boto3
import bcrypt
import pytest
import urllib.request
import urllib.error
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.minio import MinioContainer


# Fixed ports — see module docstring. If you change HUB_PORT/NEXT_PORT, also
# rebuild the frontend (the public env var is baked in at build time).
HUB_PORT = 18001
NEXT_PORT = 13001
# Use "localhost" (not 127.0.0.1). Next.js's NextResponse.redirect resolves
# `new URL(path, req.url)` against a host that Next normalizes to `localhost`
# regardless of the Host header — so a cookie set on 127.0.0.1 wouldn't be
# sent after the redirect. Standardizing on `localhost` keeps the cookie
# domain consistent with the post-redirect URL.
HUB_HOST = "localhost"
NEXT_HOST = "localhost"

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"

# Test secrets — fixed values, padded to ≥32 bytes so Hub's anti-footgun gate
# accepts them. Never used outside the e2e suite.
HUB_INTERNAL_TOKEN = "test-internal-token-padded-to-at-least-32-bytes-aaaa"
HUB_SESSION_SECRET = "test-session-secret-padded-to-at-least-32-bytes-bbbb"
HUB_IFRAME_JWT_SECRET = "test-iframe-secret-padded-to-at-least-32-bytes-cccc"
HUB_MAGIC_LINK_HASH_SECRET = "test-magic-secret-padded-to-at-least-32-bytes-dddd"
TEST_BUCKET = "hub-reports-test"

# Customer-scoped fixture data
ALLOWLIST_EMAIL = "viewer@acme.test"
OFF_ALLOWLIST_EMAIL = "stranger@acme.test"
API_KEY_PLAINTEXT = "mvk_live_" + "e" * 43  # matches mvk_live_<43chars> shape

# Long fixture: ~2000px tall. Short fixture: ~400px tall. Used by the shrink
# regression case (M7 §15.12) — the resize-poster must shrink the parent
# iframe when the inner content shrinks, not stay locked at the prior height.
LONG_REPORT_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Long</title>"
    "<style>body{margin:0;font-family:sans-serif}"
    ".block{height:200px;padding:16px}</style></head>"
    "<body>"
    + "".join(
        f"<div class='block' style='background:#{'eee' if i % 2 else 'ddd'}'>"
        f"Section {i}</div>"
        for i in range(10)
    )
    + "</body></html>"
).encode()

SHORT_REPORT_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Short</title>"
    "<style>body{margin:0;font-family:sans-serif}"
    ".block{height:120px;padding:16px;background:#f3f3f3}</style></head>"
    "<body><div class='block'>Short report — one section</div>"
    "<div class='block'>Second section</div></body></html>"
).encode()


def _port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _terminate_group(proc: subprocess.Popen[Any]) -> None:
    """Kill `proc` and any children it spawned.

    `bun run start` execs `next start` as a child, and a plain SIGTERM to the
    bun parent leaves the next-server orphaned on the port. We start each
    subprocess in its own process group (start_new_session=True) and signal
    the whole group here.
    """
    if proc.poll() is not None:
        return
    pgid = os.getpgid(proc.pid)
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _wait_http(url: str, timeout: float = 60.0, interval: float = 0.5) -> None:
    """Block until `url` returns any HTTP response, even 4xx.

    A 404 means the server is up and routing — that's the signal we want for
    `next start` (which has no /healthz). HTTPError is treated as "server is
    alive"; only connection errors keep the loop spinning.
    """
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310
                if 200 <= r.status < 500:
                    return
        except urllib.error.HTTPError:
            # Server responded — that's enough.
            return
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {url}: {last_err}")


# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def minio_container() -> Iterator[dict[str, str]]:
    with MinioContainer() as m:
        cfg = m.get_config()
        # Create the bucket Hub writes to. We use boto3 (not the bundled Minio
        # client) so behavior matches the production code path.
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{cfg['endpoint']}",
            aws_access_key_id=cfg["access_key"],
            aws_secret_access_key=cfg["secret_key"],
            region_name="us-east-1",
        )
        s3.create_bucket(Bucket=TEST_BUCKET)
        yield {
            "endpoint": f"http://{cfg['endpoint']}",
            "access_key": cfg["access_key"],
            "secret_key": cfg["secret_key"],
            "bucket": TEST_BUCKET,
        }


# ---------------------------------------------------------------------------
# Frontend build (one-shot per session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def frontend_build() -> Path:
    """Build the Next.js app once per session.

    `NEXT_PUBLIC_HUB_REPORTS_DOMAIN` is baked at build time, so we always
    rebuild (cheap if `.next` is warm; bun's cache makes deps ~free).
    """
    if shutil.which("bun") is None:
        pytest.skip("bun not on PATH — e2e suite requires bun for the Next.js build")

    env = os.environ.copy()
    env.update(
        {
            "HUB_INTERNAL_TOKEN": HUB_INTERNAL_TOKEN,
            "HUB_API_BASE_URL": f"http://{HUB_HOST}:{HUB_PORT}",
            "HUB_COOKIE_SECURE": "0",
            # Iframe origin: same Hub server, just /render/{id}. M4.2 deferred
            # Host-header routing, so Hub serves /render on whatever host hits it.
            "NEXT_PUBLIC_HUB_REPORTS_DOMAIN": f"{HUB_HOST}:{HUB_PORT}",
        }
    )
    # Ensure deps are installed (no-op if bun.lock matches).
    subprocess.run(
        ["bun", "install", "--frozen-lockfile"],
        cwd=FRONTEND_DIR,
        env=env,
        check=True,
    )
    subprocess.run(
        ["bun", "run", "build"],
        cwd=FRONTEND_DIR,
        env=env,
        check=True,
    )
    return FRONTEND_DIR


# ---------------------------------------------------------------------------
# Hub subprocess
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hub_server(
    database_url: str, minio_container: dict[str, str]
) -> Iterator[str]:
    if not _port_free(HUB_HOST, HUB_PORT):
        # Another test run or a stale subprocess. Bail loudly rather than
        # silently attaching to a server we didn't configure.
        raise RuntimeError(
            f"port {HUB_PORT} is already in use; kill the prior Hub subprocess"
        )

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "AWS_S3_ENDPOINT_URL": minio_container["endpoint"],
            "AWS_ACCESS_KEY_ID": minio_container["access_key"],
            "AWS_SECRET_ACCESS_KEY": minio_container["secret_key"],
            "AWS_S3_BUCKET": minio_container["bucket"],
            "AWS_REGION": "us-east-1",
            "HUB_PRIMARY_DOMAIN": f"{NEXT_HOST}:{NEXT_PORT}",
            "HUB_REPORTS_DOMAIN": f"{HUB_HOST}:{HUB_PORT}",
            "HUB_INTERNAL_TOKEN": HUB_INTERNAL_TOKEN,
            "HUB_SESSION_SECRET": HUB_SESSION_SECRET,
            "HUB_IFRAME_JWT_SECRET": HUB_IFRAME_JWT_SECRET,
            "HUB_MAGIC_LINK_HASH_SECRET": HUB_MAGIC_LINK_HASH_SECRET,
            "HUB_EMAIL_FROM": "Hub Test <test@hub.local>",
            "HUB_EMAIL_BRAND_NAME": "Hub-Test",
            "LOG_LEVEL": "WARNING",
            # SMTP_HOST blank → send_magic_link_email raises; the public path
            # swallows it, and tests use delivery=return for the agent path.
            "SMTP_HOST": "",
        }
    )
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "app.main:app",
            "--host",
            HUB_HOST,
            "--port",
            str(HUB_PORT),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # new process group → kill the whole tree on teardown
    )
    try:
        _wait_http(f"http://{HUB_HOST}:{HUB_PORT}/healthz", timeout=45.0)
        yield f"http://{HUB_HOST}:{HUB_PORT}"
    finally:
        _terminate_group(proc)


# ---------------------------------------------------------------------------
# Next.js subprocess
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def next_server(hub_server: str, frontend_build: Path) -> Iterator[str]:
    if not _port_free(NEXT_HOST, NEXT_PORT):
        raise RuntimeError(
            f"port {NEXT_PORT} is already in use; kill the prior Next.js subprocess"
        )
    env = os.environ.copy()
    env.update(
        {
            "HUB_INTERNAL_TOKEN": HUB_INTERNAL_TOKEN,
            "HUB_API_BASE_URL": hub_server,
            "HUB_COOKIE_SECURE": "0",
            "NEXT_PUBLIC_HUB_REPORTS_DOMAIN": f"{HUB_HOST}:{HUB_PORT}",
            "PORT": str(NEXT_PORT),
            "HOSTNAME": NEXT_HOST,
        }
    )
    proc = subprocess.Popen(
        ["bun", "run", "start", "--", "-p", str(NEXT_PORT), "-H", NEXT_HOST],
        cwd=frontend_build,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        # Next.js returns 404 on unknown paths — any HTTP response means it's up.
        _wait_http(f"http://{NEXT_HOST}:{NEXT_PORT}/__healthz_probe", timeout=45.0)
        yield f"http://{NEXT_HOST}:{NEXT_PORT}"
    finally:
        _terminate_group(proc)


# ---------------------------------------------------------------------------
# Seeded data: customers, reports, S3 objects
# ---------------------------------------------------------------------------


async def _truncate_all(async_url: str) -> None:
    eng = create_async_engine(async_url)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE customers, reports, magic_link_tokens, "
                "idempotency_keys, rate_limit_hits, events, sessions "
                "RESTART IDENTITY CASCADE;"
            )
        )
    await eng.dispose()


def _put_object(
    minio: dict[str, str], *, customer_id: uuid.UUID, report_id: uuid.UUID, body: bytes
) -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=minio["endpoint"],
        aws_access_key_id=minio["access_key"],
        aws_secret_access_key=minio["secret_key"],
        region_name="us-east-1",
    )
    s3.put_object(
        Bucket=minio["bucket"],
        Key=f"{customer_id}/{report_id}/index.html",
        Body=body,
        ContentType="text/html; charset=utf-8",
    )


async def _seed(
    async_url: str, minio: dict[str, str]
) -> dict[str, Any]:
    """Insert one customer (with allowlist + API key) and three reports.

    Returns IDs the tests need. Called once per session — tests are read-only
    against the seeded data, with the exception of magic-link rows which
    cycle freely. We don't truncate between e2e tests; instead each test mints
    its own magic-link token.
    """
    from uuid_utils import uuid7

    cid = uuid.uuid4()
    rid_main = uuid.UUID(str(uuid7()))
    rid_long = uuid.UUID(str(uuid7()))
    rid_short = uuid.UUID(str(uuid7()))
    rid_other_customer = uuid.UUID(str(uuid7()))
    other_cid = uuid.uuid4()

    api_hash = bcrypt.hashpw(API_KEY_PLAINTEXT.encode(), bcrypt.gensalt(rounds=4)).decode()

    eng = create_async_engine(async_url)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO customers (id, name, allowlist_emails, api_key_hash, "
                "api_key_prefix, is_active) VALUES (:id, :name, :emails, :hash, "
                ":prefix, true)"
            ),
            {
                "id": cid,
                "name": "Acme Test Corp",
                "emails": [ALLOWLIST_EMAIL],
                "hash": api_hash,
                "prefix": "mvk_live",
            },
        )
        other_hash = bcrypt.hashpw(b"other-key", bcrypt.gensalt(rounds=4)).decode()
        await conn.execute(
            text(
                "INSERT INTO customers (id, name, allowlist_emails, api_key_hash, "
                "api_key_prefix, is_active) VALUES (:id, :name, :emails, :hash, "
                ":prefix, true)"
            ),
            {
                "id": other_cid,
                "name": "Other Inc",
                "emails": ["other@other.test"],
                "hash": other_hash,
                "prefix": "mvk_live",
            },
        )
        for rid, cust, title, size in [
            (rid_main, cid, "Acme Q1 Report", len(LONG_REPORT_HTML)),
            (rid_long, cid, "Long Report", len(LONG_REPORT_HTML)),
            (rid_short, cid, "Short Report", len(SHORT_REPORT_HTML)),
            (rid_other_customer, other_cid, "Other Report", len(SHORT_REPORT_HTML)),
        ]:
            await conn.execute(
                text(
                    "INSERT INTO reports (id, customer_id, title, description, tags, "
                    "generated_at, s3_key, supplementary_files, size_bytes) "
                    "VALUES (:id, :cid, :title, '', '{}', now(), :s3, '[]', :sz)"
                ),
                {
                    "id": rid,
                    "cid": cust,
                    "title": title,
                    "s3": f"{cust}/{rid}/index.html",
                    "sz": size,
                },
            )
    await eng.dispose()

    _put_object(minio, customer_id=cid, report_id=rid_main, body=LONG_REPORT_HTML)
    _put_object(minio, customer_id=cid, report_id=rid_long, body=LONG_REPORT_HTML)
    _put_object(minio, customer_id=cid, report_id=rid_short, body=SHORT_REPORT_HTML)
    _put_object(
        minio, customer_id=other_cid, report_id=rid_other_customer, body=SHORT_REPORT_HTML
    )

    return {
        "customer_id": cid,
        "other_customer_id": other_cid,
        "report_id": rid_main,
        "report_id_long": rid_long,
        "report_id_short": rid_short,
        "report_id_other_customer": rid_other_customer,
        "api_key": API_KEY_PLAINTEXT,
        "allowlist_email": ALLOWLIST_EMAIL,
    }


@pytest.fixture(scope="session")
def seeded(
    database_url: str, minio_container: dict[str, str], hub_server: str
) -> dict[str, Any]:
    async def _go() -> dict[str, Any]:
        await _truncate_all(database_url)
        return await _seed(database_url, minio_container)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_go())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Magic-link mint helper (used by every test)
# ---------------------------------------------------------------------------


@pytest.fixture
def mint_link(hub_server: str, seeded: dict[str, Any], next_server: str):
    """Mint a magic-link via the agent path (delivery=return) and rewrite the
    URL host to the Next.js viewer origin.

    The agent endpoint embeds HUB_PRIMARY_DOMAIN in the returned URL via
    https://. For local e2e we run Next.js on http://127.0.0.1:13001, so the
    test rewrites the scheme+host. Hub's session row is the same either way.
    """
    import json

    def _mint(
        report_id: uuid.UUID,
        email: str = ALLOWLIST_EMAIL,
    ) -> str:
        req = urllib.request.Request(
            f"{hub_server}/r/{report_id}/request-link",
            data=json.dumps(
                {"email": email, "delivery": "return", "channel_hint": "e2e-test"}
            ).encode(),
            headers={
                "Authorization": f"Bearer {seeded['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read())
        raw_url = data["url"]
        # rewrite scheme://host to Next.js viewer origin
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(raw_url)
        return urlunparse(
            (
                "http",
                f"{NEXT_HOST}:{NEXT_PORT}",
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    return _mint
