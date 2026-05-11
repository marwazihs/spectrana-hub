"use client";

import { useEffect, useRef, useState } from "react";

/**
 * S2 chrome — stacked header above, iframe in the middle, footer trust
 * line below (DESIGN.md "Stacked composition, never sidebar").
 *
 * Iframe height tracks content via the resize-poster M4.1 injects into
 * the served HTML. The poster posts {type: "hub-resize", height} on
 * load + resize + ResizeObserver fires; we set iframe.style.height
 * to Math.ceil(h) + 1 to absorb sub-pixel rounding (M4.1 documented
 * the +1 contract).
 *
 * On mobile (≤768px) the iframe goes edge-to-edge while the header,
 * footer, and supplementary section keep 16px gutters — DESIGN.md S2
 * mobile rules.
 */

export function Chrome({
  reportId,
  reportTitle,
  customerName,
  generatedAt,
  iframeSrc,
}: {
  reportId: string;
  reportTitle: string;
  customerName: string;
  generatedAt: string;
  iframeSrc: string;
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [height, setHeight] = useState<number>(640);

  useEffect(() => {
    // Only accept resize messages from the configured reports origin. The
    // browser delivers all postMessage calls to all listeners, so origin
    // is the only thing keeping a malicious top-frame neighbor from
    // resizing our iframe. We rebuild the expected origin from the env-
    // injected hostname rather than trusting iframe.contentWindow (which
    // could be spoofed on cross-origin sub-resources).
    const expectedOrigin = new URL(iframeSrc).origin;

    function onMessage(ev: MessageEvent) {
      if (ev.origin !== expectedOrigin) return;
      const data = ev.data;
      if (
        data &&
        typeof data === "object" &&
        (data as { type?: unknown }).type === "hub-resize" &&
        typeof (data as { height?: unknown }).height === "number"
      ) {
        const h = (data as { height: number }).height;
        // +1 absorbs sub-pixel rounding that produces a stray scrollbar.
        // Match the M4.1 chrome-template behavior so users moving between
        // the old Jinja chrome and this one see identical sizing.
        setHeight(Math.ceil(h) + 1);
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [iframeSrc]);

  const generatedLabel = formatGeneratedAt(generatedAt);

  return (
    <div style={pageStyle}>
      <header style={headerStyle}>
        <div style={headerLeftStyle}>
          <h1 data-hub-chrome="title" style={titleStyle}>
            {reportTitle}
          </h1>
          <p style={metaStyle}>
            <span>{customerName}</span>
            <span aria-hidden="true"> &middot; </span>
            <span>{generatedLabel}</span>
          </p>
        </div>
        <a href={`/r/${reportId}/signout`} style={signoutStyle}>
          Sign out
        </a>
      </header>

      <div data-hub-chrome="iframe-wrap" style={iframeWrapStyle}>
        <iframe
          ref={iframeRef}
          src={iframeSrc}
          title="Report"
          // No allow= / sandbox= here: the iframe is on a different origin
          // already (separate hostname), so the browser enforces process
          // isolation. The iframe's CSP (set by Hub on /render/{id}) is
          // the real boundary, not iframe attributes.
          style={{ ...iframeStyle, height: `${height}px` }}
        />
      </div>

      <footer style={footerStyle}>
        Majeve &middot; Encrypted in transit &middot;{" "}
        <code style={footerMonoStyle}>{reportId.slice(0, 8)}</code>
      </footer>
    </div>
  );
}

const pageStyle: React.CSSProperties = {
  minHeight: "100vh",
  background: "var(--bg-canvas)",
  color: "var(--fg-1)",
  display: "flex",
  flexDirection: "column",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: "var(--space-lg)",
  padding: "var(--space-lg)",
  paddingLeft: "max(var(--space-lg), env(safe-area-inset-left))",
  paddingRight: "max(var(--space-lg), env(safe-area-inset-right))",
  borderBottom: "var(--border-hairline)",
  maxWidth: "1200px",
  width: "100%",
  margin: "0 auto",
  boxSizing: "border-box",
};

const headerLeftStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-xs)",
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  fontSize: "var(--t-card-size)",
  lineHeight: "var(--t-card-lh)",
  letterSpacing: "var(--t-card-ls)",
  fontFamily: "var(--font-ui)",
  fontWeight: 400,
  margin: 0,
};

const metaStyle: React.CSSProperties = {
  fontSize: "var(--t-caption-size)",
  lineHeight: "var(--t-caption-lh)",
  color: "var(--fg-3)",
  margin: 0,
};

const metaMonoStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  textTransform: "uppercase",
  letterSpacing: "var(--t-mono-ls)",
};

function formatGeneratedAt(iso: string): string {
  // Locale-aware, but stable for testability: use Intl with explicit
  // options rather than toLocaleString() defaults. The viewer is a
  // single-language surface in v1 (en), so en-US is acceptable.
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

const signoutStyle: React.CSSProperties = {
  fontSize: "var(--t-button-size)",
  fontWeight: 500,
  color: "var(--fg-2)",
  textDecoration: "none",
  flexShrink: 0,
};

const iframeWrapStyle: React.CSSProperties = {
  // On desktop the iframe sits inside the 1200px max-width column with a
  // hairline border around it. On mobile the parent goes edge-to-edge,
  // overriding max-width via CSS below. The container is the chrome
  // boundary; the iframe itself just fills it.
  width: "100%",
  maxWidth: "1200px",
  margin: "0 auto",
  padding: "var(--space-lg)",
  paddingLeft: "max(var(--space-lg), env(safe-area-inset-left))",
  paddingRight: "max(var(--space-lg), env(safe-area-inset-right))",
  boxSizing: "border-box",
};

const iframeStyle: React.CSSProperties = {
  width: "100%",
  border: "var(--border-hairline)",
  borderRadius: "var(--radius-xs)",
  display: "block",
  // height is set dynamically by the resize-poster listener
};

const footerStyle: React.CSSProperties = {
  fontSize: "var(--t-micro-size)",
  lineHeight: "var(--t-micro-lh)",
  color: "var(--fg-3)",
  textAlign: "center",
  padding: "var(--space-xl) var(--space-lg)",
  maxWidth: "1200px",
  width: "100%",
  margin: "0 auto",
  boxSizing: "border-box",
};

const footerMonoStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  textTransform: "uppercase",
  letterSpacing: "var(--t-mono-ls)",
};
