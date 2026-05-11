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

import { HUB_SESSION_COOKIE, consumeMagicLink } from "@/lib/hub-client";

type RouteContext = { params: Promise<{ id: string }> };

export async function GET(
  req: NextRequest,
  context: RouteContext,
): Promise<NextResponse> {
  const { id: reportId } = await context.params;
  const token = req.nextUrl.searchParams.get("token");

  if (!token) {
    return renderExpiredPage(reportId);
  }

  let consumed;
  try {
    consumed = await consumeMagicLink(reportId, token);
  } catch {
    // Hub error / network failure. Don't leak the cause — render the
    // expired-link page. Operational visibility is on the Hub side.
    return renderExpiredPage(reportId);
  }
  if (consumed === null) {
    return renderExpiredPage(reportId);
  }

  // Compute the cookie max-age from Hub's reported expiry so the cookie
  // lifetime matches the session row exactly. If the value is malformed
  // (shouldn't happen — Hub returns an ISO datetime), fall back to a
  // safe one-day window rather than session-only.
  const expiresMs = Date.parse(consumed.expires_at);
  const maxAgeSec = Number.isFinite(expiresMs)
    ? Math.max(60, Math.floor((expiresMs - Date.now()) / 1000))
    : 24 * 3600;

  const redirect = NextResponse.redirect(
    new URL(`/r/${reportId}`, req.url),
    { status: 302 },
  );
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
    headers: { "Content-Type": "text/html; charset=utf-8" },
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
