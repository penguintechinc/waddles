# Marketplace Sources (Admin Guide)

A **source** is a pinned git repo that supplies App Bundles to the marketplace. Every bundle a
source supplies lands **unapproved**; a platform admin approves it before anything installs.
Built-in (first-party) bundles are the `is_builtin` source and cannot be added/removed/rescanned here.

## Gates

| Gate | Value |
|---|---|
| Feature flag (kill-switch) | `waddles.marketplace-sources` -- OFF = every route returns 404 |
| Scope | `platform:admin` on every route |
| Prebuilt source | Professional tier (402 `UPGRADE_REQUIRED` below) |
| Sandbox source | Enterprise tier + trust-warning acknowledgement |
| Auto-update CronJob | Helm `sourceAutoUpdate.enabled` (default OFF; needs `gitProxy.enabled`) |
| Sandbox builder | Helm `sandboxBuilder.enabled` (default OFF) |

**Kill-switch:** turn the flag OFF. Routes 404 immediately; existing installed bundles keep running.
To stop a source's bundles, remove it (below) -- that disables the source and deactivates them.

## Prebuilt vs sandbox

| | Prebuilt | Sandbox |
|---|---|---|
| Artifact | committed `.wasm` in the repo | built from source in an isolated Job |
| Trust anchor | publisher Ed25519 key (`publisher_public_key`) + per-bundle attestation | trust-warning ack for the exact commit |
| Hub runs source code | never | never -- only the offline, no-network builder Job |
| Install | admin approve -> platform re-sign | same |

## Add a source

1. `POST /api/v1/marketplace/sources` with `name`, `git_url` (https only, no embedded credentials),
   `ref`, `commit_sha` (40-hex pin), `build_mode` (`prebuilt`|`sandbox`).
2. Prebuilt: also `publisher_public_key`. Sandbox: also `sandbox_trust_ack` = the current warning hash.
3. `POST .../{public_id}/rescan` -- bounded scan + ingest at the pinned SHA. Versions land
   `NEEDS_APPROVAL`; nothing installs.
4. Approve each version (permission hash echoed) -> hub re-signs with the platform key -> installed.

Only the pinned commit is ever cloned/built. Git hosts must be in `gitProxy.allowedHosts`.

## Trust warning (versioned)

The sandbox ack carries `sha256("v{N}\n{text}")` (`services/marketplace_sandbox_warning.py`). Bumping
the version/text changes the hash and **invalidates every client-held ack** -- admins must re-ack.
The ack (who, when, hash, commit) is stored on the source row and audit-logged.

| Version | Change |
|---|---|
| 1 | Initial text: untrusted code runs on the platform; isolation is not a guarantee; every build still needs explicit approval |

Add a row here on every bump (never edit a shipped version's text without bumping).

## Update flow

1. CronJob (or manual check) finds a new upstream commit -> `pending_commit_sha` recorded, status
   `NEEDS_APPROVAL`, audit `marketplace_source_update_available`. The pin never moves automatically.
2. Admin reviews, then `POST .../{public_id}/approve-update` echoing `pending_commit_sha`
   (mismatch -> 409). Sandbox sources need a **fresh** trust ack for the new commit (422 otherwise).
3. Pin advances; rescan to ingest the new version (again unapproved).

## Remove a source

`DELETE .../{public_id}`: hard-deleted when unreferenced; otherwise disabled (blocks rescan/approve)
and its installed bundles are deactivated. Built-in source: 409.

## Observability

| Signal | Names |
|---|---|
| Metrics | `marketplace.scan.total`, `.scan.bundles_examined`, `.scan.bundles_rejected`, `.scan.duration_s` (histogram), `.scan.failures`, `.sandbox.build.duration_s` (histogram), `.sandbox.build.total`, `.source.pending_updates` (up-down), `.approve.total`, `.ingest.total`, `.source.update_check.total` |
| Spans | `marketplace.source.{scan,ingest,approve,update_check}`, `marketplace.sandbox.build` -- attribute `source_public_id` only |
| Logs | penguin logging; ids + counts only |

- Zero bundles examined = FAILURE (`scan.failures{category=zero_examined}`), never a silent pass.
- Labels carry bounded categories (`guard`, `clone`, `build`, ...) -- never git URLs, names, usernames or free text.
- OTLP destination comes from the standard `OTEL_*` env vars only; a dead exporter never fails a request.

## Audit events

Written to the existing audit trail (`bundle_audit`), target = source `public_id`; details hold
ids/hashes/counts only.

| Action | When |
|---|---|
| `marketplace_source_added` | source created |
| `marketplace_sandbox_ack_recorded` | sandbox ack stored (hash + commit) |
| `marketplace_source_rescanned` / `marketplace_source_scan_rejected` | scan outcome (counts / category) |
| `marketplace_source_update_available` | new upstream commit pending |
| `marketplace_source_update_approved` | pin advanced |
| `marketplace_version_approved` / `marketplace_signature_issued` | version approved + platform-signed |
| `marketplace_source_removed` | deleted or disabled |
