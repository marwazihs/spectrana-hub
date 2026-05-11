"use client";

import { useActionState } from "react";
import { submitRequestLink, type RequestLinkState } from "./actions";

/**
 * S1 — Magic-link entry card per DESIGN.md.
 *
 * Single centered card on stone background. No logo wordmark (the headline
 * establishes brand context). On submit the action returns submitted=true
 * and we swap the input for the generic message — identical text whether
 * the email was allowlisted or not.
 */

const INITIAL: RequestLinkState = { submitted: false, error: false };

export function EmailEntryForm({ reportId }: { reportId: string }) {
  const action = submitRequestLink.bind(null, reportId);
  const [state, formAction, pending] = useActionState(action, INITIAL);

  return (
    <form action={formAction} aria-labelledby="entry-heading" style={formStyle}>
      <h1 id="entry-heading" className="t-section-h" style={headingStyle}>
        View your report
      </h1>
      <p className="t-body-l" style={subheadStyle}>
        Enter the email this report was shared with. We&rsquo;ll send a
        one-time link.
      </p>

      {state.submitted ? (
        <p className="t-body" role="status" style={submittedStyle}>
          If this email is on file, a link has been sent. The link is valid
          for 15 minutes and can be used once.
        </p>
      ) : (
        <>
          <label htmlFor="email" className="t-caption" style={labelStyle}>
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            inputMode="email"
            autoComplete="email"
            required
            minLength={3}
            maxLength={320}
            placeholder="you@company.com"
            disabled={pending}
            style={inputStyle}
          />
          <button
            type="submit"
            className="t-button"
            disabled={pending}
            style={buttonStyle}
          >
            {pending ? "Sending..." : "Send link"}
          </button>
        </>
      )}

      <p className="t-micro" style={trustLineStyle}>
        Majeve &middot; Encrypted in transit &middot;{" "}
        <code className="t-mono" style={trustMonoStyle}>
          {reportId.slice(0, 8)}
        </code>
      </p>
    </form>
  );
}

// Inline styles keep this scaffold dependency-free (no CSS modules, no
// tailwind). Tokens come from tokens.css. Values mirror DESIGN.md S1 table.

const formStyle: React.CSSProperties = {
  background: "var(--bg-canvas)",
  border: "var(--border-hairline)",
  borderRadius: "var(--radius-md)",
  padding: "var(--space-3xl)",
  maxWidth: "480px",
  width: "100%",
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-lg)",
};

const headingStyle: React.CSSProperties = {
  color: "var(--fg-1)",
  fontFamily: "var(--font-display)",
};

const subheadStyle: React.CSSProperties = {
  color: "var(--fg-2)",
};

const labelStyle: React.CSSProperties = {
  color: "var(--fg-2)",
  marginBottom: "calc(-1 * var(--space-md))",
};

const inputStyle: React.CSSProperties = {
  height: "44px",
  padding: "0 var(--space-md)",
  border: "var(--border-hairline)",
  borderRadius: "var(--radius-sm)",
  background: "var(--bg-canvas)",
  fontSize: "var(--t-body-size)",
};

const buttonStyle: React.CSSProperties = {
  height: "44px",
  border: "none",
  borderRadius: "var(--radius-sm)",
  background: "var(--color-primary)",
  color: "var(--color-on-primary)",
  cursor: "pointer",
};

const submittedStyle: React.CSSProperties = {
  color: "var(--fg-2)",
  padding: "var(--space-md) 0",
};

const trustLineStyle: React.CSSProperties = {
  color: "var(--fg-3)",
  textAlign: "center",
  marginTop: "var(--space-md)",
};

const trustMonoStyle: React.CSSProperties = {
  color: "var(--fg-3)",
};
