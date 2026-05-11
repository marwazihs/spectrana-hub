# Onboarding a new Hub customer

This is the operator runbook for provisioning a new customer on Hub. Single
operator surface: `scripts/manage_customer.py`. Pattern 1 (Majeve-hosted) only —
self-hosted is deferred to v2.

## Who runs this

A Majeve operator with shell access to a Hub deploy. In v1 that's a single
human; we rely on shell history for the audit trail (an audit-log table is
v2 trigger: "ops team grows past one person").

## Prerequisites

- Hub is deployed and reachable via SSH or a Docker exec session.
- `DATABASE_URL` is set in the runtime env (the CLI reads it via
  `app.config.settings`, same as the app process). The CLI refuses to run if
  the URL doesn't contain `hub` — foot-gun guard against pointing at the
  wrong DB.

## Onboarding flow

### 1. Create the customer

Get the customer's name and the initial allowlist of email addresses (the
humans who'll receive magic links to view reports).

```bash
uv run python -m scripts.manage_customer create \
  --name "Acme Corporation" \
  --emails ops@acme.com,cfo@acme.com,audit@acme.com
```

Output:

```
customer_id: 0193ee2c-1234-7abc-9def-0123456789ab
api_key:     mvk_live_<43 chars of base64url>
prefix:      mvk_live
```

**The `api_key` is printed exactly once. There is no way to retrieve it
later.** If you lose it before delivering, you must `rotate-key` and start
over.

### 2. Deliver the api_key to Spectra operators

The api_key is the credential Spectra uses to publish reports. Deliver it
the same way you'd deliver any other production secret:

- **1Password share** (recommended — vault item with an expiry).
- An equivalent secrets-share tool with one-time-view semantics.

**Never email plaintext. Never paste in chat. Never commit to a repo.**

Tell the Spectra operator: the key lives in their secrets manager (e.g.
AWS Secrets Manager, 1Password, Doppler) and gets injected at runtime via
the appropriate Spectra env-var contract.

### 3. Verify

Confirm the customer is provisioned and the allowlist looks right:

```bash
uv run python -m scripts.manage_customer get --email ops@acme.com
```

You should see the `customer_id`, `prefix` matching what you just printed,
`is_active=true`, and the allowlist count.

## Other operations

### Add or remove an allowlist email

```bash
uv run python -m scripts.manage_customer add-email <customer_id> new@acme.com
uv run python -m scripts.manage_customer remove-email <customer_id> gone@acme.com
```

Both are idempotent — running twice is fine.

### Rotate the api_key (suspected leak, periodic rotation, etc.)

```bash
uv run python -m scripts.manage_customer rotate-key <customer_id>
```

Prints the new key exactly once. **The old key is immediately invalid.** You
must coordinate with the Spectra operator to roll the secret over and
restart their service — v1 has no overlap window (single active key per
customer; Spectra restart is the cutover). If a customer asks for
zero-downtime rotation, that's the v2 trigger.

### Deactivate (customer churn, dispute, etc.)

```bash
uv run python -m scripts.manage_customer deactivate <customer_id>
```

Sets `is_active=false`. Existing reports are **not deleted** — Hub keeps the
data, but new Spectra API calls with this customer's key are rejected.
Existing magic-link sessions remain valid until their natural 30-day
expiry. To force-revoke all sessions, ask the customer's recipients to log
out, or delete the `sessions` rows directly via psql.

### List

```bash
uv run python -m scripts.manage_customer list                  # active + inactive
uv run python -m scripts.manage_customer list --active-only
uv run python -m scripts.manage_customer list --format json    # for scripts/CI
```

Never prints secrets — `api_key_prefix` only.

## What this script will NOT do

These are v1 non-goals (PLAN.md §11):

- **Customer self-serve signup.** Majeve onboards manually.
- **Multi-key overlap rotation.** Single active key; Spectra restart is the
  cutover.
- **Audit log of who ran which command.** Single-operator ops; shell history
  is the audit trail.
- **Web admin panel.** CLI is the surface.

If any of these become painful, that's the trigger to revisit.
