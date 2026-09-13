# Spectrana Hub

A publishing target for AI-generated reports. Agents publish HTML reports over an API key; customers view them at a stable URL after signing in with a magic link. See `PLAN.md` for the implementation contract.

---

## Using the Hub

### Setup and Onboarding

#### 1. Run the stack (local)

Prerequisites: Docker, `curl`, `jq`.

```bash
cp .env.example .env        # optional locally; see note below
docker compose up -d
curl -fsS http://localhost:8000/healthz     # {"status":"ok"}
```

| Service | Address | Purpose |
|---|---|---|
| `hub` | `http://localhost:8000` | FastAPI: publish API, iframe render, magic links |
| `frontend` | `http://localhost:3000` | Next.js report viewer |
| `postgres` | `localhost:5432` | Database (migrations run automatically via `migrate`) |
| `minio` | `localhost:9000` (console `:9001`) | S3-compatible object store |

`/healthz` only confirms the Hub process is up; it does not check the database or object store.

**What `.env` controls locally.** `docker-compose.yml` hardcodes the dev secrets and domains for the `hub` service, so setting `HUB_SESSION_SECRET` etc. in `.env` has no effect under Compose. Locally, `.env` only feeds:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_STARTTLS`: needed to actually send magic-link emails
- `HUB_HOSTNAME`: set to your LAN IP to test from a phone (defaults to `localhost`)
- `HUB_URL_SCHEME`, `HUB_EMAIL_FROM`, `HUB_EMAIL_BRAND_NAME`, `HUB_INTERNAL_TOKEN`

Without a working SMTP config, email sends fail silently. Agents can still mint links with `delivery: "return"` (see [Sharing the report](#sharing-the-report)).

For a one-shot smoke test (provisions a customer, publishes a sample report, prints a sign-in link), run `./scripts/human-test-bootstrap.sh`.

#### 2. Onboard a customer

There is no admin UI in v1. Customers are provisioned with the CLI:

```bash
docker compose exec -T hub uv run python -m scripts.manage_customer create \
  --name "Acme" \
  --emails alice@acme.com,bob@acme.com
```

Output:

```
customer_id: 3f6c2a1e-…
api_key:     mvk_live_…
prefix:      mvk_live
```

- **`api_key` is printed once and cannot be recovered.** Deliver it to the publishing tool through a secret manager (e.g. 1Password share), never plaintext email.
- **`--emails` is the allowlist.** Only these addresses can sign in to view the customer's reports. Addresses are stored as typed; enter them in lowercase.
- The publishing tool needs both `customer_id` and `api_key`.

Other operator commands:

| Command | Purpose |
|---|---|
| `list [--active-only] [--format table\|json]` | List customers |
| `get --email <email>` / `get --id <uuid>` | Look up a customer (email match is case-sensitive) |
| `add-email <customer_id> <email>` | Add a viewer to the allowlist |
| `remove-email <customer_id> <email>` | Remove a viewer from the allowlist. Case-sensitive match; does **not** end that viewer's existing sessions |
| `rotate-key <customer_id>` | Issue a new API key; the old one stops working immediately. Prints `new_api_key:` / `new_prefix:` |
| `deactivate <customer_id>` | Blocks the customer's **API key** (all agent calls return `401`). Reports are kept. Does **not** block viewer sign-in or end existing sessions |

---

### Publish Report

#### Request

```
POST /v1/reports
Authorization: Bearer <api_key>
Idempotency-Key: <UUID>
Content-Type: application/json
```

**Headers**

| Header | Required | Rule |
|---|---|---|
| `Authorization` | ✅ | `Bearer mvk_live_…`. Invalid, missing, or deactivated customer → `401` |
| `Idempotency-Key` | ✅ | A UUID. Missing or not a UUID → `422` |

**Body**

| Field | Required | Type / limits |
|---|---|---|
| `customer_id` | ✅ | UUID. Must match the customer that owns the API key, else `403` |
| `title` | ✅ | String, 1–500 chars |
| `generated_at` | ✅ | ISO 8601 datetime. Always include a timezone (e.g. `2026-09-13T08:00:00+07:00`); values without one are accepted but ambiguous |
| `html` | ✅ | Full report HTML as a string, max 10 MB (UTF-8 bytes) |
| `description` | — | String, max 5,000 chars. Default `""` |
| `tags` | — | Array of up to 50 strings, each 1–64 chars |
| `supplementary_files` | — | Array of up to 20 `{ filename, content_type, base64 }`, max 100 MB per decoded file |

Unknown fields are rejected with `422`. The request body as a whole has no size cap, but base64 adds ~33% to file sizes.

> **Supplementary files are stored but not yet served.** No viewer or API route returns them in v1, so don't link to them from the report HTML. Filenames must be unique within a report; duplicates overwrite each other.

#### Example

```bash
BODY=$(jq -n \
  --arg customer_id "$CUSTOMER_ID" \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --rawfile html report.html \
  '{customer_id: $customer_id, title: "Weekly Sales Summary",
    description: "Week 37", tags: ["sales","weekly"],
    generated_at: $generated_at, html: $html}')

curl -sS -X POST http://localhost:8000/v1/reports \
  -H "Authorization: Bearer $API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

`jq --rawfile` handles escaping of quotes and newlines in the HTML.

#### Response

```json
{
  "report_id": "0192…",
  "url": "https://hub.majeve.com/r/0192…",
  "created_at": "2026-09-13T01:00:00Z"
}
```

The `url` host is `HUB_PRIMARY_DOMAIN` and the scheme is always `https://`.

> **Local dev:** the returned `url` is `https://localhost:3000/r/<id>`, which won't load because the local viewer is plain http. Replace `https://` with `http://` when testing locally.

| Status | Meaning |
|---|---|
| `201` | Report published |
| `200` | Replay: this `Idempotency-Key` was already used by this customer; the original response is returned and nothing is re-published |
| `401` | Invalid API key or deactivated customer |
| `403` | `customer_id` does not belong to this API key |
| `422` | Validation failed (see error formats below) |
| `503` | Object storage unavailable; retry with the same `Idempotency-Key` |

**Error formats.** There are two shapes, so clients should handle both:

- **RFC 7807** (`application/problem+json`, with `type`, `title`, `status`, `detail`) for errors the Hub raises itself: `401`, `403`, `404`, `429`, `503`, and these `422`s: missing or invalid `Idempotency-Key`, invalid base64, supplementary file too large (`payload-too-large`), invalid `cursor` or timestamp.
- **FastAPI default** (`application/json`, `{"detail": [ … ]}`) for schema validation `422`s: missing or unknown fields, length or count limits, `html` over 10 MB, out-of-range `limit`.

#### Idempotency

- Generate a **new** UUID for each report, and reuse it **only** when retrying that same report. Reusing a key for a different report returns the first report with `200` and silently drops the new one; the body is not compared.
- Keys are scoped per customer.
- A key is guaranteed to replay for at least 24 hours. Expired keys are purged by a nightly job (03:17 UTC), so in practice a key replays for 24–48 hours; after the purge, the same key publishes a new report.
- A replay still needs a valid request body and matching `customer_id`, because validation runs before the replay lookup.
- Retry sequentially. Two concurrent requests with the same key can both fail instead of one replaying.

#### How the Hub serves your HTML

The HTML has no required structure, but when the Hub serves it inside the viewer iframe it injects a CSS shim, a viewport meta tag, and a resize script, and applies a Content-Security-Policy. Design reports with these in mind:

- **Injected CSS:** `html,body{overflow:hidden}`, `body{margin:0}`, `*{box-sizing:border-box}`, `:root{font-size:16px}`, `img,video,svg{max-width:100%;height:auto}`, `table{display:block;overflow-x:auto}`, `pre,code{white-space:pre-wrap}`.
- **Height:** the iframe is resized to `document.body.scrollHeight`. Avoid `100vh` / full-viewport layouts.
- **CSP:** scripts, styles, and fonts may be inline or from any `https:` origin; images only `data:` or `https:` (no `http:` or `blob:`); `fetch`/XHR only to `https:`. Everything else (frames, media, workers) is limited to the Hub's own origin.

`GET /v1/reports/{id}` returns the original HTML, without injections.

#### Sharing the report

The report `url` requires sign-in:

1. A recipient opens the `url` and enters their email.
2. If the email is on the customer's allowlist, they receive a single-use magic link that expires in 15 minutes. The page shows the same message whether or not the email is allowlisted.
3. Signing in creates a session that lasts **30 days** (extended on each view) and grants access to **all of that customer's reports**, not only the linked one.

The public email form is rate-limited to 5 requests per IP per minute and 10 per report per hour.

A tool can also mint the link itself, e.g. to deliver it over Slack or chat:

```bash
curl -sS -X POST http://localhost:8000/r/$REPORT_ID/request-link \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@acme.com", "delivery": "return", "channel_hint": "slack"}'
```

| Field | Rule |
|---|---|
| `email` | Must be on the customer's allowlist (case-insensitive) |
| `delivery` | `"email"` (Hub sends it, default) or `"return"` (URL returned in the response) |
| `channel_hint` | Must be present with `"return"`; audit label, max 64 chars |

Response `200`: `{ "status": "sent" | "returned", "url": "…" | null, "expires_in_minutes": 15 }`.

- A returned `url` is a login credential: don't log it or store it.
- `"sent"` means the link was minted and a send was attempted. Email failures are swallowed, so it does **not** confirm delivery.
- Without an `Authorization` header, the call takes the public path and always returns `200 {"status": "accepted", …}` regardless of outcome.

| Status | Meaning |
|---|---|
| `200` | Link sent or returned |
| `401` | `Authorization` header present but invalid |
| `404` | Report not found or belongs to another customer |
| `422` | Email not on allowlist (`email-not-allowlisted`), `channel_hint` missing with `"return"`, or invalid body |
| `429` | Over 100 requests per API key per hour; see the `Retry-After` header. Failed calls count toward the limit |

#### Other agent endpoints

**`GET /v1/reports`**: list or search the customer's reports.

| Query param | Rule |
|---|---|
| `search` | English full-text search over title, description, and tags; results ordered by relevance. Max 500 chars |
| `tags` | Comma-separated; a report must have **all** listed tags. Max 2,000 chars |
| `from`, `to` | ISO 8601, inclusive, filters `generated_at`. URL-encode offsets (`+07:00` → `%2B07:00`) |
| `limit` | 1–200, default 50 |
| `cursor` | Opaque; pass `next_cursor` from the previous page |

Without `search`, results are newest `generated_at` first. Response:

```json
{
  "items": [
    { "report_id": "…", "title": "…", "description": "…", "tags": ["…"],
      "generated_at": "…", "url": "…", "size_bytes": 12345 }
  ],
  "next_cursor": "…",
  "has_more": true
}
```

**`GET /v1/reports/{report_id}`**: one report. Returns the list item fields plus `html`. Sends an `ETag`; repeating the request with `If-None-Match: <etag>` (exact value) returns `304`. The ETag covers metadata only, not the HTML.

**`DELETE /v1/reports/{report_id}`**: delete a report. Returns `204`.

Both return `404` for a report that doesn't exist or belongs to another customer, and `503` if object storage is unavailable.
