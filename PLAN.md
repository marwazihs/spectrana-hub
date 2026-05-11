# PLAN: Spectrana Hub — Magic-Link Wedge with Agent Search API

**Status:** Ready for implementation
**Branch:** master
**Generated:** 2026-05-10
**Sources:**
- Design doc: `~/.gstack/projects/marwazihs-spectrana-hub/marwazisiagian-master-design-20260510-170051.md` (status APPROVED, from /office-hours)
- Test plan: `~/.gstack/projects/marwazihs-spectrana-hub/marwazisiagian-master-eng-review-test-plan-20260510-183908.md`
- PRD (superseded): `PRD.md`

This plan is the implementation contract. It consolidates 18 decisions resolved during /plan-eng-review on top of the approved design doc. Where this plan disagrees with the design doc, this plan wins.

---

## 1. Overview

Spectrana Hub is a 3-4 week wedge that gives Spectra (Majeve's existing AI analytics product) a persistence layer for shipped reports. Reports get a stable URL, customers retrieve via magic-link, agents publish/list/search/get via API key. The wedge is the observation instrument: every report view, search, and magic-link event is logged so v2/v3 decisions are data-driven.

**Out of scope** (v2/v3 trigger table — see §11): collections, public/private toggles, multi-user inside a customer, admin panels, API-key UI, slug URLs, retention policies, mobile control panel, white-label custom domains.

---

## 2. Architecture

### 2.1 Stack (locked)

| Layer | Choice | Why |
|---|---|---|
| Language / framework | Python 3.12+ / FastAPI | Mirrors Spectra |
| ORM | SQLAlchemy 2.0 async + asyncpg | Mirrors Spectra |
| DB | Postgres 16 | Mirrors Spectra |
| Migrations | Alembic | Mirrors Spectra |
| Object store | AWS S3 via `aioboto3` | Async-native, no event-loop blocking |
| UUID | `uuid-utils` (UUID v7) | Time-sortable IDs, Rust-backed |
| Email | `aiosmtplib` + Jinja2 templates + DB-backed tokens | Mirrors Spectra's password-reset pattern |
| Frontend | Next.js 16 + React 19 + TS 5 + hand-written CSS against `colors_and_type.css` (Cohere v.alpha) | Mirrors Spectra's runtime; design system inherited from Majeve parent (DESIGN.md). No Tailwind/shadcn — surface too small to justify (3 pages). |
| Config | `pydantic-settings` Settings class | Single source of truth; mirrors Spectra |
| Errors | RFC 7807 Problem+JSON | Stable contract for AI-agent client |
| Scheduler | APScheduler | For nightly sweeps |
| Local dev | Docker Compose with MinIO (S3 emulator) + Postgres 16 | `AWS_S3_ENDPOINT_URL` switches between MinIO and real S3 |
| Test stack | pytest + pytest-asyncio + httpx.AsyncClient + testcontainers-python + Hypothesis | Production-faithful with property tests on agent contract |

### 2.2 Repo / deploy model (locked)

- **Standalone repo** (`spectrana-hub`). No code-share with `nuelo-spectra`. Re-vendor patterns; do not import.
- **Pattern 1 (Majeve-hosted)** is v1 default. Single domain per Hub instance via env vars.
- **Pattern 3 (customer self-hosted)** is supported by the same image but not promoted during the wedge. No SELFHOST docs in v1.
- **Pattern 2 (per-customer custom domains)** is v2 trigger ("Hub wins a customer Majeve wouldn't have won otherwise").

### 2.3 Module layout (feature-sliced)

```
app/
  main.py                  # FastAPI app, lifespan, middleware wiring
  config.py                # Settings(BaseSettings)
  db/
    base.py                # Base = declarative_base()
    session.py             # async engine + session factory
    mixins.py              # ExpiringMixin (Issue 9)
  reports/
    api.py                 # POST/GET/LIST/DELETE /v1/reports, GET /r/{id} viewer chrome
    models.py              # Report
    schemas.py             # Pydantic in/out shapes
    service.py             # S3 upload, FTS query, idempotency, etc.
    render.py              # /render/{id} iframe origin endpoint (separate router, separate origin in deploy)
  auth/
    api.py                 # magic-link request + consume endpoints
    models.py              # MagicLinkToken, Customer
    schemas.py
    service.py             # token mint/verify, iframe JWT mint/verify
    api_key.py             # Bearer middleware
  events/
    models.py              # Event
    service.py             # log_event(type, customer_id, payload)
  jobs/
    sweep.py               # cleanup_expired_rows + s3_orphan_sweep
tests/
  unit/                    # Pure functions, no DB
  api/                     # Endpoint tests via httpx.AsyncClient
  property/                # Hypothesis tests on agent contract
  e2e/                     # Multi-step flows
docs/
  config.md                # Env-var contract table (Pattern-3 self-hosters land here)
  api/
    errors.md              # RFC 7807 error type registry
alembic/
  versions/                # Migrations
docker-compose.yml         # Local dev: Postgres + MinIO + Hub
Dockerfile
pyproject.toml
.env.example
```

### 2.4 Deployment topology

```
                       Internet
                          │
                          ▼
                ┌─────────────────────┐
                │   Load balancer /   │
                │   TLS terminator    │
                │ (Caddy/ALB/Cloud LB)│
                └─────┬───────────┬───┘
                      │           │
       hub.majeve.com │           │ reports.hub.majeve.com
                      ▼           ▼
              ┌──────────────────────────┐
              │   Hub FastAPI container  │
              │   (Uvicorn workers)      │
              └─────┬────────────────┬───┘
                    │                │
                    ▼                ▼
             ┌─────────────┐   ┌──────────────┐
             │ Postgres 16 │   │   AWS S3     │
             │  (RDS/etc)  │   │ hub-reports- │
             └─────────────┘   │    prod      │
                               └──────────────┘
```

Single FastAPI container serves both domains. Routing by `Host` header inside FastAPI: requests for `HUB_REPORTS_DOMAIN` route to `app/reports/render.py`; everything else goes to the normal API + viewer routers. The separate origin is enforced at the LB so the browser sees two distinct origins → iframe sandbox works as intended.

---

## 3. Database schema

### 3.1 Tables

```sql
-- Customers (Majeve provisions via scripts/manage_customer.py CLI — see §5.4)
CREATE TABLE customers (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  allowlist_emails TEXT[] NOT NULL DEFAULT '{}',
  api_key_hash    TEXT NOT NULL,                   -- bcrypt; one key per customer in v1
  api_key_prefix  TEXT NOT NULL,                   -- first 8 chars for log debugging
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX customers_api_key_prefix_idx ON customers (api_key_prefix);

-- Reports
CREATE TABLE reports (
  id              UUID PRIMARY KEY,                -- UUID v7, generated by app
  customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  tags            TEXT[] NOT NULL DEFAULT '{}',
  generated_at    TIMESTAMPTZ NOT NULL,
  s3_key          TEXT NOT NULL,                   -- <customer_id>/<report_id>/index.html
  supplementary_files JSONB NOT NULL DEFAULT '[]', -- [{filename, s3_key, size_bytes, content_type}]
  size_bytes      BIGINT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  search_vector   tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(array_to_string(tags, ' '), '')), 'C')
  ) STORED
);
CREATE INDEX reports_pagination_idx ON reports (customer_id, generated_at DESC, id DESC);
CREATE INDEX reports_search_idx ON reports USING GIN (search_vector);
CREATE INDEX reports_tags_idx ON reports USING GIN (tags);

-- Magic-link tokens (TTL: 15 min via ExpiringMixin)
CREATE TABLE magic_link_tokens (
  token_hash      TEXT PRIMARY KEY,                -- HMAC(HUB_MAGIC_LINK_HASH_SECRET, raw_token)
  customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  email           TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  consumed_at     TIMESTAMPTZ
);
CREATE INDEX magic_link_tokens_expires_idx ON magic_link_tokens (expires_at);

-- Idempotency keys (TTL: 24h via ExpiringMixin)
CREATE TABLE idempotency_keys (
  customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  key             TEXT NOT NULL,
  response_body   JSONB NOT NULL,
  status_code     INT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (customer_id, key)
);
CREATE INDEX idempotency_keys_expires_idx ON idempotency_keys (expires_at);

-- Rate-limit hit counters (TTL: 1h via ExpiringMixin)
CREATE TABLE rate_limit_hits (
  scope_key       TEXT NOT NULL,                   -- "ip:1.2.3.4" or "report:<uuid>"
  window_start    TIMESTAMPTZ NOT NULL,            -- rounded down to window granularity
  count           INT NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (scope_key, window_start)
);
CREATE INDEX rate_limit_hits_expires_idx ON rate_limit_hits (expires_at);

-- Observation events (NO TTL in v1; queried directly via psql for v2/v3 trigger decisions)
CREATE TABLE events (
  id              BIGSERIAL PRIMARY KEY,
  event_type      TEXT NOT NULL,                   -- 'report_view' | 'search_query' | 'magic_link_issued' | 'magic_link_consumed'
  customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX events_type_time_idx ON events (event_type, occurred_at DESC);
CREATE INDEX events_customer_time_idx ON events (customer_id, occurred_at DESC);

-- Sessions (session cookies)
CREATE TABLE sessions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  email           TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,            -- sliding: 30d from last access
  last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);
```

### 3.2 ExpiringMixin (Issue 9)

```python
# app/db/mixins.py
class ExpiringMixin:
    __ttl__: timedelta  # subclass sets this

    @classmethod
    async def cleanup_expired(cls, session: AsyncSession) -> int:
        result = await session.execute(
            delete(cls).where(cls.expires_at < datetime.now(tz=UTC))
        )
        return result.rowcount
```

Models inheriting: `MagicLinkToken`, `IdempotencyKey`, `RateLimitHit`, `Session`. Not `Event` (no TTL in v1), not `Report`, not `Customer`.

---

## 4. API contract

All agent-facing endpoints under `/v1/`, Bearer-token auth, RFC 7807 Problem+JSON on errors, JSON requests/responses.

### 4.1 POST /v1/reports

Publish a report.

**Rendering disclosure (for OpenAPI description and `docs/api/reports.md`):** "Reports render inside an iframe between 320px and 1200px viewport width. Author HTML to be responsive across that range. Hub injects a CSS shim (box-sizing, image/table/code overflow safety), a `viewport` meta if absent, and a resize-poster script so the iframe matches content height — see DESIGN.md §'Hub Server Responsibilities'."

**Headers:** `Authorization: Bearer <api_key>`, `Idempotency-Key: <uuid>` (required), `Content-Type: application/json`.

**Body:**
```json
{
  "customer_id": "uuid",
  "title": "string (max 500)",
  "description": "string (max 5000, default '')",
  "tags": ["string", ...],
  "generated_at": "ISO8601 datetime",
  "html": "string (max 10MB)",
  "supplementary_files": [
    {"filename": "string", "content_type": "string", "base64": "string"}
  ]
}
```

**Response 201 (new):**
```json
{
  "report_id": "uuid-v7",
  "url": "https://hub.majeve.com/r/<report_id>",
  "created_at": "ISO8601"
}
```

**Response 200 (idempotency replay):** byte-identical to the original 201 body.

**Errors:** 401 (invalid api key), 403 (customer_id mismatch with api key), 422 (validation, oversize), 503 (S3 down).

**Idempotency contract (Agent Usage Contract, Section 1 Issue 2):**
- Generate one `Idempotency-Key` per logical report, BEFORE the first POST attempt.
- Reuse the same key for every retry of the same publish.
- A different logical report = a new key. Re-running enrichment with identical inputs = NEW key.
- For "share the existing report again" actions, do not POST. Call `GET /v1/reports/{id}` and reuse the stored URL.
- This contract is documented in `docs/api/idempotency.md` and embedded in Spectra's agent prompt.

**Write order (Section 1 Issue 4):**
1. Verify api key, parse body, validate.
2. Check `idempotency_keys` table for `(customer_id, key)`. If found, return stored response.
3. Generate UUID v7 report_id. Compute S3 key `<customer_id>/<report_id>/index.html`.
4. PUT HTML to S3.
5. PUT supplementary files to S3.
6. INSERT row in `reports`.
7. INSERT row in `idempotency_keys` with the response body.
8. Log `events` entry (event_type='report_published').
9. Return 201.

If step 4 or 5 fails → 503, no DB row, agent retries with same key → re-PUTs to same S3 path (idempotent) → tries INSERT again.
If step 6 fails after S3 success → orphan S3 object, cleaned by nightly sweep within 24-48h.

### 4.2 GET /v1/reports

List/search reports for the api key's customer.

**Query params:** `search`, `tags` (comma-separated), `from` (ISO8601), `to` (ISO8601), `cursor` (opaque base64), `limit` (default 50, max 200).

**Response 200:**
```json
{
  "items": [
    {
      "report_id": "uuid",
      "title": "string",
      "description": "string",
      "tags": ["string", ...],
      "generated_at": "ISO8601",
      "url": "https://hub.majeve.com/r/<report_id>",
      "size_bytes": 12345
    }
  ],
  "next_cursor": "string|null",
  "has_more": true
}
```

**Pagination (Section 4 Issue 15):** Keyset cursor. Default order `(generated_at DESC, report_id DESC)`. When `?search=` is set, order becomes `(ts_rank DESC, generated_at DESC, report_id DESC)` and cursor encodes all three.

**Search (Section 4 Issue 16):** `WHERE search_vector @@ plainto_tsquery('english', :q)`, ranked by `ts_rank(search_vector, plainto_tsquery('english', :q))`.

**Tag filter:** `WHERE tags @> ARRAY[<provided tags>]`.

**Cross-customer leak guard:** every query has `WHERE customer_id = :customer_from_api_key` — no override.

### 4.3 GET /v1/reports/{report_id}

Fetch metadata + HTML for one report.

**Response 200:** same shape as list item, plus `html: "string"`.
**Response 304:** if `If-None-Match` matches the response ETag.
**Response 404:** report not found OR owned by a different customer (no enumeration).

**Caching (Section 4 Issue 18):** ETag = sha256(metadata JSON). `Cache-Control: private, max-age=0, must-revalidate`.

### 4.4 DELETE /v1/reports/{report_id}

**Response 204** on success.
**Order:** Postgres DELETE first, then S3 DELETE. If S3 delete fails, log and continue — orphan cleanup will catch it.

### 4.5 GET /r/{report_id} (human viewer chrome)

**Three states:**

1. **No session cookie:** render email-entry HTML page.
2. **Valid session cookie + matching customer_id:** render chrome page with embedded iframe pointing at `reports.<domain>/render/{report_id}?t=<jwt>`. Refresh session sliding expiration.
3. **Valid session cookie + customer_id mismatch:** 404 (no enumeration).

### 4.6 POST /r/{report_id}/request-link

Request a magic link.

**Body:** `{"email": "string"}`
**Rate limited (Section 1 Issue 5):** 5/min per IP, 10/hour per report_id (Postgres-backed counters).
**Behavior:** if email is on the report's customer's `allowlist_emails`, mint a magic-link token (32 bytes entropy, 15min TTL, single-use), hash it with HMAC, store the hash, send the raw token in the email. **Whether the email is allowlisted or not, return the same 200 response** ("if this email is on file, a link has been sent") to prevent enumeration.

### 4.7 GET /r/{report_id}?token=...

Consume a magic-link.

**Success:** verify token hash, check expiry, check not-consumed, mark consumed, INSERT into `sessions`, set session cookie, log `magic_link_consumed` event, redirect 302 to `/r/{report_id}`.
**Failure:** 410 Gone (expired or already consumed or report mismatch). Same error class for all failure modes — no info leak.

### 4.8 GET reports.<domain>/render/{report_id}?t=...

Iframe content origin. Separate origin from the rest of Hub.

**Auth:** verify the 60s JWT signed with `HUB_IFRAME_JWT_SECRET` (or `_PREVIOUS` for rotation overlap). Claims: `report_id`, `customer_id`, `iat`, `exp=iat+60s`, `iss=HUB_PRIMARY_DOMAIN`. Reject if any verification step fails.

**Response (Section 4 Issue 17):** streamed S3 body via FastAPI `StreamingResponse`, **passed through the HTML injector** (see §15.12) which adds the CSS shim, viewport-meta (if absent), and resize-poster script. Headers:
- `Content-Type: text/html; charset=utf-8`
- `Content-Security-Policy: default-src 'self'; img-src data: https:; style-src 'unsafe-inline' 'self'; script-src 'self' 'unsafe-inline'; frame-ancestors 'self';`
- `X-Content-Type-Options: nosniff`
- `Cache-Control: private, no-cache, must-revalidate`
- `ETag: <s3-object-etag-with-injector-version-suffix>`
- `Referrer-Policy: no-referrer`

`If-None-Match` matching the ETag returns 304 with no body. ETag includes the injector version suffix so a shim/resize-poster bump invalidates cached entries. **Cache-Control rationale (revised 2026-05-10):** prior draft used `max-age=3600, immutable` for performance, but `immutable` is wrong for this endpoint — Spectra can overwrite the same S3 object for an existing `report_id` (e.g. agent re-runs after a data correction), and `immutable` tells the browser not to revalidate even when the user explicitly reloads. `no-cache, must-revalidate` forces the browser to round-trip every time, and the ETag still produces a fast 304 when S3 content hasn't changed. Customers never see a stale report.

**CSP rationale (revised 2026-05-10):** the prior draft used `script-src 'none'` to block agent-supplied scripts. Per Appendix A of the Majeve brief, Spectra runs inside the customer's own AWS/GCP VPC with direct read access to source systems and the LLM — the trust boundary is upstream of Hub, not at iframe render. `script-src 'none'` would also block Hub's resize-poster (DESIGN.md §"Hub Server Responsibilities"). `'self' 'unsafe-inline'` allows Hub's injected script and any inline scripts in agent HTML, while `frame-ancestors 'self'` still blocks clickjacking and `default-src 'self'` still constrains outbound resource loads.

### 4.9 Error contract (Section 2 Issue 11)

All errors `Content-Type: application/problem+json` with RFC 7807 shape:

```json
{
  "type": "https://hub.majeve.com/errors/<slug>",
  "title": "Short human-readable title",
  "status": 4xx/5xx,
  "detail": "Specific human message",
  "instance": "/v1/reports/...",   // optional
  "...": "extension fields per error class"
}
```

Registered error slugs (full list in `docs/api/errors.md`):
- `invalid-api-key` (401)
- `customer-not-authorized` (403)
- `report-not-found` (404)
- `validation-failed` (422) — includes `errors: [{loc, msg}]`
- `payload-too-large` (422)
- `rate-limit-exceeded` (429) — includes `retry_after_seconds`
- `magic-link-expired-or-consumed` (410)
- `iframe-token-invalid` (401)
- `s3-unavailable` (503)
- `internal` (500)

**Note:** Idempotency-key replay is NOT an error — it returns 200 with the original response body.

---

## 5. Auth flows

### 5.1 Human magic-link flow

```
Customer clicks https://hub.majeve.com/r/<report_id> in their Spectra email
  │
  ▼
GET /r/<report_id>  (no session cookie)
  │  → render email-entry page
  ▼
Customer types acme-cfo@acme.com  →  POST /r/<report_id>/request-link
  │  → rate-limit check (IP + report_id)
  │  → look up report.customer_id, check allowlist_emails contains the email
  │     (always return same 200 response regardless)
  │  → if allowlisted: mint 32-byte token, HMAC-hash, store, email raw token
  │  → log magic_link_issued event
  │
  ▼  (15 min later)
Customer clicks magic-link in email  →  GET /r/<report_id>?token=<raw>
  │  → HMAC-hash the raw token, look up in magic_link_tokens
  │  → check expires_at > now, consumed_at IS NULL, report.customer_id matches
  │  → mark consumed, INSERT session row, set session cookie (HttpOnly, Secure, SameSite=Lax, 30d sliding)
  │  → log magic_link_consumed event
  │  → 302 to /r/<report_id>
  │
  ▼
GET /r/<report_id>  (with session cookie)
  │  → verify session, refresh expires_at to now+30d
  │  → render chrome page; server mints iframe JWT (exp=60s) and embeds in <iframe src="...?t=jwt">
  │  → log report_view event
  │
  ▼
Browser loads iframe  →  GET reports.hub.majeve.com/render/<report_id>?t=<jwt>
  │  → verify JWT signature (current OR previous secret), exp, iss, report_id match
  │  → stream S3 HTML with strict CSP headers
  │  → set ETag, Cache-Control: private, max-age=3600, immutable
  │
  ▼
Customer sees the report inside a sandboxed iframe. No script execution. No cross-origin leakage.
```

### 5.2 Agent API-key flow

```
Spectra agent generates report
  │
  ▼
Agent generates Idempotency-Key UUID at start of publish task
  │
  ▼
POST /v1/reports  Authorization: Bearer <api_key>, Idempotency-Key: <uuid>
  │  → middleware: bcrypt-verify api_key against customers table
  │  → middleware: check body.customer_id == api_key's customer_id
  │  → handler: dedupe via idempotency_keys (return stored response on hit)
  │  → handler: S3 PUT, then Postgres INSERT (Section 1 Issue 4 order)
  │  → log report_published event
  │  → 201 with report_id + url
  │
  ▼  (network failure mid-response)
Agent retries with SAME Idempotency-Key
  │  → handler: dedupe HIT, return original 200 response (not 201)
```

### 5.3 Three signing/encrypting secrets (Section 1 Issue 7)

All independent 32-byte random values:

- `HUB_SESSION_SECRET` — signs session cookie.
- `HUB_IFRAME_JWT_SECRET` (+ optional `_PREVIOUS` for rotation overlap) — signs iframe content JWTs.
- `HUB_MAGIC_LINK_HASH_SECRET` — HMAC key for hashing magic-link tokens at rest.

API keys are bcrypt-hashed; no extra secret needed.

### 5.4 Customer provisioning CLI (`scripts/manage_customer.py`)

Replaces the v1-draft's "Majeve provisions manually via psql" handwave (§3.1 comment). Single Python script, ~120 lines, uses the same SQLAlchemy session as the app. Lives in M1.

**Commands:**

```
manage_customer.py create   --name "Acme Corp" --emails ops@acme.com,cfo@acme.com
                            → generates 32-byte key (mvk_live_<base64url>), bcrypts,
                              inserts customers row, prints raw key ONCE to stdout,
                              never logs or stores it elsewhere

manage_customer.py list     [--active-only] [--format table|json]
                            → prints id, name, api_key_prefix, allowlist email count,
                              is_active, created_at. No secrets in output.

manage_customer.py get      --email ops@acme.com
                            → finds the customer whose allowlist_emails @> ARRAY['ops@acme.com'],
                              prints same shape as `list`. Returns multiple rows if the email
                              is on more than one customer's allowlist (unusual but possible).
                              Exit 1 if no match.

manage_customer.py get      --id <uuid>
                            → same output, lookup by primary key.

manage_customer.py add-email     <customer_id> <email>
manage_customer.py remove-email  <customer_id> <email>
                            → idempotent array updates against allowlist_emails.

manage_customer.py rotate-key    <customer_id>
                            → generates new key, bcrypts, updates api_key_hash and
                              api_key_prefix, prints raw key ONCE. v1: single active key
                              (no overlap window — Spectra restart is the cutover).

manage_customer.py deactivate    <customer_id>
                            → sets is_active = false. Does NOT delete reports.
                              Existing magic-link sessions remain valid until expiry.
```

**Implementation rules:**
- Reads `DATABASE_URL` from env via the same `pydantic-settings` Settings class as the app.
- Uses `secrets.token_urlsafe(32)` for key entropy. Prefixes with `mvk_live_`.
- bcrypt cost factor matches the app middleware (consistency with §5.2 verify path).
- `--format json` on every read command so CI/scripts can consume.
- Never accepts a raw key as input; never echoes a key on any path except the one-shot generation prints.
- Refuses to run against a database whose URL doesn't match `HUB_*` env naming (foot-gun guard against pointing at the wrong DB).

**Test plan:**
- Unit: each command against a testcontainers Postgres. Round-trip create → get-by-email → add-email → rotate-key → deactivate.
- Property test (Hypothesis): `add-email` then `remove-email` on the same value returns `allowlist_emails` to its original state for any valid email string.
- Negative: `get --email <unknown>` exits 1 with a clear message; `create` with a duplicate name is allowed (uniqueness is by id, not name); `rotate-key` on an unknown id exits 1.

**Operational doc:** `docs/operations/onboarding.md` — one page covering: who runs it, where Hub is hosted, how the raw key is delivered to the customer (1Password share or equivalent — never email plaintext), how to rotate after a suspected leak, what to tell the customer about storing the key (their secrets manager, never committed to repo). This doc is the M1 deliverable, not a "we'll write it later" item.

**What's explicitly NOT in v1:**
- Self-serve customer signup (web UI, email verification, etc.) — Majeve onboards manually.
- Multi-key support / overlap rotation — single active key per customer; Spectra restart is the cutover. Revisit when a customer asks for zero-downtime rotation.
- Audit log of who ran which command — single-operator ops in v1; relies on shell history. Add when the ops team grows past one person.
- A web admin panel — same as above. CLI is the surface in v1.

---

## 6. Test plan reference

Full test plan: `~/.gstack/projects/marwazihs-spectrana-hub/marwazisiagian-master-eng-review-test-plan-20260510-183908.md`.

Summary (Section 3 Issue 14):
- **Stack:** pytest + pytest-asyncio + httpx.AsyncClient + testcontainers-python (Postgres + MinIO) + Hypothesis.
- **65 paths enumerated** in the coverage diagram. Target: 65/65 covered at v1 launch (every path written alongside its feature).
- **5 E2E flows:** publish+view, agent-finds-old-report, agent retry resilience, JWT rotation, cross-customer isolation.
- **4 Hypothesis properties:** idempotency replay, rate-limit window correctness, magic-link single-use, JWT rotation overlap.
- **No eval tests.** Hub has no LLM calls.
- **Three non-negotiable v1 tests:** `test_idempotency_property_replay`, `test_magic_link_token_storage_is_hashed`, `test_iframe_jwt_rotation_overlap`.

---

## 7. Env-var contract (Section 2 Issue 13)

Single source of truth: `app/config.py` (`Settings(BaseSettings)`). Mirror to `.env.example` and `docs/config.md`.

```env
# --- App identity & domain (Pattern 1 / Level A) ---
HUB_PRIMARY_DOMAIN=hub.majeve.com
HUB_REPORTS_DOMAIN=reports.hub.majeve.com
HUB_EMAIL_FROM="Majeve Reports <reports@majeve.com>"
HUB_EMAIL_BRAND_NAME=Majeve

# --- Secrets (three distinct values) ---
HUB_SESSION_SECRET=
HUB_IFRAME_JWT_SECRET=
HUB_IFRAME_JWT_SECRET_PREVIOUS=     # optional; supports rotation overlap
HUB_MAGIC_LINK_HASH_SECRET=

# --- Database ---
DATABASE_URL=postgresql+asyncpg://hub:hub@localhost:5432/hub
DATABASE_POOL_SIZE=10
DATABASE_POOL_MAX_OVERFLOW=10
DATABASE_POOL_RECYCLE_SECONDS=1800

# --- AWS S3 ---
AWS_REGION=us-east-1
AWS_S3_BUCKET=hub-reports-prod
AWS_S3_ENDPOINT_URL=                # blank for real AWS; http://minio:9000 in dev
AWS_ACCESS_KEY_ID=                  # prod uses IAM role; explicit only in dev
AWS_SECRET_ACCESS_KEY=

# --- SMTP ---
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_STARTTLS=true

# --- TTLs and tunables ---
SESSION_TTL_DAYS=30
MAGIC_LINK_TTL_MINUTES=15
IFRAME_JWT_TTL_SECONDS=60
IDEMPOTENCY_TTL_HOURS=24

# --- Rate limits ---
RATE_LIMIT_MAGIC_LINK_PER_IP_PER_MIN=5
RATE_LIMIT_MAGIC_LINK_PER_REPORT_PER_HOUR=10

# --- Payload limits ---
MAX_HTML_BYTES=10485760              # 10 MB
MAX_SUPPLEMENTARY_BYTES=104857600    # 100 MB

# --- Operational ---
LOG_LEVEL=INFO
SENTRY_DSN=
```

---

## 8. Implementation milestones (worktree-parallelizable)

### Dependency table

| Step | Modules touched | Depends on |
|---|---|---|
| M0 — Bootstrap | `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `app/main.py`, `app/config.py` | — |
| M1 — DB foundation + provisioning CLI | `app/db/`, `app/reports/models.py`, `app/auth/models.py`, `app/events/models.py`, `alembic/`, `scripts/manage_customer.py`, `docs/operations/onboarding.md` | M0 |
| M2 — Auth subsystem | `app/auth/` | M1 |
| M3 — Reports API | `app/reports/api.py`, `app/reports/schemas.py`, `app/reports/service.py` | M1, M2 |
| M4 — Viewer chrome + iframe origin | `app/reports/render.py`, `app/reports/html_injector.py`, viewer templates | M1, M2 |
| M5 — Jobs + observability | `app/jobs/`, `app/events/service.py` | M1 |
| M6 — Frontend (Next.js viewer chrome) | `frontend/` (separate package) | M2, M4 contract |
| M7 — Tests | `tests/` | every preceding module |
| M8 — Deploy plumbing | CI workflow, infra config | M0–M7 |

### Parallel lanes

```
Lane A (sequential, foundation):
  M0 → M1
    (everything below blocks on M1)

Lane B (parallel after M1):
  M2 — Auth subsystem
  M5 — Jobs + observability

Lane C (parallel after M2):
  M3 — Reports API
  M4 — Viewer chrome + iframe origin

Lane D (parallel with C, after API contract is stable):
  M6 — Frontend (consumes /r/<id> contract)

Lane E (continuous, runs alongside every module):
  M7 — Tests (each module's tests land with the module)

Lane F (final):
  M8 — Deploy plumbing
```

**Execution order:** Lane A serially. Then launch Lane B (M2 + M5 in two worktrees). After M2 lands, launch Lane C (M3 + M4 in two worktrees) and Lane D (M6) in parallel. M7 runs continuously alongside whatever module is active. M8 is the final lane.

**Conflict flags:**
- M3 and M4 both touch `app/reports/`. They can be parallelized only if M3 owns `api.py`/`service.py` and M4 owns `render.py`. Confirmed: clean file boundary.
- M2 and M3 both touch `app/auth/api_key.py` interface. M2 should land the api_key middleware contract before M3 starts.

### Timeline estimate (single developer with Claude Code)

| Milestone | Human | CC-assisted |
|---|---|---|
| M0 Bootstrap | 0.5 day | 1 hr |
| M1 DB + migrations + provisioning CLI + onboarding doc | 1.5 days | 4 hr |
| M2 Auth subsystem | 2-3 days | 1 day |
| M3 Reports API | 3-4 days | 1.5 days |
| M4 Viewer + iframe origin (incl. HTML injector) | 2.5 days | 1 day + 1 hr |
| M5 Jobs + observability | 1 day | 3 hr |
| M6 Frontend (Next.js) | 1-2 days | 4-6 hr |
| M7 Tests (continuous) | 3-4 days | 1.5 days |
| M8 Deploy | 1-2 days | 4 hr |
| **Total** | **~3-4 weeks** | **~1 week** |

---

## 9. Failure modes & operational notes

For each new codepath, the realistic production failure scenario and whether v1 handles it:

| Failure | Path | Test? | Error handling? | User sees? |
|---|---|---|---|---|
| S3 region outage during POST | Reports POST | YES (testcontainers injects MinIO failure) | YES (503 with retry hint) | Agent retries with same idem-key |
| Postgres connection pool exhausted | All DB endpoints | YES (load test) | Connection wait, then 503 | Agent retries; clear error |
| SMTP provider down on magic-link request | Magic-link request | YES | YES (503; token unconsumed; can retry) | Customer sees "if exists, link sent" — they don't see SMTP fail; need ops alert |
| Magic-link token replay (security) | Magic-link consume | YES | YES (410) | Generic expired-or-consumed error |
| API key compromise | All agent endpoints | manual | YES (key rotation via psql) | Customer's reports may be tampered until rotation — operational alert on unusual `events` activity |
| iframe JWT brute force | iframe origin | YES (signature verify) | YES (60s TTL + HMAC) | Effectively infeasible |
| Postgres + S3 split brain | POST | YES (testcontainer fault injection) | YES (S3-first ordering + nightly sweep) | None — agent's view stays consistent |
| Concurrent magic-link consume | Magic-link consume | YES (Hypothesis) | YES (DB unique constraint + atomic UPDATE) | Loser gets 410 |
| Customer's allowlist email leaks via timing oracle | Magic-link request | YES | Constant-time response shape | None — generic response either way |
| Agent emits non-responsive HTML at fixed pixel widths (`width: 1400px`) | Iframe render | YES (snapshot test against fixture report) | Partial — shim handles `img/table/pre` overflow; raw `width:` styles still overflow | Mobile viewer sees horizontal scroll inside iframe; chrome stays clean |
| Resize-poster fails (script error in agent HTML stops execution before poster runs) | Iframe render | YES (Playwright assertion on iframe height after load) | Parent has 800px fallback height + visible scrollbar inside iframe | Viewer sees a scrollable iframe; degraded but readable |

**No critical gaps** — every failure has a test plan and an error handling story. The two new rows are deferred-mitigation: shim grows a `[style*="width"] { max-width: 100% !important }` rule only if observed in production (DESIGN.md §"Why this approach").

---

## 10. Observability (Section 1 Issue 6)

`events` table is the wedge's observation instrument. Four event types in v1:

| event_type | Payload shape | Emitted when |
|---|---|---|
| `report_published` | `{report_id, idempotency_key, deduped: bool}` | POST /v1/reports completes |
| `report_view` | `{report_id, session_id, ip, user_agent, viewport_width?: int}` | GET /r/<id> with valid session renders the chrome page. `viewport_width` is `window.innerWidth` reported by a one-shot client beacon on the chrome page after hydration; optional (absent if the user closes the tab before hydration). Drives the v2/v3 trigger "what % of views are mobile?" |
| `search_query` | `{query, result_count}` | GET /v1/reports?search=... runs |
| `magic_link_issued` | `{email, ip}` (no token!) | request-link endpoint mints a token |
| `magic_link_consumed` | `{token_jti, ip}` (no raw token!) | token consume endpoint succeeds |

These feed the v2/v3 trigger questions directly (e.g., "how often do customers forward URLs?" = `report_view` event count by distinct ip per report).

Additional baseline observability: structured JSON logs to stdout (consumed by Spectra's existing log infra), Sentry on unhandled exceptions, Postgres slow-query log enabled (>500ms).

---

## 11. NOT in scope (deferred to v2/v3 with trigger conditions)

| Deferred | Trigger to build |
|---|---|
| Multi-user inside a customer (separate roles, granular permissions) | Customers regularly forward URLs internally |
| Public/private toggle per report + share-with-anyone URLs | Customer asks to share with someone outside their company |
| Collections (folders) | Customer report count exceeds ~50 AND asks for organization |
| Admin/superadmin panels (self-service) | Multiple customers want to manage their own users |
| API-key management UI | Same trigger as admin panels |
| Slug-based URLs (human-readable) | Reports become publicly shareable |
| Retention policies + storage quotas | Operationally needed (storage cost or compliance ask) |
| Mobile-optimized control panel | Customer complains; viewer is already responsive |
| Per-customer custom domains (white-label) | Hub wins a customer Majeve wouldn't have won otherwise |
| Customer self-host first-class support (docs, image versioning, support runbook) | A self-host customer signs |
| GCS/Azure object store support | Self-host customer on non-AWS infra |
| Redis cache layer | Sustained read QPS > 50 req/s |
| Stripe billing / tiers | Separate product decision |
| Full HTML body in FTS corpus | Customers ask to search inside report content |

---

## 12. What already exists (cross-reference)

Hub is greenfield in this repo. The patterns being re-vendored from `nuelo-spectra` (NOT imported — see §2.2):

| Spectra pattern | Hub re-vendors as |
|---|---|
| `aiosmtplib` + Jinja2 email + DB-backed tokens (password reset) | `app/auth/service.py` mints + emails magic-link tokens, identical shape |
| `api_keys` table + Bearer middleware | `customers` table holds `api_key_hash` + `api_key_prefix`; `app/auth/api_key.py` middleware |
| SQLAlchemy 2.0 async + asyncpg setup | `app/db/session.py` — same imports, same engine kwargs |
| Alembic migration patterns | `alembic/` — same conventions |
| pydantic-settings Settings class | `app/config.py` |
| Docker Compose dev environment | `docker-compose.yml` — same shape, plus MinIO container |
| `SPECTRA_MODE` router gating | Hub doesn't need mode gating (single app surface), but borrows the env-var hygiene pattern |

---

## 13. Pre-implementation assignment (from design doc, Section "The Assignment")

Before week 1 of M0, call or message the next 3 Majeve customers who ask for an old report. Ask them, in their own words: *"If we sent you a single URL where every past report lives — secured to your email — would you actually use it? What would make you not use it?"* Capture verbatim quotes. Answers either reinforce the wedge scope (proceed) or surface a real wedge that should be built instead.

This is a hard pre-req. The implementation plan above is correct only if customer-pull evidence agrees.

---

## 14. Open items (locked decisions but ops needs the values)

These were resolved in plan but need concrete values before deploy. Capture during M0:

- HTML payload size limit (locked formula, value placeholder): default 10MB. Confirm with Spectra what real reports look like in p95 → adjust before M3 lands.
- Supplementary file count limit (not yet decided): suggest cap at 20 files per report, 100MB total. Lock during M3.
- Postgres deploy target (RDS vs Cloud SQL vs self-managed): mirror Spectra. Confirm during M0.
- TLS cert provisioning (Caddy + Let's Encrypt vs AWS ACM): match Spectra. Confirm during M0.
- Sentry DSN: optional; provision during M8 if wanted in v1.

---

## 15. Frontend plan (Next.js 16, Cohere v.alpha tokens)

The frontend is intentionally small: **two primary pages** plus **three error states**. All customer-facing. No admin UI, no agent UI, no dashboards in v1.

**Source of truth: `DESIGN.md` and `colors_and_type.css`.** This section maps DESIGN.md surfaces (S1, S2, S3) onto Next.js routes and components. If a token, color, font, or radius is needed and not in `colors_and_type.css`, that's a signal it belongs in the parent system — not invented here.

### 15.1 Page inventory

| # | Route | State | DESIGN.md surface | Purpose |
|---|---|---|---|---|
| P1 | `GET /r/[reportId]` | no session cookie (with or without `?submitted=1` flag) | S1 | Email entry form. Identical generic message ("If this email is on file, a link has been sent.") renders inline below the input on retry — no separate confirmation page (anti-enumeration, DESIGN.md §"Trust & Anti-Enumeration"). |
| P2 | `GET /r/[reportId]` | valid session cookie | S2 | Viewer chrome wrapping the iframe. Stacked composition only. |
| E1 | `GET /r/[reportId]?err=expired` | token expired or consumed | S3 (E1 variant) | "Link expired — request a new one." Same layout as P1, headline replaced. |
| E2 | (any) | rate-limited | S3 (variant) | "Too many requests, wait a few minutes." No specific count or window. |
| E3 | `GET /r/[reportId]` | report not found OR cross-customer | S3 (404 variant) | Generic "This report isn't available" — no enumeration. |
| E4 | `GET /r/[reportId]?err=session_expired` | session expired | S1 with soft note | Falls back to P1 layout. |

**Removed from prior draft:** the old "P2 — Check your inbox" confirmation page with a green checkmark. DESIGN.md §"Trust & Anti-Enumeration Patterns" forbids it: identical generic text whether the email is on the allowlist or not. The submit-then-message flow now happens inline on P1, and the page formerly called P3 (viewer chrome) has been renumbered to P2 since the inventory shrank to two primary pages.

### 15.2 Wireframes

Wireframes show layout structure only. Tokens (color, type, spacing, radii) come from DESIGN.md S1/S2 — not from this section.

**P1 — Magic-link entry (no session, before AND after submit)**

DESIGN.md S1. Page background `--bg-stone`. Centered card on `--bg-canvas`, max 480px wide, `--radius-md` (16px), `--space-3xl` (48px) padding desktop / `--space-xl` (24px) mobile. No top nav, no logo wordmark.

```
+--------------------------------------------------------------+
|                                                              |
|              [ --bg-stone page background ]                  |
|                                                              |
|     +--------------------------------------------------+     |
|     |                                                  |     |
|     |   View your Majeve report                        |     |
|     |   .t-section-h / --fg-1                          |     |
|     |                                                  |     |
|     |   Enter the email where you receive Spectra      |     |
|     |   reports.   .t-body-l / --fg-2                  |     |
|     |                                                  |     |
|     |   +----------------------------------------+     |     |
|     |   |  you@company.com                       |     |     |
|     |   +----------------------------------------+     |     |
|     |                                                  |     |
|     |   [  Send me a link  ]                           |     |
|     |   .t-button on --color-primary, --radius-sm      |     |
|     |                                                  |     |
|     |   [after submit, inline below button:]           |     |
|     |   If this email is on file, a link has been      |     |
|     |   sent.   .t-body / --fg-2                       |     |
|     |   (identical text on success AND failure;        |     |
|     |    no checkmark, no toast, no separate page)     |     |
|     |                                                  |     |
|     +--------------------------------------------------+     |
|                                                              |
|     Majeve · Encrypted in transit · <id-short>               |
|     .t-micro / --fg-3                                        |
+--------------------------------------------------------------+
```

**Mobile (≤768px):** card becomes full-width with 24px gutters; padding drops to `--space-xl`; headline drops to `.t-card` (32px). Per DESIGN.md S1.

**P2 — Viewer chrome (valid session)**

DESIGN.md S2. **Stacked composition only — header above, supplementary files + footer below, never sidebar.** Outer column max-width 1200px on desktop. Iframe height set dynamically via `postMessage` from the resize-poster (DESIGN.md §"Hub Server Responsibilities").

```
+--------------------------------------------------------------+
|  April Sales Analysis                          [Sign out]    |
|  .t-card / --fg-1                              .t-button     |
|  Acme Corp · Apr 12, 2026                                    |
|  .t-caption / --fg-3                                         |
+----------------- (--border-hairline) ------------------------+
|                                                              |
|  +--------------------------------------------------------+  |
|  |                                                        |  |
|  |   <iframe                                              |  |
|  |     src="https://reports.hub.majeve.com/render/<id>"   |  |
|  |     sandbox="allow-same-origin allow-scripts"          |  |
|  |     referrerpolicy="no-referrer"                       |  |
|  |     style="width:100%; border:0; display:block;"       |  |
|  |     [height set dynamically by parent's postMessage    |  |
|  |      listener — no nested scrollbar, page is one       |  |
|  |      continuous scroll]                                |  |
|  |   >                                                    |  |
|  |   --border-hairline 1px around, --radius-xs (4px)      |  |
|  |                                                        |  |
|  +--------------------------------------------------------+  |
|                                                              |
|  Supplementary files                                         |
|    q1-data.csv  ·  24 KB    [ download ]                     |
|    pivot.xlsx   ·  110 KB   [ download ]                     |
|    .t-mono filename + .t-caption size, --color-action-blue   |
|                                                              |
|  Majeve · Encrypted in transit · <id-short>                  |
+--------------------------------------------------------------+
```

**Mobile (≤768px), per DESIGN.md S2:**
- Outer column gets 16px gutters for header / supplementary / footer
- **Iframe is edge-to-edge, zero gutter.** A 375px viewport gives the report 375px, not 343px.
- Header collapses customer + date onto one line under the title
- Sign-out stays as a text link (no hamburger — there's nothing else to navigate to)

**E1 — Link expired**

Same S1 layout. Headline replaced ("This link has expired"), subhead ("For security, magic links expire after 15 minutes and can only be used once."), CTA replaces the input ("Request a new link"). On submit, returns to P1's identical generic message.

**E2 — Rate-limited**

Same S1 layout. Headline "Too many requests." Subhead "Please wait a few minutes before trying again." No specific count, no specific window (DESIGN.md §"Trust & Anti-Enumeration").

**E3 — Report not available** (used for both "not found" AND "cross-customer" — same UX, no enumeration)

Same S1 layout. Headline "This report isn't available." Subhead "It may have been removed, or the link is incorrect." No input field.

**E1 — Link expired**

```
+--------------------------------------------------------------+
|                      [ Majeve logo ]                         |
|                                                              |
|                  This link has expired                       |
|                                                              |
|   For security, magic links expire after 15 minutes          |
|   and can only be used once.                                 |
|                                                              |
|                [ Request a new link ]                        |
+--------------------------------------------------------------+
```

**E2 — Rate-limited**

```
+--------------------------------------------------------------+
|                      [ Majeve logo ]                         |
|                                                              |
|                    Too many requests                         |
|                                                              |
|   We've sent a lot of links recently. For security,          |
|   please wait a few minutes before trying again.             |
|                                                              |
+--------------------------------------------------------------+
```

**E3 — Report not available (used for both "not found" AND "cross-customer access" — same UX, no enumeration)**

```
+--------------------------------------------------------------+
|                      [ Majeve logo ]                         |
|                                                              |
|                  Report not available                        |
|                                                              |
|   This report can't be opened with the current link.         |
|   If this is unexpected, contact your Majeve administrator.  |
+--------------------------------------------------------------+
```

### 15.3 Visual system

**All tokens inherited from `colors_and_type.css` (Cohere v.alpha).** Do not redefine. `frontend/app/globals.css` imports `../../../colors_and_type.css` (or the file is copied verbatim at build time — see §15.4).

| Property | Token | Notes |
|---|---|---|
| Fonts | `--font-display` (Space Grotesk), `--font-ui` (Inter), `--font-mono` (JetBrains Mono) | Loaded via the `@import` already in `colors_and_type.css`. Use Next.js `next/font/google` only if FOUT becomes a real complaint — defer. |
| Type scale | `.t-section-h`, `.t-card`, `.t-body-l`, `.t-body`, `.t-button`, `.t-caption`, `.t-mono`, `.t-micro` | Defined in `colors_and_type.css`. Use class names; do not redefine sizes inline. |
| Color: surface | `--bg-canvas`, `--bg-stone` | Stone for the magic-link entry page (S1). Canvas for the viewer (S2). |
| Color: foreground | `--fg-1` (body), `--fg-2` (secondary), `--fg-3` (metadata) | Mapped to ink / body-muted / slate in `colors_and_type.css`. |
| Color: action | `--color-action-blue` (links/downloads), `--color-primary` (`#17171c`, primary CTA), `--color-on-primary` (button text), `--color-form-focus` (input focus ring) | No arbitrary hex anywhere in frontend code. |
| Radii | `--radius-xs` (4px, iframe), `--radius-sm` (8px, input/button), `--radius-md` (16px, entry card) | DESIGN.md S1/S2. |
| Spacing | `--space-xs`/`-sm`/`-md`/`-lg`/`-xl`/`-2xl`/`-3xl` (8px base) | DESIGN.md S1: `--space-3xl` (48px) card padding desktop, `--space-xl` (24px) mobile. |
| Borders | `--border-hairline` (1px `--color-hairline`) | DESIGN.md S2 iframe border, header rule. |
| Max widths | Entry/error: 480px card; Viewer outer column: **1200px** (DESIGN.md S2 — not 1280px). | |
| Motion | Focus ring fades 150ms ease-out. Button background shifts 100ms on hover. No entrance animations, no skeleton shimmer, no page transitions. | DESIGN.md §"Motion". |
| Dark mode | **Not installed in v1.** Do not add `next-themes` or any dark-mode dependency. DESIGN.md §"Dark Mode Policy": revisit when Cohere v.alpha adds dark mode upstream. | |
| Accessibility | Semantic HTML, label-input pairing, `aria-live="polite"` on the inline post-submit message, focus-visible rings using `--color-form-focus`, contrast ≥ AA. | Trust UI floor. |

**shadcn/ui:** the library's primitives (`Button`, `Input`, `Card`) ship with Tailwind class defaults that will conflict with Cohere v.alpha. Two options for v1: (a) install shadcn and override every primitive's CSS variables to point at Cohere tokens in `globals.css`, or (b) skip shadcn entirely and hand-write the 4 form/card elements as ~80 lines of CSS-modules against Cohere tokens. Recommend (b) for v1 — the surface is small enough that shadcn's abstraction is more friction than value, and (b) eliminates Tailwind-vs-Cohere token drift risk. Revisit at v2 if the component surface grows past 6 primitives.

### 15.4 Directory structure

```
frontend/
  app/
    layout.tsx                       # Root layout: <html lang>, body
    globals.css                      # @import '../../../colors_and_type.css' (Cohere v.alpha — single source of truth, DO NOT redefine tokens here). Adds only: per-deploy accent override (§15.7) and reduced-motion media query.
    page.tsx                         # Root '/' — redirect to a marketing page or 404
    not-found.tsx                    # Global 404
    error.tsx                        # Global error boundary
    r/
      [reportId]/
        page.tsx                     # Server component: reads session, branches to S1 (entry) or S2 (viewer)
        loading.tsx                  # Suspense fallback (.t-body muted "Loading…", per DESIGN.md §"NOT in scope")
        error.tsx                    # E1/E2/E3 dispatch based on err query param
        components/
          EntryCard.tsx              # Server: S1 layout shell — headline, subhead, slot for form
          EmailEntryForm.tsx         # Client: zod-validated input + submit + inline post-submit message
          ReportViewer.tsx           # Server wrapper: header, iframe, supplementary list, footer
          ResizeListener.tsx         # Client island: postMessage listener + viewport beacon
          SupplementaryFile.tsx      # Server: one row in the file list
          SignOutButton.tsx          # Client: POST /api/auth/sign-out, then router.refresh()
  styles/
    cohere.css                       # Symlink or build-time copy of repo-root colors_and_type.css
    hub.css                          # Hub-only additions: accent override (§15.7), reduced-motion query, the 4 small components from §15.3 note (b)
  lib/
    api.ts                           # Internal fetch wrappers for FastAPI calls (server-side only)
    session.ts                       # Server-side helper: validate session cookie, return SessionData | null
    config.ts                        # Reads NEXT_PUBLIC_* env vars; validates HUB_BRAND_ACCENT against Cohere allowlist (§15.7)
  public/
    logo.svg                         # Brand logo (per-deploy via build arg) — used in footer trust line only
  next.config.mjs                    # output: 'standalone'; rewrites for /api → FastAPI (in local dev only)
  tsconfig.json
  package.json
  Dockerfile.frontend
```

**Removed from prior draft:** `tailwind.config.ts`, `postcss.config.mjs`, `components/ui/` (shadcn primitives), `BrandHeader.tsx`, `BrandFooter.tsx`. Per §15.3 note: hand-written CSS against Cohere tokens is less surface than Tailwind+shadcn for a 3-page app, and avoids token-system drift. If the component surface ever grows past ~6 primitives, revisit.

### 15.5 Data flow per page

**P1 (no session) — server component fetches nothing:**

```
Browser GET /r/abc-123
  └─ Next.js: app/r/[reportId]/page.tsx (server component)
       └─ session.ts checks cookies.get('hub_session') → null
       └─ returns <EntryCard><EmailEntryForm reportId="abc-123" /></EntryCard>
```

The form is a client component. On submit, fetches FastAPI:

```
EmailEntryForm onSubmit (client)
  └─ fetch('/api/r/abc-123/request-link', {method:'POST', body:{email}})
       └─ LB routes /api/* to FastAPI
       └─ FastAPI: rate-limit check, allowlist check, mint+hash+store token, send email
       └─ FastAPI returns 200 with generic body (also for 429 — anti-enumeration)
  └─ Set local state {submitted: true}; render generic message inline below input,
     wrapped in aria-live="polite". Same input remains; same submit IS the retry.
```

**P2 (valid session) — server component fetches report metadata + mints iframe JWT:**

```
Browser GET /r/abc-123  (with hub_session cookie)
  └─ Next.js: app/r/[reportId]/page.tsx
       └─ session.ts reads cookies.get('hub_session'), forwards to FastAPI:
           GET /api/internal/sessions/me  Cookie: hub_session=...
           ← {customer_id, email, expires_at}
       └─ Fetch report metadata:
           GET /api/internal/reports/abc-123  X-Customer-Id: <uuid>
           ← {report_id, title, description, generated_at, supplementary_files:[...]}
                (returns 404 if cross-customer — Next.js maps to E3)
       └─ Mint iframe JWT:
           POST /api/internal/iframe-jwt  X-Customer-Id, body:{report_id}
           ← {token: "eyJ..."}
       └─ Renders <ReportViewer report={...} iframeSrc="https://reports.hub.majeve.com/render/abc-123?t=eyJ..." />
```

The `/api/internal/*` endpoints are FastAPI routes mounted under `/api/internal/`, **not exposed at the LB** (or restricted by a shared secret). They exist so Next.js's server components can fetch backend data without going through the public agent API. If you want to skip internal endpoints entirely, the alternative is to have FastAPI do server-side rendering of the entire viewer page (but that's the Jinja alternative we already declined).

**E1 (magic-link consume) — happens at FastAPI, not Next.js:**

```
Browser GET /r/abc-123?token=raw-token-here
  └─ LB routing rule: any /r/* request WITH a `token` query param routes to FastAPI's consume endpoint
       (alternatively, Next.js detects ?token=, server-side proxies the consume to FastAPI, forwards Set-Cookie)
  └─ FastAPI: hash the token, look up in magic_link_tokens, verify expires_at and consumed_at
  └─ Success: mark consumed, INSERT session, Set-Cookie: hub_session=...; 302 Location: /r/abc-123 (no query)
  └─ Failure: 302 Location: /r/abc-123?err=expired (or rate-limited, or invalid)
  └─ Browser follows redirect → Next.js renders P2 (success) or E1/E2 (error code in query param)
```

**Why this split:** FastAPI owns cookie issuance because cookie logic is auth logic, and auth logic should live next to the session/token tables. Next.js reads the cookie via `cookies()` server-side but never writes one. This keeps the trust boundary clean.

### 15.6 Component contracts (key components only)

**`<EmailEntryForm reportId={string}>`** — client component
- Form: react-hook-form + zod schema (`email: z.string().email()`).
- Submit: `POST /api/r/[reportId]/request-link`. On 200 → state.submitted=true; render the generic message inline below the input ("If this email is on file, a link has been sent."), with `aria-live="polite"`. On 429 → render the **same generic message** (anti-enumeration: rate limit must not leak whether the email was valid). On 5xx → render the same generic message AND log to Sentry server-side. **Never render a checkmark, toast, success icon, or separate confirmation page.**
- Disable submit for 60s after first submission to discourage spam without leaking validity (DESIGN.md §"Trust & Anti-Enumeration").
- No "request a new link" affordance distinct from the same form; the same input + same submit IS the retry.

**`<ReportViewer report={ReportMeta} iframeSrc={string}>`** — server component wrapping a tiny client island for the resize listener
- Header (in same component): report title (`.t-card`), then `Acme Corp · Apr 12, 2026` (`.t-caption / --fg-3`), Sign-out link top-right.
- Iframe element with locked attributes:
  ```tsx
  <iframe
    ref={iframeRef}
    src={iframeSrc}
    sandbox="allow-same-origin allow-scripts"
    referrerPolicy="no-referrer"
    title={report.title}
    style={{ width: '100%', border: 0, display: 'block', borderRadius: 'var(--radius-xs)' }}
    // height set dynamically by <ResizeListener /> below; initial 800px fallback
    height={800}
  />
  ```
- `<ResizeListener iframeRef={iframeRef} reportId={report.id}>` — client island, ~30 lines:
  ```tsx
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.source !== iframeRef.current?.contentWindow) return;
      if (e.data?.type !== 'hub-resize') return;
      // Math.ceil + 1px buffer: sub-pixel rounding can leave 1px overflow
      // which triggers a scrollbar on some browsers. Belt-and-suspenders
      // with the injector's `html, body { overflow: hidden }` (§15.12).
      iframeRef.current.style.height = `${Math.ceil(e.data.height) + 1}px`;
    };
    window.addEventListener('message', onMessage);
    // One-shot viewport beacon for events.report_view.viewport_width
    fetch(`/api/r/${reportId}/viewport`, {
      method: 'POST',
      body: JSON.stringify({ viewport_width: window.innerWidth }),
      keepalive: true,
    }).catch(() => {});
    return () => window.removeEventListener('message', onMessage);
  }, [iframeRef, reportId]);
  ```
- Below iframe: `<SupplementaryFile>` list — each row is `.t-mono` filename + `.t-caption` size, link in `--color-action-blue`. Pre-signed S3 URL fetched server-side at render time (5-min expiry).
- Footer trust line (`.t-micro / --fg-3`), same as P1.
- **No `BrandHeader` / `BrandFooter` wrapping.** The viewer's chrome is part of `ReportViewer` itself; entry/error pages render their own minimal chrome inline. Reduces the "shared component that's only used twice" abstraction tax.

### 15.7 Per-deploy branding (ties back to Issue 8 / Pattern 1)

Re-uses the existing env-var contract from §7. **Per DESIGN.md, accent color is constrained to the Cohere v.alpha named palette — no arbitrary hex.** Logos and brand text are still per-deploy.

| Frontend env | Source | Used for | Allowed values |
|---|---|---|---|
| `NEXT_PUBLIC_HUB_BRAND_NAME` | mirrors `HUB_EMAIL_BRAND_NAME` | Footer trust line ("Majeve · …"), browser `<title>` | Free text |
| `NEXT_PUBLIC_HUB_PRIMARY_DOMAIN` | mirrors `HUB_PRIMARY_DOMAIN` | Canonical URL in og: tags | URL |
| `NEXT_PUBLIC_HUB_REPORTS_DOMAIN` | mirrors `HUB_REPORTS_DOMAIN` | iframe `src` construction | URL |
| `NEXT_PUBLIC_HUB_BRAND_ACCENT` | new, optional, defaults to `action-blue` | The single accent color (links, focus highlight) | **One of:** `action-blue`, `deep-green`, `dark-navy`, `coral`, `primary`. Maps directly to a Cohere CSS variable; arbitrary hex is rejected at startup. |

**Accent enforcement:** `lib/config.ts` maps the env value to `var(--color-<name>)` via a fixed allowlist. Any other value throws at app boot — fail loud, never silently accept invented colors. Test: snapshot test asserting the allowlist matches `colors_and_type.css` at build time.

**Logo:** per-deploy via `Dockerfile.frontend` build arg, baked at build time. v1 simplest path. (Note: the magic-link entry page S1 has no logo wordmark per DESIGN.md — the headline establishes brand context. Logo is footer-only in v1.)

Same image, different env vars → different brand within the Cohere palette. Mirrors the backend's Pattern 1 contract while honoring DESIGN.md's "inherit Cohere v.alpha; no per-customer palette overrides" rule.

### 15.8 Build & deploy

- **Build:** `next build` with `output: 'standalone'` so the Docker image is tiny (~150MB) and contains only the runtime needed to serve.
- **Runtime:** `node server.js` on port 3000.
- **Dockerfile:** standard Next.js standalone Dockerfile pattern. Multi-stage: deps → build → runtime.
- **Local dev:** `docker compose up` brings up FastAPI (port 8000), Next.js (port 3000), Postgres (port 5432), MinIO (port 9000). Next.js dev server uses `rewrites` in `next.config.mjs` to proxy `/api/*` to `http://hub-api:8000` so the local-dev origin is single (`http://localhost:3000`).
- **Prod:** LB (Caddy/ALB/Cloud LB) terminates TLS for `hub.majeve.com` and routes:
  - `/api/*` → FastAPI service (port 8000)
  - everything else → Next.js service (port 3000)
  - `reports.hub.majeve.com/*` → FastAPI service (port 8000) — the iframe origin

```
                   hub.majeve.com                    reports.hub.majeve.com
                          │                                    │
                          ▼                                    ▼
                  ┌───────────────┐                    ┌───────────────┐
                  │  Load balancer │                    │  Load balancer │
                  │ (TLS terminate)│                    │ (TLS terminate)│
                  └───┬─────────┬─┘                    └───────┬───────┘
                      │         │                              │
            path /api │         │ everything else              │
                      ▼         ▼                              ▼
                ┌──────────┐ ┌──────────┐                ┌──────────┐
                │ FastAPI  │ │ Next.js  │                │ FastAPI  │
                │ container│ │ container│                │ container│
                │ :8000    │ │ :3000    │                │ :8000    │
                └────┬─────┘ └─────┬────┘                │ (same    │
                     │             │                      │  one)    │
                     ▼             │                      └──────────┘
              Postgres + S3        │
                                   │
                            calls /api/internal/*
                            via Docker DNS (no public LB)
```

### 15.9 Frontend test plan (extends Section 3)

| Test type | What | Tool |
|---|---|---|
| Component unit | EmailEntryForm validates email format; disables submit for 60s after first submission; renders **identical** generic message on 200, 429, AND 5xx (anti-enumeration) | Vitest + React Testing Library |
| Component unit | SupplementaryFile renders file metadata correctly | Vitest + RTL |
| Component unit | ResizeListener sets iframe.style.height on `hub-resize` postMessage; ignores messages from other windows | Vitest + RTL + jsdom |
| Component unit | `lib/config.ts` rejects invalid `HUB_BRAND_ACCENT` values at boot; allowlist matches `colors_and_type.css` | Vitest |
| E2E | Full magic-link flow: load /r/<id>, enter email, click email link (simulated), see iframe load | Playwright (single browser: Chromium) |
| E2E | E1 — token expired → same S1 layout, headline replaced, same input + submit | Playwright |
| E2E | E3 — cross-customer access → see generic "isn't available" | Playwright |
| E2E | Resize fidelity — load fixture report at viewports 375 / 768 / 1200; assert iframe height matches `documentElement.scrollHeight` within 50ms of load AND no nested scrollbar appears | Playwright |
| E2E | Anti-enumeration — submit allowlisted email AND non-allowlisted email; assert response time, status, and rendered DOM are byte-identical | Playwright |
| Visual regression | Wireframe-level snapshots of S1 (before submit), S1 (after submit, generic message visible), S2, E1, E2, E3 at 375px and 1200px | Playwright screenshots + diff |

Add `frontend/tests/` for component tests and `tests/e2e/` (shared with backend) for full-flow tests. E2E tests use the same testcontainers Postgres + MinIO + FastAPI + Next.js stack via Docker Compose.

### 15.10 Frontend non-goals for v1

- **No Tailwind, no shadcn/ui, no `next-themes`.** Cohere v.alpha is the design system; hand-written CSS against its tokens is less surface for 3 pages. Revisit if component count grows past ~6.
- **No dark mode.** DESIGN.md §"Dark Mode Policy" defers to v2 when Cohere v.alpha adds dark tokens upstream.
- **No per-customer palette overrides.** `HUB_BRAND_ACCENT` is constrained to a Cohere-named token, not arbitrary hex (§15.7).
- **No client-side routing animations / page transitions.** DESIGN.md §"Motion": minimal-functional only.
- **No skeleton shimmer / loading choreography.** Page is server-rendered; show nothing until ready, then show everything.
- No infinite scroll, no client-side search, no filters — the viewer page shows ONE report.
- No PDF rendering inside the viewer — supplementary PDFs are download links only.
- No analytics SDK (PostHog, Mixpanel, etc.) — `events` table is the analytics surface in v1.
- No service worker, no offline mode, no PWA install.
- No customer-facing settings page — Majeve manages allowlist emails via psql.
- No "list all my reports" page — the agent (Spectra) is the discovery surface; the customer always arrives via a URL.

If a customer asks "I want to browse my report history myself," that's a v2 trigger ("customers ask to browse their own history") that adds a `/reports` index page. Not in the wedge.

### 15.11 Frontend timeline (replaces M6 row in §8)

| Substep | Human | CC-assisted |
|---|---|---|
| FE-1 Bootstrap Next.js 16 (no Tailwind, no shadcn) + symlink/copy `colors_and_type.css` | 0.25 day | 30 min |
| FE-2 `hub.css` (accent override + reduced-motion + 4 hand-written components against Cohere tokens) + layout.tsx | 0.5 day | 1 hr |
| FE-3 EntryCard + EmailEntryForm with inline generic message (S1, anti-enumeration) | 0.5 day | 1 hr |
| FE-4 ReportViewer + ResizeListener + SupplementaryFile (S2 with dynamic iframe height) | 0.75 day | 1.5 hr |
| FE-5 Error pages (E1, E2, E3) — same S1 layout, headline/subhead swaps | 0.25 day | 30 min |
| FE-6 lib/session.ts + lib/api.ts + lib/config.ts (with HUB_BRAND_ACCENT allowlist validation) | 0.5 day | 1 hr |
| FE-7 Dockerfile.frontend + docker-compose wiring + `colors_and_type.css` build-time copy | 0.25 day | 30 min |
| FE-8 Component unit tests + E2E Playwright tests (incl. resize fidelity + anti-enumeration byte-identical assertion) | 1.25 days | 3.5 hr |
| FE-9 LB routing rules + prod deploy config | 0.25 day | 30 min |
| **Total** | **~4.5 days** | **~1 day** |

This breaks out M6 from §8's "1-2 days" estimate to ~4 days human time, which is more honest. Update §8 timeline total accordingly: **3-4 weeks → 3.5-4.5 weeks** for v1 with a single human dev (or **~1 week** with full CC assist).

### 15.12 HTML injector module (M4 backend, ties §4.8 to DESIGN.md)

`app/reports/html_injector.py` — pure function, no I/O. Called by the iframe-render endpoint after fetching the S3 body, before streaming to the client.

**Contract:**
```python
def inject(html: bytes) -> bytes:
    """Idempotent. Inject CSS shim, viewport-meta (if absent), resize-poster.
    Detection markers: data-hub-shim="v1", data-hub-resize="v1".
    If both markers are already present, return html unchanged.
    """
```

**What it injects** (verbatim from DESIGN.md §"Hub Server Responsibilities"):

1. **CSS shim** — `<style data-hub-shim="v1">` block immediately after `<head>` (or at start of `<body>` if `<head>` absent). Rules: `:root { font-size: 16px; -webkit-text-size-adjust: 100%; }`, `*, *::before, *::after { box-sizing: border-box; }`, `html, body { overflow: hidden; }`, `body { margin: 0; }`, `img, video, svg { max-width: 100%; height: auto; }`, `table { display: block; max-width: 100%; overflow-x: auto; }`, `pre, code { overflow-x: auto; word-wrap: break-word; white-space: pre-wrap; }`. The `html, body { overflow: hidden }` rule enforces the "one continuous scroll" contract (DESIGN.md S2) during the 50-100ms window between iframe load and first postMessage arrival — without it, users see a brief flash of nested scrollbar before the parent ResizeListener (§15.6) takes over.

2. **Viewport meta** — `<meta name="viewport" content="width=device-width, initial-scale=1">` injected into `<head>` only if absent.

3. **Resize-poster** — `<script data-hub-resize="v1">` immediately before `</body>` (or at end of `<body>` if `</body>` absent). Posts `{type: 'hub-resize', height: document.body.scrollHeight}` to parent on `load`, `resize`, and `ResizeObserver` body mutations. **Must read `document.body.scrollHeight`, NOT `document.documentElement.scrollHeight`** — `documentElement.scrollHeight` inflates to the iframe viewport size when content is shorter than the viewport, which prevents the iframe from ever shrinking after a content swap (e.g. user navigates from a long report to a short one in the same session). Verified live during P2 mockup review: a 370px-content iframe stayed locked at 1159px when `documentElement.scrollHeight` was used.

**Idempotency rule:** detection is a literal byte-search for `data-hub-shim="v1"` and `data-hub-resize="v1"`. Each marker is checked independently; missing markers get injected, present markers get skipped. If Spectra ever emits HTML with all three already, Hub serves it untouched.

**Test plan (M7):**
- Unit: 6 fixture HTMLs (no head; no body; no viewport; all-three-already-present; malformed; minified-on-one-line). Assert: idempotent (run twice, second pass = no change), all three markers present after first pass, valid HTML out for valid HTML in.
- Integration: e2e Playwright — load `/render/<id>` for a fixture report at viewport 375px, 768px, 1200px; assert iframe content reflows AND parent iframe height matches `document.body.scrollHeight` within 50ms of load. **Shrink-case fixture required:** load a long report, then swap the iframe `src` to a short report (≤500px content) in the same session, assert parent iframe height shrinks to the short report's `body.scrollHeight` (regression guard for the `documentElement.scrollHeight` trap).
- Regression: snapshot test asserting injector version suffix in ETag changes when the shim or poster changes (otherwise stale cached entries serve old shim).

**Versioning:** marker stays `v1` until shim CSS or poster JS changes meaningfully. When it does, bump to `v2` AND change the ETag suffix in §4.8 so caches invalidate.

**Distribution:** module ships as part of M4. No new container, no separate deploy. Lives in the same FastAPI process as `/render`.

**Why this lives in M4, not M3:** the injector runs on the *render* path (read), not the *publish* path (write). M3 owns POST /v1/reports and stores HTML to S3 verbatim — no transformation at ingest. The injector is purely a serve-time concern.

**Pre-implementation findings captured 2026-05-10** (from P2 mockup review, verified live with Playwright across short/medium/long sample reports):

| # | Severity | Finding | Landed in |
|---|---|---|---|
| 1 | High | Resize-poster must use `document.body.scrollHeight`, not `documentElement.scrollHeight` (shrink-case bug) | §15.12 above (contract + test plan) |
| 2 | Medium | CSS shim must include `html, body { overflow: hidden }` (race-window scrollbar flash) | §15.12 above |
| 3 | Low | Parent ResizeListener should use `Math.ceil(h) + 1` (sub-pixel overflow guard) | §15.6 ResizeListener |
| 4 | Low | Render endpoint Cache-Control must drop `immutable` (Spectra can rewrite same `report_id`) | §4.8 |

All four were caught during the plan-driven HTML mockup pass (P1 + P2 via `/design-html`); P2 finalized artifacts at `~/.gstack/projects/marwazihs-spectrana-hub/designs/p2-report-viewer-20260510/finalized.{html,json}`.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | (not run; scope already pressure-tested in /office-hours design doc) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | (not run; Outside Voice offered, declined) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | CLEAR | Pass 2 (2026-05-10, post-DESIGN.md): rewrote §15 against DESIGN.md (Cohere v.alpha tokens, stacked composition, edge-to-edge mobile, dynamic iframe height); relaxed §4.8 CSP from `script-src 'none'` to `'self' 'unsafe-inline'` after re-evaluating threat model vs Appendix A architecture (Spectra is trusted-insider in customer VPC, prior CSP was security theater); dropped over-engineered API-ingest `<script>` rejection ticket; added §15.12 HTML injector module (M4); added OpenAPI width disclosure (§4.1) and `viewport_width` field on `events.report_view` payload (§10); deleted P2 confirmation page (anti-enumeration); constrained `HUB_BRAND_ACCENT` to Cohere named tokens. 0 critical gaps. Pass 1: 18 issues resolved, 65-path test coverage diagram. |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | (DESIGN.md authored via /design-consultation 2026-05-10; embedded into PLAN.md §15 via this eng review pass 2) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | n/a | (Hub's "DX" is the agent contract; embedded in §4 + §5.2 + Agent Usage Contract) |

- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement. Recommend running /plan-design-review at the start of M4 (when the viewer chrome contract crystallizes) and /plan-ceo-review only if scope drifts beyond the §11 trigger conditions.
