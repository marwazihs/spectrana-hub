# Design System — Spectrana Hub

## Product Context

- **What this is:** A magic-link-gated report archive. Spectra (Majeve's AI analyst agent) POSTs reports to Hub; customers retrieve them at stable URLs after email verification.
- **Who it's for:** SMB ops/finance leaders (the human viewer) and Spectra/other Majeve agents (the API caller).
- **Space/industry:** Enterprise AI, financial/operational analyst reports for SMB customers.
- **Project type:** Trust-first reading surface. Three screens total: magic-link entry, report viewer chrome, error/empty states. **Not** a dashboard, **not** a marketing site.
- **Memorable thing:** "This is Majeve's serious data archive." Institutional, banky, warm — never playful, never breezy.

## Source of Truth: Cohere v.alpha

Hub inherits Majeve's parent design system. **All tokens (colors, type, spacing, radii, fonts) come from `colors_and_type.css` in the repo root.** Do not redefine these in Hub code. Import the stylesheet directly, or copy the `:root` block verbatim.

This document only specifies:
1. How the parent system maps onto Hub's specific surfaces.
2. Hub-only decisions the parent system doesn't cover (iframe chrome, trust copy, mobile collapse, dark mode policy).
3. Hub's server-side responsibilities for guaranteeing report rendering quality.

If a token or pattern is needed and is not in `colors_and_type.css`, that's a signal it belongs in the parent system — not invented here.

## Aesthetic Direction

- **Direction:** Institutional but warm. The banky/serious posture comes from Majeve's brand anchors (near-black `--color-primary`, deep-green, dark-navy). The warmth comes from `--color-soft-stone` and pale tints used as entry-page surfaces, not cold pure-white.
- **Decoration level:** Minimal. Typography and whitespace carry the work. No gradients, no shadows beyond hairlines, no decorative blobs.
- **Mood:** Quiet competence. The chrome disappears so the report content reads cleanly.
- **Reference posture:** Closer to Stripe Dashboard / Linear settings panels than to Bloomberg Terminal. Restrained enterprise AI surface, not data-dense fintech.

## Surfaces (Screen → Token Mapping)

### S1 — Magic-link entry (`/r/{report_id}` unauthed)

| Element | Token / value |
|---|---|
| Page background | `--bg-stone` (`#eeece7`) |
| Card | `--bg-canvas`, `--border-hairline`, `--radius-md` (16px), centered, max-width 480px |
| Card padding | `--space-3xl` (48px) on desktop, `--space-xl` (24px) on mobile |
| Headline | `.t-section-h` (48px Space Grotesk), `--fg-1` |
| Subhead / explainer | `.t-body-l` (18px Inter), `--fg-2` |
| Input | Inter 16px, `--border-hairline`, `--radius-sm` (8px), 44px tall, `--color-form-focus` on focus |
| Primary CTA | `.t-button` on `--color-primary` (`#17171c`), `--color-on-primary` text, `--radius-sm`, 44px tall |
| Footer trust line | `.t-micro` (12px), `--fg-3`, single line: "Majeve · Encrypted in transit · `<report_id-short>`" |

**Composition:** single centered card, vertical stack. Nothing else on the page. No top nav, no logo wordmark (the headline establishes brand context).

**Mobile (≤768px):** card becomes full-width with 24px gutters; padding drops to `--space-xl`; headline drops to `.t-card` (32px).

### S2 — Report viewer chrome (`/r/{report_id}` authed)

| Element | Token / value |
|---|---|
| Page background | `--bg-canvas` |
| Outer column | Centered, `max-width: 1200px` on desktop; full width on mobile |
| Header bar | `--space-lg` (16px) vertical padding, `--border-hairline` bottom |
| Report title | `.t-card` (32px on desktop, 24px on mobile) |
| Metadata row | `.t-caption` (14px), `--fg-3`: `<customer_name> · <generated_at>` |
| Sign-out link | `.t-button` ghost variant, top-right |
| Iframe container | `--border-hairline` 1px around, `--radius-xs` (4px) corners, no shadow |
| Iframe height | Set dynamically via resize-poster (see "Hub Server Responsibilities") |
| Supplementary files | Below iframe. Each row: `.t-mono` filename + `.t-caption` size, link in `--color-action-blue` |
| Footer | `.t-micro`, `--fg-3`, same trust line as S1 |

**Composition: stacked, never sidebar.** Header bar above the iframe, supplementary files + footer below. No element sits beside the iframe — that would crop the report's responsive HTML at unpredictable widths.

**Mobile (≤768px):**
- Outer column gets 16px gutters for header/footer/supplementary
- **Iframe is edge-to-edge, zero gutter.** Every pixel goes to the report content. A 375px viewport gives the report 375px, not 343px.
- Header collapses customer + date onto one line under the title
- Sign-out moves to a single-icon button (text-only "Sign out" link is fine; no hamburger menu needed — there's nothing else to navigate to)

**Desktop (>768px):**
- Iframe sits in the 1200px-max column. Chrome and iframe share the same horizontal bounds.

### S3 — Error / empty / generic responses

- "If this email is on file, a link has been sent" — generic, identical for success and failure (anti-enumeration, per security spec). Renders on S1 below the input. `--fg-2`, `.t-body`.
- "Link expired or already used" — renders on S1's layout with the generic message replaced. Action: "Request a new link." Same input, same submit, same generic response on retry.
- 404 / report not found / report deleted — same minimal layout as S1, headline "This report isn't available," subhead "It may have been removed or the link is incorrect," no input field.

## Hub Server Responsibilities (the "perfect like original" guarantee)

The report HTML is generated by Spectra and is responsive by default. Hub does not impose rules on the agent. Instead, Hub guarantees three things at serve time so the report renders perfectly inside the iframe at any viewport between 320px and 1200px:

### 1. CSS Shim Injection

Hub injects this `<style>` block immediately after `<head>` (or at the start of `<body>` if `<head>` is absent):

```css
<style data-hub-shim="v1">
  :root { font-size: 16px; -webkit-text-size-adjust: 100%; }
  *, *::before, *::after { box-sizing: border-box; }
  body { margin: 0; }
  img, video, svg { max-width: 100%; height: auto; }
  table { display: block; max-width: 100%; overflow-x: auto; }
  pre, code { overflow-x: auto; word-wrap: break-word; white-space: pre-wrap; }
</style>
```

Idempotent: detection is a string search for `data-hub-shim="v1"`. Inject only if absent.

### 2. Viewport Meta Injection

If the report `<head>` does not contain `<meta name="viewport">`, Hub injects:

```html
<meta name="viewport" content="width=device-width, initial-scale=1">
```

The iframe document has its own viewport, independent of Hub's chrome. This is what makes the report render at the iframe's actual width instead of the desktop default.

### 3. Resize Poster Injection

Hub injects this `<script>` immediately before `</body>` (or at end of `<body>` if `</body>` is absent), detected via `data-hub-resize="v1"`:

```html
<script data-hub-resize="v1">
  (function () {
    function postHeight() {
      var h = document.documentElement.scrollHeight;
      parent.postMessage({ type: 'hub-resize', height: h }, '*');
    }
    window.addEventListener('load', postHeight);
    window.addEventListener('resize', postHeight);
    if ('ResizeObserver' in window) {
      new ResizeObserver(postHeight).observe(document.body);
    }
  })();
</script>
```

Hub's parent page listens for `message` events of type `hub-resize` and sets `iframe.style.height = event.data.height + 'px'`. Result: one continuous page scroll, no nested scrollbars, iframe height always matches content height. The report behaves identically to opening the original HTML in a browser.

### Why this approach

- Spectra produces responsive HTML by default — Hub trusts that and fills in the common gaps.
- Zero rules to maintain in Spectra's prompt. Zero coordination between teams.
- Three injections (~30 lines of server code), all idempotent, all detected by marker attributes.
- When Spectra HTML has all three already, Hub serves it untouched.

The trade-off: an agent that generates fixed-pixel-width containers (`width: 1400px`) will still overflow on narrow viewports. If that becomes a real problem, the shim can grow one rule: `[style*="width"] { max-width: 100% !important }`. Defer until observed.

## Trust & Anti-Enumeration Patterns

These are not generic design choices — they are required by the security spec.

- **Single message for all magic-link submissions.** Identical text whether the email is on the allowlist or not, whether the report exists or not, whether it was already deleted. "If this email is on file, a link has been sent."
- **No green checkmark, no success icon, no celebratory state.** Submission acknowledgment is `--fg-2` body text below the input, not a toast or modal.
- **No "did you mean..." suggestions.** Reveals nothing about which addresses are valid.
- **Rate-limit feedback is generic too.** "Too many attempts, try again later" — no specific count, no specific window.

## Motion

- **Minimal-functional only.** Focus ring fades in 150ms ease-out. Button background shifts 100ms on hover. Link underlines appear instantly.
- **No entrance animations.** No scroll choreography. No skeleton shimmer (page is server-rendered; show nothing until ready, then show everything).
- **The iframe content is allowed any motion the agent chose.** Hub does not constrain or animate iframe boundaries.

## Dark Mode Policy

**Deferred to v2. Hub is light-mode only in v1.**

Reasons:
1. Cohere v.alpha doesn't define dark surfaces. Inventing dark tokens for Hub risks drift from the parent brand.
2. Report HTML is itself light (white backgrounds, dark text). Wrapping it in dark chrome would create a jarring visual seam at the iframe boundary.
3. Trust UI typically performs better in light mode in this category (Stripe, Carta, Bloomberg's auth flows are all light).

Revisit when Cohere v.alpha adds dark mode at the parent level.

## Browser Support

- Chrome / Safari / Firefox / Edge, last 2 versions.
- Mobile Safari (iOS 15+), Chrome Android (last 2 versions).
- No IE 11. No legacy Edge. The customer base is SMB ops/finance on modern devices; not worth the cost.

## What's Explicitly Out of Scope for v1

- Custom branding per customer (no per-tenant logos, palette overrides, or domain customization)
- Search UI inside the viewer (search is agent-only via the API)
- Report comments, annotations, sharing UI, print-optimized stylesheets
- Email-template design (magic-link emails inherit Spectra's existing transactional email template)
- Loading states beyond a generic "Loading..." with `.t-body` muted text (the iframe is the loader)

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-10 | Inherit Cohere v.alpha; do not re-design | Hub is one of many Majeve surfaces. Brand consistency across Spectra-delivered emails and Hub-rendered reports requires shared tokens. |
| 2026-05-10 | Stacked composition (chrome above + below iframe), never sidebar | Sidebar would crop the iframe horizontally and break agent-generated responsive HTML at unpredictable widths. |
| 2026-05-10 | Mobile iframe is edge-to-edge, zero gutter | A 375px viewport with 16px gutters leaves only 343px for the report. Edge-to-edge gives the agent every pixel. |
| 2026-05-10 | Hub injects CSS shim + viewport-meta + resize-poster; no agent rules | Hub guarantees rendering quality at serve time. Spectra and future agents are not burdened with a contract to maintain. |
| 2026-05-10 | No success state on magic-link submission | Anti-enumeration security requirement: identical response for valid and invalid emails. |
| 2026-05-10 | Light mode only in v1; dark mode deferred to v2 | Parent system has no dark tokens; report HTML is light; trust UI conventions favor light. |

## Related Documents

- `colors_and_type.css` — Source of truth for all tokens. Cohere v.alpha.
- `PLAN.md` — Implementation plan. §15 (frontend) needs rewriting to reference this document.
- `~/.gstack/projects/marwazihs-spectrana-hub/marwazisiagian-master-design-20260510-170051.md` — Original /office-hours design doc. Source for product scope, security spec, MVP boundaries.
