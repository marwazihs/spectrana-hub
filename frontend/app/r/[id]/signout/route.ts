/**
 * /r/[id]/signout — POST-only sign-out.
 *
 * POST (not GET) is the canonical sign-out shape: SameSite=Lax cookies are
 * sent on top-level GET navigations, so a `<a href>` link could be triggered
 * cross-site to log a user out. POST is not auto-fired by navigation, and
 * SameSite=Lax does not send the cookie on cross-site POSTs, so the action
 * is self-DoS-proof.
 *
 * Behavior:
 *   - Read the hub_session cookie. If present, call Hub to revoke server-side.
 *   - Clear the cookie on the browser regardless (matches anti-enum posture:
 *     the response is identical whether or not a session existed).
 *   - 303 See Other → /r/[id] so a refresh doesn't replay the POST.
 */

import { NextResponse, type NextRequest } from "next/server";

import { HUB_SESSION_COOKIE, revokeSession } from "@/lib/hub-client";

type RouteContext = { params: Promise<{ id: string }> };

export async function POST(
  req: NextRequest,
  context: RouteContext,
): Promise<NextResponse> {
  const { id: reportId } = await context.params;
  const sessionId = req.cookies.get(HUB_SESSION_COOKIE)?.value;

  if (sessionId) {
    try {
      await revokeSession(sessionId);
    } catch {
      // Hub error / network failure. Don't surface — the cookie clear below
      // is what gates the next request. Hub's session row is best-effort
      // cleanup; the worst case is an orphaned row that expires on its TTL.
    }
  }

  // Build the redirect URL from the inbound Host header rather than
  // `req.url`. Inside the Next.js container, `req.url` resolves to the
  // container bind address (e.g. http://0.0.0.0:3000), and redirecting
  // there sends the browser to a different origin than the one that
  // received the Set-Cookie clear — the browser appears not to have
  // signed out because it lands on an origin that never had the cookie.
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

  const redirect = NextResponse.redirect(redirectUrl, { status: 303 });
  redirect.headers.set("Cache-Control", "no-store");
  redirect.cookies.set({
    name: HUB_SESSION_COOKIE,
    value: "",
    httpOnly: true,
    secure: process.env.HUB_COOKIE_SECURE !== "0",
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return redirect;
}
