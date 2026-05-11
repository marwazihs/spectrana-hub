/**
 * /r/[id] — three-state surface.
 *
 * M6.5: unauthed state renders the email-entry card (S1). Chrome state
 * (S2) lands in M6.7. Until then, even a request that DOES have the
 * hub_session cookie falls through to email-entry — Hub will accept the
 * cookie when M6.7 swaps the branch.
 *
 * Stone background per DESIGN.md S1.
 */

import { EmailEntryForm } from "./email-entry-form";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

export default async function ReportPage({ params }: Props) {
  const { id } = await params;

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
