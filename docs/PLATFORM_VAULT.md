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

## Escrow (S3 disaster-recovery copy)

Postgres is the source of truth. After every commit that creates a version (bootstrap generate / import / restore, and rotation) hub-api writes a DR copy to the **dedicated escrow bucket** (`infrastructure.seaweedfs.escrow.bucketName`, default `waddles-platform-escrow`), through a **dedicated S3 identity** (`hub-api-escrow`, Read/Write/List on that bucket only). No bundle, data-plane, streaming or general hub-api identity names the bucket, and the escrow credentials live in their own Secret `<release>-platform-escrow-s3` (generated at runtime by the provision-secrets hook, never the envFrom'd `waddlebot-secrets`).

| Object | Content |
|---|---|
| `platform/secrets-escrow/<type>/<scope or global>/<version>.sealed` | the value, AES-GCM sealed under a KEK-derived key (HKDF label `escrow/v1`), AAD = type, scope, version, kek_id, public_id, keyed fingerprint |
| `platform/secrets-escrow/manifest.json` | one signed document: generation counter, and per version the identity, kek_id, keyed fingerprint, algorithm, state, kid, object key; MAC under a KEK-derived key (label `escrow-manifest/v1`) |

**Restore** runs at hub-api bootstrap only, when the `secrets` table is empty and the root marker does not mismatch (a changed KEK refuses first: `root_mismatch`). It verifies, in order: the manifest signature; each entry against the registry (type, scope rule, algorithm, canonical object key, highest version = the single active one); each object under its manifest-bound AAD; the fingerprint recomputed from the opened value. Anything off refuses the boot and generates nothing (`escrow_tampered`, `escrow_invalid`, `escrow_missing`, `escrow_unavailable`). A restore is permitted automatically only in `alpha`/`local` (`DEPLOYMENT_TIER`); everywhere else set `WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE=1` for that one boot (otherwise `restore_blocked`). Restored values are re-sealed under the current KEK; older versions come back verify-only.

**Drift** (metric `waddles_platform_secrets_drift_detected_total{type,outcome}`, outcome `escrow_tampered`, `escrow_drift`, `escrow_ahead`): a remote manifest that does not verify, an entry whose fingerprint no longer matches the database, or a manifest listing a version the database lacks (the DB went backwards) is alerted and **never overwritten**; bucket versioning keeps history. Resolve by restoring the previous object version in the bucket (or deleting the manifest after confirming the database is right) and restarting hub-api. Other signals: `waddles_platform_secrets_escrow_total{type,outcome}` (`ok`, `error`, `restored`), span `platform_secrets.escrow`. Page on any drift outcome; never auto-remediate.

**Alpha with a replaced root KEK**: the old escrow cannot be opened, so a pristine database plus a stale escrow refuses to boot (`escrow_tampered`). Wipe the escrow bucket together with the database when you deliberately start over.

**Versioning / object lock**: the bucket-init Job enables bucket versioning (`escrow.versioning`, logged loudly if SeaweedFS rejects it). Object-lock is not enabled: it must be set at bucket creation and a retention lock would block the manifest rewrite on every rotation; versioning plus the sealed, signed objects is the integrity layer. Residual risk: an attacker with write access to the bucket AND old valid objects can replay an older, still validly-signed manifest; restore then yields an older but genuine key set. Restore is operator-gated outside alpha for that reason.
