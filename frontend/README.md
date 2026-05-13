# Spectrana Hub — Frontend

Next.js viewer chrome. Owns `/r/[id]` end-to-end. Calls Hub's `/internal/*`
endpoints server-to-server using `HUB_INTERNAL_TOKEN`.

## Setup

```bash
cp .env.example .env.local
# Fill in HUB_INTERNAL_TOKEN (must match Hub-side value)
bun install
bun run dev
```

Hub backend (FastAPI) must be running on `HUB_API_BASE_URL` (default
`http://localhost:8000`). Start it from the repo root:

```bash
cd ..
uv run uvicorn app.main:app --reload
```

## Routes

| Path | Handler | Status |
|---|---|---|
| `/r/[id]` (no cookie) | Email-entry form | scaffolded — M6.5 wires |
| `/r/[id]` (with cookie) | Chrome + iframe | scaffolded — M6.7 wires |
| `/r/[id]/consume?token=` | Consume + cookie set | scaffolded — M6.6 wires |
| `/` | 404 | done |

## Design tokens

`app/tokens.css` is a verbatim copy of the repo-root `colors_and_type.css`
(Cohere v.alpha). See DESIGN.md for the source-of-truth policy. Sync from
the root when the parent system updates — do not edit token values here.

Hub is light-mode only in v1.
