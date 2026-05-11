"""HTML injector (PLAN.md §15.12, DESIGN.md "Hub Server Responsibilities").

Pure function. Idempotent: detection markers `data-hub-shim="v1"` and
`data-hub-resize="v1"` are checked independently — present markers are
skipped, absent markers are injected.

Three injections:
  1. CSS shim — <style data-hub-shim="v1"> immediately after <head>, or at
     start of <body> if <head> is absent. Includes `html, body {overflow:
     hidden}` to suppress the brief scrollbar flash before the parent
     ResizeListener takes over.
  2. Viewport meta — only if no <meta name="viewport" ...> is present.
  3. Resize-poster — <script data-hub-resize="v1"> immediately before
     </body>, or at end of <body> if </body> is absent. Reads
     document.body.scrollHeight (NOT documentElement) — the latter
     inflates to viewport height when content is shorter, blocking shrink.

Marker version bumps to v2 require a parallel bump in §4.8 ETag suffix so
cached entries invalidate.
"""

from __future__ import annotations

import re


INJECTOR_VERSION = "v1"

SHIM_MARKER = b'data-hub-shim="v1"'
RESIZE_MARKER = b'data-hub-resize="v1"'

CSS_SHIM = (
    b'<style data-hub-shim="v1">'
    b":root{font-size:16px;-webkit-text-size-adjust:100%}"
    b"*,*::before,*::after{box-sizing:border-box}"
    b"html,body{overflow:hidden}"
    b"body{margin:0}"
    b"img,video,svg{max-width:100%;height:auto}"
    b"table{display:block;max-width:100%;overflow-x:auto}"
    b"pre,code{overflow-x:auto;word-wrap:break-word;white-space:pre-wrap}"
    b"</style>"
)

VIEWPORT_META = (
    b'<meta name="viewport" content="width=device-width, initial-scale=1">'
)

# Posts {type, height} to parent on load, resize, and ResizeObserver mutations.
# Uses document.body.scrollHeight — see §15.12 P2 finding #1 (shrink-case bug).
RESIZE_POSTER = (
    b'<script data-hub-resize="v1">'
    b"(function(){"
    b"function post(){"
    b"try{parent.postMessage({type:'hub-resize',"
    b"height:document.body.scrollHeight},'*');}catch(e){}"
    b"}"
    b"window.addEventListener('load',post);"
    b"window.addEventListener('resize',post);"
    b"if(window.ResizeObserver){"
    b"new ResizeObserver(post).observe(document.body);"
    b"}"
    b"})();"
    b"</script>"
)


# Case-insensitive matchers for the HTML structure points. HTML5 is
# case-insensitive for tag names; an agent might emit <HEAD> or <Body>.
_HEAD_OPEN = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE = re.compile(rb"</head\s*>", re.IGNORECASE)
_BODY_OPEN = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE = re.compile(rb"</body\s*>", re.IGNORECASE)
_VIEWPORT = re.compile(rb'<meta[^>]+name=["\']viewport["\']', re.IGNORECASE)


def _insert_after(html: bytes, match: re.Match[bytes], insertion: bytes) -> bytes:
    end = match.end()
    return html[:end] + insertion + html[end:]


def _insert_before(html: bytes, match: re.Match[bytes], insertion: bytes) -> bytes:
    start = match.start()
    return html[:start] + insertion + html[start:]


def _inject_shim(html: bytes) -> bytes:
    if SHIM_MARKER in html:
        return html
    head_open = _HEAD_OPEN.search(html)
    if head_open is not None:
        return _insert_after(html, head_open, CSS_SHIM)
    body_open = _BODY_OPEN.search(html)
    if body_open is not None:
        return _insert_after(html, body_open, CSS_SHIM)
    # No head, no body — prepend.
    return CSS_SHIM + html


def _inject_viewport(html: bytes) -> bytes:
    if _VIEWPORT.search(html):
        return html
    head_open = _HEAD_OPEN.search(html)
    if head_open is not None:
        return _insert_after(html, head_open, VIEWPORT_META)
    body_open = _BODY_OPEN.search(html)
    if body_open is not None:
        return _insert_before(html, body_open, VIEWPORT_META)
    return VIEWPORT_META + html


def _inject_resize_poster(html: bytes) -> bytes:
    if RESIZE_MARKER in html:
        return html
    body_close = _BODY_CLOSE.search(html)
    if body_close is not None:
        return _insert_before(html, body_close, RESIZE_POSTER)
    body_open = _BODY_OPEN.search(html)
    if body_open is not None:
        return html + RESIZE_POSTER  # append at end of doc
    return html + RESIZE_POSTER


def inject(html: bytes) -> bytes:
    """Idempotent injection of CSS shim, viewport meta, resize-poster."""
    out = _inject_shim(html)
    out = _inject_viewport(out)
    out = _inject_resize_poster(out)
    return out
