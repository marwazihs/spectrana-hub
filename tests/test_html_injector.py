"""HTML injector (PLAN.md §15.12) — six-fixture coverage + idempotency."""

from __future__ import annotations

from app.reports.html_injector import (
    CSS_SHIM,
    RESIZE_POSTER,
    SHIM_MARKER,
    VIEWPORT_META,
    inject,
)


def _has_all_markers(html: bytes) -> bool:
    return (
        SHIM_MARKER in html
        and b'data-hub-resize="v2"' in html
        and b'name="viewport"' in html
    )


# --- Fixtures ------------------------------------------------------------


def test_fixture_canonical_html_gets_all_three() -> None:
    src = b"<html><head><title>R</title></head><body><h1>hi</h1></body></html>"
    out = inject(src)
    assert _has_all_markers(out)
    # Shim sits after <head>, before <title>
    assert out.index(b"<head>") < out.index(SHIM_MARKER) < out.index(b"<title>")
    # Resize-poster sits before </body>
    assert out.index(b'data-hub-resize="v2"') < out.index(b"</body>")


def test_fixture_no_head_injects_into_body() -> None:
    src = b"<html><body><p>hi</p></body></html>"
    out = inject(src)
    assert _has_all_markers(out)
    # No <head> in source — shim goes after <body>, viewport-meta sits before <body>
    assert out.index(VIEWPORT_META) < out.index(b"<body>")
    assert out.index(b"<body>") < out.index(SHIM_MARKER)


def test_fixture_no_body_appends_resize_poster() -> None:
    src = b"<html><head></head>standalone fragment</html>"
    out = inject(src)
    assert _has_all_markers(out)
    # No </body> and no <body> → poster gets appended at the end.
    assert out.endswith(RESIZE_POSTER)


def test_fixture_existing_viewport_is_left_alone() -> None:
    src = (
        b'<html><head><meta name="viewport" content="width=1024"></head>'
        b"<body><p>x</p></body></html>"
    )
    out = inject(src)
    # Only one viewport meta — ours is not added.
    assert out.count(b'name="viewport"') == 1
    assert b'content="width=1024"' in out
    # Other two markers still land.
    assert SHIM_MARKER in out
    assert b'data-hub-resize="v2"' in out


def test_fixture_all_three_markers_already_present_is_untouched() -> None:
    src = (
        b'<html><head><meta name="viewport" content="width=device-width">'
        + CSS_SHIM
        + b"</head><body><p>x</p>"
        + RESIZE_POSTER
        + b"</body></html>"
    )
    out = inject(src)
    assert out == src


def test_fixture_minified_one_line() -> None:
    src = (
        b"<!doctype html><html><head><title>x</title></head>"
        b"<body><h1>y</h1></body></html>"
    )
    out = inject(src)
    assert _has_all_markers(out)


# --- Idempotency contract ------------------------------------------------


def test_inject_is_idempotent() -> None:
    src = b"<html><head></head><body><p>x</p></body></html>"
    once = inject(src)
    twice = inject(once)
    assert once == twice


def test_partial_markers_present_only_missing_get_added() -> None:
    # Has the shim already, missing viewport + resize-poster.
    src = b"<html><head>" + CSS_SHIM + b"</head><body><p>x</p></body></html>"
    out = inject(src)
    # Shim is not duplicated.
    assert out.count(SHIM_MARKER) == 1
    # Other two are injected.
    assert b'name="viewport"' in out
    assert b'data-hub-resize="v2"' in out


# --- Case-insensitivity ---------------------------------------------------


def test_uppercase_head_and_body_tags_handled() -> None:
    src = b"<HTML><HEAD></HEAD><BODY><P>x</P></BODY></HTML>"
    out = inject(src)
    assert _has_all_markers(out)


# --- Anti-regression for the documentElement.scrollHeight trap -----------


def test_resize_poster_reads_body_scrollheight_not_documentElement() -> None:
    """PLAN.md §15.12 P2 finding #1 — shrink-case bug.

    A 370px-content iframe stayed locked at 1159px when
    documentElement.scrollHeight was used. Guard the constant.
    """
    assert b"document.body.scrollHeight" in RESIZE_POSTER
    assert b"documentElement.scrollHeight" not in RESIZE_POSTER


# --- Anti-regression for the scrollbar-flash race window ------------------


def test_css_shim_includes_html_body_overflow_hidden() -> None:
    """PLAN.md §15.12 P2 finding #2 — race-window scrollbar flash.

    `html, body { overflow: hidden }` must be in the shim so the brief
    50-100ms gap between iframe load and first postMessage doesn't show a
    nested scrollbar.
    """
    assert b"html,body{overflow:hidden}" in CSS_SHIM
