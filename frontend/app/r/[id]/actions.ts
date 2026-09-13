"use server";

import { headers } from "next/headers";

import { clientIpFrom, requestMagicLink } from "@/lib/hub-client";

/**
 * Server action for the email-entry form. The Next.js viewer is the
 * unauthenticated caller — Hub returns the anti-enum 200 regardless of
 * report existence, allowlist membership, or SMTP outcome. We don't
 * branch on the response; the page always shows the same generic
 * submitted-state message (DESIGN.md "Trust & Anti-Enumeration").
 *
 * `prevState` exists so the form can re-render with the submitted state
 * via useActionState() without a full navigation. The returned shape is
 * intentionally identical for success and the Hub-error fallback so a
 * transient backend hiccup never leaks whether the email was real.
 */
export type RequestLinkState = {
  submitted: boolean;
  error: false; // reserved for future shape; always false to match DESIGN.md
};

export async function submitRequestLink(
  reportId: string,
  _prev: RequestLinkState,
  formData: FormData,
): Promise<RequestLinkState> {
  const raw = formData.get("email");
  const email = typeof raw === "string" ? raw.trim() : "";

  // Client-side validation already enforces a min/shape via the input;
  // here we only filter empty submits so we don't waste a Hub round-trip.
  if (email.length < 3) {
    // Same submitted-state response — never tell the user their email
    // was malformed (anti-enum). A non-email string just won't be on any
    // allowlist anyway.
    return { submitted: true, error: false };
  }

  try {
    await requestMagicLink(reportId, email, clientIpFrom(await headers()));
  } catch {
    // Hub/transport bug. Still show the generic submitted state so the
    // failure mode looks identical to the success mode. Operational
    // visibility comes from Hub logs, not from the user.
  }
  return { submitted: true, error: false };
}
