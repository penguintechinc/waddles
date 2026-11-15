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

## JWT signing (ES256, T10)

The platform's user / session JWT is ES256. The shared HS256 `JWT_SECRET` is gone: no env var,
Helm value, Secret key, script or code path accepts or mints HS256, and a CI gate
(`scripts/ci/check-no-shared-jwt-secret.sh`, allowlist `scripts/ci/jwt-hs256-allowlist.txt`) fails if it reappears.

| Piece | Behaviour |
|---|---|
| Key | `SecretType.JWT_SIGNING` (3), ES256 / P-256, freshly generated at first boot, **never imported** from env/Secret (security review H1) |
| Signing | hub-api only, `PlatformVaultJwtKeyring` -> active version from the process cache; header `alg=ES256`, `kid` = the key id. No active key -> sign-in answers 503, never a fallback |
| Verifying (Python) | `flask_core.auth.verify_jwt_token(token)`: header vetted (alg pinned to ES256 in code, `none`/HS*/RS*/unknown rejected, `jku/jwk/x5u/x5c/crit` rejected, `kid` required), key chosen by `kid` from the key ring, `iss`/`aud`/`exp` enforced, 30 s skew on `iat`/`nbf` only, max lifetime 1 h |
| Verifying (Rust) | `svc_streaming` reads public keys through `core/platform_keys` (view `platform_vault.public_keys`), same rules; points are 65-byte uncompressed P-256 |
| Rotation | new version active, previous version **verify-only** until `rotated_at + 2 h` (`verify_window_s`), then refused even if no refresh happened (M6); a Rust cache additionally refuses verify-only keys when its snapshot is > 15 min stale |
| Kill-switch | `waddles.disable-db-platform-keys` ON skips the vault bootstrap: JWTs then fail CLOSED (no signing key). It is not an HS256 fallback |

`SESSION_MAC_KEY` is the Flask session-cookie / refresh-token MAC key (independent random value, Helm
`global.sessionMacKey`, generated create-if-absent on first run). It is a NEW key name on purpose: the old `SECRET_KEY`
was derived from the retired shared JWT secret, so its value is never reused. It can sign no JWT.

### Migration notes (HS256 -> ES256)

1. Deploy: hub-api bootstraps the ES256 key on start; every previously issued access/refresh JWT is refused, so
   **users re-login once** (access tokens live <= 15 min; sessions refresh-rotate after re-login).
2. There is deliberately **no dual-accept window** (security review H3): HS256 acceptance is removed in code.
3. Remove the orphaned `JWT_SECRET` / `JWT_SECRET_KEY` / `SECRET_KEY` entries from the `waddlebot-secrets` Secret in each cluster
   (`kubectl patch secret waddlebot-secrets --type=json -p '[{"op":"remove","path":"/data/JWT_SECRET"},{"op":"remove","path":"/data/JWT_SECRET_KEY"},{"op":"remove","path":"/data/SECRET_KEY"}]'`);
   leaked copies verify nothing, but they should not linger.
4. Helm: `global.jwtSecret` / `WADDLEBOT_ALPHA_JWT_SECRET` are removed; `global.sessionMacKey` / `WADDLEBOT_ALPHA_SESSION_MAC_KEY` replace them; a values file that still sets `global.jwtSecret` now FAILS the render.

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
