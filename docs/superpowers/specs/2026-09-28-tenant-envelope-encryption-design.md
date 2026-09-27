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

**Load-bearing exception — `hub_users`/`hub_user_identities`/`hub_user_profiles` are NOT per-tenant encrypted.** Per `critical-rules.md` PII Tokenization there is exactly one identity table for the whole product, and `hub_users` has no `tenant_id` — a user reaches multiple tenants via `community_members`/`tenant_admins` rows on `communities`, which *do* carry `tenant_id`. Identity isn't partitionable by tenant, so forcing a per-tenant DEK on it would either (a) require picking an arbitrary "home tenant" that breaks on multi-tenant users, or (b) block login during any one tenant's key outage for a user who isn't even a member of that tenant. Instead: one **platform identity DEK** (same root KEK, same rotation/cache machinery, keyed by a fixed sentinel instead of `tenant_id`) protects PII in the identity table. Tenant-level crypto-shredding does not apply to identity rows — GDPR/DSAR erasure there is handled per-user (see `data_deletion_requests`, out of scope for this doc) and is a row-anonymization operation, not a key-destruction one.

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
| `audit_log`, `activity_logs`, `workflow_audit_log` | `ip_address` (+ per-tenant HMAC blind index for equality search), `user_agent` | `action`, `target_type`/`id`, `details`/`changes`/`metadata` JSONB, `user_id`, timestamps | IP/UA are PII-adjacent per `security.md`/`critical-rules.md` Observability. JSONB blobs stay plaintext at the container level — a secret ever placed inside one is a bug to fix by extracting it to a real encrypted column, not a reason to encrypt arbitrary JSON (which kills key-based `@>` queries). |
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
AAD = f"{tenant_id}|{table}|{column}|{row_id}|{dek_version}"
```

GCM's authentication tag covers the AAD, so a raw-bytes copy of one tenant's (or one row's, or one column's) ciphertext into another row fails to decrypt rather than silently decrypting under the wrong context — this is what makes "per-tenant" a cryptographic property instead of a query-discipline convention.

**Key/version columns, added consistently to every encrypted column pair** (mirrors `connection_credentials.dek_version` exactly):

```sql
<col>_ciphertext BYTEA NOT NULL,
<col>_iv         BYTEA NOT NULL,   -- 12-byte GCM nonce, unique per encryption
<col>_dek_version INTEGER NOT NULL
```

---

## 3. Querying encrypted data

- **Equality lookups → per-tenant (or platform-identity) HMAC-SHA256 blind index**, derived via HKDF from the same DEK (`info="blind-index-v1"`), stored as `<col>_bidx BYTEA` alongside the ciphertext. Concrete case: `hub_users.email_bidx = HMAC(platform_identity_hmac_key, lower(trim(email)))`, `UNIQUE NOT NULL`, used for both the uniqueness constraint and the login `WHERE` clause — the ciphertext column is decrypted only after the row is found, for display.
- **Existing UNIQUE constraints are unaffected** — every one of them (`ingest_sources (tenant_id, platform, external_id)`, `ai_byok_keys (community_id, provider)`, `music_oauth_tokens (community_id, platform)`, `community_overlay_tokens (community_id)`) keys off metadata columns, never the encrypted value itself.
- **What can't be searched:** substring/`LIKE`/full-text search on `message_content` or any encrypted free-text field — a blind index is equality-only. No message-search feature exists today; if one is added later it needs an app-level decrypt-and-reindex step (e.g., a per-tenant search shard keyed the same as the DEK), out of scope here.
- **Audit investigative queries** ("all actions from IP X") keep working via `ip_address_bidx`, same HMAC pattern, scoped to the tenant the audit row belongs to (resolved from `community_id`/`user_id` at write time).
- **JSONB `config`/`details`/`metadata` stay plaintext containers.** A specific sensitive key found inside one is pulled out into its own encrypted column, not solved by encrypting the whole blob (which breaks every `@>`/key-path query on the non-sensitive rest of it).

---

## 4. Key lifecycle

**`tenant_encryption_keys`** (new, hub-api schema only):

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
-- rbac-matrix.yaml: hub_api gets SELECT/INSERT/UPDATE; every other role
-- (waddles_bundle_reader, svc_ingest/svc_action/svc_process, webui, migration_runner)
-- gets privileges: [] -- identical posture to connection_credentials.
```

- **Creation:** a DEK is generated (CSPRNG, 256-bit) and wrapped under the root KEK the moment a `tenants` row is inserted — no lazy/first-use generation, so there's never a window where tenant data exists without a key.
- **Caching:** hub-api caches *unwrapped* DEKs (and their HKDF-derived subkeys) in-process, versioned by `dek_version`, TTL 10 minutes — this is the exact same cache the credential broker (§2.1 of the connections doc) already runs; it is extended to cover this DEK, not duplicated. KMS unwrap volume is therefore ~300 tenants / 10-min TTL ≈ 0.5 calls/sec steady state, identical to the connections doc's number, because it's the same call.
- **Rotation:**
  - **KEK rotation** (root key rotated) → background job re-wraps every `tenant_encryption_keys.wrapped_dek` under the new KEK. O(tenants), zero data re-encryption — the DEK itself never changes.
  - **DEK rotation** (single-tenant compromise suspicion) → insert a new `dek_version` row (`status='active'`), flip the old row to `retired` (kept, not destroyed — old ciphertext must stay decryptable). New writes use the new version immediately; existing ciphertext is **re-encrypted lazily** (on next natural write) plus a low-priority background sweep with a bounded SLA (7 days) for rows that are never naturally rewritten — a forced synchronous re-encrypt across 10,000s of channels'/20,000 communities' worth of rows is a lock-contention/availability risk this design explicitly avoids.
  - **Cache invalidation:** hub-api publishes `dek-rotated:{tenant_id}` on the same Valkey pub/sub channel the connections doc uses for fast-path credential revocation; every hub-api replica evicts that tenant's cached DEK immediately. Poll-based self-healing fallback if a pub/sub message is missed (same posture, not a new mechanism).
- **Tenant deletion = crypto-shredding.** Mark `tenant_encryption_keys` `destroyed` and delete `wrapped_dek` (baseline) — every ciphertext row for that tenant becomes permanently unrecoverable instantly, without touching or scanning the tenant's actual data rows. Row deletion of the now-garbage ciphertext is a housekeeping cleanup that can lag safely; the security event is the key destruction, not the row deletion.
- **Backups.** Baseline (platform KMS): a stolen DB backup contains wrapped DEKs but the root KEK lives in KMS, outside the DB — useless without live KMS access. However, deleting `wrapped_dek` from the primary doesn't erase it from *existing* backups, so baseline crypto-shred is bounded by the backup retention window (documented, finite), not instant against arbitrarily old backups. **Enterprise BYOK gives a strictly stronger guarantee here** (§6): revoking the customer's own KMS key makes even old backups' wrapped DEKs permanently unwrappable, since unwrap always requires a live call to a key the customer controls.

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
| Customer revokes IAM policy / deletes their KMS key | Cached DEK keeps serving up to its TTL/max-staleness bound (same stale-while-revalidate posture as the credential broker); once the cache expires, unwrap fails → **that tenant's** encrypted reads/writes fail closed with a clear "tenant encryption key unavailable" error, alerted to ops. Never silently falls back to the platform KEK — that would quietly downgrade the isolation guarantee the customer paid for. |
| Customer KMS regional outage (transient) | Same stale-while-revalidate grace window absorbs short blips without an outage. |
| One tenant's BYOK KMS is down/revoked | **Zero effect on other tenants** — each tenant's KMS call is independent, no shared blocking resource, matching the "one bad KMS never blocks the fleet" posture already established for the credential broker. |

---

## 7. Performance & migration plan

**KMS call volume scales with tenants, not rows** — unwrap only happens on a 10-minute-TTL cache miss per active tenant (§4), the same ~0.5 calls/sec steady state and ≤900-call batched cold-start burst already computed in the connections doc, because it's the same cache. Per-request cost is a single in-process AES-256-GCM open (microseconds) against an already-unwrapped subkey — KMS is never in the per-query hot path.

**Migration — numbered increments, security-first, no disable-once-enforced kill-switch** (an "encryption off" flag would itself be a standing plaintext-fallback vulnerability, unlike a rollback-safe mechanism flag — the migration flag below is default-OFF and is *deleted*, not flipped, once complete):

1. **`tenant_encryption_keys` + RBAC grants** (hub-api only; every other role `privileges: []`) + platform-KMS integration + DEK generation wired into tenant creation (new tenants only). Behind default-OFF `waddles.core.tenant-envelope-encryption`.
2. **Shared crypto library**: AES-256-GCM field-encryption helper with the AAD scheme (§2), HKDF blind-index derivation, `EncryptedField`-style wrapper reused by every table below. Unit tests cover AAD-mismatch rejection (cross-tenant/row/column swap must fail).
3. **Dual-write shadow migration** on `connection_credentials`/`ingest_sources` (executed as the connections doc's own step 1, under this shared key hierarchy) and `ai_byok_keys` (move off its bespoke key onto the shared tenant DEK). New writes hit both old and new columns; reads still come from old columns; mismatch logging catches drift.
4. **Cutover reads** to the new ciphertext path once shadow-verified with zero mismatches for a full backup-retention cycle; stop populating old plaintext columns (don't drop yet).
5. **Extend to `platform_integrations`, `music_oauth_tokens`**; convert `browser_source_tokens`/`community_overlay_tokens` to hashed lookup (no encryption needed there, §1).
6. **Extend to `hub_chat_messages.message_content` and audit-table `ip_address`/`user_agent`** (with blind index) — the largest row counts at this scale, so this step runs as a rate-limited, resumable, watermark-cursor background backfill, off-peak, never a blocking migration.
7. **Drop legacy plaintext columns** only after a full backup cycle has rolled past the cutover point (so no un-migrated backup restore reintroduces plaintext), **and delete the migration flag and its plaintext-fallback code path** — this step, not a flag flip, is what makes encryption permanently enforced.
8. **Enterprise BYOK** (§6): customer-KMS `kek_kind` option, license-gated, additive on top of 1–7.
