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

# --- 1b. SMTP smoke test ---------------------------------------------------
#
# Compose auto-loads `.env` into the hub container's environment, so
# SMTP_HOST/PORT/USER/PASS/STARTTLS are already set inside the container if
# the operator populated `.env`. We mirror that locally (best-effort, just
# for display) and then run a real connect+STARTTLS+login round-trip from
# inside the hub container using the same aiosmtplib client the real send
# path uses. Same library, same TLS stack — if this passes, the live send
# path will too.
#
# Failure policy: if SMTP_HOST is set but the smoke test fails, tear the
# stack down (`docker compose down`) and exit non-zero. A misconfigured
# SMTP integration that fails silently at runtime is worse than refusing
# to bootstrap — the human test would proceed thinking emails are wired
# when they're not. If SMTP_HOST is empty, we continue with a warning;
# the bootstrap script itself uses `delivery=return` and doesn't need
# email to print a clickable link.

# Compose auto-loads `.env` into each service's env at container creation.
# We don't `source .env` in bash because values like `HUB_EMAIL_FROM=Name
# <addr@host>` contain shell-meta characters that would error or worse.
# Read the SMTP_HOST value as the hub container actually sees it.
SMTP_HOST_IN_CONTAINER=$(docker compose exec -T hub printenv SMTP_HOST 2>/dev/null | tr -d '\r\n' || true)

# If `.env` exists with SMTP settings but the container reports SMTP_HOST
# empty, the container was created before .env was populated. Recreate so
# compose re-injects the values. Detect by grepping `.env` directly (no
# bash sourcing — avoids the shell-meta trap above).
if [ -z "$SMTP_HOST_IN_CONTAINER" ] && [ -f .env ] && grep -qE '^[[:space:]]*SMTP_HOST=.+' .env; then
  step "SMTP_HOST is set in .env but empty in the hub container — recreating hub"
  docker compose up -d --force-recreate hub >/dev/null 2>&1 || die "Failed to recreate hub container"
  for i in $(seq 1 30); do
    if curl -fsS "$HUB_URL/healthz" >/dev/null 2>&1; then
      ok "Hub healthy again after recreate (${i}s)"
      break
    fi
    sleep 1
    [ "$i" -eq 30 ] && die "Hub did not come back healthy after recreate"
  done
  SMTP_HOST_IN_CONTAINER=$(docker compose exec -T hub printenv SMTP_HOST 2>/dev/null | tr -d '\r\n' || true)
fi

if [ -z "$SMTP_HOST_IN_CONTAINER" ]; then
  printf '%s• SMTP_HOST not configured — magic-link emails will silently no-op.%s\n' "$c_dim" "$c_off"
  printf '%s  Populate SMTP_HOST/PORT/USER/PASS/STARTTLS in .env, then re-run.%s\n' "$c_dim" "$c_off"
else
  SMTP_PORT_IN_CONTAINER=$(docker compose exec -T hub printenv SMTP_PORT 2>/dev/null | tr -d '\r\n' || echo "587")
  step "SMTP smoke test: $SMTP_HOST_IN_CONTAINER:${SMTP_PORT_IN_CONTAINER:-587}"

  # The probe script lives in-process inside the hub container so it
  # picks up the SAME env vars + the SAME aiosmtplib version the live
  # send path uses. stdin via heredoc keeps the script out of the repo.
  if ! docker compose exec -T hub uv run python - <<'PY'
import asyncio
import os
import sys

import aiosmtplib


async def main() -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        print("SMTP_HOST is empty inside the hub container", file=sys.stderr)
        sys.exit(2)
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_starttls = os.environ.get("SMTP_STARTTLS", "true").lower() == "true"
    user = os.environ.get("SMTP_USER") or None
    password = os.environ.get("SMTP_PASS") or None

    # start_tls=False here: we open a plaintext connection, then upgrade
    # via STARTTLS only if requested. This matches the live send path in
    # app/auth/email.py and supports both implicit-TLS (port 465 with
    # start_tls=True at send time) and STARTTLS (587) servers when the
    # operator's .env is shaped correctly.
    client = aiosmtplib.SMTP(hostname=host, port=port, start_tls=False, timeout=10)
    try:
        await client.connect()
        if use_starttls:
            await client.starttls()
        if user and password:
            await client.login(user, password)
        await client.quit()
    except Exception as exc:  # noqa: BLE001 — surface any SMTP-side error
        print(f"SMTP probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    print("SMTP probe OK", flush=True)


asyncio.run(main())
PY
  then
    printf '%s✗ SMTP probe failed. Tearing down the stack so the next run starts clean.%s\n' "$c_red" "$c_off" >&2
    docker compose down
    die "Fix SMTP_* in .env and re-run. Common causes: wrong port (587 STARTTLS vs 465 implicit-TLS), app-password not enabled, host firewall blocking outbound 587."
  fi
  ok "SMTP probe OK ($SMTP_HOST_IN_CONTAINER)"
fi

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

# Use the Indonesia credit-card dashboard sample as the embedded report.
# `REPORT_HTML_FILE` override lets you swap fixtures without editing this script.

REPORT_HTML_FILE="${REPORT_HTML_FILE:-report-sample/indonesia-credit-card-dashboard.html}"
REPORT_TITLE="${REPORT_TITLE:-Indonesia’s Credit Card Story}"
REPORT_DESCRIPTION="${REPORT_DESCRIPTION:-Growth nationwide, divergence by region — 2009-01 → 2025-11}"

[ -f "$REPORT_HTML_FILE" ] || die "Report HTML not found: $REPORT_HTML_FILE"

step "Publishing report from $REPORT_HTML_FILE"

# --rawfile slurps the file as a single JSON string, handling all escaping
# (quotes, newlines, backslashes) correctly. Avoids the brittle bash-quoting
# games the earlier inline HTML required.
BODY=$(jq -n \
  --arg customer_id "$CUSTOMER_ID" \
  --arg title "$REPORT_TITLE" \
  --arg description "$REPORT_DESCRIPTION" \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --rawfile html "$REPORT_HTML_FILE" \
  '{customer_id: $customer_id, title: $title, description: $description,
    tags: ["sample","indonesia-credit-card"], generated_at: $generated_at,
    html: $html, supplementary_files: []}')

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
  Report:      $REPORT_TITLE ($REPORT_ID)

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
