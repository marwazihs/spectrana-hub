/**
 * /r/[id]/consume?token=... — magic-link consume.
 *
 * The Next.js viewer takes responsibility for issuing the hub_session
 * cookie (host-only on the viewer origin). Hub stays the source of truth
 * for the session row — Next.js just receives an opaque session_id from
 * /internal/magic-link/consume and puts it in the cookie.
 *
 * On Hub's 410 (bad token, expired, replay, cross-customer, unknown
 * report) → render an inline expired-link page on this route, not a
 * redirect back to /r/[id]. The DESIGN.md "Link expired or already used"
 * surface lives here so the user sees the cause directly when they click
 * a stale email link.
 */

import { NextResponse, type NextRequest } from "next/server";

import {
  HUB_SESSION_COOKIE,
  clientIpFrom,
  consumeMagicLink,
} from "@/lib/hub-client";

type RouteContext = { params: Promise<{ id: string }> };

// In-memory dedup cache: when a magic link is consumed, several browser
// agents can race to GET the consume URL — Chrome speculation/prerender,
// macOS LaunchServices delivering the URL to multiple processes, link
// scanners, etc. Only the first reaches Hub successfully; the rest see
// 410 and render the expired-link page even though the legitimate user
// just clicked.
//
// We cache the consumed payload by token for a short window and re-issue
// the same cookie/redirect for duplicates. The cache key is the raw token,
// so only requests carrying the same single-use token benefit — a stolen
// or guessed token still has to pass Hub's check.
//
// Module-level Map persists for the life of the Next.js worker (long
// enough to cover the burst of duplicate requests, short enough that it
// doesn't act as a session store). The TTL is 90s — comfortably longer
// than any plausible burst, well under the magic-link TTL itself.
type CachedConsume = {
  session_id: string;
  expires_at: string;
  cached_at: number;
};
const CONSUME_DEDUP_TTL_MS = 90_000;
const consumeCache = new Map<string, CachedConsume>();

function getCachedConsume(token: string): CachedConsume | null {
  const hit = consumeCache.get(token);
  if (!hit) return null;
  if (Date.now() - hit.cached_at > CONSUME_DEDUP_TTL_MS) {
    consumeCache.delete(token);
    return null;
  }
  return hit;
}

function putCachedConsume(
  token: string,
  payload: { session_id: string; expires_at: string },
): void {
  consumeCache.set(token, { ...payload, cached_at: Date.now() });
  // Opportunistic GC: if the map grew unbounded, sweep expired entries.
  if (consumeCache.size > 256) {
    const cutoff = Date.now() - CONSUME_DEDUP_TTL_MS;
    for (const [k, v] of consumeCache) {
      if (v.cached_at < cutoff) consumeCache.delete(k);
    }
  }
}

export async function GET(
  req: NextRequest,
  context: RouteContext,
): Promise<NextResponse> {
  const { id: reportId } = await context.params;
  const token = req.nextUrl.searchParams.get("token");

  if (!token) {
    return renderExpiredPage(reportId);
  }

  // Belt: refuse browser speculation when the browser tells us. Modern
  // Chrome sends `Sec-Purpose: prefetch[;prerender]` for speculation-rule
  // fetches — returning 204 lets the real navigation hit a live token.
  const secPurpose = req.headers.get("sec-purpose") || "";
  if (secPurpose.includes("prefetch") || secPurpose.includes("prerender")) {
    return new NextResponse(null, {
      status: 204,
      headers: { "Cache-Control": "no-store" },
    });
  }

  // Suspenders: many duplicate-consume sources don't set Sec-Purpose
  // (macOS LaunchServices opening the URL in multiple processes, link
  // scanners, browser-back replays). If we just succeeded for this token
  // a moment ago, re-issue the same cookie+redirect instead of asking
  // Hub to consume an already-consumed token (which 410s).
  const cached = getCachedConsume(token);
  let consumed: { session_id: string; expires_at: string } | null;
  if (cached) {
    consumed = { session_id: cached.session_id, expires_at: cached.expires_at };
  } else {
    try {
      consumed = await consumeMagicLink(reportId, token, clientIpFrom(req.headers));
    } catch {
      return renderExpiredPage(reportId);
    }
    if (consumed === null) {
      return renderExpiredPage(reportId);
    }
    putCachedConsume(token, consumed);
  }

  // Compute the cookie max-age from Hub's reported expiry so the cookie
  // lifetime matches the session row exactly. If the value is malformed
  // (shouldn't happen — Hub returns an ISO datetime), fall back to a
  // safe one-day window rather than session-only.
  const expiresMs = Date.parse(consumed.expires_at);
  const maxAgeSec = Number.isFinite(expiresMs)
    ? Math.max(60, Math.floor((expiresMs - Date.now()) / 1000))
    : 24 * 3600;

  // Build the redirect target from the incoming Host header rather than
  // `req.url`. Inside the Next.js container, `req.url` resolves to the
  // container bind address (e.g. http://0.0.0.0:3000), and redirecting
  // there sends the browser to a different origin than the one that just
  // received the Set-Cookie — losing the session cookie. Honour the
  // forwarded host first (proxies), then Host.
  const forwardedHost = req.headers.get("x-forwarded-host");
  const host = forwardedHost || req.headers.get("host") || "";
  const proto =
    req.headers.get("x-forwarded-proto") ||
    (host.startsWith("localhost") || host.startsWith("127.")
      ? "http"
      : req.nextUrl.protocol.replace(":", ""));
  const redirectUrl = host
    ? `${proto}://${host}/r/${reportId}`
    : new URL(`/r/${reportId}`, req.url).toString();

  const redirect = NextResponse.redirect(redirectUrl, { status: 302 });
  redirect.headers.set("Cache-Control", "no-store");
  redirect.cookies.set({
    name: HUB_SESSION_COOKIE,
    value: consumed.session_id,
    httpOnly: true,
    secure: process.env.HUB_COOKIE_SECURE !== "0",
    sameSite: "lax",
    path: "/",
    maxAge: maxAgeSec,
  });
  return redirect;
}

function renderExpiredPage(reportId: string): NextResponse {
  // DESIGN.md S3 "Link expired or already used". Headline replaces the S1
  // form heading, CTA links back to /r/[id] for a fresh attempt. Status
  // 410 matches Hub's slug ("magic-link-expired-or-consumed").
  //
  // Token values inlined: this route emits HTML without going through the
  // Next.js CSS pipeline, so var() refs wouldn't resolve. Values mirror
  // colors_and_type.css verbatim and must be kept in sync if the parent
  // system updates (low risk — DESIGN.md S3 is stable surface).
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Majeve Reports</title>
<style>
  body { background: #eeece7; min-height: 100vh; margin: 0;
         display: flex; align-items: center; justify-content: center;
         font-family: "Inter", system-ui, sans-serif; padding: 24px; color: #212121; }
  .card { background: #ffffff; border: 1px solid #d9d9dd;
          border-radius: 16px; padding: 48px;
          max-width: 480px; width: 100%; }
  h1 { font: 400 48px/1.2 "Space Grotesk", "Inter", system-ui, sans-serif;
       letter-spacing: -0.48px; margin: 0 0 16px; color: #212121; }
  p { color: #616161; font-size: 18px; line-height: 1.4;
      margin: 0 0 24px; }
  a.cta { display: inline-block; height: 44px; padding: 0 16px;
          background: #17171c; color: #ffffff;
          border-radius: 8px; text-decoration: none;
          line-height: 44px; font-size: 14px; font-weight: 500; }
</style>
</head>
<body>
<main class="card">
<h1>This link has expired</h1>
<p>For security, magic links expire after 15 minutes and can only be used once.</p>
<a class="cta" href="/r/${escapeAttr(reportId)}">Request a new link</a>
</main>
</body>
</html>`;
  return new NextResponse(html, {
    status: 410,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function escapeAttr(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    c === "&"
      ? "&amp;"
      : c === "<"
        ? "&lt;"
        : c === ">"
          ? "&gt;"
          : c === '"'
            ? "&quot;"
            : "&#39;",
  );
}
