# Lulu License Worker

Source artifact only. This folder documents and implements the intended Cloudflare Workers + D1 authorization service shape without creating, deploying, or modifying real Cloudflare resources.

Do not commit or paste:

- Cloudflare account IDs
- D1 binding IDs
- admin tokens
- `CODE_HASH_PEPPER` values
- plaintext license codes
- plaintext lease tokens
- `RATE_LIMIT_PEPPER` values

Local App boundary:

- The desktop app talks to generic `/v1/activate`, `/v1/verify`, and
  `/v1/deactivate` HTTPS contracts.
- The app never receives D1/admin privileges.
- The app never uploads user content, model paths, Obsidian/Feishu paths, or third-party API keys.
- Production/staging selection happens outside source code through endpoint configuration.
- Self-deactivation only accepts the authenticated current device. This source
  does not expose a user device list, remote kick-out, remote replacement, or
  third-device swap API.

Security controls represented here:

- `licenses.code_hash` stores hashed license codes only.
- `lease_tokens.token_hash` stores lease token hashes only and supports revoke/expiry.
- `rate_limits` stores activation/verify failure windows and cooldowns server-side.
- `rate_limits.bucket` stores a server-peppered hash of the Cloudflare client IP context and route, never the raw client IP or forwarded header value.
- Device records store privacy-preserving `device_hash`, not raw hardware IDs.
- Admin endpoints keep `x-admin-token` only for server-to-server/CLI operations backed by a Worker secret; the desktop app and browser admin page must never know it.
- The Worker browser admin page is not production-enabled by source alone. Production management uses the localhost gateway, which keeps the long-lived admin token in its Node backend. Browser JavaScript must not store or send the long-lived admin token. A future public browser admin must add a separately reviewed, cryptographically verified server-side identity gate.
- `/admin` and `/admin/*` responses set explicit no-store headers so management pages, lists, and state-change responses are not cached by browsers or shared intermediaries.
- Web-admin state changes require both the server-side identity gate and a same-origin request proof: the request `Origin` must match the Worker request origin, and the browser page sends the non-secret `x-lulu-admin-csrf: same-origin` header. Missing or foreign origins fail closed. The Worker does not enable CORS for admin state changes.
- The browser admin page can capture issue ledger fields (`recipient`, `contact`, `batch`, `remark`) and stores them only as structured `licenses.note` metadata. It never stores historical plaintext license codes server-side.
- The one-time CSV ledger is generated locally in the browser immediately after creation from the operator's current form input plus the returned `license_id`. Refreshing the page or reloading the list cannot recover plaintext license codes.
- The Worker-embedded admin page is legacy: its directory view loads only the newest 50 licenses (fixed `limit=50&offset=0`) and client-side search covers only that page. The maintained management UI is `local-admin/`, which pages through the full directory before filtering.
- Admin license creation reads plaintext authorization codes from a hidden prompt, stdin supplied by a secret manager, or a one-time owner-only file; do not pass codes as CLI arguments or write codes into shell command text.
- Successful daily verify updates `devices.last_seen_at` and lease `last_seen_at`; it does not write full audit rows every time.
- `licenses.max_devices` is a per-license positive integer. Its schema/admin
  default is 2, while activation, responses, messages, and guards always use
  the stored value. Values such as 1, 2, 5, and higher all use the same
  `active_device_count >= max_devices` rule.
- Active-device insert/reactivation triggers re-check the current license
  limit at write time, so concurrent activations cannot both consume the final
  seat.
- `/v1/deactivate` hashes `request_id` and the credential fingerprint with a
  server-side pepper. Plain request IDs, lease tokens, and device hashes are
  not persisted in the recovery table or audit reason.
- Rejected unauthenticated deactivation attempts write null audit identifiers.
  Authenticated failures and replays use only license/device identifiers
  resolved from server-side rows, never request-body identifiers.
- Current-device self-deactivation uses one five-statement D1 batch: reserve
  the operation after re-validating the active credential, revoke all active
  leases for that device, revoke that device, write a de-identified audit
  event, and mark the operation committed. The final database trigger refuses
  commit unless the lease/device/audit invariants all hold.
- Repeating the same request and credential returns the committed result with
  `replayed:true`. Reusing a request for another license, device, hash, or lease
  fails with `idempotency_conflict`; a failed or unavailable database never
  fabricates a stopped result.
- Permanent purge has no bulk route and rejects `active` licenses. A mutation-
  protected preview binds the license ID, non-active status, related row counts,
  and a five-minute expiry into an HMAC token. The final request also requires
  the exact target suffix phrase.
- The final purge rechecks the preview state inside one D1 batch, deletes
  device-deactivation operations, leases, devices, and the license in foreign-
  key order, checks every affected-row count, and writes one de-identified
  `admin_purge_license` audit event. Any failed guard rolls the batch back.
- Purge retains identities, rate-limit buckets, historical audit rows, and the
  localhost CSV ledger. Repeating a successfully committed purge is idempotent.
- `audit_events` records activate, deny, `self_deactivate_committed`, and admin
  state-change events only.

Files:

- `worker.js`: public `/v1/activate`, `/v1/verify`, `/v1/deactivate` plus minimal admin endpoints.
- `GET /admin`: minimal same-origin web admin page for source probes only. The dev identity path additionally requires `LULU_LICENSE_ENV=test` and a reserved `.invalid` hostname; a production-capable hostname rejects it even if the dev flags are accidentally set. It never accepts a bare upstream email header as identity. The maintained production management UI is the localhost gateway, which proxies with `x-admin-token` from Keychain. Before any public browser exposure, a separately reviewed identity system is mandatory.
- `PATCH /admin/licenses/:license_id`: updates status, device limit, expiry, and structured ledger metadata. Raw `note` patches are rejected so server-to-server callers cannot bypass the ledger schema. Unknown IDs return controlled `not_found`; invalid input is rejected before writes. The mutation, lease revocation, and audit event share one D1 batch. Restoring `active` never restores an old lease, so the App must activate again before receiving a new lease.
- `POST /admin/licenses/:license_id/devices/reset`: revokes the target license's devices and leases and writes its audit event in one D1 batch. Unknown license/device IDs return controlled `not_found`; other licenses are not touched. A reset device must activate again before it can verify.
- `POST /admin/licenses/:license_id/purge-preview`: rejects active licenses, active leases, and pending deactivation operations; returns only safe target fields, related-row counts, the exact confirmation phrase, and a short-lived HMAC preview token. Legacy production schemas without the optional deactivation-operation table are supported only when D1 explicitly reports that table missing; other database errors remain fail-closed.
- `DELETE /admin/licenses/:license_id`: requires the preview token and exact confirmation phrase, then performs the guarded atomic purge. It never deletes identities, rate limits, prior audits, other licenses, or localhost CSV files.
- `migrations/0001_initial.sql`: versioned D1 migration.
- `migrations/0002_device_self_deactivation.sql`: dynamic active-device
  guards plus the current-device deactivation operation/commit guards.
- `schema.sql`: migration index and safety note.
- `probe_device_self_deactivation.mjs`: dependency-free local SQLite/D1 probe
  for dynamic limits, concurrency, transaction rollback, replay, rate limits,
  strict permanent purge (including the legacy schema without the optional
  deactivation-operation table), server time, and sensitive-field persistence
  boundaries.
- `tools/admin.mjs`: optional admin helper that reads endpoint/admin token from environment variables and sends the same-origin `Origin` plus `x-lulu-admin-csrf: same-origin` headers for state-changing admin calls.

Local verification only:

```bash
node app/workers/license/probe_device_self_deactivation.mjs
```

The probe uses an in-memory database and fake peppers/codes. It does not make
network requests, read real authorization codes, or touch Cloudflare/D1
resources. A passing probe is source-level evidence, not deployment or
production evidence.

Minimum Worker secrets/bindings expected later:

- `DB`: D1 binding.
- `CODE_HASH_PEPPER`: server-only secret used to hash plaintext authorization codes.
- `LEASE_TOKEN_PEPPER`: server-only secret used to hash lease tokens.
- `RATE_LIMIT_PEPPER`: server-only secret used to hash rate-limit buckets. Missing pepper is a configuration error and must not fall back to raw IP storage.
- `ADMIN_TOKEN`: server-only admin token for management actions.
- `ADMIN_WEB_DEV_IDENTITY=true` + `ADMIN_DEV_IDENTITY` + `LULU_LICENSE_ENV=test`: source-probe-only mock identity gate. It is accepted only on reserved `.invalid` hostnames and fails closed on production-capable hostnames.

No real values are included in this repository.

Example staging command shape only:

```bash
LULU_LICENSE_ADMIN_ENDPOINT=https://staging.example.invalid \
LULU_LICENSE_ADMIN_TOKEN=not-a-real-token \
node tools/admin.mjs list
```

Manual staging license creation shape:

```bash
LULU_LICENSE_ADMIN_ENDPOINT=https://staging.example.invalid \
LULU_LICENSE_ADMIN_TOKEN=not-a-real-token \
node tools/admin.mjs create --max-devices 2 --note "staging smoke"
```

After running the command, type or paste the issue code only into the hidden
`License code (hidden):` prompt. The code is not echoed and is not written into
the shell command line.

Automation may use `--code-stdin` only when stdin is produced by an approved
secret manager or other non-logging secret channel. Do not document or execute a
one-line shell command that embeds the real issue code text.

Automation may also use `--code-file /path/to/file` when the file is temporary,
owner-only, excluded from backups/logs/reports, and deleted immediately after
use. The helper rejects files readable by group or other users.

The deprecated `create --code ...` shape is intentionally rejected because CLI
arguments can leak through shell history, process lists, recordings, and logs.

Do not use real values in screenshots, reports, fixtures, or committed files. Production deployment requires a separate security review with real Cloudflare bindings and secrets supplied outside the repository.
