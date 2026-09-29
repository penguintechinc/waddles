# Guild <-> Community Binding Schema Contract (#500)

**Status:** Draft — hub-api schema half of #500 (spec #499 Sec3.0/Sec3.10).
**Migration:** `alembic/versions/0038_guild_tenant_pairing.py`
**Audience:** the Rust data plane (ingest/action connectors) reading routing and role-ownership; hub-api service authors.

## 1. Tables (hub-api-owned, write access only from `hub_api`)

| Table | Purpose | Key uniqueness |
|---|---|---|
| `tenant_platform_credentials` | Per-tenant Discord application/bot credentials (client id, encrypted client secret, encrypted bot token) | `UNIQUE(tenant_id, platform)` |
| `guild_tenant_pairings` | One row per (guild, tenant) — active only from that tenant's own OAuth2 bot-install callback | `UNIQUE(platform, guild_id, tenant_id)` |
| `community_channel_bindings` | Binds a community to a channel (`channel_id` set) or a guild default (`channel_id NULL`) | Partial unique indexes, see Sec3 below |
| `managed_roles` | One owning community per external Discord role id per guild | `UNIQUE(platform, guild_id, role_id)` |

All four tables carry `tenant_id`; `community_channel_bindings` and `managed_roles` additionally carry `pairing_id` (FK to `guild_tenant_pairings`). No PII columns — guild/channel/role ids are Discord platform identifiers, not PII; every human actor (installing admin, guild authority, approver) is referenced by `hub_users.id` (today's actual PK type — see Sec6, "PII/UUID note").

## 2. One bot per tenant, not a shared bot (owner correction, 2026-09-29)

Discord allows an application to be installed into a guild only once. Consent is therefore **the tenant's own OAuth2 bot-install callback** (`bot` + `applications.commands` scopes), never a shared-bot "Manage Server" permission check.

- A shared guild hosting N paired tenants has **N separate Discord bots** installed, one per tenant, each with its own `tenant_platform_credentials` row and its own role-hierarchy position in that guild — this is what bounds cross-tenant blast radius for `managed_roles` (a tenant's bot can only touch roles positioned below its own bot's role).
- **Credential resolution (fail-closed, mandatory):**

  ```
  resolve_credentials(tenant_id, platform):
      if tenants.is_global(tenant_id):
          return platform_credentials_from_cluster_secret()  # waddlebot-platform-credentials (#478)
      row = SELECT * FROM tenant_platform_credentials WHERE tenant_id=? AND platform=? AND is_active
      if row is None:
          fail_closed("tenant Discord app not configured")   # NEVER fall back to the platform bot
      return decrypt(row)
  ```

  A non-global tenant with no `tenant_platform_credentials` row gets a hard failure, never the platform bot. Enforced today by a service-layer check plus a DB trigger (`trg_reject_global_tenant_credentials`) that rejects any row insert/update for the global tenant, preventing the inverse mistake (accidentally DB-storing the global tenant's credentials).
- `tenant_platform_credentials` is granted to `hub_api` only in the RBAC matrix — the data plane **never** reads credentials off a table directly. It resolves them through hub-api's own internal credential-resolution endpoint (to be added alongside PR #442's `internal_keys.py` pattern — same "resolve, don't hand out raw grants" shape). This is a documented dependency, not implemented in this migration.
- `key_ref` on `tenant_platform_credentials` is a placeholder today (ciphertext/IV columns, same shape as `ingest_sources.secret_ciphertext`/`secret_iv`). Once PR #442 (per-tenant DEK broker) merges, a follow-up migration repoints `key_ref` at a `tenant_keystore` key id and re-encrypts existing rows under a tenant DEK — **not implemented here**, PR #442 is still open.

## 3. Routing precedence (data plane reads `v_guild_routing`, never the base tables)

For an interaction in `(platform, guild_id, channel_id)`:

1. **Channel-level binding** — `v_guild_routing` row where `channel_id` matches. Exclusive across every tenant paired with the guild (partial unique index `uq_channel_binding_exclusive` on `(platform, guild_id, channel_id) WHERE channel_id IS NOT NULL AND status='active'`).
2. **Explicit community tag/prefix** — resolved by bundle/command logic, not this schema (a command argument or per-channel naming convention); not modeled as a table here.
3. **Guild-level default** — `v_guild_routing` row where `channel_id IS NULL`. Exclusive across every tenant (partial unique index `uq_guild_default_binding_exclusive` on `(platform, guild_id) WHERE channel_id IS NULL AND status='active'`).
4. **Ambiguous → fail closed.** Zero or more-than-one resolved row is a routing failure, never a broadcast to all candidates.

A binding row can only exist under an `active` pairing for the *same* tenant — enforced by the `trg_require_active_pairing_for_binding` trigger (a cross-table check a plain `CHECK` constraint can't express), not just a service-layer guard.

## 4. Role ownership (data plane reads `v_managed_roles_active`)

Every grant/revoke, and every *read* used as a sync decision input (spec Sec3.0's "read-state authorization gap"), resolves the target role through `v_managed_roles_active` by `(platform, guild_id, role_id)` — an unregistered or wrongly-owned role is treated as absent, never inferred from Discord-side presence alone. `registered_via='adopted'` rows require `approved_by_user_id` (DB-enforced via `chk_managed_roles_adopted_approval`); `registered_via='created'` rows need none.

## 5. Revocation

A pairing's `status` flips to `revoked` from either:
- a tenant admin revoking (`revoked_by='tenant_admin'`), or
- the bot being removed from the guild — a Discord guild-delete or integration-removed gateway event (`revoked_by='guild_removed_bot'` / `'integration_removed'`).

Revocation is immediate: `v_guild_routing`/`v_managed_roles_active` stop returning any row under that pairing the moment `status != 'active'` (the views filter on pairing status). `community_channel_bindings`/`managed_roles` rows are **not deleted** — they're left in place, `pending_cleanup` marked on `managed_roles` for the data plane's best-effort unwind pass (actual Discord role removal happens there, not in hub-api).

## 6. Backward compatibility

Every pre-existing `ingest_sources` row with a `community_id` is backfilled into one `guild_tenant_pairings` row (`status='active'`, backdated `consent_at`/`created_at`) and one guild-default `community_channel_bindings` row (`channel_id IS NULL`) — today's 1:1 routing is preserved exactly, no `ingest_sources` row changes behavior.

**Known gap, not silently bridged:** grandfathered non-global-tenant pairings get **no** `tenant_platform_credentials` row (there is no secret to migrate from `ingest_sources`). Any real Discord bot action for such a tenant fails closed until that tenant configures its own Discord app — this is an explicit operational follow-up, not a fallback to the platform bot.

## 7. PII/UUID note (deviation from the original ask, documented)

The task brief asked for guild-authority/consenting-user references via `hub_users.uuid`. That column does not exist yet on `release/v3.0.X` — it lands in open PR #434 (`hub_users.uuid identity column`), not yet merged. This migration references `hub_users.id` (today's actual integer PK) instead, to avoid either duplicating PR #434's schema ownership or taking a hard dependency on an unmerged PR. **Follow-up required once #434 merges:** a small migration repointing `installed_by_user_id`/`revoked_by_user_id`/`approved_by_user_id` at `hub_users.uuid` per the PII-reference-by-UUID rule.

## 8. Grants summary

| Table/View | `hub_api` | `svc_ingest` | `svc_action` | everyone else |
|---|---|---|---|---|
| `tenant_platform_credentials` | full | none | none | none |
| `guild_tenant_pairings` | full | none | none | none |
| `community_channel_bindings` | full | none | none | none |
| `managed_roles` | full | none | none | none |
| `v_guild_routing` | SELECT | SELECT | SELECT | none |
| `v_managed_roles_active` | SELECT | SELECT | SELECT | none |

Rendered from `config/postgres/rbac-matrix.yaml` by `scripts/db/rbac_matrix.py`, same generator every prior hub-api migration uses — no hand-written GRANT.
