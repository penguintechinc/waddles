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
