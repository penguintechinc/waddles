# Waddles v3 Per-Tenant Envelope Encryption — Design

**Date:** 2026-09-28
**Status:** Proposed design
**Scope:** `hub_api` (the only RW path to Postgres); `core/svc_ingest`, `core/svc_action`, `core/svc_process` (RO-replica consumers via `waddles_bundle_reader`)
**Principle:** cryptographic tenant separation — a compromised RO replica credential, a stolen backup, or one tenant's key compromise/offboarding must never expose or entangle another tenant's data.
**Sizing target:** ~300 tenants, ~20,000 communities, ~10,000 channels (matches the `connections-credentials-design` sizing).
**Alignment:** this design shares one key hierarchy with `2026-09-28-connections-credentials-design.md` — it does not stand up a second KMS integration, a second per-tenant key table, or a second broker/cache. `connection_credentials.dek_version` is the same tenant DEK version this document rotates.

```
Root KEK (KMS: platform default key | Enterprise: customer KMS key)
 ├─ per-tenant DEK (tenant_encryption_keys, dek_version, wrapped by root KEK)
 │    ├─ HKDF → AES-256-GCM data subkey   (field encryption, this doc + connection_credentials)
 │    └─ HKDF → HMAC-SHA256 blind-index subkey (equality lookups, §3)
 └─ platform identity DEK (single row, NOT per-tenant — hub_users/identities/profiles, §1)
```

---

## 1. Scope: what gets per-tenant encryption

**Rule:** encrypt tenant-*owned* secrets and user content whose blast radius should stay inside one tenant. Leave plaintext anything needed for joins/routing/indexing whose disclosure alone isn't a tenant-isolation breach (ids, FKs, enums, timestamps, status).

**Load-bearing exception — `hub_users`/`hub_user_identities`/`hub_user_profiles` are NOT per-tenant encrypted.** Per `critical-rules.md` PII Tokenization there is exactly one identity table for the whole product, and `hub_users` has no `tenant_id` — a user reaches multiple tenants via `community_members`/`tenant_admins` rows on `communities`, which *do* carry `tenant_id`. Identity isn't partitionable by tenant, so forcing a per-tenant DEK on it would either (a) require picking an arbitrary "home tenant" that breaks on multi-tenant users, or (b) block login during any one tenant's key outage for a user who isn't even a member of that tenant. Instead: one **platform identity DEK** (same root KEK, same rotation/cache machinery, keyed by a fixed sentinel instead of `tenant_id`) protects PII in the identity table. **One DEK, deliberately not per-user**: a per-user DEK was considered and rejected — at hundreds of thousands of users it multiplies KEK-rotation cost from O(1) to O(users), and a per-user key record is a larger, more numerous attack surface (one key store row per human) for no isolation benefit, since identity is platform-scoped by design, not a tenant-separation boundary. Tenant-level crypto-shredding does not apply to identity rows — GDPR/DSAR erasure there is handled per-user (see `data_deletion_requests`, out of scope for this doc) and is a row-anonymization operation, not a key-destruction one; if the identity DEK itself is ever rotated/destroyed for compromise response, it follows the same separate-key-store/tombstone discipline as tenant DEKs (§4).

| Table (source) | Encrypt (per-tenant DEK unless noted) | Stays plaintext | Why |
|---|---|---|---|
| `hub_users` (`000_create_base_schema.sql:81`) | `email` (ciphertext+bidx, **platform identity DEK**) | `id`, `username`, `password_hash` (already one-way, argon2/bcrypt), `is_super_admin`, timestamps | `password_hash` is already irreversible — encrypting it adds nothing. `email` is the one reversible PII field and needs a login-time equality lookup. |
| `hub_user_identities` / `hub_user_profiles` | `platform_username`, `bio`, `location*` (**platform identity DEK**) | `hub_user_id`, `platform`, `platform_user_id`, flags | Same identity-table exception as above. |
| `communities`, `community_members`, `community_servers`, `community_server_channels`, `community_domains` | — none | everything | Structural/routing data at 10,000s-of-channels scale; disclosure of a channel id or Discord guild id is not a tenant-secrecy breach, and these are exactly the columns svc-ingest/svc-process range-scan. |
| `hub_chat_messages` (`000_create_base_schema.sql:221`) | `message_content` (per-tenant, via `communities.tenant_id`) | `community_id`, `channel_name`, `sender_hub_user_id`, `sender_username`, `created_at` | Retained chat text is the highest-volume user-content field named in scope; sender display fields stay plaintext (already denormalized for read-path rendering, low sensitivity alone). No substring/full-text search survives encryption — accepted limitation (§3). |
| `ingest_sources` (0020) / `connections`+`connection_credentials` (connections-credentials-design) | already designed there — this doc supplies the DEK, not a new column | `platform`, `external_id`, `status`, `mapping` | Converges on the *same* tenant DEK; do not duplicate. |
| `platform_integrations` (030) | `access_token`, `refresh_token`, `client_secret` (per-tenant via `community_id`→`tenant_id`; **user-scoped rows** (`integration_type='user_oauth'`, no community) fall under the **platform identity DEK**, same reasoning as hub_users) | `platform`, `integration_type`, `expires_at`, `scopes`, `is_active` | Currently plaintext `TEXT` columns despite an `is_encrypted` flag — the flag is intent, not a schema guarantee. This is the largest existing plaintext-secret exposure in the schema. |
| `music_oauth_tokens` (005) | `access_token`, `refresh_token` (per-tenant via `community_id`) | `platform`, `expires_at`, `scope` | Same OAuth-token pattern as above, smaller surface. |
| `ai_byok_keys` (077) | `encrypted_key` — **migrate onto the shared tenant DEK**, don't re-derive its own key | `provider`, `key_last4`, `is_active` | Already AES-256-GCM app-encrypted — the right *mechanism*, wrong (bespoke, undocumented) key source today. `key_last4` is the existing "masked display without decrypt" precedent this doc reuses everywhere (§3). |
| `browser_source_tokens` (004), `community_overlay_tokens` (000) | **convert to hashed lookup, not encryption** — replace `token`/`overlay_key` plaintext columns with `token_hash BYTEA` (SHA-256, unkeyed — tokens are high-entropy random, no brute-force risk) | everything else | These are bearer capability tokens only ever *compared*, never displayed or replayed outbound — the existing `user_access_tokens`/`community_access_tokens` `token_hash` pattern (048) already proves this out; no KMS/decrypt path needed at all. |
| `revoked_tokens`, `user_access_tokens`, `community_access_tokens` | — none, already hash-only | everything | No change; cited as the precedent above. |
| `audit_log`, `activity_logs`, `workflow_audit_log` | `ip_address`, `user_agent` — ciphertext, **no blind index** | `action`, `target_type`/`id`, `details`/`changes`/`metadata` JSONB (selectively, §3), `user_id`, timestamps | IP/UA are PII-adjacent per `security.md`/`critical-rules.md` Observability. Low-entropy/low-cardinality fields (an IPv4 is ~32 bits, most `user_agent` strings collapse to a few hundred distinct values) are **not blind-indexed** — a blind index on a small value space is a dictionary attack away from a full plaintext-equivalent lookup table, so "all actions from IP X" becomes a bounded decrypt-and-scan (per tenant, per time window) instead of an indexed equality query. |
| `module_db_accounts`, `app_versions`, `app_active_versions`, `app_source_bindings`, `workstreams`, `modules` | — none | everything | Platform/module-operational metadata, not tenant secrets; `module_db_accounts` holds no password (provisioned externally). |

---

## 2. Mechanism: application-level field encryption

| Option | Verdict |
|---|---|
| TDE / volume encryption alone | **Baseline, not sufficient alone** — protects stolen disks/backups but every tenant shares one encryption domain; a compromised DB role or RO replica reads everyone's plaintext. Kept as the underlying floor (`security.md` Storage baseline), not the isolation mechanism. |
| `pgcrypto` (`pgp_sym_encrypt`) | **Rejected.** Key material has to be passed into the query (`pgp_sym_decrypt(col, key)`) — the DB role executing the query sees the key, defeating "RO role has zero grant" and putting key material in query logs/`pg_stat_statements`. |
| **Application-level AES-256-GCM, AAD-bound** | **Chosen.** Same posture as `connection_credentials` — hub-api is the only process holding unwrapped DEKs; ciphertext-only columns are safe for any role, including `waddles_bundle_reader`. |

**AAD construction (prevents cross-tenant/row/column ciphertext swap):**

```
AAD = f"{tenant_id}|{table}|{column}|{row_uuid}|{dek_version}"
```

**`row_uuid`, not the auto-increment `id`.** Every target table's PK is `SERIAL`/`BIGSERIAL`, assigned by Postgres only *after* the `INSERT` commits — encryption happens in the application before that id exists, so it cannot be part of the AAD computed at encrypt time. `row_uuid` is an app-generated UUIDv4, minted in code and included as a literal value in the same `INSERT`, so it's always known before the ciphertext is built. Every table gaining an encrypted column also gains a `row_uuid UUID NOT NULL DEFAULT gen_random_uuid()` column (migration plan §7 step 2) if it doesn't already expose a stable non-serial identifier; `hub_users`/`ingest_sources`/`connections` etc. that already carry a natural or existing UUID reuse it instead of adding a second one.

GCM's authentication tag covers the AAD, so a raw-bytes copy of one tenant's (or one row's, or one column's) ciphertext into another row fails to decrypt rather than silently decrypting under the wrong context — this is what makes "per-tenant" a cryptographic property instead of a query-discipline convention.

**Key/version columns, added consistently to every encrypted column pair** (mirrors `connection_credentials.dek_version` exactly):

```sql
<col>_ciphertext BYTEA NOT NULL,
<col>_iv         BYTEA NOT NULL,   -- 96-bit (12-byte) GCM nonce, CSPRNG, unique per encryption
<col>_dek_version INTEGER NOT NULL
```

**Nonce discipline and why AES-256-GCM over GCM-SIV/XChaCha20-Poly1305.** Nonces are random 96-bit values from a CSPRNG (never a counter — counters require durable per-DEK state that a crash/restart can desynchronize). NIST SP 800-38D's random-nonce collision bound means risk becomes non-negligible well before 2^32 encryptions under one key; this design caps each `dek_version` at **2^30 encryption operations** (tracked by an atomic counter alongside the cache entry, well inside the safe margin) and **auto-triggers a DEK rotation** (§4) on hitting the cap, same mechanism as compromise-triggered rotation, just a different trigger condition. AES-256-GCM is kept — not AES-256-GCM-SIV (nonce-misuse-resistant) or XChaCha20-Poly1305 (192-bit nonce, no birthday-bound concern) — **because neither is FIPS 140-3 approved**, and Enterprise/FedRAMP customers require FIPS-validated crypto (`security.md` SOC2/HIPAA/FedRAMP). A non-FIPS algorithm is not built day-one; if a future tenant explicitly doesn't need FIPS and wants misuse-resistance headroom, it would be an opt-in `kek_kind`-style per-tenant algorithm choice, not the default.

---

## 3. Querying encrypted data

- **Equality lookups → per-tenant (or platform-identity) HMAC-SHA256 blind index, restricted to high-entropy fields only.** Derived via HKDF from the same DEK (`info="blind-index-v1"`), stored as `<col>_bidx BYTEA` alongside the ciphertext. Concrete case: `hub_users.email_bidx = HMAC(platform_identity_hmac_key, lower(trim(email)))`, `UNIQUE NOT NULL`, used for both the uniqueness constraint and the login `WHERE` clause — the ciphertext column is decrypted only after the row is found, for display. **Not applied to low-entropy fields** (`ip_address`, `user_agent` — see §1 audit row): a blind index is a deterministic function of the plaintext, so a small value space (a /24 has 256 IPs; common `user_agent` strings number in the low hundreds) is trivially dictionary-attacked back to plaintext even without the key, which defeats the point of encrypting the field at all.
- **Leakage this design accepts for the fields it does blind-index:** a blind index reveals *equality and frequency* within its key scope — two rows sharing the same `email` produce the same `email_bidx`, so duplicate/repeat values are visible without decryption, though the plaintext itself is not. Scoping the HMAC key per tenant (or, for identity, to the single platform-identity key) means this leakage never crosses that boundary — cross-tenant correlation of the same underlying value is not possible. This is the accepted cost of equality search on encrypted data, not a defect introduced by this design.
- **Blind-index rotation is not a separate process** — the blind-index subkey is HKDF-derived from the row's DEK, so it rotates exactly when the DEK does (§4): a DEK rotation invalidates old `_bidx` values the same way it invalidates old ciphertext, and both are recomputed together by the same lazy-reencrypt-plus-bounded-sweep job, never as two out-of-sync migrations.
- **Existing UNIQUE constraints are unaffected** — every one of them (`ingest_sources (tenant_id, platform, external_id)`, `ai_byok_keys (community_id, provider)`, `music_oauth_tokens (community_id, platform)`, `community_overlay_tokens (community_id)`) keys off metadata columns, never the encrypted value itself.
- **What can't be searched:** substring/`LIKE`/full-text search on `message_content` or any encrypted free-text field — a blind index is equality-only. No message-search feature exists today; if one is added later it needs an app-level decrypt-and-reindex step (e.g., a per-tenant search shard keyed the same as the DEK), out of scope here.
- **Audit investigative queries** ("all actions from IP X") are a bounded decrypt-and-scan (per tenant, per time window), not an indexed lookup — the tradeoff accepted above for not blind-indexing low-entropy fields.
- **JSONB `config`/`details`/`metadata`: selective, path-level encryption, not whole-blob encryption or a hard extract-to-column requirement.** Whole-blob encryption is rejected outright (kills every `@>`/key-path query on the non-sensitive majority of the document). Where a sensitive key is static and always present (e.g. a fixed `client_secret` field), extracting it to its own real encrypted column (as elsewhere in this doc) is still preferred — it's simpler and gets a real `dek_version`/AAD row. Where the sensitive key is dynamic/nested per-platform config that doesn't warrant a schema migration per key, the ORM layer encrypts that specific JSON path's value **in place** (the JSON key stays, its value becomes a self-describing ciphertext envelope string) on write and decrypts it transparently on read — never a plaintext value at a known-sensitive path. **Defense in depth:** each table with JSONB config carries an allow-list of key names considered sensitive (e.g. `client_secret`, `token`, `password`), enforced by a `CHECK` constraint (or, where the check is too dynamic for SQL, an ORM-level validator on every write) that rejects any row where one of those keys is present with a value that doesn't match the ciphertext-envelope shape — so a developer bypassing the ORM's encrypt step fails the write, rather than silently landing plaintext in the column.

---

## 4. Key lifecycle

**Key material lives in a separate key store from the data it protects** — a dedicated schema (`keystore.tenant_encryption_keys`/`keystore.identity_keys`, own Postgres role, own backup policy) rather than a table in the same database/backup set as `platform_integrations`/`hub_chat_messages`/etc. This is what makes crypto-shredding actually final instead of "final until the next restore": if wrapped DEKs backed up alongside tenant data, restoring an old backup after a shred silently resurrects the destroyed key next to the (still-present, never-deleted) ciphertext it used to unlock. Splitting the store means a data-DB restore never carries key material with it.

- **Short/no long-term retention on the key store's own backups** — point-in-time recovery only, over a materially shorter window than the main DB's backup retention (e.g. 24-48h vs. 30-90 days), so an old key-store backup restore has a much narrower window in which a shredded key could reappear at all.
- **Shredded-keys tombstone list** — a durable, append-only record of `{tenant_id | user_id, dek_version, shredded_at}` for every crypto-shred ever performed, kept indefinitely and replicated independently of the key store's own backup boundary (e.g. written to the audit/compliance log pipeline, not just the key-store DB). Applies to **both tenant DEKs and the identity DEK** identically.
- **Restore runbook (mandatory step, not optional cleanup):** any restore of the key store, or of a backup that could reintroduce key rows — full DB restore, DR failover, a stray snapshot restore for debugging — MUST reconcile the restored key rows against the tombstone list *before* the restored system serves traffic: any row matching a tombstoned `(tenant_id/user_id, dek_version)` is re-destroyed immediately post-restore. This makes the tombstone list, not the key store's own `status='destroyed'` flag, the actual source of truth for "is this key allowed to exist."

**`tenant_encryption_keys`** (new, `keystore` schema, hub-api's dedicated key-store role only):

```sql
CREATE TABLE tenant_encryption_keys (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
    dek_version   INTEGER NOT NULL,
    wrapped_dek   BYTEA NOT NULL,
    kek_ref       VARCHAR(255) NOT NULL,   -- platform: fixed KMS key id; enterprise: customer KMS ARN/resource id
    kek_kind      VARCHAR(20) NOT NULL DEFAULT 'platform'
        CHECK (kek_kind IN ('platform', 'customer_kms')),
    status        VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'retired', 'destroyed')),
    activated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at    TIMESTAMPTZ,
    destroyed_at  TIMESTAMPTZ,
    UNIQUE (tenant_id, dek_version)
);
-- Lives in the `keystore` schema/database, not alongside application tables
-- (separate backup policy, §4 intro). rbac-matrix.yaml: hub_api's dedicated
-- key-store role gets SELECT/INSERT/UPDATE; every other role (waddles_bundle_reader,
-- svc_ingest/svc_action/svc_process, webui, migration_runner) gets privileges: []
-- -- identical posture to connection_credentials, extended to the store boundary.
```

- **Creation:** a DEK is generated (CSPRNG, 256-bit) and wrapped under the root KEK the moment a `tenants` row is inserted — no lazy/first-use generation, so there's never a window where tenant data exists without a key.
- **Caching:** hub-api caches *unwrapped* DEKs (and their HKDF-derived subkeys) in-process, versioned by `dek_version`, TTL 10 minutes — this is the exact same cache the credential broker (§2.1 of the connections doc) already runs; it is extended to cover this DEK, not duplicated. KMS unwrap volume is therefore ~300 tenants / 10-min TTL ≈ 0.5 calls/sec steady state, identical to the connections doc's number, because it's the same call.
- **Rotation:**
  - **KEK rotation** (root key rotated) → background job re-wraps every `tenant_encryption_keys.wrapped_dek` under the new KEK. O(tenants), zero data re-encryption — the DEK itself never changes.
  - **DEK rotation** (single-tenant compromise suspicion) → insert a new `dek_version` row (`status='active'`), flip the old row to `retired` (kept, not destroyed — old ciphertext must stay decryptable). New writes use the new version immediately; existing ciphertext is **re-encrypted lazily** (on next natural write) plus a low-priority background sweep with a bounded SLA (7 days) for rows that are never naturally rewritten — a forced synchronous re-encrypt across 10,000s of channels'/20,000 communities' worth of rows is a lock-contention/availability risk this design explicitly avoids.
  - **Cache invalidation:** hub-api publishes `dek-rotated:{tenant_id}` on the same Valkey pub/sub channel the connections doc uses for fast-path credential revocation; every hub-api replica evicts that tenant's cached DEK immediately. Poll-based self-healing fallback if a pub/sub message is missed (same posture, not a new mechanism).
- **Tenant deletion = crypto-shredding.** Mark `tenant_encryption_keys` `destroyed`, delete `wrapped_dek` from the key store, and append the tombstone entry — every ciphertext row for that tenant becomes permanently unrecoverable instantly, without touching or scanning the tenant's actual data rows. Row deletion of the now-garbage ciphertext in the *data* DB is a housekeeping cleanup that can lag safely; the security event is the key destruction, not the row deletion. The tombstone is what makes this durable across restores (see above) — the `destroyed` status flag alone would not survive a key-store restore from a pre-shred backup.
- **Backups.** Baseline (platform KMS): a stolen *data-DB* backup contains only ciphertext, never key material (separate store, above) — useless on its own regardless of KMS state. A stolen *key-store* backup, if taken within its short retention window, does contain wrapped DEKs, but is still useless without live root-KEK/KMS access, and any restore from it is tombstone-reconciled before serving traffic — so baseline crypto-shred is bounded by the key store's short retention window, not the data DB's long one. **Enterprise BYOK gives a strictly stronger guarantee** (§6): revoking the customer's own KMS key makes even an in-window key-store backup's wrapped DEKs permanently unwrappable, since unwrap always requires a live call to a key the customer controls — belt-and-suspenders on top of the tombstone/short-retention mechanism, not a replacement for it.

---

## 5. Data plane (Rust, `waddles_bundle_reader` RO role)

The RO replica never holds key material — this is already the connections doc's design and this document changes nothing about it. Concretely, for the fields in scope here:

- **`connection_credentials`** — already fully covered: svc-ingest/svc-action call hub-api's credential broker; the RO role's grant on it is `privileges: []`.
- **`message_content`, `ai_byok_keys`, `platform_integrations`** — the Rust data plane has **no operational need** to decrypt any of these today. `svc_ingest`/`svc_action`/`svc_process` operate on live in-flight events (via the connections/relay path), not on stored chat history; BYOK keys are consumed only by hub-api's Python AI-routing path. No new broker endpoint is introduced for them.
- **If a future data-plane need for a decrypted field ever appears**, it extends the existing `POST /internal/v1/credentials/resolve-batch`-shaped broker pattern (batched, cached, circuit-broken) rather than granting `SELECT` on ciphertext+key material to any RO role — the rule stays "hub-api decrypts, everyone else asks."

---

## 5a. Purpose-limited stream keys (`ingest-stream`)

**Amendment, 2026-09-28 (post security-review, PR #442).** Section 5 above states the data plane has "no operational need" to decrypt stored fields today via the at-rest DEK. That remains true and unchanged: **at-rest DEKs (`tenant_encryption_keys` rows with `purpose='at-rest'`) NEVER leave hub-api, in plaintext or wrapped form, under any circumstance.** The original `POST /api/v1/internal/keys/tenant-dek` implementation reviewed in PR #442 violated this by re-wrapping and returning the at-rest DEK to any caller presenting a matching scope for any of `message_content`/`connection_credentials`/`identity` — those three purposes are **removed**. This section defines the one purpose the data plane is actually approved for (Justin, 2026-09-28): encrypting/decrypting identity fields inline in the Rust ingest stream (`svc_ingest` encrypts, `svc_process` decrypts — see PR #443/#440), which needs a key the data plane genuinely holds, not one hub-api decrypts on its behalf per-call.

**A separate per-tenant STREAM DEK, `keystore.tenant_encryption_keys` rows with `purpose='ingest-stream'`**, is the only key this broker ever issues:

- **Independent key material from the at-rest DEK** — own row, own `dek_version` sequence, own KEK-wrap, own rotation clock. Compromising the stream key never exposes `message_content`/`platform_integrations`/etc., and vice versa.
- **Server-side purpose→service allowlist, never client/request-supplied:**

  | Purpose | Allowed service identity | Capability |
  |---|---|---|
  | `ingest-stream` | `svc-ingest` | encrypt only |
  | `ingest-stream` | `svc-process` | decrypt only |

  Enforced in `services/tenant_keystore.py::is_service_allowed(purpose, service_id)` against a fixed in-code mapping (`_PURPOSE_SERVICE_CAPABILITIES`) — **not** derived from the request body, the JWT's `scope` claim's string value, or any other caller-controlled input. The JWT scope (`keys:tenant-dek:read:ingest-stream`) is checked *in addition to*, never instead of, this server-side mapping — a forged or over-broad scope on a compromised `svc-action` token still cannot obtain the ingest-stream key, because `svc-action` isn't in the mapping at all. `svc-ingest`'s grant is encrypt-capability only and `svc-process`'s is decrypt-capability only at the design level (both receive the same DEK bytes today, since AES-256-GCM has no asymmetric encrypt/decrypt split — the capability column is enforced procedurally: `svc-ingest` calling the endpoint with an intent to decrypt, or `svc-process` with intent to encrypt, is out of scope for this key's issued use and is an audit-log anomaly per the anomaly metric below, not a cryptographic impossibility).

- **Rotation: time-based, 24 hours, plus a usage-count backstop far below the 2^30 nonce-collision bound (spec Sec2):**

  `STREAM_KEY_USAGE_CAP = 2**24` (16,777,216 operations per `dek_version`). Math: even a single tenant sustaining an anomalously high 1,000 ingest events/sec (well above this platform's ~300-tenant/20,000-community sizing target for any one tenant) exhausts 2^24 in ~4.7 hours — comfortably inside the 24h rotation window, so the *time-based* trigger is expected to fire first under normal load and the usage cap exists purely as a backstop for a single anomalous tenant, not the primary control. 2^24 is 64x below the 2^30 bound Sec2 sets for the at-rest DEK's own nonce discipline, leaving wide margin. A background scheduler rotates every active `ingest-stream` key every 24h regardless of usage (`TenantKeystore.rotate(tenant_id, purpose="ingest-stream")`, same mint-new/retire-old mechanism as §4's at-rest rotation, just on a fixed clock instead of purely usage-triggered).
- **Grace window for previous versions:** a retired `ingest-stream` version stays fetchable (via `version=` in the request body) and decryptable for **48 hours** (`STREAM_KEY_GRACE_WINDOW = 2x rotation interval`) after retirement — long enough to cover in-flight events straddling a rotation boundary plus any consumer-side lag reading its cached key. After the grace window, a retired version is eligible for a background sweep to `destroyed` (housekeeping; the tombstone/shred machinery in §4 is unchanged and unaffected — a routine rotation is not a crypto-shred and never writes a tombstone).

- **Transport (see §5b) and cache invalidation (see §5c)** are described below since they apply to this key type specifically, not to the at-rest DEK (which never transits the network at all).

## 5b. Transport: ephemeral-key sealing, not platform-KEK wrap

**The PR #442 response body's `"wrapped_dek": kek_provider.wrap(tenant_id, dek).hex()` step is removed entirely.** Wrapping under the platform root KEK for transport was never actually usable by the caller — `svc-ingest`/`svc-process` have no access to `TENANT_KEK_HEX` (by design; it's hub-api's own secret) and thus no way to unwrap that response, and worse, logging or replaying that response would carry a KEK-wrapped copy of a key hub-api itself can unwrap, i.e. functionally still "the DEK" for confidentiality purposes as far as anyone who ever obtains hub-api's KEK is concerned. This is the concrete defect the security review flagged.

**Replacement: the caller supplies an ephemeral X25519 public key with every request; hub-api seals the stream DEK to it.**

```
POST /api/v1/internal/keys/tenant-dek
{
  "tenant_id": 123,
  "purpose": "ingest-stream",
  "version": null,                      // omit for "current active"
  "client_ephemeral_pubkey": "<32 bytes, base64>"
}
```

Sealing construction — **X25519 + HKDF-SHA256 + AES-256-GCM**, functionally HPKE's base mode (`DHKEM(X25519, HKDF-SHA256)` + `AES-256-GCM` AEAD) without requiring the `hpke` PyPI package as a new dependency, since `cryptography>=44.0.1` (already pinned, `hub_api/requirements.in`) provides every primitive needed:

1. hub-api generates a fresh X25519 ephemeral key pair **per request** (never reused across calls, even to the same caller/tenant).
2. `shared_secret = X25519(hub_api_ephemeral_private, client_ephemeral_pubkey)` (caller performs the mirror operation with its own ephemeral private key to derive the same secret).
3. `salt = hub_api_ephemeral_pubkey_bytes || client_ephemeral_pubkey_bytes` (both public keys, fixed 32+32 bytes, publicly visible — binds the derived key to this exact key-exchange instance, standard HPKE-style construction).
4. `info = f"{service_id}|{tenant_id}|{purpose}|{dek_version}".encode()` — **bound to the resolved `service_id` from the verified JWT (§5d), the requested `tenant_id`, `purpose`, and the DEK's own `dek_version`**, mirroring the at-rest DEK's AAD discipline (spec Sec2) one level up: a sealed blob replayed against a different service/tenant/purpose/version fails HKDF-derived-key + AEAD-tag verification rather than silently decrypting.
5. `transport_key = HKDF-SHA256(shared_secret, salt=salt, info=info, length=32)`.
6. `nonce = secrets.token_bytes(12)` (CSPRNG, never reused — same nonce discipline as spec Sec2).
7. `sealed = AES-256-GCM-Encrypt(transport_key, nonce, plaintext=stream_dek, aad=info)`.
8. Response carries `hub_api_ephemeral_pubkey`, `nonce`, `sealed` (all base64), never the raw or platform-KEK-wrapped DEK bytes in any field, and never logs any of steps 2-8's intermediate or output values (only `tenant_id`/`purpose`/`dek_version`/`service_id` reach the audit log, per §4's existing discipline).
9. The caller reverses steps 2-7 with its own ephemeral private key (which it never transmits) to recover `stream_dek`, then discards its own ephemeral key pair — one-shot, forward-secret per request. A network observer or log capturing the full request+response pair recovers nothing without one of the two ephemeral private keys, neither of which is ever transmitted or persisted.

**Rust client contract (`svc_ingest`/`svc_process`, PR #443's `HubApiDekProvider`):** generate an X25519 ephemeral key pair per call (`x25519-dalek` or equivalent), send the public half, perform the mirrored HKDF+AES-256-GCM open using the response's `hub_api_ephemeral_pubkey`/`nonce`/`sealed` fields and the same `info` construction (the four fields are all present in the response so the client never has to separately track `dek_version` for this purpose). This is a breaking change to the wire contract `#443` was drafted against (which expected a KEK-wrapped hex blob) — `#443` must land the sealing-open side before this PR is usable end-to-end; that dependency is called out explicitly in this PR's description.

**Max-cache TTL:** every response includes `"max_cache_ttl_s": 300` (5 minutes) — callers MUST NOT cache the unsealed DEK past this TTL regardless of the key's own 24h rotation/48h grace window; this bounds how long a cached-but-since-invalidated key (§5c) can remain in use if a consumer somehow misses the invalidation stream message.

## 5c. Cache invalidation

On every `ingest-stream` rotation (time-based or usage-cap-triggered) and on any tenant shred (§4, unchanged), hub-api publishes to a Valkey **STREAM** (not pub/sub — durable, replayable, matches this codebase's existing `waddles:usage` XADD convention in `services/usage_aggregator_service.py` rather than introducing a second messaging primitive):

```
XADD keys:tenant-dek:invalidate * tenant_id <id> purpose ingest-stream version <old_dek_version> reason rotation|shred
```

**Consumers (`svc_ingest`/`svc_process`) MUST drop their cached copy of `(tenant_id, purpose)` on receipt** — not just the specific version named, since a rotation means the *active* pointer moved and continuing to encrypt under a retired version needlessly shortens that version's remaining grace window. This is a documented contract obligation for `#443`'s consumer-side cache, not enforced by hub-api (hub-api cannot force a remote process to evict a local cache) — the 5-minute `max_cache_ttl_s` above is the enforceable backstop if a consumer misses or mishandles this stream message (e.g. consumer-group lag, process restart with a stale local snapshot).

## 5d. Server-side service identity for purpose checks and audit

`internal_keys.py`'s `_authenticate()` now returns `(claims, None)` on success (previously discarded the verified claims and fell back to `getattr(request, "service_identity", "unknown")` for both the purpose-allowlist check and the audit row — a request-object attribute nothing in this codebase ever sets, meaning both checks silently used the literal string `"unknown"`). The route now uses `claims["sub"]` (the machine-JWT subject, hub-api's own `ServiceJwtVerifier`-issued service identity) as `service_id` for **both** `is_service_allowed(purpose, service_id)` and the audit log — the one property this fixes is that the purpose-allowlist enforcement in §5a is now checked against the cryptographically-verified caller identity, not an unset placeholder that would have made the allowlist check vacuous (every caller was effectively `"unknown"`, which matches nothing in `_PURPOSE_SERVICE_CAPABILITIES`, so the allowlist would have rejected everyone — a fail-closed bug, but still a bug masking the real enforcement path until #438 lands and a real caller shows up).

## 5e. Per-service tenant-fanout observability

`services/tenant_keystore_metrics.py` tracks, per `service_id`, the distinct set of `tenant_id`s that service has fetched a key for (process-local, bounded, TTL-evicted) and emits:

- `waddles_hub_keystore_distinct_tenants_total{service_id}` — a counter incremented only the first time a given `(service_id, tenant_id)` pair is observed in the current tracking window; its rate is a proxy for fan-out breadth, not raw call volume.
- An anomaly threshold alert (`waddles_hub_keystore_tenant_fanout_anomaly_total{service_id}`, plus a `logger.system(..., result="DEGRADED")` line) when one `service_id`'s distinct-tenant count in the window exceeds `KEYSTORE_TENANT_FANOUT_THRESHOLD` (default 50, env-overridable) — `svc-ingest`/`svc-process` are expected to fan out across many tenants by design (that's the entire point of a shared data-plane service), so this is a coarse anomaly signal (a sudden step-change or an unexpectedly high absolute count for a deployment's actual tenant count), not a hard block.

**Accepted multi-tenant trust tradeoff, stated explicitly:** `svc-ingest` and `svc-process` are shared, multi-tenant processes by architecture (spec's whole "Rust data plane" premise) — a single compromised instance can request the `ingest-stream` key for *any* tenant it chooses, one at a time, since the purpose/service allowlist (§5a) authorizes the service identity, not a specific tenant. This is the same trust boundary already accepted for `waddles_bundle_reader`'s RO-replica grant and the existing `connection_credentials` broker (spec §5 intro) — a multi-tenant data-plane process is inherently a multi-tenant blast radius if compromised, and no per-request tenant-scoping short of a separate service identity per tenant (not adopted, same O(tenants) operational cost rejected for per-user DEKs in §1) closes it further. The fanout counter above is the compensating detective control: it does not prevent a compromised instance from touching many tenants, but it makes systematic enumeration (as opposed to normal, expected multi-tenant operation) visible.

## 5f. Usage accounting

The stream DEK's usage counter (§5a's `STREAM_KEY_USAGE_CAP` backstop) is incremented **once per key-fetch call** (`services/tenant_keystore.py::get_dek`), exactly like the at-rest DEK's existing counter (§4) — it counts *key issuances*, not the caller's actual downstream encrypt/decrypt operation count, since hub-api has no visibility into how many events `svc-ingest` encrypts with a DEK once it has issued it. **Decision: rely primarily on the 24h time-based rotation (§5a) for `ingest-stream`, with the usage-count backstop as a secondary safety net, not the primary control** — the reverse of the at-rest DEK's original design (spec Sec2/Sec4), which has no time-based trigger and relies on usage count alone, because the at-rest DEK's usage count *does* correspond 1:1 to real AES-GCM operations (hub-api itself performs every at-rest encrypt/decrypt). Callers are not required to report actual encryption-op counts back to hub-api on renewal — that would require a trusted-client-reported metric hub-api cannot verify, which is a weaker control than a hub-api-owned wall-clock trigger it doesn't need to trust the caller for.

---

## 6. Enterprise BYOK / external KMS

Additive on the baseline (`critical-rules.md` Feature Flags & License Tiers) — a per-tenant `tenant_encryption_keys.kek_kind = 'customer_kms'` row points `kek_ref` at the customer's own AWS/GCP/Azure KMS key instead of the platform key. Unwrap calls go to the customer's KMS via a workload-identity-federated role (no static customer credentials stored).

**Failure modes — fail closed, isolated per tenant:**

| Event | Behavior |
|---|---|
| Customer revokes IAM policy / deletes their KMS key (**permission/access-denied response**) | **Immediate cache eviction, no TTL grace period.** A permission-denied response is an explicit revocation signal, not a transient blip — riding the stale-while-revalidate window here would keep serving a tenant's data for up to 10 more minutes after they revoked access, which is the opposite of what they asked for. Evict that tenant's cached DEK on the first access-denied response and fail closed immediately with a clear "tenant encryption key unavailable" error, alerted to ops. Never silently falls back to the platform KEK — that would quietly downgrade the isolation guarantee the customer paid for. |
| Customer KMS regional outage / timeout / 5xx (**transient, not a denial**) | Distinguished from the above by error type — a timeout or 5xx is not evidence of intentional revocation, so the normal stale-while-revalidate grace window still applies, absorbing short blips without an outage. |
| One tenant's BYOK KMS is down/revoked | **Zero effect on other tenants** — each tenant's KMS call is independent, no shared blocking resource, matching the "one bad KMS never blocks the fleet" posture already established for the credential broker. |

---

## 7. Performance & migration plan

**KMS call volume scales with tenants, not rows** — unwrap only happens on a 10-minute-TTL cache miss per active tenant (§4), the same ~0.5 calls/sec steady state and ≤900-call batched cold-start burst already computed in the connections doc, because it's the same cache. Per-request cost is a single in-process AES-256-GCM open (microseconds) against an already-unwrapped subkey — KMS is never in the per-query hot path.

**Migration — numbered increments, security-first, no disable-once-enforced kill-switch** (an "encryption off" flag would itself be a standing plaintext-fallback vulnerability, unlike a rollback-safe mechanism flag — the migration flag below is default-OFF and is *deleted*, not flipped, once complete):

Every table below moves through the **same five phases**, never skipping ahead: *(a) shadow dual-write* (write both, read old) → *(b) dual-read verification* (read both, compare, log mismatches, still serve old) → *(c) cutover* (serve new, **keep writing old**) → *(d) soak* (new is authoritative, old still updated, rollback still possible) → *(e) drop* (stop writing old, drop the column). **Plaintext writes never stop before phase (e)** — the whole point of (c)/(d) is that a rollback to reading plaintext stays possible for the entire soak period; only the drop itself removes that option, and only after the soak shows zero mismatches with no further exceptions.

1. **Key-store infrastructure**: separate `keystore` schema/database/role (§4), `tenant_encryption_keys` + `identity_keys` tables, tombstone table, short-retention backup policy for the key store distinct from the main DB's. RBAC grants (hub-api's key-store role only; every other role `privileges: []`). Platform-KMS integration + DEK generation wired into tenant/user creation (new tenants/users only). Behind default-OFF `waddles.core.tenant-envelope-encryption`.
2. **`row_uuid` columns** added to every table gaining an encrypted field that doesn't already have a stable non-serial identifier (§2) — a plain additive migration, no behavior change, must land before any ciphertext column that depends on it for AAD.
3. **Shared crypto library**: AES-256-GCM field-encryption helper with the AAD scheme (§2, using `row_uuid`), the 2^30-operation nonce-cap counter and its auto-rotation trigger, HKDF blind-index derivation (high-entropy fields only, §3), the JSONB selective-path encrypt/decrypt helper + sensitive-key-name `CHECK`/validator. Unit tests cover AAD-mismatch rejection (cross-tenant/row/column swap must fail) and nonce-cap-triggers-rotation.
4. **Phases (a)+(b) on `connection_credentials`/`ingest_sources`** (executed as the connections doc's own step 1, under this shared key hierarchy) **and `ai_byok_keys`** (move off its bespoke key onto the shared tenant DEK): dual-write both columns, then a verification job decrypts-and-compares against the still-present plaintext, alerting on any mismatch.
5. **Phases (c)+(d)**: cutover reads to ciphertext once verification reports zero mismatches, **while dual-write continues** — plaintext stays populated through the entire soak period (one full release cycle minimum) so a read-path rollback remains a config flip, not a data-recovery exercise.
6. **Repeat phases (a)-(d) for `platform_integrations`, `music_oauth_tokens`**; convert `browser_source_tokens`/`community_overlay_tokens` to hashed lookup (no encryption needed there, §1).
7. **Repeat phases (a)-(d) for `hub_chat_messages.message_content` and audit-table `ip_address`/`user_agent`** (no blind index, per §3) — the largest row counts at this scale, so backfill runs as a rate-limited, resumable, watermark-cursor background job, off-peak, never a blocking migration.
8. **Phase (e) — drop legacy plaintext columns, table by table**, only once each table's soak (step 5/6/7) has run a full backup-retention cycle with zero further mismatches, **and delete the migration flag and its plaintext-fallback code path** — this drop, not a flag flip, is what makes encryption permanently enforced (no disable-once-enforced kill-switch, §"Migration" intro above).
9. **Enterprise BYOK** (§6): customer-KMS `kek_kind` option in the key store, license-gated, additive on top of 1–8.
