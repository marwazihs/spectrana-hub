"""End-to-end Playwright coverage for the Next.js viewer + Hub backend (M6.8).

Eight scenarios, one fixture report each (or sharing the seeded `report_id`):

  1. S1 email entry — submit + render generic submitted state.
  2. Consume happy path — mint, click, cookie set, S2 chrome renders.
  3. Iframe resize — chrome iframe height grows past the 640px placeholder.
  4. Shrink-case (M7 §15.12) — long report → short report, height shrinks.
  5. Expired token — replay consume URL, 410 expired-page renders.
  6. Mobile 375 edge-to-edge — chrome iframe-wrap padding collapses to 0.
  7. Anti-enum parity — allowlisted + off-allowlist render byte-identical S1.
  8. Agent cross-customer — API key cannot mint against a sibling's report.

Every test starts on a clean browser context so cookies don't leak between
scenarios. `seeded` data and the Hub/Next.js servers live for the whole
session.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import (
    ALLOWLIST_EMAIL,
    NEXT_HOST,
    NEXT_PORT,
    OFF_ALLOWLIST_EMAIL,
)


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 1. S1 email entry — happy path
# ---------------------------------------------------------------------------


def test_email_entry_renders_submitted_state(
    page: Page, next_server: str, seeded: dict[str, Any]
) -> None:
    rid = seeded["report_id"]
    page.goto(f"{next_server}/r/{rid}")
    expect(page.get_by_role("heading", name="View your report")).to_be_visible()
    page.get_by_label("Email").fill(ALLOWLIST_EMAIL)
    page.get_by_role("button", name="Send link").click()
    # Form swaps to submitted state — same text whether allowlisted or not.
    expect(page.get_by_role("status")).to_contain_text("If this email is on file")
    # The input is gone, the heading remains (DESIGN.md S1 submitted state).
    expect(page.get_by_label("Email")).to_have_count(0)


# ---------------------------------------------------------------------------
# 2. Consume → cookie → S2 chrome renders
# ---------------------------------------------------------------------------


def test_consume_url_sets_cookie_and_renders_chrome(
    page: Page, mint_link, seeded: dict[str, Any]
) -> None:
    url = mint_link(seeded["report_id"])
    page.goto(url, wait_until="networkidle")
    # 302 → /r/[id], chrome renders the report title.
    expect(page.get_by_role("heading", name="Acme Q1 Report")).to_be_visible()
    expect(page.locator("iframe[title='Report']")).to_be_visible()
    # hub_session cookie landed on the viewer origin (host-only).
    cookies = page.context.cookies(f"http://{NEXT_HOST}:{NEXT_PORT}/")
    assert any(c["name"] == "hub_session" for c in cookies), cookies


# ---------------------------------------------------------------------------
# 3. Iframe resize — height adjusts from placeholder to content height
# ---------------------------------------------------------------------------


def test_iframe_resize_message_grows_iframe(
    page: Page, mint_link, seeded: dict[str, Any]
) -> None:
    url = mint_link(seeded["report_id_long"])
    page.goto(url)
    iframe = page.locator("iframe[title='Report']")
    # Long fixture is ~2000px (10 sections × 200px). Wait for the resize-poster
    # postMessage to land — chrome bumps height from 640 placeholder up.
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector(\"iframe[title='Report']\");"
        "  return el && el.clientHeight > 1500;"
        "}",
        timeout=10_000,
    )
    box = iframe.bounding_box()
    assert box is not None and box["height"] > 1500


# ---------------------------------------------------------------------------
# 4. Shrink case — swap iframe.src to a short report; height must shrink
# ---------------------------------------------------------------------------


def test_iframe_shrinks_when_inner_content_shrinks(
    page: Page, mint_link, seeded: dict[str, Any]
) -> None:
    """Regression guard: the resize-poster reads `document.body.scrollHeight`
    (not `documentElement.scrollHeight`). With the wrong source, navigating
    from a long report to a short report keeps the iframe locked at the long
    height. PLAN.md §15.12 marker — verified live during P2 mockup review.
    """
    long_url = mint_link(seeded["report_id_long"])
    short_url = mint_link(seeded["report_id_short"])
    page.goto(long_url)
    page.wait_for_function(
        "() => document.querySelector(\"iframe[title='Report']\").clientHeight > 1500",
        timeout=10_000,
    )

    # Swap the iframe src in-place — same browser session, same chrome page.
    # We can't drive Hub's iframe-jwt for the short report from the chrome's
    # JS (it lacks the internal token), so instead we re-navigate the whole
    # page to the short report's consume URL. The hub_session cookie persists
    # since both reports belong to the same customer.
    page.goto(short_url)
    # Iframe height must drop materially. Short fixture is ~240px of content
    # (two 120px sections) — we assert under 800 to leave headroom for
    # platform paint timing.
    page.wait_for_function(
        "() => document.querySelector(\"iframe[title='Report']\").clientHeight < 800",
        timeout=10_000,
    )


# ---------------------------------------------------------------------------
# 5. Expired token — replay consume URL → 410 expired page
# ---------------------------------------------------------------------------


def test_replayed_token_renders_expired_page(
    page: Page, mint_link, seeded: dict[str, Any]
) -> None:
    url = mint_link(seeded["report_id"])
    # First consume succeeds.
    page.goto(url)
    expect(page.get_by_role("heading", name="Acme Q1 Report")).to_be_visible()
    # Wipe cookies so the second visit hits the consume route fresh (without
    # the cookie, the route still 410s — token is single-use on Hub's side).
    page.context.clear_cookies()
    response = page.goto(url)
    assert response is not None and response.status == 410
    expect(page.get_by_role("heading", name="This link has expired")).to_be_visible()
    expect(page.get_by_role("link", name="Request a new link")).to_be_visible()


# ---------------------------------------------------------------------------
# 6. Mobile 375 — iframe-wrap goes edge-to-edge
# ---------------------------------------------------------------------------


def test_mobile_iframe_is_edge_to_edge(
    page: Page, mint_link, seeded: dict[str, Any]
) -> None:
    page.set_viewport_size({"width": 375, "height": 800})
    url = mint_link(seeded["report_id"])
    page.goto(url)
    wrap = page.locator("[data-hub-chrome='iframe-wrap']")
    expect(wrap).to_be_visible()
    # CSS rule in globals.css overrides the inline padding to 0 on ≤768px.
    pad_left = wrap.evaluate("(el) => getComputedStyle(el).paddingLeft")
    pad_right = wrap.evaluate("(el) => getComputedStyle(el).paddingRight")
    assert pad_left == "0px", f"expected 0px, got {pad_left}"
    assert pad_right == "0px", f"expected 0px, got {pad_right}"
    # Title drops to 24px on mobile.
    title_size = page.locator("[data-hub-chrome='title']").evaluate(
        "(el) => getComputedStyle(el).fontSize"
    )
    assert title_size == "24px", f"expected 24px, got {title_size}"


# ---------------------------------------------------------------------------
# 7. Anti-enum parity — allowlisted vs off-allowlist submitted state
# ---------------------------------------------------------------------------


def test_antienum_submitted_state_is_byte_identical(
    page: Page, next_server: str, seeded: dict[str, Any]
) -> None:
    rid = seeded["report_id"]

    def _capture_submitted_html(email: str) -> str:
        page.context.clear_cookies()
        page.goto(f"{next_server}/r/{rid}")
        page.get_by_label("Email").fill(email)
        page.get_by_role("button", name="Send link").click()
        # Wait for the submitted-state status region to be visible — this is
        # the surface that must be byte-identical between paths.
        page.wait_for_selector("[role='status']")
        return page.locator("form").inner_html()

    a = _capture_submitted_html(ALLOWLIST_EMAIL)
    b = _capture_submitted_html(OFF_ALLOWLIST_EMAIL)
    assert a == b, "submitted DOM differs between allowlisted and off-allowlist"


# ---------------------------------------------------------------------------
# 8. Agent path — API key cannot mint against a sibling customer's report
# ---------------------------------------------------------------------------


def test_agent_path_returns_404_for_cross_customer_report(
    hub_server: str, seeded: dict[str, Any]
) -> None:
    """The agent endpoint scopes mint to the caller's reports. A foreign
    report_id MUST surface as 404 (`report-not-found`), identical to other
    `/v1/*` routes. The public path stays anti-enum, but the agent path
    returns real errors."""
    other_rid = seeded["report_id_other_customer"]
    req = urllib.request.Request(
        f"{hub_server}/r/{other_rid}/request-link",
        data=json.dumps(
            {
                "email": ALLOWLIST_EMAIL,
                "delivery": "return",
                "channel_hint": "e2e-test",
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {seeded['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    err = ei.value
    assert err.code == 404, f"expected 404, got {err.code}"
    payload = json.loads(err.read())
    # Slug surface (errors.py): "report-not-found".
    assert "report-not-found" in payload.get("slug", payload.get("type", "")), payload
