# Dokploy Deployment — Tracker & Handoff

**Purpose:** single source of truth for deploying Spectrana Hub to Dokploy via Docker Compose. Any Claude Code session (or human) picking this up should read this file first, then continue from the first unchecked item in [Tracker](#tracker).

**Branch:** work on `develop`; merge to `master` only after the deploy passes smoke test (Dokploy auto-deploy will track `master`).

**Last updated:** 2026-09-13

---

## Decisions (locked)

| # | Topic | Decision | Notes |
|---|---|---|---|
| D1 | Dokploy app type | **Docker Compose** (not Stack) | Stack mode doesn't support `build:` |
| D2 | Compose file | New `docker-compose.prod.yml`; local `docker-compose.yml` unchanged | Dokploy *Compose Path* = `./docker-compose.prod.yml` |
| D3 | Viewer domain | `hub.majie.ai` → `frontend:3000` | |
| D4 | Agent API | `hub.majie.ai/api` → `hub:8000` with **Strip Path** | No code change; agents call `https://hub.majie.ai/api/v1/reports` |
| D5 | Reports (iframe) domain | `reports.hub.majie.ai` → `hub:8000` | Confirmed by owner. Must be a different origin from D3 |
| D6 | Postgres | Dokploy-managed Postgres in the same Dokploy project | Owner provides connection string; use internal host. Driver must be `postgresql+asyncpg://` |
| D7 | Object storage | MinIO inside the compose stack (named volume) | Revisit external S3/R2 later |
| D8 | Client-IP fix | Fix before go-live | See [Code change: client IP](#code-change-client-ip) |
| D9 | Networking | Enable **Isolated Deployments** in Dokploy | Keeps MinIO off the shared `dokploy-network` |
| D10 | Replicas | `hub` = 1 | APScheduler nightly sweep runs in-process |
| D11 | MinIO image | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` (pinned) | Docker Hub `minio/minio` and `minio/mc` are **no longer pullable** (verified 2026-09-13). Quay image is official but frozen (no updates since 2025-09). Maintained fork alternative: `pgsty/minio`. `bucket-init` reuses the MinIO image (ships `mc`) |
| D12 | Service naming | MinIO service is `spectrana-minio` | `hub` joins shared `dokploy-network` for Postgres; a generic `minio` name could DNS-collide with another project |
| D13 | SMTP | Required (`:?`) in prod compose. Brevo relay (`smtp-relay.brevo.com:587`, STARTTLS), same credentials as local `.env` | Deploy fails fast rather than silently not sending magic links |
| D14 | Sender | `HUB_EMAIL_FROM=marginleak@majie.ai`, `HUB_EMAIL_BRAND_NAME="MarginLeak Report"` | Sender/domain must be verified in Brevo, or mail is rejected |
| D15 | DNS | No Cloudflare proxy (DNS-only) | Client IP seen by Traefik is the real visitor |
| D16 | First deploy branch | `develop` | Switch Dokploy to `master` after Phase 3 passes (4.3) |
| D17 | Env injection | Named `${VAR:?}` references per service, **not** `env_file: .env` | Least privilege (MinIO/frontend don't get DB/SMTP secrets) + deploy fails on missing values. Accepted cost: secret scanners may flag `PASSWORD: ${...}` lines as false positives — mark as false positive, don't restructure |

## Open questions

- [x] **Q1 — Reports domain.** `reports.hub.majie.ai` (D5).
- [x] **Q2 — Cloudflare.** No proxy (D15).
- [ ] **Q3 — Managed Postgres reachability with Isolated Deployments.** Dokploy managed DBs live on `dokploy-network`, so `hub` and `migrate` join it explicitly in `docker-compose.prod.yml`. Verify on first deploy (3.1) that `migrate` connects.
- [x] **Q4 — SMTP provider.** Brevo, same as local `.env` (D13, D14). Owner to confirm `marginleak@majie.ai` is a verified Brevo sender.
- [x] **Q5 — MinIO image.** Keep the frozen official quay image (D11). Fallback if it misbehaves: owner provides Google Cloud Storage (S3-compatible interop) — set `AWS_S3_ENDPOINT_URL=https://storage.googleapis.com` + HMAC keys and drop `spectrana-minio`/`bucket-init`.

---

## Tracker

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

### Phase 1 — Code & config (repo, on `develop`)
- [x] 1.1 Research Dokploy compose docs (findings in [Dokploy facts](#dokploy-facts-verified-against-docs))
- [x] 1.2 Client-IP fix — Hub (`app/client_ip.py`, used by `render.py` + `internal/api.py`)
- [x] 1.3 Client-IP fix — frontend forwards `X-Forwarded-For` (request-link action + consume route)
- [x] 1.4 Tests for client-IP fix
- [x] 1.5 Run backend test suite + frontend typecheck, all green (194 passed; `tsc --noEmit` OK)
- [x] 1.6 Write `docker-compose.prod.yml`
- [x] 1.7 `docker compose -f docker-compose.prod.yml config` validates; local Dokploy-like smoke test passed (fake `dokploy-network` + throwaway Postgres: migrate/bucket-init exit 0, hub healthz, CLI create, publish 201 to MinIO, frontend 200; torn down)
- [x] 1.8 Add `.env.dokploy.example` (the env var list to paste into Dokploy)
- [x] 1.9 Commit on `develop`, push

### Phase 2 — Dokploy setup (owner, in Dokploy UI)
- [x] 2.1 DNS: A records for `hub.majie.ai` and reports domain → Dokploy server IP (**before** adding domains)
- [x] 2.2 Create managed Postgres in the project (database or user name must contain `hub`); copy **internal** connection string, change scheme to `postgresql+asyncpg://`
- [x] 2.3 Create Compose app: GitHub source `marwazihs/spectrana-hub`, branch **`develop`** (D16), Compose Path `./docker-compose.prod.yml`
- [x] 2.4 Enable Isolated Deployments
- [x] 2.5 Paste env vars (from `.env.dokploy.example`, with real secrets)
- [x] 2.6 Add domains (HTTPS on, Let's Encrypt): D3, D4 (path `/api`, strip path), D5
- [x] 2.7 Deploy

### Phase 3 — Verify

Live facts (2026-09-13): server `hostinger-vps` (`srv01-elgean`, 72.62.80.76, single swarm node, Dokploy v0.29.13). Compose project `majie-hub-hubmain-8fvnkj`; managed DB service `majie-hub-hubmajiedb-a4ltpm` (**postgres:18**, db `majiehubdb`). Let's Encrypt certs valid to 2026-12-12 on both hosts; HTTP→HTTPS 301 works.
- [x] 3.1 `migrate` container exited 0 (Logs tab)
- [x] 3.2 `https://hub.majie.ai/api/healthz` → `{"status":"ok"}`
- [x] 3.3 Create first customer via `hub` container terminal — test customer **Demo Bistro** (`b7f9350d-240f-46d0-88d7-3e547ff65a66`, allowlist `marwazihs@gmail.com`)
- [x] 3.4 Publish a report via `https://hub.majie.ai/api/v1/reports`; returned `url` loads — sample report `01a09cc5-9068-7ad1-a071-f6164a21830a` for Demo Bistro: 201, API get/list OK, stored in MinIO, viewer URL 200
- [~] 3.5 Magic link by email arrives; sign in; iframe renders (desktop + phone) — **desktop ✅** (email from `marginleak@majie.ai` via Brevo received, consume → session → iframe JWT → render all 200). Phone: pending
- [x] 3.6 Rate-limit sanity: `events` rows show real client IPs, not a container IP — request came via frontend container `10.0.1.169`, but `magic_link_issued`/`magic_link_consumed` and `rate_limit_hits` recorded the owner's real public IP. **Client-IP fix verified in production**
- [x] 3.7 Render guards: no token → 422, bad token → 401, `/render` via primary host → 404; iframe renders inside viewer with a valid JWT

### Phase 4 — Harden & hand over
- [ ] 4.1 Postgres backups (managed DB → S3 schedule) + test restore
- [ ] 4.2 MinIO volume backup (Volume Backups) schedule
- [ ] 4.3 Switch Dokploy branch to `master`; enable auto-deploy
- [ ] 4.4 Update README "Setup and Onboarding" with Dokploy notes (terminal instead of `docker compose exec`)
- [ ] 4.5 Merge `develop` → `master`

---

## Session log

Append one line per session: date, what was done, where it stopped.

- 2026-09-13 — Analysis + decisions D1–D13. Client-IP fix + tests, `docker-compose.prod.yml`, `.env.dokploy.example`, local smoke test. Next: 1.9 (commit/push), then owner answers Q1–Q5 and starts Phase 2.
- 2026-09-13 — Owner answered Q1, Q2, Q4, Q5 (D14–D16). Pushed `develop`. Next: owner runs Phase 2 in Dokploy.
- 2026-09-13 — First deploy failed: (1) `DATABASE_URL` scheme `postgresql://` → `No module named 'psycopg2'`; (2) managed Postgres created in UI but never deployed (no swarm service). Owner deployed DB + fixed scheme; redeploy OK: migrate/bucket-init exit 0, hub+frontend up. External checks pass (healthz, viewer 200, API 401 without key, render guards, certs). Next: 3.3–3.6.
- 2026-09-13 — Created test customer Demo Bistro, published sample report, owner signed in on desktop via emailed magic link. Verified client-IP fix in prod DB. Next: 3.5 phone check, then Phase 4.
- 2026-09-13 — GitGuardian false positives on placeholders: blanked `.env.dokploy.example` secrets, bare `${VAR:?}` credential refs, `mc` keys via stdin (`a0c387b`). CI green. Decided D17. Next: owner runs Phase 2 in Dokploy; mark old GitGuardian incidents as false positive.

---

## Code change: client IP

**Bug.** Per-IP rate limit (`RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN`, 5/min) on the public magic-link form keyed on the wrong IP:
1. Browser → Traefik → Next.js → Hub. Next.js didn't forward the client IP, so Hub saw the **frontend container IP** for every visitor → all users shared one bucket.
2. Hub took the **leftmost** `X-Forwarded-For` entry, which is client-controlled if any proxy appends rather than overwrites.

**Fix.**
- Hub: `app/client_ip.py::client_ip()` takes the **rightmost** non-empty `X-Forwarded-For` entry (the one written by the proxy directly in front of Hub), validates it with `ipaddress`, else falls back to the socket peer. Replaces the two duplicated `_client_ip` helpers.
- Frontend: `lib/hub-client.ts` reads the rightmost entry of the incoming `X-Forwarded-For` and forwards it on `request-link` and `magic-link/consume` calls.

**Why this is safe in this topology (verified):**
- Dokploy's Traefik entrypoints set no `forwardedHeaders` (checked `Dokploy/dokploy` `packages/server/src/setup/traefik-setup.ts`), so Traefik treats clients as untrusted: it drops client-supplied `X-Forwarded-*` and sets `X-Forwarded-For` to the real peer.
- Next.js 15 only sets `x-forwarded-for` if absent (`base-server.js`: `??=`), so it doesn't append Traefik's IP.
- Hub has no host port in prod; the only paths in are Traefik and the frontend container.
- IPs are stored as strings (rate-limit `scope_key`, events JSON payload), so an unexpected value can't cause a DB type error; invalid values fall back to the peer anyway.

**Known limits.** Behind Cloudflare proxy (Q2) the rightmost IP is Cloudflare's. Locally (hub on `:8000` exposed, no proxy) a client can still set the header — dev only.

---

## Dokploy facts (verified against docs)

Source: `github.com/Dokploy/website` docs (`apps/docs/content/docs/core/`), checked 2026-09-13. `D` = `https://docs.dokploy.com/docs/core`.

- Deploy runs `docker compose -p <appName> --env-file .env -f <composePath> up -d --build --remove-orphans`, repo re-cloned each deploy.
- UI env vars are written to `.env` next to the compose file but **not injected into containers** — reference them as `${VAR}` or use `env_file`. (`D/docker-compose`)
- `NEXT_PUBLIC_*` build args: `args: VAR: ${VAR}`. (`D/docker-compose/example`)
- Domains tab: per service + container port; Dokploy injects Traefik labels at deploy; **redeploy after domain changes**. (`D/docker-compose/domains`, `D/domains`)
- Don't use `ports: "3000:3000"`. (`D/troubleshooting/domains`)
- Don't set `container_name`. (`D/docker-compose/example`)
- A failing healthcheck makes the domain never route. (`D/troubleshooting/domains`)
- DNS A record must exist before adding domain, else no Let's Encrypt cert. (`D/troubleshooting/domains`)
- Named volumes persist across deploys and are the only thing Volume Backups support. (`D/docker-compose`, `D/volume-backups`)
- Isolated Deployments: per-app network, Traefik joined to it; no need to add `dokploy-network`. (`D/docker-compose/utilities`)
- A failed one-shot container (e.g. `migrate`) does **not** fail the Dokploy deploy; dependents just don't start (from Dokploy source, not docs).

## Known issues outside this task

- `/api/docs` (FastAPI Swagger UI) and `/api/openapi.json` are publicly reachable. Not a secret leak (routes are auth-guarded) but exposes the API surface; consider disabling docs in prod.
- Uvicorn access log records the full `/render/<id>?t=<iframe JWT>` URL. JWT TTL is 60s so exposure is small, but tokens in logs are a hygiene issue; consider filtering the query string from access logs.
- Managed DB runs **postgres:18**; code, CI, and tests use postgres:16. Migrations ran fine; watch for version-specific issues.
- Dokploy troubleshooting: a created database does nothing until **Deploy** is clicked on the database itself.

- Local `docker-compose.yml` still uses `minio/minio:latest` and `minio/mc:latest`, which can't be pulled on a fresh machine (works only where cached). Same fix as D11 when someone touches local dev.
- Local `.env` containing `HUB_HOSTNAME` makes `Settings()` fail (`extra="forbid"`), which breaks running pytest from the repo root. Workaround: run tests from a copy without `.env`.
- Publish response `url` hardcodes `https://` (`app/reports/service.py:75`) — correct in prod, wrong for local http.

## Runbook snippets

**Create a customer (Dokploy → Compose app → `hub` service → Terminal, or `docker exec` on the server):**
```bash
python -m scripts.manage_customer create --name "Acme" --emails alice@acme.com
```
Use plain `python`, **not** `uv run`: inside the prod image `uv run` syncs the dev dependency group into the container's venv at runtime (observed: "Installed 34 packages"). The venv is already on `PATH`.

**Generate a secret (≥32 bytes):**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```
