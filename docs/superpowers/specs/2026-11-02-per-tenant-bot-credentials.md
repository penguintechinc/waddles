# Spec: Per-tenant bot credentials

Date: 2026-11-02
Status: DRAFT (surfaced during the #971 platform_integrations security review)
Owner decision captured from review session; needs design sign-off before implementation.

## Problem

Platform **bot** credentials (the Discord/Twitch/YouTube/etc. *application* OAuth
`client_id`/`client_secret` the bot authenticates with) are currently **global** —
one row per platform, shared by every tenant. The intended model is that bot
credentials are **per-tenant**:

- The **default/"global" tenant** (`tenant_slug = "global"`, resolved by
  `public_service.resolve_default_tenant()`) carries the main/company bot id and
  secret **by default, configured via Docker ENV**.
- **Every additional tenant supplies its own** bot credentials (their own Discord
  app, Twitch app, etc.). A tenant with no configured credential for a platform
  does not silently fall back to the global tenant's bot.

This gap was found while reviewing #971: `pi_list_metadata` returns
`SELECT * FROM platform_integrations` with **no tenant filter** and is documented
(regression `test_no_tenant_scope_bundle_can_list_platform_config`, gh-969) as
"cross-tenant BY DESIGN". That "by design" is the bug — it is only correct under
the current global-bot model, which we are replacing.

## Current model (verified 2026-11-02)

`config/postgres/migrations/030_platform_integrations.sql`:

- `platform_integrations` has **no `tenant_id` column**. Tenancy is derived only
  for the `community_oauth` path via `community_id -> communities.tenant_id`.
- `integration_type IN ('bot', 'user_oauth', 'community_oauth')`, enforced by a
  CHECK:
  - `bot`            -> `community_id IS NULL AND user_id IS NULL`  (fully global)
  - `community_oauth`-> `community_id IS NOT NULL AND user_id IS NULL`
  - `user_oauth`     -> `user_id IS NOT NULL AND community_id IS NULL`
- `UNIQUE (platform, integration_type, community_id, user_id)` — for bot rows this
  is effectively `UNIQUE (platform, 'bot', NULL, NULL)` = one global bot per platform.
- #971's `0069_pi_definer_functions` tenant-scopes the `community_oauth` functions
  (via `pi_tenant_ok(community_id, tenant_id)`), but the **bot path has no tenant
  concept to scope against**, and `pi_list_metadata` returns all rows.

## Target model

A bot credential belongs to exactly one tenant. The default/global tenant's bot
credentials are seeded from Docker ENV; other tenants configure theirs via the
superadmin UI/API.

### Data model changes

1. Add `tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE` to
   `platform_integrations` (new alembic migration — **do not** edit the already-applied
   `030_*.sql`).
2. Update the integration-type CHECK so the bot path is tenant-scoped:
   - `bot`            -> `tenant_id IS NOT NULL AND community_id IS NULL AND user_id IS NULL`
   - `community_oauth`-> `tenant_id` is derivable from `community_id` (either store it
     denormalized and assert `tenant_id = communities.tenant_id`, or keep deriving it —
     decide in design review; denormalizing makes the definer functions more surgical).
   - `user_oauth`     -> unchanged (user-scoped).
3. Replace the bot UNIQUE constraint with
   `UNIQUE (platform, integration_type, tenant_id, community_id, user_id)` so each
   tenant gets its own bot row per platform.
4. Backfill: existing global bot rows are assigned to the **global/default tenant**.

### Default/global tenant from ENV

- On hub-api startup (or a seeding job), if the global/default tenant has no active
  bot credential for a platform **and** the corresponding ENV is set, upsert it.
- ENV naming (proposal): `WADDLES_BOT_<PLATFORM>_CLIENT_ID` /
  `WADDLES_BOT_<PLATFORM>_CLIENT_SECRET` (+ token fields as applicable). Secrets go
  through the existing envelope-encryption seal path, never stored plaintext.
- Only the global/default tenant is ENV-seeded; other tenants are never ENV-seeded.

### SQL must be surgical (review ruling)

- `pi_list_metadata` must take a **required** `p_tenant_id` and filter
  `WHERE tenant_id = p_tenant_id` (plus the existing integration_type/platform
  filters). It must never return another tenant's rows.
- If a platform-operator "all tenants" view is genuinely needed, it is a **separate,
  explicitly superadmin-gated** function (e.g. `pi_list_metadata_all_tenants`), not
  the default behavior of the per-tenant reader.
- All bot-path definer functions get the same `pi_tenant_ok`-style fail-closed tenant
  gate the community_oauth functions already have.

## Security considerations

- Fail closed: a NULL/absent tenant must never widen the result set.
- A tenant must not be able to read, use, or enumerate another tenant's bot creds.
- Removing the "cross-tenant BY DESIGN" behavior will require updating/retiring
  `test_no_tenant_scope_bundle_can_list_platform_config` and the gh-969 comment.
- The superadmin `users:admin` scope currently gates the metadata route; after this
  change the route must also pass the caller's tenant (or an explicit all-tenants
  flag only the platform operator role may set).

## Relationship to #971

- #971 (SECURITY DEFINER hardening for the **community_oauth** path) is **correct and
  independent** of this change and should land on its own. The review confirmed its
  community-path tenant isolation (`pi_tenant_ok` fails closed, functions pinned
  search_path, granted by exact signature, owner is NOLOGIN/NOSUPERUSER/BYPASSRLS
  with no memberships).
- This per-tenant bot change is a **follow-up**: it edits the same table + definer
  module, so sequence it **after** #971 merges to avoid churn, as its own PR.
- Small #971-local nit from the review worth doing in #971 itself: the
  `GRANT USAGE ON SEQUENCE %s` in the migration should use `%I` (identifier quoting),
  not `%s`.

## Open questions (design review)

1. Denormalize `tenant_id` onto `community_oauth`/`user_oauth` rows too (uniform, more
   surgical definer SQL), or only add it for bot rows?
2. Exact ENV variable names + which platforms are ENV-seeded for the global tenant.
3. Is there a legitimate platform-operator "all tenants" bot view, and which role
   gates it?
4. Migration backfill: are there existing non-global tenants already relying on the
   shared global bot that need explicit per-tenant rows created at migrate time?
