/**
 * /r/[id] — three-state surface.
 *
 *   No hub_session cookie         → email-entry card (S1)
 *   Cookie + Hub mints iframe JWT → chrome with iframe (S2)
 *   Cookie but Hub returns 401    → email-entry card (S1, anti-enum parity)
 *
 * Cross-customer mismatch and report-deleted both surface as 401 from
 * /internal/iframe-jwt, so they collapse into the no-cookie path. The
 * user sees the email-entry form in every failure mode — DESIGN.md
 * "Anti-Enumeration" parity with the M4 Jinja behavior.
 */

import { cookies } from "next/headers";

import { HUB_SESSION_COOKIE, mintIframeJwt } from "@/lib/hub-client";

import { Chrome } from "./chrome";
import { EmailEntryForm } from "./email-entry-form";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

export default async function ReportPage({ params }: Props) {
  const { id } = await params;
  const cookieJar = await cookies();
  const sessionId = cookieJar.get(HUB_SESSION_COOKIE)?.value;

  if (sessionId) {
    let jwt;
    try {
      jwt = await mintIframeJwt(id, sessionId);
    } catch {
      jwt = null;
    }
    if (jwt) {
      const reportsDomain = process.env.NEXT_PUBLIC_HUB_REPORTS_DOMAIN;
      if (!reportsDomain) {
        // Configuration error: don't render the chrome without knowing
        // where to load the iframe from. Fall through to the entry form
        // so the surface stays anti-enum.
        return renderEntry(id);
      }
      const proto = process.env.HUB_COOKIE_SECURE === "0" ? "http" : "https";
      const iframeSrc = `${proto}://${reportsDomain}/render/${id}?t=${jwt.token}`;
      return (
        <Chrome
          reportId={id}
          reportTitle={jwt.report_title}
          customerName={jwt.customer_name}
          generatedAt={jwt.generated_at}
          iframeSrc={iframeSrc}
        />
      );
    }
  }

  return renderEntry(id);
}

function renderEntry(id: string) {
  return (
    <main style={pageStyle}>
      <EmailEntryForm reportId={id} />
    </main>
  );
}

const pageStyle: React.CSSProperties = {
  minHeight: "100vh",
  background: "var(--bg-stone)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "var(--space-xl)",
};
