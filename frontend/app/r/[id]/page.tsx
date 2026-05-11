/**
 * /r/[id] — three-state surface.
 *
 * Scaffolded in M6.4. Wired in M6.5 (email-entry) + M6.7 (chrome).
 * Today: renders a placeholder so `next build` succeeds and the route
 * structure exists.
 */
export const dynamic = "force-dynamic";

type Props = { params: Promise<{ id: string }> };

export default async function ReportPage({ params }: Props) {
  const { id } = await params;
  return (
    <main style={{ padding: "var(--space-3xl)" }}>
      <h2 className="t-section-h">Majeve Reports</h2>
      <p className="t-body" style={{ color: "var(--fg-2)" }}>
        Viewer scaffold for report <code className="t-mono">{id}</code>. M6.5
        wires the email-entry form; M6.7 wires the chrome.
      </p>
    </main>
  );
}
