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

### Bring up the stack and bootstrap a test report

```bash
./scripts/human-test-bootstrap.sh
```

That script:

1. Runs `docker compose up -d` if the stack isn't already up.
2. Waits for `/healthz`.
3. Creates a customer (`Bootstrap Test Co`) with `TEST_EMAIL` allowlisted.
   Defaults to `marwazihs@gmail.com`; override via `TEST_EMAIL=you@example.com`.
4. Publishes a fixture report (~4 sections, long enough to exercise the
   resize-poster past the 640px placeholder).
5. Mints a single-use magic link in return-mode.
6. Prints the consume URL ready to click, and offers to `open` it on macOS.

Re-running creates a fresh customer + report (new IDs each time). Reset
everything with `docker compose down -v && ./scripts/human-test-bootstrap.sh`.

### Manual fallback (if you want to walk it by hand)

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

# 3. Publish (JSON body — html inline as a string)
jq -n --arg cid "$CUSTOMER_ID" --arg gen "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{customer_id:$cid, title:"Q1", description:"", tags:["q1"],
    generated_at:$gen, html:"<html><body><h1>Hi</h1></body></html>",
    supplementary_files:[]}' \
| curl -sS -X POST http://localhost:8000/v1/reports \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: $(uuidgen)" \
    -d @- | tee /tmp/publish.json
export REPORT_ID=$(jq -r .report_id /tmp/publish.json)

# 4. Mint link (return-mode → URL in response, no SMTP needed)
curl -sS -X POST "http://localhost:8000/r/$REPORT_ID/request-link" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","delivery":"return","channel_hint":"manual"}' \
| jq -r '.url' | sed 's|https://|http://|'
```

---

## Scenarios

Each scenario is a checkbox. Open the consume URL from step 0 in a real
browser and walk through:

### S1 — First click, chrome renders

- [ ] Lands on `/r/[id]` with chrome rendered
- [ ] Title, customer name, generated-at all show
- [ ] Iframe loads with report content visible

### S2 — Iframe height adjusts past placeholder

- [ ] No nested scrollbar inside the iframe
- [ ] Iframe height matches inner content (reads as one continuous page)
- [ ] Chrome (header, footer) stays put while content scrolls

### S3 — Desktop resize fidelity

- [ ] Slowly resize the browser window between 600px and 1400px wide
- [ ] Iframe height re-adjusts smoothly; no visible flicker
- [ ] Chrome layout stays stable; max-width caps at 1200px

### S4 — Mobile feel (375px viewport)

DevTools → Toggle device toolbar → iPhone 12 (or any 375px width).

- [ ] Iframe is **edge-to-edge** — no side padding on the iframe wrap
- [ ] Report title shrinks to ~24px
- [ ] **Sign out** button still tappable (≥44px touch target)
- [ ] No horizontal scrollbar on the page
- [ ] Content inside the iframe reflows; no horizontal scroll inside either

### S5 — Sign out

- [ ] Click **Sign out**
- [ ] Lands back on `/r/[id]` email-entry page
- [ ] DevTools → Application → Cookies → `localhost:3000`: `hub_session` is gone

### S6 — Sign out persists across refresh

- [ ] Refresh the page (the email-entry page from S5)
- [ ] Still on email-entry; no iframe; cookie still absent

### S7 — Email submit (real email)

- [ ] Type `TEST_EMAIL` (the same one the bootstrap script used) into the form
- [ ] Submit
- [ ] Form swaps to: "If this email is on file, a link has been sent."
- [ ] Email input field disappears; heading remains

### S8 — Anti-enum parity (the load-bearing check)

- [ ] Open a private window, navigate to `/r/<same-id>`
- [ ] Submit a **bogus email** like `not-on-allowlist@example.com`
- [ ] **Response text is byte-identical to S7** (same wording, same DOM)
- [ ] DevTools → Network → compare both submissions' response times
- [ ] Times should be within ~50ms (the rate-limit gate fires before any branch on report existence/allowlist membership)

### S9 — Replayed link (single-use)

- [ ] Take the consume URL from step 0 (already clicked in S1)
- [ ] Paste it again in a fresh tab
- [ ] Renders the "This link has expired" page (HTTP 410)
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
- [ ] Same surface as S9

### S15 — Session sliding-window refresh

- [ ] Mint + click a fresh link (you're signed in)
- [ ] Leave the tab open ~90 seconds
- [ ] Reload `/r/[id]` — chrome still renders (no re-auth)
- [ ] DevTools → Network → /r/[id] request → no 302 to email-entry
- [ ] (The session row's `last_accessed_at` and `expires_at` slide forward server-side; the cookie value is unchanged.)

---

## Optional: real email rendering (requires SMTP)

If you want to verify the actual email template:

1. Add SMTP env vars to `docker-compose.yml` under the `hub` service:
   ```yaml
   SMTP_HOST: smtp.your-provider.com
   SMTP_PORT: 587
   SMTP_USER: ...
   SMTP_PASS: ...
   HUB_EMAIL_FROM: 'Hub Test <you@yourdomain.com>'
   ```
2. `docker compose up -d --build hub`
3. Mint a link with `delivery: "email"` instead of `"return"`:
   ```bash
   curl -sS -X POST "http://localhost:8000/r/$REPORT_ID/request-link" \
     -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"email":"you@example.com","delivery":"email"}'
   ```
4. Check your inbox.

- [ ] From name matches `HUB_EMAIL_FROM`
- [ ] Subject line reads sensibly ("Your Majeve report is ready" or similar)
- [ ] Body has a clear CTA button
- [ ] CTA link opens the consume URL and the flow works end-to-end via email

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

- [ ] S1–S13 all pass as described
- [ ] S14 expired-link state looks right (use SQL fast-path or wait 16 minutes)
- [ ] S15 session refresh works
- [ ] No console errors in the browser DevTools across the whole flow
- [ ] Mobile feel is acceptable (S4)
- [ ] **S8 anti-enum response timing doesn't reveal allowlist membership** — this is the load-bearing one

If any scenario fails, fix on `develop` (commit + test), then:

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
