# gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:

- `/office-hours`
- `/plan-ceo-review`
- `/plan-eng-review`
- `/plan-design-review`
- `/design-consultation`
- `/design-shotgun`
- `/design-html`
- `/review`
- `/ship`
- `/land-and-deploy`
- `/canary`
- `/benchmark`
- `/browse`
- `/connect-chrome`
- `/qa`
- `/qa-only`
- `/design-review`
- `/setup-browser-cookies`
- `/setup-deploy`
- `/setup-gbrain`
- `/retro`
- `/investigate`
- `/document-release`
- `/codex`
- `/cso`
- `/autoplan`
- `/plan-devex-review`
- `/devex-review`
- `/careful`
- `/freeze`
- `/guard`
- `/unfreeze`
- `/gstack-upgrade`
- `/learn`

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore

## Design System

Always read `DESIGN.md` before making any visual or UI decision. All font choices, colors, spacing, radii, and aesthetic direction come from there (which in turn inherits from `colors_and_type.css` — Majeve's parent system, code-named Cohere v.alpha).

Do not redefine design tokens in Hub code. Import `colors_and_type.css` directly or copy its `:root` block verbatim.

Hub-specific rules (covered in DESIGN.md, summarized here):
- Light mode only in v1. Do not introduce dark surfaces.
- Iframe chrome is stacked (above + below the iframe), never sidebar.
- Mobile iframe is edge-to-edge, zero gutter.
- Magic-link submissions show identical generic text for success and failure (anti-enumeration).
- Hub server injects a CSS shim, viewport-meta tag, and resize-poster script into every served report. The agent (Spectra) is not constrained by an HTML contract.

In QA mode, flag any code that doesn't match DESIGN.md.
