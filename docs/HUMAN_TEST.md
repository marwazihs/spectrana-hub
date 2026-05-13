# Human end-to-end test — Spectrana Hub v1

This is the merge gate: the whole project should be exercised by a human
against the local docker-compose stack before `develop` merges to `master`.
The Playwright e2e suite covers the happy paths in code; this document
covers the things only humans catch — visual feel, mobile touch, anti-enum
timing, error-state UX, real email rendering.

If anything below fails, fix on `develop` with a regression test, commit,
`docker compose down -v && ./scripts/human-test-bootstrap.sh`, and re-run.

---

## 0. Prep

### 0.1 Configure SMTP (optional but recommended)

Email-delivery scenarios (S6, S16) need a working SMTP relay. The bootstrap
script itself uses `delivery=return` and doesn't need email — it can print a
clickable link directly. But if you want to test the full inbox flow, populate
`.env` in the project root (it's already gitignored):

```
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
SMTP_STARTTLS=true
HUB_EMAIL_FROM=You <you@your-verified-domain.com>
HUB_EMAIL_BRAND_NAME=Spectrana Hub
```

Compose auto-loads `.env`. The bootstrap script will detect drift (`.env`
populated but the running hub container has an empty `SMTP_HOST`) and
`--force-recreate hub` for you, then run a real connect → STARTTLS → login
SMTP probe before going further. **If the probe fails, the script tears the
stack down and exits non-zero** — better than proceeding with a silently
broken email path.

Skip this step if you only want to walk the in-browser scenarios; just leave
`SMTP_HOST` unset.

### 0.2 Bring up the stack and bootstrap a test report

```bash
./scripts/human-test-bootstrap.sh
```

That script:

1. Runs `docker compose up -d` if the stack isn't already up.
2. Waits for `/healthz`.
3. If `.env` has new SMTP values that aren't injected yet, recreates `hub`.
4. Runs the SMTP smoke test (skipped if `SMTP_HOST` empty). Failure → teardown.
5. Creates a customer (`Bootstrap Test Co`) with `TEST_EMAIL` allowlisted.
   Defaults to `marwazihs@gmail.com`; override via `TEST_EMAIL=you@example.com`.
6. Publishes `report-sample/indonesia-credit-card-dashboard.html` as the
   embedded report. Plotly charts via CDN, ~17 years of monthly time-series
   data, three sections (national trend, seasonality, Jakarta vs Bali).
   Override with `REPORT_HTML_FILE=path/to/other.html`.
7. Mints a single-use magic link in return-mode and prints the consume URL.
8. On a TTY, asks if you want to `open` the URL in your default browser.

Re-running creates a fresh customer + report (new IDs each time). Reset
everything with `docker compose down -v && ./scripts/human-test-bootstrap.sh`.

### 0.3 Manual fallback (if you want to walk it by hand)

```bash
# 1. Stack
docker compose up -d
curl -sf http://localhost:8000/healthz   # {"status":"ok"}

# 2. Customer
docker compose exec hub uv run python -m scripts.manage_customer create \
  --name "Test Co" --emails you@example.com
# Copy customer_id and api_key from the output:
export CUSTOMER_ID='...'
export API_KEY='mvk_live_...'

# 3. Publish (JSON body — html inline as a string; --rawfile slurps a file)
jq -n --arg cid "$CUSTOMER_ID" --arg gen "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --rawfile html report-sample/indonesia-credit-card-dashboard.html \
  '{customer_id:$cid, title:"Indonesia Q4", description:"", tags:["sample"],
    generated_at:$gen, html:$html, supplementary_files:[]}' \
| curl -sS -X POST http://localhost:8000/v1/reports \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $(uuidgen)" \
    -d @- | tee /tmp/publish.json
export REPORT_ID=$(jq -r .report_id /tmp/publish.json)

# 4. Mint link (return-mode → URL in response; comes back http:// for local
#    because HUB_URL_SCHEME=http is set in compose. No bash rewrite needed.)
curl -sS -X POST "http://localhost:8000/r/$REPORT_ID/request-link" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","delivery":"return","channel_hint":"manual"}' \
| jq -r '.url'
```

---

## Scenarios

Each scenario is a checkbox. Open the consume URL from step 0 in a real
browser and walk through:

### S1 — First click, chrome + charts render

- [ ] Lands on `/r/[id]` with chrome rendered (no "Link expired" page)
- [ ] Title, customer name, generated-at all show
- [ ] Iframe loads with report content visible
- [ ] **Plotly charts render**: national trend (3 lines), seasonality
      (bar + line), Jakarta vs Bali (grouped bars). If the iframe shows the
      surrounding cards/text but the chart areas are blank, the CSP is
      blocking the CDN — that's a regression on the M9 CSP relaxation.
- [ ] No console errors in DevTools (CSP violations show up here)

### S2 — Iframe height adjusts past placeholder

- [ ] No nested scrollbar inside the iframe
- [ ] Iframe height matches inner content (reads as one continuous page)
- [ ] Chrome (header, footer) stays put while content scrolls

### S3 — Desktop resize fidelity

- [ ] Slowly resize the browser window between 600px and 1400px wide
- [ ] Iframe height re-adjusts smoothly; no visible flicker
- [ ] Plotly charts re-flow (they're `responsive: true`)
- [ ] Chrome layout stays stable; max-width caps at 1200px

### S4 — Mobile feel (375px viewport)

DevTools → Toggle device toolbar → iPhone 12 (or any 375px width).

- [ ] Iframe is **edge-to-edge** — no side padding on the iframe wrap
- [ ] Report title shrinks to ~24px
- [ ] **Sign out** button still tappable (≥44px touch target)
- [ ] No horizontal scrollbar on the page
- [ ] Content inside the iframe reflows; KPI cards stack to 1 column

### S5 — Sign out

- [ ] Click **Sign out**
- [ ] Lands back on `/r/[id]` email-entry page (NOT on `0.0.0.0:3000` —
      the M8.1 host-header fix guards against this regression)
- [ ] DevTools → Application → Cookies → `localhost:3000`: `hub_session`
      is gone
- [ ] URL bar shows `localhost:3000/r/[id]`, not `0.0.0.0:3000/...`

### S6 — Real email delivery (skip if SMTP not configured)

- [ ] Submit your `TEST_EMAIL` on the email-entry page from S5
- [ ] **Check your inbox.** Magic link should arrive within ~30s
- [ ] Subject line reads `Your <HUB_EMAIL_BRAND_NAME> report link`
- [ ] From name matches `HUB_EMAIL_FROM`
- [ ] Body has a clear CTA / link to the report
- [ ] **Click the link in the email** — should sign you in directly
      (NOT land on the email-entry form). If it does land on the form,
      the M9 emailed-link path regression is back: `build_magic_link`
      must produce `/r/<id>/consume?token=...`, not `/r/<id>?token=...`.
- [ ] URL scheme matches `HUB_URL_SCHEME` — `http` for local compose,
      `https` for any deployed env

### S7 — Email submit, anti-enum copy

- [ ] Sign out again (or use a fresh private window)
- [ ] Type `TEST_EMAIL` into the form on `/r/[id]`
- [ ] Submit
- [ ] Form swaps to: "If this email is on file, a link has been sent."
- [ ] Email input field disappears; heading remains

### S8 — Anti-enum parity (the load-bearing check)

- [ ] Open a private window, navigate to `/r/<same-id>`
- [ ] Submit a **bogus email** like `not-on-allowlist@example.com`
- [ ] **Response text is byte-identical to S7** (same wording, same DOM)
- [ ] DevTools → Network → compare both submissions' response times
- [ ] Times should be within ~50ms (the rate-limit gate fires before any
      branch on report existence/allowlist membership)

### S9 — Replayed link (single-use after dedup window)

The consume route caches successful consumes for ~90s to handle
duplicate-fire from prefetch/macOS-LaunchServices/etc — clicking the same
link twice in quick succession both succeed. After the cache expires,
replay returns 410 as expected.

- [ ] Take the consume URL from step 0 (already clicked in S1)
- [ ] Paste it again in a fresh tab within ~90s → still works (dedup)
- [ ] Wait 90s, paste it again → "This link has expired" page (HTTP 410)
- [ ] Big headline, body copy, "Request a new link" button visible
- [ ] Button leads back to `/r/[id]` email-entry

### S10 — Fabricated report ID (anti-enum)

- [ ] Navigate to `http://localhost:3000/r/00000000-0000-0000-0000-000000000000`
- [ ] Renders the **email-entry page** as if the report existed
- [ ] No 404, no leak that the ID is invalid

### S11 — Viewer security headers

DevTools → Network → click the `/r/[id]` request → Response headers.

- [ ] `X-Content-Type-Options: nosniff`
- [ ] `Referrer-Policy: no-referrer`
- [ ] `X-Frame-Options: DENY` (the chrome page itself refuses to be framed)

### S12 — Iframe origin security headers

DevTools → Network → click the `localhost:8000/render/...` request → Response headers.

- [ ] `Content-Security-Policy` contains `frame-ancestors localhost:3000`
- [ ] CSP allows `https:` for `script-src`, `style-src`, `font-src`,
      `connect-src` (needed for Plotly + other CDN libs)
- [ ] `Cache-Control: private, no-cache, must-revalidate`
- [ ] `ETag` ends with `v2` (the injector version)
- [ ] `Referrer-Policy: no-referrer`

### S13 — Host-header guard on /render

From a terminal:

```bash
# Right host → 401 (route exists, JWT is fake)
curl -sI "http://localhost:8000/render/00000000-0000-0000-0000-000000000000?t=fake" | head -1
# HTTP/1.1 401 Unauthorized

# Wrong host → 404 (guard fires, looks like the route doesn't exist)
curl -sI -H "Host: hub.test" \
  "http://localhost:8000/render/00000000-0000-0000-0000-000000000000?t=fake" | head -1
# HTTP/1.1 404 Not Found
```

- [ ] Right host returns 401
- [ ] Wrong host returns 404

### S14 — Expired link (TTL boundary)

Two options:

**Patient:** mint a fresh link, wait 16 minutes, click it.

**Impatient:** mint a fresh link, then expire it via SQL:

```bash
docker compose exec postgres psql -U hub -d hub -c \
  "UPDATE magic_link_tokens SET expires_at = NOW() - INTERVAL '1 minute' \
   WHERE consumed_at IS NULL ORDER BY created_at DESC LIMIT 1;"
```

- [ ] Click the (now expired) link
- [ ] "This link has expired" page renders (HTTP 410)
- [ ] Same surface as S9 (post-90s)

### S15 — Session sliding-window refresh

- [ ] Mint + click a fresh link (you're signed in)
- [ ] Leave the tab open ~90 seconds
- [ ] Reload `/r/[id]` — chrome still renders (no re-auth)
- [ ] DevTools → Network → /r/[id] request → no 302 to email-entry
- [ ] (The session row's `last_accessed_at` and `expires_at` slide forward
      server-side; the cookie value is unchanged.)

### S16 — SMTP probe failure mode (skip if not testing SMTP path)

Verify the bootstrap script's fail-closed behavior on a bad SMTP config.

```bash
docker compose down -v
# Temporarily break .env (wrong password)
SMTP_PASS_BACKUP=$(grep '^SMTP_PASS=' .env)
sed -i.bak 's/^SMTP_PASS=.*/SMTP_PASS=wrong-password-on-purpose/' .env
./scripts/human-test-bootstrap.sh
# Restore
echo "$SMTP_PASS_BACKUP" > /tmp/x && mv .env.bak .env
```

- [ ] Script reaches "SMTP smoke test" step and prints the SMTP error class
- [ ] Script runs `docker compose down` automatically
- [ ] Exit code is non-zero
- [ ] Stack is gone (`docker compose ps` shows nothing)

---

## Not covered here (test elsewhere or in remote-deploy phase)

| Concern | Why it's not in this runbook |
|---|---|
| TLS / HTTPS cookie `Secure` flag | Local stack is http; needs remote deploy with real TLS |
| Real DNS / `reports.hub.<domain>` routing under HTTPS | Local uses `localhost:8000`; needs a reverse proxy in front |
| Concurrent load / DB pool exhaustion | No load test in v1; capture via real production metrics |
| Real S3 IAM / region behavior | MinIO is API-compatible but doesn't reproduce subtle IAM edge cases |
| Email deliverability (SPF/DKIM/bounce handling) | Hosting + DNS work, separate from the app |

---

## Pass / fail decision

**Ready to merge to master when:**

- [ ] S1–S5 all pass — chrome, charts, resize, mobile, sign-out
- [ ] S6 emailed-link works end-to-end (or SMTP intentionally not configured)
- [ ] S7–S8 anti-enum copy + timing pass — **S8 is load-bearing**
- [ ] S9–S10 single-use + fake-ID surfaces match
- [ ] S11–S13 security headers + host guard pass
- [ ] S14 expired-link state looks right
- [ ] S15 session refresh works
- [ ] S16 SMTP fail-closed works (or SMTP not configured)
- [ ] No console errors in DevTools across the whole flow
- [ ] Mobile feel is acceptable (S4)

If any scenario fails, fix on `develop` (commit + regression test), then:

```bash
docker compose down -v
./scripts/human-test-bootstrap.sh    # fresh stack, fresh data
# re-walk the affected scenario
```

When the list is clean:

```bash
git checkout master
git merge --no-ff develop
git push
```
