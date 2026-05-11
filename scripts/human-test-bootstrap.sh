#!/usr/bin/env bash
# scripts/human-test-bootstrap.sh
#
# One-shot bootstrap for the human-test pass against the local docker-compose
# stack. Brings up the stack if it's not running, provisions a fresh customer
# with TEST_EMAIL allowlisted, publishes a fixture report, mints a magic link,
# and prints the consume URL ready to click.
#
# Usage:
#   ./scripts/human-test-bootstrap.sh                  # uses defaults
#   TEST_EMAIL=you@example.com ./scripts/human-test-bootstrap.sh
#   CUSTOMER_NAME="Test Co" ./scripts/human-test-bootstrap.sh
#
# Re-run is safe but not idempotent: each run creates a new customer + report.
# Wipe with `docker compose down -v` to reset.
#
# See docs/HUMAN_TEST.md for the full S1-S15 scenarios to walk through after
# the printed URL opens.

set -euo pipefail

# --- config ----------------------------------------------------------------

TEST_EMAIL="${TEST_EMAIL:-marwazihs@gmail.com}"
CUSTOMER_NAME="${CUSTOMER_NAME:-Bootstrap Test Co}"
HUB_URL="${HUB_URL:-http://localhost:8000}"
VIEWER_URL="${VIEWER_URL:-http://localhost:3000}"

# --- pretty output ---------------------------------------------------------

c_blue=$'\033[34m'
c_green=$'\033[32m'
c_red=$'\033[31m'
c_dim=$'\033[2m'
c_off=$'\033[0m'

step() { printf '%s==>%s %s\n' "$c_blue" "$c_off" "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_off" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------

command -v docker >/dev/null || die "docker not found on PATH"
command -v jq     >/dev/null || die "jq not found on PATH (brew install jq)"
command -v curl   >/dev/null || die "curl not found on PATH"

# --- 1. Stack up? ----------------------------------------------------------

step "Checking stack health at $HUB_URL/healthz"
if ! curl -fsS "$HUB_URL/healthz" >/dev/null 2>&1; then
  step "Stack not responding — running 'docker compose up -d'"
  docker compose up -d
  # Wait up to 60s for /healthz to come up. The migrate + bucket-init
  # one-shots run before hub starts, so first-up can take ~20s.
  for i in $(seq 1 60); do
    if curl -fsS "$HUB_URL/healthz" >/dev/null 2>&1; then
      ok "Hub responsive after ${i}s"
      break
    fi
    sleep 1
    if [ "$i" -eq 60 ]; then
      die "Hub did not become healthy within 60s. Check: docker compose logs hub"
    fi
  done
else
  ok "Hub already up"
fi

# Confirm Next.js is also up. The root path 404s (no index route) so we
# probe a known-shape path and accept any HTTP response as "alive".
VIEWER_PROBE_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$VIEWER_URL/r/probe" 2>/dev/null || echo "000")
case "$VIEWER_PROBE_CODE" in
  2*|3*|4*) ok "Viewer up at $VIEWER_URL (probe → $VIEWER_PROBE_CODE)" ;;
  *) die "Viewer not responding at $VIEWER_URL (got $VIEWER_PROBE_CODE). Check: docker compose logs frontend" ;;
esac

# --- 2. Provision customer -------------------------------------------------

step "Creating customer '$CUSTOMER_NAME' with allowlist [$TEST_EMAIL]"
CREATE_OUT=$(
  docker compose exec -T hub \
    uv run python -m scripts.manage_customer create \
    --name "$CUSTOMER_NAME" \
    --emails "$TEST_EMAIL"
)

CUSTOMER_ID=$(printf '%s\n' "$CREATE_OUT" | awk '/^customer_id:/ {print $2}')
API_KEY=$(printf     '%s\n' "$CREATE_OUT" | awk '/^api_key:/     {print $2}')

[ -n "$CUSTOMER_ID" ] || die "Could not parse customer_id from create output:\n$CREATE_OUT"
[ -n "$API_KEY" ]     || die "Could not parse api_key from create output:\n$CREATE_OUT"

ok "customer_id: $CUSTOMER_ID"
ok "api_key:     ${API_KEY:0:12}…  (full value in API_KEY var; one-shot, won't reprint)"

# --- 3. Publish a fixture report -------------------------------------------

# Build a JSON body with a multi-section HTML payload large enough to exercise
# the iframe resize-poster (long content forces height >640 placeholder) plus
# a short section at the end so S2-S4 see a real layout. The HTML is inlined
# as a string per the publish contract (app/reports/schemas.py:PublishRequest).

step "Publishing fixture report"

HTML='<!doctype html><html><head><meta charset="utf-8"><title>Bootstrap Q1 Report</title>
<style>body{font-family:system-ui,sans-serif;line-height:1.55;color:#212121;margin:0;padding:24px;max-width:760px}
h1{font-size:32px;margin:0 0 16px}
h2{font-size:22px;margin:48px 0 12px;color:#222}
section{padding:24px 0;border-bottom:1px solid #eee}
.metric{display:inline-block;margin-right:24px}
.metric strong{display:block;font-size:28px;color:#0a7}
.lipsum{color:#555;font-size:15px}</style></head>
<body>
<h1>Bootstrap Q1 Report</h1>
<p class="lipsum">Synthetic data for the local human-test pass. Generated at bootstrap time.</p>
<section><h2>Key metrics</h2>
<div class="metric"><strong>+12%</strong>Revenue QoQ</div>
<div class="metric"><strong>+8%</strong>Active users</div>
<div class="metric"><strong>-3pp</strong>Churn</div></section>
<section><h2>Narrative</h2>
<p class="lipsum">Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
<p class="lipsum">Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.</p></section>
<section><h2>Detail</h2>
<p class="lipsum">Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium, totam rem aperiam, eaque ipsa quae ab illo inventore veritatis et quasi architecto beatae vitae dicta sunt explicabo.</p>
<p class="lipsum">Nemo enim ipsam voluptatem quia voluptas sit aspernatur aut odit aut fugit, sed quia consequuntur magni dolores eos qui ratione voluptatem sequi nesciunt.</p>
<p class="lipsum">Neque porro quisquam est, qui dolorem ipsum quia dolor sit amet, consectetur, adipisci velit, sed quia non numquam eius modi tempora incidunt ut labore et dolore magnam aliquam quaerat voluptatem.</p></section>
<section><h2>Closing</h2>
<p class="lipsum">End of report. Iframe should grow to match this content height; chrome stays put above and below.</p></section>
</body></html>'

BODY=$(jq -n \
  --arg customer_id "$CUSTOMER_ID" \
  --arg title "Bootstrap Q1 Report" \
  --arg description "Synthetic fixture for human-test pass" \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg html "$HTML" \
  '{customer_id: $customer_id, title: $title, description: $description,
    tags: ["q1","bootstrap"], generated_at: $generated_at, html: $html,
    supplementary_files: []}')

PUBLISH_OUT=$(curl -sS -X POST "$HUB_URL/v1/reports" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d "$BODY")

REPORT_ID=$(printf '%s' "$PUBLISH_OUT" | jq -r '.report_id // empty')
[ -n "$REPORT_ID" ] || die "Could not parse report_id from publish response:\n$PUBLISH_OUT"
ok "report_id:   $REPORT_ID"

# --- 4. Mint a magic link (return-mode, no SMTP needed) --------------------

step "Minting magic link (delivery=return)"
MINT_BODY=$(jq -n \
  --arg email "$TEST_EMAIL" \
  '{email: $email, delivery: "return", channel_hint: "human-test-bootstrap"}')

MINT_OUT=$(curl -sS -X POST "$HUB_URL/r/$REPORT_ID/request-link" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$MINT_BODY")

CONSUME_URL=$(printf '%s' "$MINT_OUT" | jq -r '.url // empty')
[ -n "$CONSUME_URL" ] || die "Could not parse url from mint response:\n$MINT_OUT"

# Hub's _viewer_consume_url hardcodes https:// (correct for prod). Local
# compose runs over http, so rewrite the scheme when the viewer is http.
# Same trick the e2e harness uses (tests/e2e/conftest.py mint_link()).
case "$VIEWER_URL" in
  http://*)
    CONSUME_URL="${CONSUME_URL/https:\/\//http://}"
    ;;
esac
ok "Consume URL ready"

# --- 5. Print the click target + next steps --------------------------------

cat <<EOF

${c_green}════════════════════════════════════════════════════════════════${c_off}
${c_green}READY FOR HUMAN TEST${c_off}
${c_green}════════════════════════════════════════════════════════════════${c_off}

  Customer:    $CUSTOMER_NAME ($CUSTOMER_ID)
  Test email:  $TEST_EMAIL
  Report:      Bootstrap Q1 Report ($REPORT_ID)

  ${c_blue}Click to start:${c_off}
  $CONSUME_URL

  Walk through scenarios S1-S15 in:
    docs/HUMAN_TEST.md

  ${c_dim}Re-mint a fresh link (the one above is single-use):${c_off}
    curl -sS -X POST "$HUB_URL/r/$REPORT_ID/request-link" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d '{"email":"$TEST_EMAIL","delivery":"return","channel_hint":"rerun"}' \\
      | jq -r .url

  ${c_dim}Reset everything and start over:${c_off}
    docker compose down -v && ./scripts/human-test-bootstrap.sh

EOF

# Auto-open on macOS if a TTY (interactive run).
if [ -t 1 ] && command -v open >/dev/null 2>&1; then
  read -r -p "Open the URL in your browser now? [Y/n] " ans
  case "${ans:-Y}" in
    [Yy]*|"") open "$CONSUME_URL" ;;
  esac
fi
