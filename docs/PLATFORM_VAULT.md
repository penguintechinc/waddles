# Platform secret store (`platform_vault`)

Storage and sealing for the platform's own generated secrets (signing keys, HMAC keys, DEKs,
service passwords). This page covers the schema and the vault functions only; generation, bootstrap,
rotation and escrow build on it in later changes.

## Where the data lives

A dedicated Postgres schema, `platform_vault` (migration `0109_platform_vault`), **not** `public`:
`public_dml` roles (hub-api and the legacy pods) receive DML on every `public` table automatically,
so a table there would be fail-open. Access is the explicit grants of the migration, nothing else.

| Role | Privileges |
|---|---|
| `waddles_hub_api` | schema USAGE; `secrets` SELECT/INSERT/UPDATE; `secret_events` SELECT/INSERT; `root_marker` SELECT/INSERT/UPDATE. Never DELETE or TRUNCATE. |
| `waddles_svc_ingest` / `_process` / `_action` / `_streaming` / `_presentation` | schema USAGE; SELECT on view `platform_vault.public_keys` only (never `sealed_value`, never a table). |
| every other role | nothing |

`platform_vault.secrets`, `.secret_events` and `.root_marker` are also in `legacy_deny_tables` of
`config/postgres/service-roles.yaml` (defense in depth).

## Tables

- `secrets` -- one row per (type, scope, version). `sealed_value` is the vault envelope; `state`:
  1 pending, 2 active, 3 verify-only, 4 retired, 5 destroyed (crypto-shredded, `sealed_value` NULL).
  One ACTIVE row per (type, scope). Int codes for `algorithm`, `external_sync_state`, `reason`.
- `secret_events` -- append-only audit log, keyed-HMAC hash chain (single linear chain enforced by
  unique indexes).
- `root_marker` -- one row: the KEK id plus a keyed MAC. "First run" is only "no marker AND no
  secrets"; a marker with an empty `secrets` table means the store was wiped and hub-api must
  refuse to start.
- `public_keys` (view) -- active and verify-only rows that carry public material, public columns only.

Guards (triggers, enforced for every role including the owner): DELETE/TRUNCATE refused on all
three tables; `secrets.state` only moves forward; a destroyed row is immutable; identity and public
columns never change; an older version can never be activated after a newer one. Downgrading while
sealed secrets exist is refused unless `WADDLES_ALLOW_PLATFORM_VAULT_DROP=1`.

## Sealing (`hub_api/services/provider_connections/vault.py`)

`seal_platform_secret` / `open_platform_secret` are new functions with **no legacy path**: no
no-AAD fallback, no `seal_version=0`, no `CREDENTIAL_ENCRYPTION_KEY`. The only root is the platform
KEK (`TENANT_KEK_HEX`, with `TENANT_KEK_HEX_PREVIOUS` during a rotation).

- Key: HKDF subkey `waddles/platform-secrets/v1` of the KEK named by the envelope header's `kek_id`
  (explicit, never trial decryption). Fingerprints, the root marker MAC and the event chain use
  sibling labels in the same reserved namespace.
- Envelope: `"WPS" || seal_version(1) || kek_id(8) || iv(12) || AES-256-GCM(ct || tag)`.
- AAD (length-prefixed): `psv1 | secret_type | scope_id or "-" | version | kek_id | public_id`.
  `state` is not bound (it is mutable; grants, triggers and the event log protect it).
- Errors are fixed strings: `PlatformSecretCorruptError` (any authentication/parse failure),
  `PlatformSecretKekUnavailableError` (KEK not loaded -- the root-change signal),
  `PlatformSecretInputError`. No plaintext, key, ciphertext or id ever appears in an error or log.
- Helpers: `platform_secret_fingerprint` (keyed HMAC), `platform_root_mac` /
  `verify_platform_root_mac`, `platform_event_hash`.

The seal-callers guard (`hub_api/tests/test_seal_callers_guard.py`) pins that the AEAD, KEK subkey
derivation and `hmac` run only inside these functions, that the namespace label appears only in the
vault, and ratchets the set of modules allowed to import `cryptography` / `hmac`.

## Data-plane verifying keys (`core/platform_keys`, design T5)

Data-plane services verify signatures against the PUBLIC keys in the view `public_keys`, read with
their existing read-only replica role (schema USAGE + view SELECT only; **no new grants**). The Rust
crate `core/platform_keys` is the cache between the two (README in the crate):

- seed at startup with bounded backoff; readiness (`KeyCache::ready`) is false until every required
  key type has a usable row;
- lazy-load one kid on a miss, 30 s negative cache; unknown kid **fails closed**;
- 60 s poll replaces the snapshot (a retired key disappears at the next poll); on a database outage
  the last-known keys are kept;
- verify-only keys are honoured until `verify_until = rotated_at + window` and only while the last
  successful read is < 15 min old (security review M6), so a retired/compromised key cannot keep
  verifying because hub-api or the database is down; active keys are not affected by staleness;
- statements are fixed literals with the kid as a bound parameter (`pg::ALL_SQL`, `pg::KID_SQL`).

**Key-source switch.** `*_KEY_SOURCE=db|configmap` is a migration switch evaluated once at startup
(`KeySource::resolve`): explicit value wins, a typo is an error, unset follows the kill-switch flag
`waddles.disable-db-platform-keys` (OFF/unseen = `db`). With `db` selected there is no fallback to
the ConfigMap: an unreadable store fails readiness and lookups.

**Bundle executor.** Artifact/precompile signature verification
(`core/bundle_executor/src/signing.rs`) resolves the signing key through
`verify_artifact_signature_async`, which uses either the static `BUNDLE_SIGNING_PUBLIC_KEYS` set or,
for `db`, the cache (`Executor::with_key_cache`) -- never both. The standalone executor binary is
credential-less and its dependency policy bans sqlx/sea-orm, so it links only the cache core:
`BUNDLE_SIGNING_KEY_SOURCE` defaults to `configmap` there and an explicit `db` is refused at startup
until an embedder supplies a `KeyLoader` (see the PR's open questions).

**Telemetry.** Counters `platform_keys_{cache_hits,cache_misses,refresh_failures,unknown_kid,
rejected,refreshes}_total` (labels: `key_type`, closed `reason` set -- never a kid), histogram
`platform_keys_refresh_duration_seconds`, span `platform_keys.refresh`. Logs carry key type, a
sanitised kid and version only.
