/**
 * Hub backend client. All calls are server-to-server — never imported into
 * client components. The internal token in the header must not reach the
 * browser, so every export here is implicitly server-only by being called
 * only from server components, route handlers, and server actions.
 *
 * The matching endpoints land in M6.5-M6.7:
 *   - requestMagicLink  -> POST /r/{id}/request-link (unauth, anti-enum)
 *   - consumeMagicLink  -> POST /internal/magic-link/consume
 *   - mintIframeJwt     -> POST /internal/iframe-jwt
 */

import "server-only";

function hubBase(): string {
  const base = process.env.HUB_API_BASE_URL;
  if (!base) throw new Error("HUB_API_BASE_URL is not set");
  return base.replace(/\/+$/, "");
}

function internalHeaders(): HeadersInit {
  const token = process.env.HUB_INTERNAL_TOKEN;
  if (!token) {
    // Fail loud in any environment — a misconfigured token is a hard
    // outage, not a soft fallback. Hub itself enforces the same rule.
    throw new Error("HUB_INTERNAL_TOKEN is not set");
  }
  return {
    "Content-Type": "application/json",
    "X-Hub-Internal-Token": token,
  };
}

export type RequestLinkResponse = {
  status: "accepted";
  message: string;
};

/**
 * Public Hub endpoint — always anti-enum 200, even when the email is not
 * on the allowlist or the report doesn't exist. The viewer renders the
 * same submitted-state message regardless.
 */
export async function requestMagicLink(
  reportId: string,
  email: string,
): Promise<RequestLinkResponse> {
  const r = await fetch(`${hubBase()}/r/${reportId}/request-link`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
    cache: "no-store",
  });
  if (!r.ok) {
    // Any non-200 here is a Hub/transport bug, not user error. Surface it
    // as an exception; the caller can render a generic error page so the
    // viewer never reveals the underlying failure.
    throw new Error(`Hub request-link failed: ${r.status}`);
  }
  return (await r.json()) as RequestLinkResponse;
}

export type ConsumeResponse = {
  session_id: string;
  customer_id: string;
  email: string;
  expires_at: string; // ISO 8601
};

/**
 * Consume a magic-link token. On any failure (bad token, expired, replay,
 * cross-customer, unknown report) Hub returns 410. Caller catches that
 * and renders the expired-link page; no other branch is needed.
 */
export async function consumeMagicLink(
  reportId: string,
  token: string,
): Promise<ConsumeResponse | null> {
  const r = await fetch(`${hubBase()}/internal/magic-link/consume`, {
    method: "POST",
    headers: internalHeaders(),
    body: JSON.stringify({ report_id: reportId, token }),
    cache: "no-store",
  });
  if (r.status === 410) return null;
  if (!r.ok) {
    throw new Error(`Hub consume failed: ${r.status}`);
  }
  return (await r.json()) as ConsumeResponse;
}

export type IframeJwtResponse = {
  token: string;
  ttl_seconds: number;
  report_title: string;
  customer_name: string;
  generated_at: string; // ISO 8601
};

/**
 * Mint a fresh 60s iframe JWT. Called every render of the chrome page.
 * 401 → caller renders the email-entry page (session is gone / cross-
 * customer / report deleted; same anti-enum surface as the no-session case).
 */
export async function mintIframeJwt(
  reportId: string,
  sessionId: string,
): Promise<IframeJwtResponse | null> {
  const r = await fetch(`${hubBase()}/internal/iframe-jwt`, {
    method: "POST",
    headers: internalHeaders(),
    body: JSON.stringify({ report_id: reportId, session_id: sessionId }),
    cache: "no-store",
  });
  if (r.status === 401) return null;
  if (!r.ok) {
    throw new Error(`Hub iframe-jwt failed: ${r.status}`);
  }
  return (await r.json()) as IframeJwtResponse;
}

export const HUB_SESSION_COOKIE = "hub_session";

/**
 * Revoke a session server-side. Idempotent on Hub: a missing row is not an
 * error. Caller still clears the cookie on the browser regardless of the
 * outcome — the cookie is what gates the next request.
 */
export async function revokeSession(sessionId: string): Promise<void> {
  const r = await fetch(`${hubBase()}/internal/session/revoke`, {
    method: "POST",
    headers: internalHeaders(),
    body: JSON.stringify({ session_id: sessionId }),
    cache: "no-store",
  });
  if (!r.ok) {
    // Surface the failure so the caller can decide. Sign-out itself doesn't
    // block on Hub success — we always clear the cookie — but a non-2xx is
    // worth logging on the route side.
    throw new Error(`Hub session revoke failed: ${r.status}`);
  }
}
