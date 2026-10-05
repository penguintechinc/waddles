"""pydal table bindings for the M1 Core Identity/Auth group.

Schema is owned by `config/postgres/migrations/*.sql`, never by this
process (`backend-database.md`: "NO automatic Alembic migrations on
startup"; `app.py::_bind_reference_tables` already documents this for
`tenants`). `bind_auth_tables()` exists only so pydal has table/field
objects to build queries against -- every `define_table()` call below
passes `migrate=False` in production (see `bind_auth_tables()`'s own
docstring for the test-only exception).

Column provenance (so the next reader doesn't have to re-derive it):
`config/postgres/migrations/000_create_base_schema.sql` (hub_users,
hub_sessions, hub_temp_passwords, hub_user_identities, hub_oauth_states,
hub_user_profiles), `049_add_auth_settings.sql` (user_passkeys),
`058_tenants_and_claims.sql` (tenant_admins, tenants), `060_analytics_
consumer_role.sql` (hub_users.is_analytics_consumer). `communities`/
`community_members` are 000's own tables, bound here only for the
narrow slice `auth_service.add_user_to_global_community()` and
`get_current_user()`'s communities-list need.

Two known pre-existing gaps surfaced while porting the Node source
faithfully (not introduced by this port -- see `hub_api/PORTING.md`):
  1. `hub_users.email_verification_expires` is referenced by
     `authController.js` register()/verifyEmail()/resendVerification()
     but is defined only in `config/postgres/init.sql` (a separate,
     drifted bootstrap script), not in the numbered migrations that
     actually run in every real environment. Bound here to keep the
     Python port byte-faithful to Node; querying it will 500 exactly
     like Node does today if the column is truly absent. Needs a
     migration to reconcile -- out of scope for a "no schema changes"
     port PR.
  2. `hub_oauth_states` has no `metadata` column, but Node's
     `startOAuth()` INSERTs one (to stash `tenantSlug` for the login
     flow) -- also a pre-existing runtime error in Node today. This
     port omits the metadata insert and resolves login-flow OAuth
     against `DEFAULT_TENANT_SLUG` only (see `oauth_service.py`),
     which matches this app's actual default-tenant-only OAuth-login
     reality rather than porting a call that would 500.
  3. `platform_configs.enabled` (queried by `authController.js`'s
     `getTenantLoginInfo()`) is likewise undefined by any numbered
     migration -- bound here for the same byte-faithful-to-Node reason
     as gaps (1)/(2).
  4. `communities.about_extended`/`social_links`/`website_url`/
     `discord_invite_url`/`visibility` -- see the `communities`
     `define_table()` call's own inline comment for detail. Added by
     the M2 Core Tenancy-Misc group.
  5. `communities.license_key`/`license_expires_at`/`license_tier`
     (queried by `workflowController.js::validateLicense()`) are not
     defined by ANY numbered migration either (verified: `grep -rl
     license_key config/postgres/migrations/` -> no hits) -- same gap
     class as (1)-(3), added by the M-automation port group (see
     `hub_api/blueprints/v1/workflow.py`). `communities` can only be
     `define_table()`-d once per DAL instance (pydal), so these fields
     extend THIS SAME call rather than a second, competing definition
     -- the established pattern (see `app.py::_bind_reference_tables`'s
     own docstring re: `tenants`).
  6. `support_tickets`/`support_ticket_comments` (queried by
     `githubSyncService.js::syncTicketToGithub()`/
     `processInboundIssueComment()`) do not exist in ANY numbered
     migration at all -- apparently owned by a not-yet-ported Support
     module. Bound in `bind_github_sync_tables()` below anyway, byte-
     faithful to Node: a query against them will 500 exactly like
     Node's own code does against the real schema today. See
     `hub_api/blueprints/v1/github_sync.py`.

M2 (Core Tenancy-Misc: `communityProfileController.js` +
`joinRequestController.js`) extends this function's existing
`communities`/`community_members` calls with the additional columns
those two controllers need, and adds `community_roles`/
`community_join_requests` -- per this file's own precedent (`app.py`'s
`tenants` table) of extending the ONE existing `define_table()` call
rather than redefining a table a second time elsewhere. `bind_auth_tables()`
is still the sole call site (`app.py::_bind_reference_tables()`), so no
`app.py` edit is needed for a group that only needs more columns on an
already-bound table.

`community_roles` (`058_tenants_and_claims.sql`, already bound by the M2
group above) and the `community_members.community_role_id` FK the same
migration adds are also relied on by `services/community_authz.py`'s
faithful port of `middleware/auth.js::requireCommunityAdmin()`, shared by
the workflow and github_sync port groups (both Node route files gate
every endpoint with `requireCommunityAdmin`); `bind_community_authz_tables()`
below is this group's own idempotent-binding entry point mirroring that
module's own lazy-bind call pattern.
"""

from __future__ import annotations

from typing import Any

from pydal import Field


def bind_auth_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the M1 auth/identity/passkey/profile/user-management group queries.

    Idempotent per-DAL-instance -- a second call on the same `dal` is a
    no-op (guards against double-binding if `app.py` startup and a test
    fixture both call this against the same instance).

    `migrate=False` (the default) matches production: schema owned by
    `config/postgres/migrations/*.sql`, this process never runs DDL. Tests
    against a throwaway sqlite file (`tests/conftest.py::auth_db`) pass
    `migrate=True` so pydal actually creates the tables -- there is no
    separate migration step for a test-only database.
    """
    if "hub_users" in dal.tables:
        return

    dal.define_table(
        "hub_users",
        Field("display_name", "string", length=255),
        Field("username", "string", length=255),
        Field("email", "string", length=255),
        Field("password_hash", "string", length=255),
        Field("avatar_url", "text"),
        Field("is_super_admin", "boolean", default=False),
        Field("is_vendor", "boolean", default=False),
        Field("is_analytics_consumer", "boolean", default=False),
        Field("email_verified", "boolean", default=False),
        Field("email_verification_token", "string", length=255),
        Field("email_verification_expires", "datetime"),  # see module docstring gap (1)
        Field("password_reset_token", "string", length=255),
        Field("password_reset_expires", "datetime"),
        Field("last_login", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("is_active", "boolean", default=True),
        migrate=migrate,
    )

    dal.define_table(
        "hub_sessions",
        Field("session_token", "text", notnull=True),
        Field("user_id", "integer"),
        Field("platform", "string", length=50),
        Field("platform_user_id", "string", length=255),
        Field("platform_username", "string", length=255),
        Field("avatar_url", "text"),
        Field("is_active", "boolean", default=True),
        Field("expires_at", "datetime"),
        Field("revoked_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_user_identities",
        Field("hub_user_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("platform_username", "string", length=255),
        Field("avatar_url", "text"),
        Field("is_primary", "boolean", default=False),
        Field("linked_at", "datetime"),
        Field("last_used", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_oauth_states",
        Field("state", "string", length=255, notnull=True, unique=True),
        Field("mode", "string", length=50, default="login"),
        Field("platform", "string", length=50, notnull=True),
        Field("user_id", "integer"),
        Field("redirect_uri", "text"),
        Field("expires_at", "datetime", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_oauth_exchange_codes",
        # `config/postgres/migrations/075_oauth_exchange_codes.sql` -- backs the
        # exchange-code handoff fix for the JWT-in-URL leak in
        # `blueprints/v1/auth.py::oauth_callback` (see that route's docstring
        # and `hub_api/PORTING.md` Gotcha #8). Single-use is enforced by an
        # atomic `UPDATE ... WHERE used = FALSE AND expires_at > NOW()` claim
        # in `oauth_service.redeem_oauth_exchange_code` -- the database, not
        # application logic, arbitrates a concurrent-redemption race (same
        # pattern as `community_welcomed_users`, migration 068).
        Field("code", "string", length=255, notnull=True, unique=True),
        Field("token", "text", notnull=True),
        Field("platform", "string", length=50),
        Field("used", "boolean", default=False),
        Field("used_at", "datetime"),
        Field("expires_at", "datetime", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_temp_passwords",
        Field("user_identifier", "string", length=255, notnull=True),
        Field("password_hash", "string", length=255, notnull=True),
        Field("community_id", "integer"),
        Field("force_oauth_link", "boolean", default=False),
        Field("linked_oauth_platform", "string", length=50),
        Field("linked_oauth_user_id", "string", length=255),
        Field("is_used", "boolean", default=False),
        Field("used_at", "datetime"),
        Field("expires_at", "datetime", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_user_profiles",
        Field("hub_user_id", "integer", notnull=True, unique=True),
        Field("display_name", "string", length=255),
        Field("bio", "text"),
        Field("location", "string", length=255),
        Field("location_city", "string", length=100),
        Field("location_state", "string", length=100),
        Field("location_country", "string", length=2),
        Field("website_url", "string", length=500),
        Field("custom_avatar_url", "text"),
        Field("banner_url", "text"),
        Field("visibility", "string", length=50, default="public"),
        Field("show_activity", "boolean", default=True),
        Field("show_communities", "boolean", default=True),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "user_passkeys",
        Field("user_id", "integer", notnull=True),
        Field("credential_id", "text", notnull=True, unique=True),
        Field("public_key", "text", notnull=True),
        Field("sign_count", "integer", default=0),
        Field("device_name", "string", length=100),
        Field("created_at", "datetime"),
        Field("last_used_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "tenant_admins",
        Field("tenant_id", "integer", notnull=True),
        Field("user_id", "integer", notnull=True),
        Field("role", "string", length=50, default="tenant-admin"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "communities",
        Field("name", "string", length=255),
        Field("display_name", "string", length=255),
        # Added by the Analytics-module port (M9): this table can only be
        # `define_table()`-d once per DAL instance, and `create_app()`
        # binds this M1 definition (via `_bind_reference_tables`) before
        # any request runs -- `services/community_common.py::
        # ensure_community_tables()`'s own idempotent guard then silently
        # skips its (separately-defined) `tenant_id` column, so
        # `community_in_tenant()` would `AttributeError` in production
        # against a real app (pre-existing gap, only masked because every
        # Community-module blueprint's own tests build an isolated app +
        # dal that never loads `bind_auth_tables` at all). Extending here
        # -- not redefining -- per this module's own docstring guidance for
        # `tenants` columns, generalized to `communities`: the real
        # Postgres column already exists (058_tenants_and_claims.sql),
        # this just maps pydal onto it.
        Field("tenant_id", "integer"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("member_count", "integer", default=0),
        Field("config", "json"),
        # license_key/license_expires_at/license_tier: added by the
        # M-automation port group -- see module docstring gap (5).
        Field("license_key", "string", length=255),
        Field("license_expires_at", "datetime"),
        Field("license_tier", "string", length=50),
        # Columns below added by the M3 Platform-admin group (adminController.js
        # /superadminController.js), the M2 Core Tenancy-Misc group
        # (communityProfileController.js / joinRequestController.js port),
        # and the M3 Platform-admin/Public group (platformController.js/
        # publicController.js -- see `hub_api/PORTING.md`) -- `communities`
        # is bound once, here, by M1; every column any later group needs
        # joins this same Field list rather than a second define_table()
        # call (pydal allows exactly one per table name per DAL instance).
        # `tenant_id`, `description`, `platform`, `owner_id`, `join_mode`,
        # `is_public`, `deleted_at` match 000_create_base_schema.sql +
        # 058_tenants_and_claims.sql's `ALTER TABLE communities ADD COLUMN
        # tenant_id` verbatim. `tenant_id` is NOT NULL in Postgres -- every
        # public/platform-admin query that lists communities MUST filter on
        # this (security.md Tenant Isolation); see services/public_service.
        # py's module docstring for the pre-auth resolution mechanism. Also
        # relied on by the M7 Streaming (music/stream/streaming) group AND
        # the M7 Streaming (overlay/calls) group: `flask_core.tenancy.
        # tenant_scoped()`'s community_id-owning-table fallback path reads
        # `dal.communities.tenant_id` directly -- no group before M7 queried
        # a community_id-owning table through that helper, so this was the
        # first caller to actually need the column bound (not just present
        # in Postgres). `services/community_access.py` (overlay/calls) is
        # the second `tenant_scoped()` caller with the identical need.
        # `about_extended`, `social_links`, `website_url`,
        # `discord_invite_url`, `visibility` are a pre-existing schema gap:
        # `communityProfileController.js`'s own raw SQL reads/writes these
        # exact column names on `communities`, but no numbered migration
        # ever adds them (`social_links` migration 037 adds a same-named
        # column to `community_members`, a DIFFERENT table -- not this
        # one). Bound here anyway to stay byte-faithful to Node: a query
        # touching these columns 500s against real Postgres exactly like
        # Node's own controller does today. Needs a migration to
        # reconcile -- out of scope for a "no schema changes" port PR.
        # `logo_url`/`banner_url`/`primary_platform` are genuine columns
        # platformController.js/publicController.js read, same
        # 000_create_base_schema.sql origin as the rest of this list.
        Field("tenant_id", "integer"),
        Field("description", "text"),
        Field("about_extended", "text"),  # gap -- see docstring above
        Field("social_links", "json"),  # gap -- see docstring above
        Field("website_url", "string", length=500),  # gap -- see docstring above
        Field("discord_invite_url", "string", length=500),  # gap -- see docstring above
        Field("visibility", "string", length=30, default="public"),  # gap -- see docstring above
        Field("logo_url", "text"),
        Field("banner_url", "text"),
        Field("primary_platform", "string", length=50),
        Field("platform", "string", length=50, default="discord"),
        Field("platform_server_id", "string", length=255),
        # VARCHAR in Postgres, not a hub_users.id FK -- see community_members.user_id below.
        Field("owner_id", "string", length=255),
        Field("owner_name", "string", length=255),
        Field("community_type", "string", length=50, default="creator"),
        Field("join_mode", "string", length=50, default="open"),
        Field("is_public", "boolean", default=True),
        Field("is_premium", "boolean", default=False),
        Field("seat_limit", "integer"),
        Field("created_by", "string", length=255),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("deleted_at", "datetime"),
        Field("deleted_by", "string", length=255),
        migrate=migrate,
    )

    dal.define_table(
        "community_members",
        Field("community_id", "integer"),
        # VARCHAR in Postgres (legacy platform-identity membership model),
        # not a FK to hub_users.id -- bound as string and callers pass
        # str(user_id), matching how Node's pg driver serializes it.
        Field("user_id", "string", length=255),
        # platform/platform_user_id/display_name/reputation added by the M3
        # group (platformController.js's getUsers/getUser/getStats) -- all
        # real columns in 000_create_base_schema.sql.
        Field("platform", "string", length=50),
        Field("platform_user_id", "string", length=255),
        Field("display_name", "string", length=255),
        Field("reputation", "integer", default=600),
        Field("role", "string", length=50, default="member"),
        Field("is_active", "boolean", default=True),
        Field("joined_at", "datetime"),
        # Gap (4): platformController.js's getUsers()/getUser()/getStats()
        # all reference `community_members.last_activity`/`created_at`, but
        # no numbered migration (nor init.sql) ever adds either -- only an
        # unrelated `analytics_suspected_bots.last_activity_at` exists. Same
        # class of pre-existing gap as (1)/(2)/(3): bound here to stay
        # byte-faithful to Node; a real query against either 500s exactly
        # like Node's does today. Needs a migration to reconcile -- out of
        # scope here.
        Field("last_activity", "datetime"),
        Field("created_at", "datetime"),
        # Columns below added by the M3 Platform-admin group -- same
        # single-define_table() rationale as `communities` above.
        # `claims_cache` matches 000_create_base_schema.sql /
        # 058_tenants_and_claims.sql exactly (`reputation` is already bound
        # above by the M3 Platform-admin/Public group -- same column, not
        # duplicated). `community_role_id` is the same FK to
        # community_roles.id (`058_tenants_and_claims.sql`'s "1f. Update
        # community_members" ALTER) the M2 Core Tenancy-Misc group's, the
        # M-automation group's, and the M7 Streaming group's own
        # `services/community_authz.py` (`_scoped` variant) all rely on
        # for their per-community admin checks -- bound once here, not
        # duplicated. `claims_cache` is the same faithful-port source the
        # M7 group's `resolve_community_membership_scoped()` parses for
        # the caller's community-scoped OIDC scopes.
        # `removed_at`/`removed_by`/`removal_reason` are a pre-existing
        # schema gap (adminController.js's removeMember() references them,
        # but no migration defines them -- same class of gap as
        # hub_users.email_verification_expires above; bound here to stay
        # byte-faithful to Node, which 500s against real Postgres today for
        # the exact same reason).
        Field("community_role_id", "integer"),
        Field("claims_cache", "json"),
        Field("removed_at", "datetime"),
        Field("removed_by", "integer"),
        Field("removal_reason", "text"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "community_roles",
        # Schema: `058_tenants_and_claims.sql`'s "1e. Community roles
        # table". Added by the M2 group -- `services/community_authz.py`
        # resolves a caller's granted scopes for one community via this
        # table (`base_claims.scopes`), the same DB-backed check Node's
        # `middleware/auth.js::requireCommunityAdmin`/`requireMember` do.
        Field("community_id", "integer", notnull=True),
        Field("name", "string", length=50, notnull=True),
        Field("display_name", "string", length=100),
        Field("priority", "integer", default=0),
        Field("base_claims", "json"),
        migrate=migrate,
    )

    dal.define_table(
        "community_join_requests",
        # Schema: `049_add_auth_settings.sql`. Added by the M2 group
        # (joinRequestController.js port). Unlike `community_members.
        # user_id` (legacy VARCHAR), `user_id` here is a real INTEGER FK
        # to `hub_users.id` -- matches Node's own INSERT/SELECT, which
        # pass `req.user.userId` (an int) directly, never `str(...)`.
        Field("community_id", "integer", notnull=True),
        Field("user_id", "integer", notnull=True),
        Field("status", "string", length=20, default="pending"),
        Field("message", "text"),
        Field("reviewed_by", "integer"),
        Field("reviewed_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "platform_configs",
        Field("tenant_id", "integer"),
        Field("platform", "string", length=50),
        Field("config_key", "string", length=100),
        Field("config_value", "text"),
        Field("is_encrypted", "boolean", default=False),
        # `enabled` is queried by authController.js's getTenantLoginInfo()
        # (`WHERE tenant_id = ... AND enabled = true`) but -- like gaps (1)
        # and (2) above -- is not defined by any numbered migration. Bound
        # here to stay byte-faithful to Node; a query against it will 500
        # exactly like Node's would against the real, gapped schema.
        Field("enabled", "boolean", default=False),
        migrate=migrate,
    )

    # hub_settings is schema-owned by the Platform-admin group (M3's
    # platformConfigController.js), not this group -- bound here anyway
    # because auth_service.register()/resend_verification() read it
    # (signup_enabled/email_configured/captcha_* flags). Table BINDING for
    # query purposes tracks "who queries it", not "who owns the schema" --
    # the M3 group extends this same definition if/when it needs to write
    # to it, rather than redefining the table a second time.
    dal.define_table(
        "hub_settings",
        Field("setting_key", "string", length=100, notnull=True),
        Field("setting_value", "text"),
        migrate=migrate,
    )


def bind_support_token_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the Support-ticket + PAT/CAT-token port group queries.

    Idempotent per-DAL-instance, same contract as `bind_auth_tables()` above.
    Not wired into `app.py::_bind_reference_tables()` -- `app.py`/`routers/*.py`/
    `blueprints/__init__.py` are frozen for the parallel port wave (see
    `services/community_common.py::ensure_community_tables()`'s own docstring
    for why). Callers (`services/support_service.py`, `services/
    access_token_service.py`) call this idempotently at the top of every
    service function instead, matching that same established pattern.

    Does NOT bind `communities`/`hub_users` -- those are owned by
    `bind_auth_tables()`/`ensure_community_tables()`; callers needing tenant
    isolation (`community_in_tenant()`) must ensure one of those has already
    run against the same `dal` (matches `services/community_*.py`'s own
    established convention of calling `ensure_community_tables(dal)` first).

    Column provenance: `support_ticket_categories`/`support_tickets`/
    `support_ticket_comments` are created at Node runtime startup
    (`admin/hub_module/backend/src/index.js`'s `initializeDatabase()`), not
    by any numbered SQL migration -- ported here verbatim from that CREATE
    TABLE block. `user_access_tokens`/`community_access_tokens` come from
    `config/postgres/migrations/048_add_pat_cat_tables.sql`.

    One more pre-existing gap, same category as `hub_api/PORTING.md`'s
    Gotcha #4: `048_add_pat_cat_tables.sql` never creates a `permission_scopes`
    table with `scope_key`/`display_name` columns -- the only migration that
    creates `permission_scopes` at all (`011_add_scoped_tokens.sql`) defines
    `scope_name` (not `scope_key`) and has no `display_name` column, yet
    `tokenController.js`'s `createCAT()`/`listScopes()` query exactly
    `scope_key`/`display_name` from it. This is a pre-existing runtime gap in
    Node's own code today (its query would already fail against the real,
    migrated schema), not introduced by this port. Bound here byte-faithful
    to Node's query, not silently renamed to the migration's real column --
    needs a migration to reconcile (`ALTER TABLE permission_scopes RENAME
    COLUMN scope_name TO scope_key`, `ADD COLUMN display_name`), out of scope
    for a "no schema changes" port PR.
    """
    if "support_ticket_categories" not in dal.tables:
        dal.define_table(
            "support_ticket_categories",
            Field("community_id", "integer", notnull=True),
            Field("name", "string", length=255, notnull=True),
            Field("description", "text"),
            Field("sort_order", "integer", default=0),
            Field("is_active", "boolean", default=True),
            Field("form_fields", "json"),
            Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "support_tickets" not in dal.tables:
        dal.define_table(
            "support_tickets",
            Field("community_id", "integer", notnull=True),
            Field("category_id", "integer"),
            Field("ticket_number", "string", length=20, notnull=True),
            Field("subject", "string", length=500, notnull=True),
            Field("description", "text"),
            Field("status", "string", length=20, default="open"),
            Field("priority", "string", length=20, default="medium"),
            Field("reporter_user_id", "integer"),
            Field("reporter_name", "string", length=255),
            Field("reporter_email", "string", length=255),
            Field("assignee_user_id", "integer"),
            Field("custom_fields", "json"),
            Field("resolved_at", "datetime"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "support_ticket_comments" not in dal.tables:
        dal.define_table(
            "support_ticket_comments",
            Field("ticket_id", "integer", notnull=True),
            Field("author_user_id", "integer"),
            Field("author_name", "string", length=255),
            Field("content", "text", notnull=True),
            Field("is_internal", "boolean", default=False),
            Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "user_access_tokens" not in dal.tables:
        dal.define_table(
            "user_access_tokens",
            Field("user_id", "integer", notnull=True, unique=True),
            Field("name", "string", length=100, notnull=True),
            # SHA-256 hex of the plaintext token; plaintext is never stored.
            Field("token_hash", "string", length=64, notnull=True, unique=True),
            # NULL = inherit the user's full permissions; pydal has no native
            # Postgres TEXT[] type -- "list:string" round-trips a Python list
            # portably across Postgres/MySQL/sqlite (pydal's own abstraction).
            Field("scope_ceiling", "list:string"),
            Field("created_at", "datetime"),
            Field("last_used_at", "datetime"),
            Field("expires_at", "datetime"),
            Field("is_revoked", "boolean", notnull=True, default=False),
            migrate=migrate,
        )

    if "community_access_tokens" not in dal.tables:
        dal.define_table(
            "community_access_tokens",
            Field("community_id", "integer", notnull=True),
            Field("created_by_user_id", "integer"),
            Field("name", "string", length=100, notnull=True),
            Field("token_hash", "string", length=64, notnull=True, unique=True),
            Field("scopes", "list:string", notnull=True),
            Field("created_at", "datetime"),
            Field("last_used_at", "datetime"),
            Field("expires_at", "datetime"),
            Field("is_revoked", "boolean", notnull=True, default=False),
            migrate=migrate,
        )

    if "permission_scopes" not in dal.tables:
        dal.define_table(
            "permission_scopes",
            # See this function's own docstring -- `scope_key`/`display_name`
            # match Node's query, not `011_add_scoped_tokens.sql`'s real
            # `scope_name` column (pre-existing schema-drift gap).
            Field("scope_key", "string", length=100, notnull=True, unique=True),
            Field("display_name", "string", length=255),
            Field("description", "text"),
            Field("category", "string", length=50),
            migrate=migrate,
        )


def bind_overlay_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the M7 Streaming (overlay/calls) group's own tables.

    Named `bind_overlay_tables` (not `bind_streaming_tables`) even though
    this group is also nominally M7 Streaming -- the M7 music/stream/
    streaming controller group (a separate, parallel port task) already
    landed its own `bind_streaming_tables()` above for a disjoint table
    set (`community_music_settings`, `coordination`, `community_servers`,
    etc.); this group's own tables (`community_overlay_tokens`,
    `overlay_access_log`) are unrelated, so this function keeps its own
    name rather than colliding on identical text with incompatible bodies
    (the same class of same-name-different-function collision
    `services/community_authz.py`'s own `_scoped` suffix already
    documents for the M-automation/M7-music groups).

    Column provenance: `config/postgres/migrations/000_create_base_schema.sql`
    ("community_overlay_tokens", "overlay_access_log").

    Deliberately NOT wired into `app.py::_bind_reference_tables()` the way
    `bind_auth_tables()` is -- this port's task scope explicitly forbids
    editing `app.py`/`routers/*.py`/`blueprints/__init__.py` (the
    collision-avoidance boundary for the parallel M1..M9 port wave, see
    `hub_api/PORTING.md`'s own "single new module" extension point).
    `services/overlay_service.py` calls this idempotently (matching the
    `if "<table>" in dal.tables: return` guard below) at the top of every
    entry point instead of relying on app startup -- safe under Quart's
    single-threaded-per-request-coroutine model since `define_table()`
    itself awaits nothing, so no two coroutines can interleave mid-call.
    A future group that gets an `app.py` edit in scope should fold this
    into `_bind_reference_tables()` the normal way and delete this note.
    """
    if "community_overlay_tokens" in dal.tables:
        return

    dal.define_table(
        "community_overlay_tokens",
        Field("community_id", "integer", notnull=True, unique=True),
        Field("overlay_key", "string", length=64, notnull=True, unique=True),
        Field("previous_key", "string", length=64),
        Field("is_active", "boolean", default=True),
        Field("theme_config", "json"),
        Field("enabled_sources", "json"),
        Field("last_accessed", "datetime"),
        Field("access_count", "integer", default=0),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("rotated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "overlay_access_log",
        Field("community_id", "integer", notnull=True),
        Field("overlay_key", "string", length=64),
        Field("ip_address", "string", length=45),
        Field("user_agent", "text"),
        Field("accessed_at", "datetime"),
        migrate=migrate,
    )


def bind_streaming_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the M7 Streaming group (music/stream/streaming) queries.

    Idempotent per-DAL-instance, `migrate=False` in production -- same
    contract as `bind_auth_tables()` above (see its own docstring and
    `hub_api/PORTING.md`'s Gotcha #2). Unlike M1, this function is never
    called from `app.py::_bind_reference_tables()` -- the M7 port's own
    scope explicitly forbids editing `app.py`/`routers/*.py`/
    `blueprints/__init__.py` (auto-discovery is the only wiring point for
    blueprints; table binding has no equivalent auto-discovery hook yet).
    Each M7 service module calls this itself, guarded by pydal's own
    `if "<table>" in dal.tables: return`-per-`define_table()` idempotency
    (`DAL.define_table()` no-ops on a name that's already bound against
    this `dal` instance) -- safe to call on every request; the real cost
    is paid exactly once per process.

    `community_roles` (migration 058_tenants_and_claims.sql) is relied on
    by `services.community_authz`'s community-scoped-role join
    (`middleware/auth.js`'s `requireMember`/`requireCommunityAdmin`,
    byte-faithfully ported: LEFT JOIN `community_members.community_role_id
    -> community_roles.id`, parse `claims_cache`/`base_claims` for the
    caller's scopes) but is NOT bound here -- `bind_auth_tables()` above
    (extended by the M2 Core Tenancy-Misc group) already binds it
    unconditionally at app startup, before this function's own lazy first
    call could ever run. `coordination` (also bound by `bind_platform_tables()`,
    identical field list either way) and `community_servers` (also bound
    by `bind_admin_tables()`, whose field list is a strict superset of the
    4 fields this group's own `stream_service.py` reads) are each
    individually guarded below rather than redefined, so whichever group's
    binding runs first in this shared, long-lived `dal` instance wins
    without error -- this function deliberately mirrors `bind_admin_tables()`'s
    fuller `community_servers` field list (not just this group's own
    narrower 4) so a streaming route hit before any admin route never
    leaves `community_servers` bound too narrowly for `bind_admin_tables()`
    to use later.

    This function's own top-level idempotency guard therefore checks
    `community_music_settings` (a table genuinely unique to this group),
    not `coordination`/`community_roles`/any other table another group
    also binds -- checking a shared table would short-circuit this
    function's own tables as a false-positive "already done" the moment
    that other group's binding landed first (caught the hard way during
    the M7-onto-release merge: this function originally guarded on
    `community_roles`, which `bind_auth_tables()` already binds
    unconditionally at app startup, so the guard skipped every table below
    -- including this function's own -- unconditionally, for every real
    request; `coordination`/`community_music_settings`/etc. never bound at
    all). `coordination`/`community_servers` are likewise real, pre-existing
    tables (`004_add_missing_tables.sql` / `000_create_base_schema.sql`)
    queried read-only by `stream_service.py`.

    Schema gap (`hub_api/PORTING.md` Gotcha #4's pattern, a larger
    instance of it): `musicController.js` queries five tables --
    `community_music_settings`, `community_music_providers`,
    `community_radio_stations`, `oauth_state_tokens`, `oauth_tokens` --
    that exist in NEITHER the numbered migrations NOR
    `config/postgres/init.sql`. `config/postgres/migrations/
    005_add_music_tables.sql` / `012_add_music_providers.sql` define a
    DIFFERENT, non-overlapping music schema (`music_settings`,
    `music_provider_config`, `music_radio_state`, `music_queue`, ...) that
    appears to supersede whatever `musicController.js` was originally
    written against -- and `admin/hub_module/backend/src/routes/music.js`
    (the only place these 8 controller functions are wired to routes) is
    itself never mounted in `routes/index.js`, so this entire code path is
    unreachable dead code in the Node app today, hit by nothing in
    production. Per Gotcha #4's rule ("document it, don't silently invent
    a column, don't silently drop the whole feature either") and the M7
    port's explicit instruction to port the EXISTING controller endpoints
    faithfully: these 5 tables are bound here with the exact columns
    Node's SQL references, `migrate=False` in production (byte-faithful --
    a real deployment 500s on first use exactly like Node's dead code
    would if it were ever wired up), `migrate=True` in tests (so the
    ported logic itself has real characterization coverage against
    sqlite). A follow-up ticket should either (a) write the missing
    migration for this schema, or (b) confirm with product that
    `music.py`'s settings/providers/radio-stations surface should be
    rebuilt against the real `music_provider_config`/`music_radio_state`
    schema instead -- out of scope for a byte-faithful controller port.
    """
    if "community_music_settings" in dal.tables:
        return

    # `coordination` is ALSO bound by `bind_platform_tables()` above (same
    # exact field list -- both groups ported it from the same real
    # `004_add_missing_tables.sql` table independently) and
    # `community_servers` by `bind_admin_tables()` below (a strict
    # superset of the 4 fields this group's own `stream_service.py` reads
    # -- `platform_server_name`/`link_type`/`is_primary`/`config`/
    # `added_by`/`approved_by`/`verified_at`/`created_at` on top of this
    # group's `community_id`/`platform`/`platform_server_id`/`status`).
    # Both guarded on existence here (not redefined) so whichever group's
    # binding runs first in this process wins -- for `coordination` that's
    # inconsequential (identical fields either way); for `community_servers`
    # this function intentionally uses `bind_admin_tables()`'s fuller field
    # list even though this group only reads the narrower 4, so a caller
    # that hits a streaming route before any admin route never ends up
    # with an admin-incompatible narrower table bound first (the exact
    # failure mode this docstring's `coordination`/`community_roles`
    # history above already describes for a same-name guard chosen wrong).
    if "coordination" not in dal.tables:
        dal.define_table(
            "coordination",
            Field("entity_id", "string", length=255, notnull=True),
            Field("platform", "string", length=50, notnull=True),
            Field("server_id", "string", length=255),
            Field("channel_id", "string", length=255),
            Field("channel_name", "string", length=255),
            Field("is_live", "boolean", default=False),
            Field("viewer_count", "integer", default=0),
            Field("live_since", "datetime"),
            Field("stream_title", "text"),
            Field("game_name", "string", length=255),
            Field("thumbnail_url", "text"),
            Field("last_updated", "datetime"),
            migrate=migrate,
        )

    if "community_servers" not in dal.tables:
        dal.define_table(
            "community_servers",
            Field("community_id", "integer", notnull=True),
            Field("platform", "string", length=50, notnull=True),
            Field("platform_server_id", "string", length=255, notnull=True),
            Field("platform_server_name", "string", length=255),
            Field("link_type", "string", length=50, default="standard"),
            Field("status", "string", length=50, default="pending"),
            Field("is_primary", "boolean", default=False),
            Field("config", "json"),
            Field("added_by", "integer"),
            Field("approved_by", "integer"),
            Field("verified_at", "datetime"),
            Field("created_at", "datetime"),
            migrate=migrate,
        )

    # --- Schema-gap tables (see module docstring above) ---------------

    dal.define_table(
        "community_music_settings",
        Field("community_id", "integer", notnull=True, unique=True),
        Field("default_provider", "string", length=50),
        Field("autoplay_enabled", "boolean", default=False),
        Field("volume_limit", "integer", default=100),
        Field("allowed_genres", "json"),
        Field("blocked_artists", "json"),
        Field("require_dj_approval", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "community_music_providers",
        Field("community_id", "integer", notnull=True),
        Field("provider_name", "string", length=50, notnull=True),
        Field("is_connected", "boolean", default=False),
        Field("is_active", "boolean", default=False),
        Field("oauth_expires_at", "datetime"),
        Field("last_sync", "datetime"),
        Field("config", "text"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "community_radio_stations",
        Field("community_id", "integer", notnull=True),
        Field("name", "string", length=255, notnull=True),
        Field("url", "string", length=2048, notnull=True),
        Field("description", "text"),
        Field("genre", "string", length=100),
        Field("is_active", "boolean", default=True),
        Field("created_by", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "oauth_state_tokens",
        Field("community_id", "integer", notnull=True),
        Field("provider", "string", length=50, notnull=True),
        Field("state_token", "string", length=255, notnull=True, unique=True),
        Field("redirect_uri", "text"),
        Field("expires_at", "datetime", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "oauth_tokens",
        Field("community_id", "integer", notnull=True),
        Field("provider", "string", length=50, notnull=True),
        migrate=migrate,
    )


def bind_community_authz_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define `community_roles` (`058_tenants_and_claims.sql`) for `services/community_authz.py`.

    `app.py` is frozen for this port (`hub_api/PORTING.md`'s per-group
    isolation note during the parallel M-phase port wave -- editing it
    would conflict with every other group's own worktree/PR) -- unlike
    `bind_auth_tables()`, which app.py's own `_bind_reference_tables()`
    calls unconditionally at startup, this is called LAZILY (idempotent,
    cheap: a `dal.define_table()` with no real DDL when `migrate=False`)
    from `community_authz.require_community_admin()` on first use rather
    than at app startup. Safe: `dal` is a single long-lived pydal
    instance for the app's lifetime, and every caller goes through this
    same idempotency guard before ever touching `dal.community_roles`.
    """
    if "community_roles" in dal.tables:
        return

    dal.define_table(
        "community_roles",
        Field("community_id", "integer", notnull=True),
        Field("name", "string", length=50, notnull=True),
        Field("display_name", "string", length=100),
        Field("description", "text"),
        Field("is_system", "boolean", default=False),
        Field("priority", "integer", default=0),
        # base_claims: JSONB `{"scopes": [...]}"` -- see requireCommunityAdmin's
        # `claims.scopes` read in middleware/auth.js.
        Field("base_claims", "json"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )


def bind_github_sync_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the tables `githubSyncService.js` owns/queries -- M-automation port group.

    `github_repo_connections`/`ticket_github_sync`/`github_sync_log` are
    real, migrated tables (`066_github_sync.sql`) -- fields bound below
    match that migration's columns exactly. `support_tickets`/
    `support_ticket_comments` are NOT (module docstring gap (5)) -- bound
    anyway to stay byte-faithful to Node; a query against them 500s
    exactly like Node's own code does against the real, gapped schema.

    Lazy/idempotent, same rationale as `bind_community_authz_tables()`
    above (app.py is frozen for this port).
    """
    if "github_repo_connections" in dal.tables:
        return

    dal.define_table(
        "github_repo_connections",
        Field("community_id", "integer"),
        Field("vendor_id", "integer"),
        Field("module_id", "integer"),
        Field("repo_owner", "string", length=255, notnull=True),
        Field("repo_name", "string", length=255, notnull=True),
        Field("sync_mode", "string", length=30, default="tickets_only"),
        Field("default_labels", "list:string"),
        Field("auto_close_on_github_close", "boolean", default=True),
        Field("auth_type", "string", length=20, notnull=True),
        Field("encrypted_token", "text", notnull=True),
        Field("webhook_secret", "string", length=255, notnull=True),
        Field("installation_id", "string", length=255),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "ticket_github_sync",
        Field("ticket_id", "integer", notnull=True),
        Field("github_repo_connection_id", "integer", notnull=True),
        Field("github_issue_number", "integer", notnull=True),
        Field("github_issue_node_id", "string", length=255),
        Field("sync_status", "string", length=30, default="synced"),
        Field("last_synced_at", "datetime"),
        Field("last_error", "text"),
        Field("retry_count", "integer", default=0),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "github_sync_log",
        Field("ticket_github_sync_id", "integer"),
        Field("direction", "string", length=10, notnull=True),
        Field("event_type", "string", length=50, notnull=True),
        Field("payload", "json"),
        Field("success", "boolean", notnull=True),
        Field("error_message", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    # --- gap (5): not in any numbered migration, bound byte-faithful to Node ---
    dal.define_table(
        "support_tickets",
        Field("subject", "string", length=255),
        Field("description", "text"),
        Field("status", "string", length=50),
        Field("priority", "string", length=50),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "support_ticket_comments",
        Field("ticket_id", "integer", notnull=True),
        Field("content", "text"),
        Field("author_name", "string", length=255),
        Field("is_internal", "boolean", default=False),
        Field("source", "string", length=50),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_privacy_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the Privacy/Compliance group (GDPR DSAR + cookie consent) queries.

    Idempotent per-DAL-instance, same guard pattern as `bind_auth_tables()`.
    `migrate=False` (default) matches production -- schema owned by
    `config/postgres/migrations/*.sql`; tests pass `migrate=True`.

    Deliberately NOT called from `app.py::_bind_reference_tables()` --
    this port PR (`blueprints/v1/data_privacy.py` +
    `blueprints/v1/cookie_consent.py`) is scoped to never touch
    `app.py`/`routers/*.py`/`blueprints/__init__.py` (shared files a
    parallel M-phase port wave would collide on). Instead,
    `blueprints/v1/data_privacy.py` and `blueprints/v1/cookie_consent.py`
    each call this from their own `before_request` hook -- a correctness-
    equivalent substitute given the idempotent guard below: the first
    request against either blueprint binds every table in this function,
    every request after that is a no-op check. A future PR that touches
    `app.py` for an unrelated reason should fold this call into
    `_bind_reference_tables()` alongside `bind_auth_tables()` and delete
    the two `before_request` hooks.

    Column provenance: `config/postgres/migrations/000_create_base_schema.sql`
    (cookie_policy_versions, cookie_consent, cookie_audit_log, hub_chat_messages),
    `044_add_activity_tables.sql` (activity_watch_sessions, activity_message_events),
    `062_data_deletion_requests.sql` (data_deletion_requests). Note
    `006_add_cookie_consent.sql` defines an OLDER, incompatible shape for
    the three cookie_* tables (different columns, e.g. `session_id`/
    `consent_timestamp` instead of `consent_id`/`consented_at`) -- it never
    runs in practice because `000_create_base_schema.sql`'s `CREATE TABLE
    IF NOT EXISTS` always applies first (numeric ordering), so the columns
    bound here match `000`'s definition, the one that actually wins in
    every real environment. `hub_users`/`hub_user_profiles`/
    `hub_user_identities`/`hub_sessions`/`user_passkeys` (also read by the
    DSAR export) are already bound by `bind_auth_tables()`, called first
    by every blueprint's `_ensure_tables()` hook.
    """
    if "cookie_consent" in dal.tables:
        return

    dal.define_table(
        "cookie_policy_versions",
        Field("version", "string", length=50, notnull=True, unique=True),
        Field("content", "text", notnull=True),
        Field("changes_summary", "text"),
        Field("is_active", "boolean", default=False),
        Field("effective_date", "datetime"),
        Field("created_by", "integer"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "cookie_consent",
        Field("user_id", "integer"),
        Field("consent_id", "string", length=255, notnull=True, unique=True),
        Field("preferences", "json"),
        Field("consent_version", "string", length=50, notnull=True),
        Field("consent_method", "string", length=50, default="banner"),
        Field("ip_address", "string", length=45),
        Field("user_agent", "text"),
        Field("consented_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("expires_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "cookie_audit_log",
        Field("consent_id", "string", length=255),
        Field("user_id", "integer"),
        Field("action", "string", length=50, notnull=True),
        Field("category", "string", length=50),
        Field("previous_value", "boolean"),
        Field("new_value", "boolean"),
        Field("consent_version", "string", length=50),
        Field("ip_address", "string", length=45),
        Field("user_agent", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "data_deletion_requests",
        Field("hub_user_id", "integer", notnull=True),
        Field("requested_at", "datetime", notnull=True),
        Field("completed_at", "datetime"),
        Field("status", "string", length=20, default="pending"),
        Field("deletion_scope", "json"),
        Field("error_detail", "text"),
        migrate=migrate,
    )

    # Read by the DSAR export (`data_privacy_service.py::collect_user_data`)
    # only -- schema-owned by the Engagement/Leaderboard group
    # (activityController.js), not this group. Bound here the same way
    # `bind_auth_tables()` binds `hub_settings` for a different group's
    # own reads: table BINDING tracks "who queries it", not "who owns the
    # schema".
    dal.define_table(
        "activity_message_events",
        Field("community_id", "integer", notnull=True),
        Field("hub_user_id", "integer"),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("platform_username", "string", length=255),
        Field("channel_id", "string", length=255),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "activity_watch_sessions",
        Field("community_id", "integer", notnull=True),
        Field("hub_user_id", "integer"),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("platform_username", "string", length=255),
        Field("channel_id", "string", length=255, notnull=True),
        Field("session_start", "datetime"),
        Field("session_end", "datetime"),
        Field("duration_seconds", "integer", default=0),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    # Read by the DSAR export only -- schema-owned by the Tenancy/
    # Community group (communityInteractionController.js et al), same
    # cross-group binding rationale as activity_* above.
    dal.define_table(
        "hub_chat_messages",
        Field("community_id", "integer", notnull=True),
        Field("channel_name", "string", length=255),
        Field("sender_hub_user_id", "integer"),
        Field("sender_platform", "string", length=50),
        Field("sender_username", "string", length=255),
        Field("sender_avatar_url", "text"),
        Field("message_content", "text", notnull=True),
        Field("message_type", "string", length=50, default="text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_platform_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the M3 Platform-admin/Public group queries.

    Calls `bind_auth_tables()` first (dependency: `communities`,
    `community_members`, `hub_settings`, `tenants` all live there, extended
    in place -- see that function's own field-list comments), then defines
    this group's own new tables. Idempotent per-DAL-instance, same guard
    pattern as `bind_auth_tables()`.

    Gap (5): `platformController.js`'s `getUsers()`/`getUser()`/
    `updateUserRole()`/`deactivateUser()` all query a `platform_admins`
    table that does not exist in ANY migration OR `init.sql` -- a step
    beyond gaps (1)-(4) above (those were missing *columns* on real
    tables; this is a missing *table*). Combined with `requirePlatformAdmin`
    checking `req.user.roles.includes('platform-admin')`, a claim
    `createSession()` (authController.js) never actually populates (only
    'admin'/'super_admin'/'vendor' are ever pushed), this makes
    `platformController.js`'s entire route group unreachable AND
    non-functional in Node today: every caller gets 403 (no JWT ever
    carries the role) and, even if that were bypassed, half the queries
    would 500 (table absent). This port's authz fix (see
    `blueprints/v1/platform.py`'s module docstring) grants access via the
    existing `*:admin` global-admin wildcard scope instead of the
    never-populated role string -- restoring the group to reachable AND
    functional, matching the evident product intent (Node's own
    `createSession()` pushes 'admin' for `is_super_admin`, strongly
    suggesting a global admin was always meant to satisfy this check).
    `platform_admins` is bound here (byte-faithful field guesses from the
    controller's own SELECT/INSERT usage) so the group is fully testable;
    real Postgres needs a migration to add the table before this is
    functional in a live environment -- tracked as a follow-up, not
    silently invented schema.

    Gap (6): `getModuleRegistry()` queries a `collector_modules` table,
    likewise absent from every migration and `init.sql`. Same treatment:
    bound here from the controller's own column usage, real-environment
    functionality blocked on a follow-up migration.
    """
    if "platform_admins" in dal.tables:
        return

    bind_auth_tables(dal, migrate=migrate)

    dal.define_table(
        "platform_admins",
        Field("user_id", "integer", notnull=True),
        Field("role", "string", length=50),
        Field("is_active", "boolean", default=True),
        Field("deactivated_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "collector_modules",
        Field("module_name", "string", length=255, notnull=True),
        Field("module_version", "string", length=50),
        Field("platform", "string", length=50),
        Field("endpoint_url", "text"),
        Field("status", "string", length=50),
        Field("last_heartbeat", "datetime"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "audit_log",
        Field("user_id", "integer"),
        Field("action", "string", length=100, notnull=True),
        Field("target_type", "string", length=50),
        Field("target_id", "string", length=255),
        Field("details", "json"),
        Field("ip_address", "string", length=45),
        Field("user_agent", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    # Also bound by `bind_streaming_tables()` above (identical field list --
    # both groups ported it from the same real `004_add_missing_tables.sql`
    # table independently); guarded on existence here (not redefined) so
    # whichever group's binding runs first in this shared, long-lived `dal`
    # instance wins without a duplicate-define error.
    if "coordination" not in dal.tables:
        dal.define_table(
            "coordination",
            Field("entity_id", "string", length=255, notnull=True),
            Field("platform", "string", length=50, notnull=True),
            Field("server_id", "string", length=255),
            Field("channel_id", "string", length=255),
            Field("channel_name", "string", length=255),
            Field("is_live", "boolean", default=False),
            Field("viewer_count", "integer", default=0),
            Field("live_since", "datetime"),
            Field("stream_title", "text"),
            Field("game_name", "string", length=255),
            Field("thumbnail_url", "text"),
            Field("last_updated", "datetime"),
            migrate=migrate,
        )

    dal.define_table(
        "hub_modules",
        Field("name", "string", length=255, notnull=True),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("version", "string", length=50),
        Field("author", "string", length=255),
        Field("category", "string", length=100),
        Field("icon_url", "text"),
        Field("is_published", "boolean", default=False),
        Field("is_core", "boolean", default=False),
        Field("config_schema", "json"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_module_reviews",
        Field("module_id", "integer"),
        Field("community_id", "integer"),
        Field("user_id", "integer"),
        Field("rating", "integer"),
        Field("review_text", "text"),
        Field("admin_notes", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_module_installations",
        Field("community_id", "integer", notnull=True),
        Field("module_id", "integer"),
        Field("installed_by", "integer"),
        Field("config", "json"),
        Field("is_enabled", "boolean", default=True),
        Field("installed_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "platform_integrations",
        Field("platform", "string", length=50, notnull=True),
        Field("integration_type", "string", length=20, notnull=True),
        Field("community_id", "integer"),
        Field("user_id", "integer"),
        Field("access_token", "text"),
        Field("refresh_token", "text"),
        Field("client_id", "string", length=255),
        Field("client_secret", "text"),
        Field("token_type", "string", length=50, default="Bearer"),
        Field("expires_at", "datetime"),
        Field("scopes", "list:string"),
        Field("config_data", "json"),
        Field("is_active", "boolean", default=True),
        Field("is_encrypted", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("created_by_user_id", "integer"),
        Field("updated_by_user_id", "integer"),
        migrate=migrate,
    )


def bind_admin_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the M3 Platform-admin group (admin/superadmin) queries.

    `app.py::_bind_reference_tables` is frozen (`routers/_discovery.py`'s
    auto-discovery contract: port agents never edit `app.py`/
    `routers/*.py`/`blueprints/__init__.py`), so -- matching the precedent
    the Community-module port (M6) already established in
    `services/community_common.py::ensure_community_tables` -- this group's
    tables are bound lazily, guarded per-table, from the top of every
    `admin_service`/`superadmin_service` function rather than at app
    startup. Idempotent: a second call on the same `dal` is a cheap no-op
    (`dal.tables` membership check).

    `migrate=False` (production default) matches every other group in this
    file: schema owned by `config/postgres/migrations/*.sql`, this process
    never runs DDL. Tests pass `migrate=True` against a throwaway sqlite
    file the same way `tests/conftest.py::auth_db` does.
    """
    if "hub_modules" not in dal.tables:
        dal.define_table(
            "hub_modules",
            Field("name", "string", length=255, notnull=True, unique=True),
            Field("display_name", "string", length=255),
            Field("description", "text"),
            Field("version", "string", length=50),
            Field("author", "string", length=255),
            Field("category", "string", length=100),
            Field("icon_url", "text"),
            Field("is_published", "boolean", default=False),
            Field("is_core", "boolean", default=False),
            Field("config_schema", "json"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "hub_module_installations" not in dal.tables:
        dal.define_table(
            "hub_module_installations",
            Field("community_id", "integer", notnull=True),
            Field("module_id", "integer"),
            Field("installed_by", "integer"),
            Field("config", "json"),
            Field("is_enabled", "boolean", default=True),
            Field("installed_at", "datetime"),
            Field("updated_at", "datetime"),
            # `module_name` is a pre-existing schema gap: adminController.js's
            # getCommands() LEFT JOINs `hmi.module_name = c.module_name`, but
            # no migration defines this column on hub_module_installations
            # (only module_id, an integer FK to hub_modules.id) -- same class
            # of gap as hub_users.email_verification_expires (see this
            # module's top-of-file docstring gap list). Bound here to stay
            # byte-faithful; a real query against it 500s against Postgres
            # today exactly like Node's does.
            Field("module_name", "string", length=255),
            migrate=migrate,
        )

    if "hub_module_reviews" not in dal.tables:
        dal.define_table(
            "hub_module_reviews",
            Field("module_id", "integer"),
            Field("community_id", "integer"),
            Field("user_id", "integer"),
            Field("rating", "integer"),
            Field("review_text", "text"),
            Field("admin_notes", "text"),
            Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "hub_admins" not in dal.tables:
        dal.define_table(
            "hub_admins",
            Field("username", "string", length=255, notnull=True, unique=True),
            Field("password_hash", "string", length=255, notnull=True),
            Field("email", "string", length=255),
            Field("is_active", "boolean", default=True),
            Field("is_super_admin", "boolean", default=False),
            Field("last_login", "datetime"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    # `modules`/`module_installations` (community-level module toggles,
    # 046_add_remaining_admin_tables.sql + 004_add_missing_tables.sql) are a
    # SEPARATE table family from `hub_modules`/`hub_module_installations`
    # (marketplace registry, 000_create_base_schema.sql) above -- not a
    # typo. adminController.js's getModules()/updateModuleConfig() query
    # this family; superadminController.js's marketplace registry queries
    # the `hub_*` family. Both are real, distinct tables in Node's actual
    # schema (confirmed against the migrations directly).
    if "modules" not in dal.tables:
        dal.define_table(
            "modules",
            Field("name", "string", length=100, notnull=True, unique=True),
            Field("display_name", "string", length=255),
            Field("description", "text"),
            Field("category", "string", length=50),
            Field("version", "string", length=20, default="1.0.0"),
            Field("is_active", "boolean", default=True),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "module_installations" not in dal.tables:
        dal.define_table(
            "module_installations",
            Field("community_id", "integer"),
            # VARCHAR(100) in Postgres -- getModules() joins
            # `modules m ON m.id::text = mi.module_id`, i.e. module_id is
            # the *text* representation of modules.id, not an integer FK.
            Field("module_id", "string", length=100, notnull=True),
            Field("is_enabled", "boolean", default=True),
            Field("config", "json"),
            Field("installed_at", "datetime"),
            Field("installed_by", "integer"),
            migrate=migrate,
        )

    if "community_servers" not in dal.tables:
        dal.define_table(
            "community_servers",
            Field("community_id", "integer", notnull=True),
            Field("platform", "string", length=50, notnull=True),
            Field("platform_server_id", "string", length=255, notnull=True),
            Field("platform_server_name", "string", length=255),
            Field("link_type", "string", length=50, default="standard"),
            Field("status", "string", length=50, default="pending"),
            Field("is_primary", "boolean", default=False),
            Field("config", "json"),
            Field("added_by", "integer"),
            Field("approved_by", "integer"),
            Field("verified_at", "datetime"),
            Field("created_at", "datetime"),
            migrate=migrate,
        )

    if "commands" not in dal.tables:
        dal.define_table(
            "commands",
            Field("command", "string", length=100, notnull=True),
            Field("module_name", "string", length=255, notnull=True),
            Field("description", "text"),
            Field("usage", "text"),
            Field("category", "string", length=100, default="general"),
            Field("permission_level", "string", length=50, default="everyone"),
            Field("cooldown_seconds", "integer", default=0),
            Field("community_id", "integer"),
            Field("is_enabled", "boolean", default=True),
            Field("is_active", "boolean", default=True),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            # `platforms` is a pre-existing schema gap: adminController.js's
            # getCommands() selects `c.platforms`, but 002_add_commands_table.sql
            # never defines it -- same class of gap as
            # hub_module_installations.module_name above. Bound here to stay
            # byte-faithful.
            Field("platforms", "json"),
            migrate=migrate,
        )

    if "community_roles" not in dal.tables:
        dal.define_table(
            "community_roles",
            Field("community_id", "integer", notnull=True),
            Field("name", "string", length=50, notnull=True),
            Field("display_name", "string", length=100),
            Field("description", "text"),
            Field("is_system", "boolean", default=False),
            Field("priority", "integer", default=0),
            Field("base_claims", "json"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )


def bind_superadmin_tenant_fields(dal: Any, *, migrate: bool = False) -> None:
    """Redefine `tenants` with the extra columns superadmin tenant CRUD needs.

    `app.py::_bind_reference_tables` (frozen) only binds the subset M1's
    auth chain needs (slug/display_name/logo_url/is_global/is_active/
    config). `superadminController.js`'s tenant management
    (listTenants/createTenant/updateTenant/deleteTenant) additionally
    needs `description`/`allowed_module_ids`/`seat_limit`/`created_at`/
    `updated_at` -- the full column set from
    `058_tenants_and_claims.sql`'s `CREATE TABLE tenants`. pydal forbids a
    second `define_table("tenants", ...)` call unless `redefine=True`,
    which replaces the table's field metadata in place (no DDL --
    `migrate=False` in production, so this never touches the real schema,
    only pydal's own Python-side Table object). Guarded by a field-presence
    check so this only runs once per DAL instance rather than on every
    request.
    """
    if "seat_limit" in dal.tenants.fields:
        return

    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        Field("allowed_module_ids", "list:integer"),
        Field("seat_limit", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )


def bind_tenant_tables(dal: Any, *, migrate: bool = False) -> None:
    """Extend `tenants`/`communities` + bind new tables for the M2 Core Tenant group.

    This group's task explicitly scopes `app.py`/`routers/*.py`/
    `blueprints/__init__.py` as never-edit (avoids a shared-file collision
    point across the parallel M1..M9 port wave -- the same rationale
    `app.py::_bind_reference_tables`'s own docstring gives for keeping
    that function's diff small). That means this group can't follow
    `hub_api/PORTING.md`'s literal "extend the ONE define_table("tenants",
    ...) call already in app.py" instruction the way M1 did -- `tenants`
    and `communities` are both already bound (by app.py's
    `_bind_reference_tables` and this module's own `bind_auth_tables`,
    respectively) by the time any request reaches a tenant-blueprint
    route.

    Instead, this function is called lazily (see `blueprints/v1/tenant.py`
    ::_dal()`) and uses pydal's `redefine=True` to ADD fields to those two
    already-bound `Table` objects, always paired with `migrate=migrate`
    (production is always `False`) so this is a pure in-memory Field-list
    update -- no DDL, same "schema owned by
    config/postgres/migrations/*.sql, never by this process" invariant
    `_bind_reference_tables` documents for `tenants` itself. Verified
    empirically: `redefine=True` REPLACES a table's field list wholesale
    (not merge-by-name), so both redefine calls below repeat every field
    the table's original definition already had -- dropping one here would
    silently break `flask_core.tenancy.resolve_tenant_context`'s
    `row.is_active` access (`tenants`) or `auth_service.
    add_user_to_global_community()`'s `dal.communities.name`/`.display_name`
    access (`communities`) for every request served after this function's
    first call. Safe under Quart's single-threaded event loop: `define_table`
    has no `await` inside it, so no concurrent request can observe a
    half-rebuilt `Table` object mid-call. Idempotent (`"tenant_settings" in
    dal.tables` guard) -- cheap to call on every request via `_dal()`.

    New columns, all real (not invented) per `config/postgres/migrations/
    058_tenants_and_claims.sql` (`tenants.description`/`allowed_module_ids`/
    `seat_limit`/`created_at`, `communities.tenant_id`) and `000_create_base_
    schema.sql`/`008_add_community_types.sql` (`communities.is_public`/
    `community_type`/`created_at`) -- `tenantController.js`'s `getTenant`/
    `updateTenant`/`getTenantModules`/`getTenantCommunities` all read them.

    `tenant_settings` (`058_tenants_and_claims.sql`) and `hub_modules`
    (`000_create_base_schema.sql`) are new tables, owned outright by this
    group -- bound the normal (non-redefine) way.
    """
    if "tenant_settings" in dal.tables:
        return

    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        Field("description", "text"),
        Field("allowed_module_ids", "list:integer"),
        Field("seat_limit", "integer"),
        Field("created_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )

    dal.define_table(
        "communities",
        Field("name", "string", length=255),
        Field("display_name", "string", length=255),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("member_count", "integer", default=0),
        Field("config", "json"),
        Field("tenant_id", "integer"),
        Field("is_public", "boolean", default=True),
        Field("community_type", "string", length=50, default="creator"),
        Field("created_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )

    dal.define_table(
        "tenant_settings",
        Field("tenant_id", "integer", notnull=True),
        Field("key", "string", length=100, notnull=True),
        Field("value", "text"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_modules",
        Field("name", "string", length=255),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("category", "string", length=100),
        Field("is_core", "boolean", default=False),
        Field("is_published", "boolean", default=False),
        Field("version", "string", length=50),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_marketplace_catalog_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the marketplace-catalog group's port queries.

    `app.py`/`routers/*.py`/`blueprints/__init__.py` are frozen (the
    parallel port wave's auto-discovery contract -- see
    `routers/_discovery.py`'s own docstring), so unlike `bind_auth_tables`
    this is never called from `app.py::_bind_reference_tables`. Instead it
    is idempotent (`dal.tables` membership guard, same idiom as
    `services/community_common.py::ensure_community_tables`) and called at
    the top of every `services/marketplace_catalog_service.py` function,
    matching the pattern the Community-module port (M6) already
    established once `app.py` stopped being editable.

    `marketplace_catalog` is a read-only Postgres VIEW
    (`config/postgres/migrations/059_marketplace_consolidation.sql`,
    unions `hub_modules` + approved `marketplace_modules`), not a table --
    pydal has no notion of "view" so it is bound as an ordinary table this
    process only ever `select()`s (`migrate=True` in tests creates a real
    throwaway TABLE with the same columns instead of a view; the test
    fixture seeds rows directly rather than replicating the view's
    Postgres-side UNION/aggregation, matching this repo's established
    "one field definition, tests seed it directly" convention -- see
    `tests/conftest.py::auth_db`). No single-column primary key exists on
    the view, so `primarykey=["source", "source_id"]` (the view's own
    natural composite key) is used instead, the same `primarykey=`
    pattern `flask_core.app_bundle_tables.init_app_bundle_tables` uses for
    `app_catalog`.

    `hub_modules.is_featured` -- see this module's own docstring gap (4)
    -- and `marketplace_modules` itself are NOT queried directly by
    either ported controller (`catalogController.js` only ever queries
    the `marketplace_catalog` view; `moduleController.js` only ever
    queries `hub_modules`/`hub_module_reviews`/`hub_module_installations`)
    -- `marketplace_modules` is therefore intentionally not bound here;
    a future group porting vendor self-service
    (`vendorController.js`) binds it then.
    """
    if "marketplace_catalog" in dal.tables:
        return

    dal.define_table(
        "marketplace_catalog",
        Field("source", "string", length=20, notnull=True),
        Field("source_id", "integer", notnull=True),
        Field("name", "string", length=255),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("category", "string", length=100),
        Field("icon_url", "text"),
        Field("is_core", "boolean", default=False),
        Field("pricing_type", "string", length=50, default="free"),
        Field("price_cents", "integer", default=0),
        Field("pricing_model", "string", length=50, default="flat"),
        Field("version", "string", length=50),
        Field("author", "string", length=255),
        Field("webhook_url", "text"),
        Field("communication_model", "string", length=50),
        Field("integration_type", "string", length=50),
        Field("avg_rating", "double", default=0),
        Field("review_count", "integer", default=0),
        Field("install_count", "integer", default=0),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        # NULL for 'core' rows (always globally visible); the owning
        # tenant for 'marketplace' rows (backfilled to the global tenant
        # by 059's own migration when unset). Filtered by
        # `visible_tenant_ids()` -- see marketplace_catalog_service.py's
        # module docstring for the cross-tenant-leak fix this closes.
        Field("tenant_id", "integer"),
        primarykey=["source", "source_id"],
        migrate=migrate,
    )

    dal.define_table(
        "hub_modules",
        Field("name", "string", length=255, notnull=True, unique=True),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("version", "string", length=50),
        Field("author", "string", length=255),
        Field("category", "string", length=100),
        Field("icon_url", "text"),
        Field("is_published", "boolean", default=False),
        Field("is_core", "boolean", default=False),
        # See module docstring gap (4) -- no numbered migration defines
        # this column on hub_modules; bound anyway to stay byte-faithful
        # to moduleService.js's create/update/format queries.
        Field("is_featured", "boolean", default=False),
        Field("config_schema", "json", default={}),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_module_reviews",
        Field("module_id", "integer", notnull=True),
        Field("community_id", "integer"),
        Field("user_id", "integer"),
        Field("rating", "integer"),
        Field("review_text", "text"),
        Field("admin_notes", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "hub_module_installations",
        Field("community_id", "integer", notnull=True),
        Field("module_id", "integer"),
        Field("installed_by", "integer"),
        Field("config", "json", default={}),
        Field("is_enabled", "boolean", default=True),
        Field("installed_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    # Minimal projection -- schema-owned by
    # `059_marketplace_consolidation.sql`'s extension of
    # `017_add_marketplace.sql`'s original table; only the columns the
    # catalog's install-status enrichment needs.
    dal.define_table(
        "marketplace_subscriptions",
        Field("community_id", "integer", notnull=True),
        Field("module_id", "integer", notnull=True),
        Field("is_enabled", "boolean", default=True),
        Field("tenant_id", "integer"),
        migrate=migrate,
    )


def bind_marketplace_vendor_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the Marketplace-vendor port group queries.

    `app.py` is frozen (see `services/community_common.py`'s module
    docstring for the full rationale -- port agents never edit
    `app.py`/`routers/*.py`/`blueprints/__init__.py`), so this is NOT
    wired into `app.py::_bind_reference_tables`. Instead every
    `marketplace_vendor`/`marketplace_admin_review` blueprint handler
    calls this idempotently first (`"marketplace_sellers" not in
    dal.tables` guard), mirroring `services/bot_tables.py::bind_bot_tables`'s
    call-from-the-blueprint-not-app.py shape -- `blueprints/v1/bot.py` is
    the precedent to follow, not `bind_auth_tables` (M1 is the one group
    old enough to predate the "app.py is frozen" constraint).

    Idempotent per-DAL-instance. `migrate=False` (production default):
    schema owned by `config/postgres/migrations/017_add_marketplace.sql`,
    `021_add_vendor_submissions.sql`, `023_add_vendor_requests.sql`,
    `059_marketplace_consolidation.sql`, `064_vendor_discount_codes.sql`.
    Tests pass `migrate=True` against a throwaway sqlite file.

    Schema-drift gaps (Gotcha #4 pattern -- pre-existing in Node, not
    introduced by this port; documented here rather than silently
    invented away or silently dropped):
      1. `commands.module_url` is referenced by
         `routerIntegrationController.js`/`commandRegistrationService.js`
         but is not defined by `002_add_commands_table.sql` or any later
         migration. Bound anyway to stay byte-faithful to Node -- a real
         Postgres deployment 500s on this exact query today, same as
         Node's raw SQL would.
      2. `vendorAnalyticsService.js` queries `community_vendor_installations`
         columns (`module_id`, `status`, `uninstalled_at`, `last_active_at`,
         `discount_code_id`) that don't exist on the table
         `021_add_vendor_submissions.sql` actually creates (which has
         `vendor_module_id`, no status/uninstalled_at/last_active_at/
         discount_code_id). Bound here using Node's EXPECTED shape
         (byte-faithful porting target), not the migration's real shape --
         same gap, not introduced by this port.
      3. `vendor_payments` similarly: Node's analytics queries expect
         `seller_id`, `module_id`, `amount_cents`, `status`, `paid_at`;
         the real migration defines `submission_id`, `gross_amount`,
         `net_amount`, `payment_status`, no `paid_at`. Same treatment.
      4. `vendor_discount_codes.module_id` REFERENCES
         `approved_vendor_modules(id)` per migration 064, but
         `vendorAnalyticsService.js`'s `getDiscountCodePerformance` joins
         it against `marketplace_modules` instead -- a pre-existing Node
         logic gap (wrong join target), ported byte-faithfully rather
         than silently "corrected" to the migration's real FK target.
    """
    if "marketplace_sellers" in dal.tables:
        return

    # hub_users: M1's bind_auth_tables() always runs first in production
    # (app.py::_bind_reference_tables is unconditional at startup); this
    # minimal stand-in only matters for a test DAL that binds this group
    # in isolation -- same "define if missing, never redefine" guard
    # bot_tables.py uses for the same reason.
    if "hub_users" not in dal.tables:
        dal.define_table(
            "hub_users",
            Field("username", "string", length=255),
            Field("email", "string", length=255),
            Field("display_name", "string", length=255),
            Field("is_super_admin", "boolean", default=False),
            Field("is_vendor", "boolean", default=False),
            migrate=migrate,
        )

    if "communities" not in dal.tables:
        dal.define_table(
            "communities",
            Field("name", "string", length=255),
            Field("tenant_id", "integer"),
            migrate=migrate,
        )

    dal.define_table(
        "marketplace_sellers",
        Field("user_id", "integer", notnull=True, unique=True),
        Field("tenant_id", "integer"),
        Field("display_name", "string", length=255),
        Field("description", "text"),
        Field("website_url", "string", length=500),
        Field("payout_method", "string", length=50),
        Field("payout_account_id", "string", length=255),
        Field("total_revenue_cents", "integer", default=0),
        Field("total_subscribers", "integer", default=0),
        Field("is_verified", "boolean", default=False),
        Field("verified_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "marketplace_modules",
        Field("seller_id", "integer"),
        Field("tenant_id", "integer"),
        Field("name", "string", length=255, notnull=True),
        Field("slug", "string", length=255, notnull=True, unique=True),
        Field("description", "text"),
        Field("category", "string", length=100),
        Field("developer_user_id", "integer"),
        Field("documentation_url", "string", length=500),
        Field("support_url", "string", length=500),
        Field("icon_url", "string", length=500),
        Field("webhook_url", "string", length=500, notnull=True),
        Field("webhook_secret", "string", length=255, notnull=True),
        Field("webhook_timeout_ms", "integer", default=5000),
        Field("trigger_commands", "list:string"),
        Field("trigger_events", "list:string"),
        Field("requested_scopes", "list:string"),
        Field("response_types", "list:string"),
        Field("pricing_type", "string", length=50, default="free"),
        Field("pricing_model", "string", length=50, default="flat"),
        Field("price_cents", "integer", default=0),
        Field("min_seats", "integer", default=1),
        Field("billing_period", "string", length=20, default="monthly"),
        Field("currency", "string", length=10, default="USD"),
        Field("api_base_url", "string", length=500),
        Field("auth_type", "string", length=50, default="hmac"),
        Field("auth_config", "json"),
        Field("communication_model", "string", length=50, default="webhook_push"),
        Field("integration_type", "string", length=50, default="command_handler"),
        Field("status", "string", length=50, default="pending"),
        Field("approved_by", "integer"),
        Field("approved_at", "datetime"),
        Field("rejection_reason", "text"),
        Field("install_count", "integer", default=0),
        Field("total_requests", "integer", default=0),
        Field("failed_requests", "integer", default=0),
        Field("version", "string", length=50, default="1.0.0"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("deleted_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "marketplace_submissions",
        Field("module_id", "integer", notnull=True),
        Field("version", "string", length=50),
        Field("changes_description", "text"),
        Field("submitted_by", "integer"),
        Field("submitted_at", "datetime"),
        Field("status", "string", length=50, default="pending"),
        Field("reviewed_by", "integer"),
        Field("reviewed_at", "datetime"),
        Field("review_notes", "text"),
        migrate=migrate,
    )

    dal.define_table(
        "marketplace_settings",
        Field("setting_key", "string", length=100, notnull=True, unique=True),
        Field("setting_value", "text"),
        Field("updated_by", "integer"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_role_requests",
        Field("request_id", "string", length=36, unique=True),
        Field("user_id", "integer", notnull=True),
        Field("user_email", "string", length=255),
        Field("user_display_name", "string", length=255),
        Field("company_name", "string", length=255, notnull=True),
        Field("company_website", "string", length=500),
        Field("business_description", "text", notnull=True),
        Field("experience_summary", "text"),
        Field("contact_email", "string", length=255, notnull=True),
        Field("contact_phone", "string", length=20),
        Field("status", "string", length=50, default="pending"),
        Field("rejection_reason", "text"),
        Field("reviewed_by", "integer"),
        Field("admin_notes", "text"),
        Field("requested_at", "datetime"),
        Field("reviewed_at", "datetime"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_submissions",
        Field("submission_id", "string", length=36, unique=True),
        Field("tenant_id", "integer"),
        Field("vendor_name", "string", length=255, notnull=True),
        Field("vendor_email", "string", length=255, notnull=True),
        Field("company_name", "string", length=255),
        Field("contact_phone", "string", length=20),
        Field("website_url", "string", length=500),
        Field("module_name", "string", length=255, notnull=True),
        Field("module_description", "text"),
        Field("module_category", "string", length=100, default="interactive"),
        Field("module_version", "string", length=50),
        Field("repository_url", "string", length=500),
        Field("webhook_url", "string", length=500, notnull=True),
        Field("webhook_secret", "string", length=255),
        Field("webhook_per_community", "boolean", default=False),
        Field("scopes", "json"),
        Field("scope_justification", "text"),
        Field("pricing_model", "string", length=50, notnull=True),
        Field("pricing_amount", "double", default=0),
        Field("pricing_currency", "string", length=3, default="USD"),
        Field("payment_method", "string", length=50, notnull=True),
        Field("payment_details", "json"),
        Field("status", "string", length=50, default="pending"),
        Field("rejection_reason", "text"),
        Field("admin_notes", "text"),
        Field("supported_platforms", "json"),
        Field("documentation_url", "string", length=500),
        Field("support_email", "string", length=255),
        Field("support_contact_url", "string", length=500),
        Field("submitted_at", "datetime"),
        Field("reviewed_at", "datetime"),
        Field("reviewed_by", "integer"),
        Field("is_verified", "boolean", default=False),
        Field("requires_special_review", "boolean", default=False),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_submission_scopes",
        Field("submission_id", "integer", notnull=True),
        Field("scope_name", "string", length=100, notnull=True),
        Field("risk_level", "string", length=50, default="low"),
        Field("description", "text"),
        Field("data_shared", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_submission_reviews",
        Field("submission_id", "integer", notnull=True),
        Field("reviewer_id", "integer"),
        Field("action", "string", length=50, notnull=True),
        Field("comments", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "approved_vendor_modules",
        Field("submission_id", "integer", notnull=True),
        Field("vendor_name", "string", length=255, notnull=True),
        Field("module_name", "string", length=255, notnull=True),
        Field("module_slug", "string", length=255, unique=True, notnull=True),
        Field("webhook_url", "string", length=500, notnull=True),
        Field("webhook_secret", "string", length=255),
        Field("webhook_per_community", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("suspension_reason", "text"),
        Field("suspended_at", "datetime"),
        Field("is_featured", "boolean", default=False),
        Field("feature_position", "integer"),
        Field("install_count", "integer", default=0),
        Field("rating", "double"),
        Field("review_count", "integer", default=0),
        Field("published_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    # Node-expected shape (Gotcha #4 note (2) above) -- NOT the shape
    # `021_add_vendor_submissions.sql` actually creates.
    dal.define_table(
        "community_vendor_installations",
        Field("community_id", "integer", notnull=True),
        Field("module_id", "integer", notnull=True),
        Field("status", "string", length=50, default="active"),
        Field("discount_code_id", "integer"),
        Field("installed_at", "datetime"),
        Field("uninstalled_at", "datetime"),
        Field("last_active_at", "datetime"),
        migrate=migrate,
    )

    # Node-expected shape (Gotcha #4 note (3) above) -- NOT the shape
    # `021_add_vendor_submissions.sql` actually creates.
    dal.define_table(
        "vendor_payments",
        Field("seller_id", "integer"),
        Field("module_id", "integer"),
        Field("amount_cents", "integer", default=0),
        Field("status", "string", length=50, default="pending"),
        Field("paid_at", "datetime"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_module_reviews",
        Field("vendor_module_id", "integer", notnull=True),
        Field("community_id", "integer", notnull=True),
        Field("reviewer_id", "integer"),
        Field("rating", "integer", notnull=True),
        Field("review_text", "text"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "vendor_discount_codes",
        Field("code", "string", length=50, notnull=True),
        Field("vendor_id", "integer", notnull=True),
        Field("module_id", "integer"),
        Field("discount_type", "string", length=20, notnull=True),
        Field("discount_value", "double", default=0),
        Field("max_uses", "integer"),
        Field("current_uses", "integer", default=0),
        Field("valid_from", "datetime"),
        Field("valid_until", "datetime"),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "discount_code_redemptions",
        Field("discount_code_id", "integer", notnull=True),
        Field("community_id", "integer", notnull=True),
        Field("discount_amount_cents", "integer", notnull=True),
        Field("redeemed_at", "datetime"),
        migrate=migrate,
    )

    # Gotcha #4 note (1) above -- `module_url` is not defined by
    # `002_add_commands_table.sql` or any later migration.
    dal.define_table(
        "commands",
        Field("command", "string", length=100, notnull=True),
        Field("module_name", "string", length=255, notnull=True),
        Field("module_url", "string", length=500),
        Field("description", "text"),
        Field("usage", "text"),
        Field("category", "string", length=100, default="general"),
        Field("permission_level", "string", length=50, default="everyone"),
        Field("cooldown_seconds", "integer", default=0),
        Field("community_id", "integer"),
        Field("is_enabled", "boolean", default=True),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )


def bind_marketplace_billing_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the M4 Marketplace Billing group (subs/payments/premium/discount) queries.

    Idempotent per-table (mirrors `community_common.py::ensure_community_tables`,
    not `bind_auth_tables`'s single-table early-return) -- this group shares
    `communities`/`hub_users`/`community_members` with the Core/Community
    groups, and `bind_auth_tables()` (called first, itself idempotent) is
    the one that actually defines them with the `role` string column this
    group's `is_community_admin()` check needs (`community_common.py`'s own
    `community_members` binding omits `role` -- it never needed it). Calling
    `bind_auth_tables()` here guarantees that column exists regardless of
    which group's blueprint handles the first request in a given process.

    Schema provenance: `config/postgres/migrations/017_add_marketplace.sql`
    (marketplace_subscriptions, marketplace_payments, marketplace_settings),
    `059_marketplace_consolidation.sql` (tenant_id columns,
    community_premium_subscriptions), `000_create_base_schema.sql`
    (hub_modules, hub_module_installations), `064_vendor_discount_codes.sql`
    (vendor_discount_codes, discount_code_redemptions).

    Gap fixed during this port (not a pre-existing-gap note like
    `bind_auth_tables`'s three -- this one changes behavior, see
    `hub_api/PORTING.md` Gotcha #4 pattern): Node's own
    `discountCodeService.js` queries tables named `marketplace_discount_codes`/
    `marketplace_discount_code_redemptions`, which do not exist in any
    numbered migration -- the real tables (064) are `vendor_discount_codes`/
    `discount_code_redemptions`, with a different column set (`vendor_id`
    directly on `hub_users`, no `marketplace_sellers` indirection;
    `discount_type` CHECK is `percentage`/`fixed_amount`/`free`, not
    `percentage`/`fixed_cents`; `discount_amount_cents`, not
    `savings_cents`). Node's code would 500 against the real schema; this
    port binds and queries the REAL tables instead of faithfully
    reproducing a query against tables that don't exist.
    """
    bind_auth_tables(dal, migrate=migrate)

    if "hub_modules" not in dal.tables:
        dal.define_table(
            "hub_modules",
            Field("name", "string", length=255, notnull=True, unique=True),
            Field("display_name", "string", length=255),
            Field("description", "text"),
            Field("version", "string", length=50),
            Field("author", "string", length=255),
            Field("category", "string", length=100),
            Field("icon_url", "text"),
            Field("is_published", "boolean", default=False),
            Field("is_core", "boolean", default=False),
            migrate=migrate,
        )

    if "hub_module_installations" not in dal.tables:
        dal.define_table(
            "hub_module_installations",
            Field("community_id", "integer", notnull=True),
            Field("module_id", "integer"),
            Field("installed_by", "integer"),
            Field("config", "json"),
            Field("is_enabled", "boolean", default=True),
            Field("installed_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "marketplace_subscriptions" not in dal.tables:
        dal.define_table(
            "marketplace_subscriptions",
            Field("community_id", "integer", notnull=True),
            Field("module_id", "integer", notnull=True),
            Field("tenant_id", "integer"),
            Field("status", "string", length=50, default="active"),
            Field("is_enabled", "boolean", default=True),
            Field("stripe_subscription_id", "string", length=255),
            Field("paypal_subscription_id", "string", length=255),
            Field("pricing_model", "string", length=50, default="flat"),
            Field("current_seat_count", "integer"),
            Field("last_seat_update", "datetime"),
            Field("current_period_start", "datetime"),
            Field("current_period_end", "datetime"),
            Field("cancel_at_period_end", "boolean", default=False),
            Field("subscribed_at", "datetime"),
            Field("canceled_at", "datetime"),
            migrate=migrate,
        )

    if "marketplace_payments" not in dal.tables:
        dal.define_table(
            "marketplace_payments",
            Field("subscription_id", "integer"),
            Field("community_id", "integer", notnull=True),
            Field("module_id", "integer"),
            Field("tenant_id", "integer"),
            # `external_payment_id` is this group's idempotency key -- see
            # `services/marketplace_webhook_service.py`'s module docstring.
            # No DB-level UNIQUE constraint (no new migration in this PR,
            # matching the established "no schema changes" port convention
            # -- see `hub_api/PORTING.md` Gotcha #4) -- enforced at the
            # application layer via a SELECT-before-INSERT check instead.
            Field("payment_provider", "string", length=50, notnull=True),
            Field("external_payment_id", "string", length=255),
            Field("amount_cents", "integer", notnull=True),
            Field("currency", "string", length=10, default="USD"),
            Field("status", "string", length=50, notnull=True),
            Field("platform_fee_cents", "integer", default=0),
            Field("developer_amount_cents", "integer", default=0),
            Field("created_at", "datetime"),
            Field("metadata", "json"),
            migrate=migrate,
        )

    if "community_premium_subscriptions" not in dal.tables:
        dal.define_table(
            "community_premium_subscriptions",
            Field("community_id", "integer", notnull=True, unique=True),
            Field("tenant_id", "integer"),
            Field("status", "string", length=50, default="active"),
            Field("stripe_subscription_id", "string", length=255),
            Field("paypal_subscription_id", "string", length=255),
            Field("current_seat_count", "integer", default=0),
            Field("base_price_cents", "integer", notnull=True, default=500),
            Field("overage_price_cents", "integer", notnull=True, default=10),
            Field("base_seat_limit", "integer", notnull=True, default=50),
            Field("current_period_start", "datetime"),
            Field("current_period_end", "datetime"),
            Field("cancel_at_period_end", "boolean", default=False),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "marketplace_settings" not in dal.tables:
        dal.define_table(
            "marketplace_settings",
            Field("setting_key", "string", length=100, notnull=True, unique=True),
            Field("setting_value", "text"),
            Field("updated_by", "integer"),
            Field("updated_at", "datetime"),
            migrate=migrate,
        )

    if "vendor_discount_codes" not in dal.tables:
        dal.define_table(
            "vendor_discount_codes",
            Field("code", "string", length=50, notnull=True),
            Field("vendor_id", "integer", notnull=True),
            Field("module_id", "integer"),
            Field("discount_type", "string", length=20, notnull=True),
            Field("discount_value", "decimal(10,2)", default=0),
            Field("max_uses", "integer"),
            Field("current_uses", "integer", notnull=True, default=0),
            Field("usage_window_days", "integer"),
            Field("application_months", "integer"),
            Field("valid_from", "datetime", notnull=True),
            Field("valid_until", "datetime"),
            Field("is_active", "boolean", notnull=True, default=True),
            Field("description", "text"),
            Field("created_at", "datetime", notnull=True),
            Field("updated_at", "datetime", notnull=True),
            migrate=migrate,
        )

    if "discount_code_redemptions" not in dal.tables:
        dal.define_table(
            "discount_code_redemptions",
            Field("discount_code_id", "integer", notnull=True),
            Field("community_id", "integer", notnull=True),
            Field("subscription_id", "integer"),
            Field("original_price_cents", "integer", notnull=True),
            Field("discounted_price_cents", "integer", notnull=True),
            Field("discount_amount_cents", "integer", notnull=True),
            Field("redeemed_at", "datetime", notnull=True),
            Field("expires_at", "datetime"),
            migrate=migrate,
        )


def bind_lifecycle_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define `app_catalog` / `app_tenant_availability` / `app_activations` (App Bundle 3-tier).

    Schema owned by `config/postgres/migrations/069_app_bundle_tiers.sql`
    (+ `070_app_bundle_catalog_name.sql`'s `app_catalog.name` column,
    `071_app_catalog_stages.sql`'s `app_catalog.stages` column -- see
    below) -- `migrate=False` always in production, same "this process
    never issues DDL" invariant every `bind_*_tables()` function in this
    file documents (see `bind_auth_tables()`'s own docstring). `migrate=True`
    is test-only (`hub_api/PORTING.md` Gotcha #2): pydal never issues
    `CREATE TABLE` DDL against the throwaway sqlite file otherwise.

    `libs/flask_core/flask_core/app_bundle_tables.py::init_app_bundle_tables`
    already defines these same three tables, but hardcodes `migrate=False`
    unconditionally -- unusable for this group's own test fixtures, which
    need `migrate=True` against a file-backed sqlite DB (Gotcha #2). This
    function is a parallel, hub-api-owned definition (not a wrapper around
    that one) so `migrate` can thread through normally; field lists are
    kept in lockstep with `app_bundle_tables.py`'s own (and with migration
    069/070/071's columns) -- verify both if either ever drifts.

    Plain `"integer"`/`"string"` FK-shaped columns (`tenant_id`,
    `community_id`, `app_id` cross-references), not pydal `"reference ..."`
    fields -- matches every other `bind_*_tables()` call in this file
    (e.g. `oauth_state_tokens.community_id` above); the subset invariant
    those FKs imply (`activated <= available <= installed`) is enforced at
    the application layer at write time
    (`flask_core.app_installations_db.check_availability_insert_allowed`/
    `check_activation_insert_allowed`), matching migration 069's own
    top-of-file comment that this is deliberately not a SQL-level
    constraint spanning three tables.

    Called once, unconditionally, at the END of
    `app.py::_bind_reference_tables` (append-only per this port's task
    scope) -- idempotency guard below makes a second call (e.g. from a
    test fixture that also wants `migrate=True`) a cheap no-op check
    rather than a `pydal` "table already defined" error. This is also the
    ONLY `app_catalog`/`app_tenant_availability`/`app_activations`
    definition that ever actually runs in production: this function
    registers these three tables before any request is served, so
    `blueprints/v1/distribution.py`'s own lazy `bind_app_bundle_tables()`
    call (Distribution-API port group, `services/distribution_service.py`)
    always hits ITS OWN identical idempotency guard and no-ops -- the two
    functions independently defined the same three tables (parallel port
    tasks, same precedent `bind_admin_tables`'s `community_servers` vs
    `bind_streaming_tables`'s own copy already sets in this file), so
    `stages` (needed only by the Distribution API's own code) is added
    HERE, not left in the shadowed definition, so it's actually present in
    production. `bind_app_bundle_tables()`'s own field list below is kept
    in lockstep with this one (same plain-FK types, same lengths) so a
    test fixture calling it directly (`tests/conftest.py`) builds the
    identical schema this function does -- no split-brain between the two
    entry points.
    """
    if "app_catalog" in dal.tables:
        return

    dal.define_table(
        "app_catalog",
        Field("app_id", "string", length=255, notnull=True),
        Field("name", "string", length=255),
        Field("manifest_version", "string", length=50, notnull=True),
        Field("module", "string", length=100, notnull=True),
        Field("feature", "string", length=150, notnull=True),
        Field("provider", "string", length=50, notnull=True),
        Field("execution_model", "string", length=50, notnull=True),
        Field("is_default", "boolean", default=False),
        Field("compatible_with", "list:string"),
        Field("incompatible_with", "list:string"),
        Field("platform_compatibility", "json", notnull=True),
        Field("status", "string", length=20, default="active"),
        Field("installed_at", "datetime"),
        # migration 071 -- {"ingest": {"entrypoint", "config", "spec"}, ...},
        # keyed by stage name. Added by the Distribution-API port group
        # (`bind_app_bundle_tables()` below) -- see this function's own
        # docstring for why it lives here, not there. default={} so a
        # pre-071 row (or a row written before this column existed) reads
        # back as an empty dict, never None.
        Field("stages", "json", default={}),
        # migration 0026 -- attribution/marketplace metadata (bundle.yaml's
        # optional `author`/`license`/`source_url`/`alternative_to`/
        # `homepage_url`/`notice`/`category` block, see `hub_api/services/
        # bundle_manifest_v2.py` and `flask_core.app_manifest`). All
        # nullable -- a pre-0026 row (or any first-party `builtin` bundle
        # that never declares the block) reads back as `None`/`[]`.
        # `license_review_required` is derived (`flask_core.
        # bundle_attribution.license_requires_review`), never client-set.
        Field("author", "string", length=255),
        Field("license", "string", length=50),
        Field("license_review_required", "boolean", default=False),
        Field("source_url", "text"),
        Field("alternative_to", "list:string", default=[]),
        Field("homepage_url", "text"),
        Field("notice", "text"),
        Field("category", "string", length=50),
        primarykey=["app_id"],
        migrate=migrate,
    )

    dal.define_table(
        "app_tenant_availability",
        Field("tenant_id", "integer", notnull=True),
        Field("app_id", "string", length=255, notnull=True),
        Field("available", "boolean", default=True),
        Field("config_defaults", "json"),
        migrate=migrate,
    )

    dal.define_table(
        "app_activations",
        Field("community_id", "integer", notnull=True),
        Field("tenant_id", "integer", notnull=True),
        Field("app_id", "string", length=255, notnull=True),
        Field("enabled", "boolean", default=True),
        Field("config", "json"),
        Field("activated_by", "integer"),
        Field("activated_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )


def bind_app_bundle_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define `app_catalog`/`app_tenant_availability`/`app_activations` for the distribution API.

    `app.py` is frozen for this port (same rationale as `bind_community_authz_tables`/
    `bind_privacy_tables` above) -- called lazily, idempotent, from
    `blueprints/v1/distribution.py`'s own request path rather than at app
    startup. Depends on `tenants`/`communities` already being bound on
    `dal` -- both are unconditionally bound by `app.py::_bind_reference_tables`
    before any request is served, so this ordering is always satisfied in
    production; test fixtures bind them via `bind_auth_tables()` first.

    SHADOWED IN PRODUCTION by `bind_lifecycle_tables()` above: that
    function is called unconditionally at the END of
    `app.py::_bind_reference_tables`, so `app_catalog` already exists on
    `dal` by the time any request handler runs -- this function's own
    `if "app_catalog" in dal.tables: return` guard then no-ops every time
    in production. Field lists below are kept in lockstep with
    `bind_lifecycle_tables()`'s own (same plain `"integer"`/`"string"`
    FK-shaped columns, not pydal `"reference ..."` fields, same lengths)
    -- this function exists so `tests/conftest.py` fixtures that build an
    isolated `dal` and call this one directly (without first calling
    `bind_lifecycle_tables()`) still get the identical schema production
    actually uses, `stages` included. Two independently-callable entry
    points defining the same three tables has precedent in this exact file
    (see `bind_admin_tables`'s `community_servers` vs
    `bind_streaming_tables`'s own copy) -- `bind_lifecycle_tables()` is the
    one that matters at runtime; this one exists for lazy/test call sites
    and must never drift from it.

    `stages` (migration 071): per-stage `{entrypoint, config, spec}` JSON,
    keyed by `ingest`/`process`/`action` -- see that migration's own
    docstring. `default={}` so a pre-071 row (or a row a future writer
    inserts without stage data) reads back as an empty dict, never `None`.
    """
    if "app_catalog" in dal.tables:
        return

    dal.define_table(
        "app_catalog",
        Field("app_id", "string", length=255, notnull=True),
        Field("name", "string", length=255),
        Field("manifest_version", "string", length=50, notnull=True),
        Field("module", "string", length=100, notnull=True),
        Field("feature", "string", length=150, notnull=True),
        Field("provider", "string", length=50, notnull=True),
        Field("execution_model", "string", length=50, notnull=True),
        Field("is_default", "boolean", default=False),
        Field("compatible_with", "list:string"),
        Field("incompatible_with", "list:string"),
        Field("platform_compatibility", "json", notnull=True),
        Field("status", "string", length=20, default="active"),
        Field("installed_at", "datetime"),
        Field("stages", "json", default={}),
        # migration 0026 -- kept in lockstep with `bind_lifecycle_tables()`'s
        # own copy above; see that function's own inline comment.
        Field("author", "string", length=255),
        Field("license", "string", length=50),
        Field("license_review_required", "boolean", default=False),
        Field("source_url", "text"),
        Field("alternative_to", "list:string", default=[]),
        Field("homepage_url", "text"),
        Field("notice", "text"),
        Field("category", "string", length=50),
        primarykey=["app_id"],
        migrate=migrate,
    )

    dal.define_table(
        "app_tenant_availability",
        Field("tenant_id", "integer", notnull=True),
        Field("app_id", "string", length=255, notnull=True),
        Field("available", "boolean", default=True),
        Field("config_defaults", "json"),
        migrate=migrate,
    )

    dal.define_table(
        "app_activations",
        Field("community_id", "integer", notnull=True),
        Field("tenant_id", "integer", notnull=True),
        Field("app_id", "string", length=255, notnull=True),
        Field("enabled", "boolean", default=True),
        Field("config", "json"),
        Field("activated_by", "integer"),
        Field("activated_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )


def bind_ai_routing_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the tables the premium-AI model-routing layer owns.

    Greenfield feature (no Node controller to port) --
    `config/postgres/migrations/077_premium_ai_routing.sql` is this
    group's own migration. `ai_model_config`/`ai_byok_keys` back
    `services/ai_routing/config_service.py` (per-community tier choice +
    AES-256-GCM-encrypted-at-rest BYOK provider keys -- see
    `services/ai_routing/byok_crypto.py`, the same primitive/pattern
    `services/github_sync_service.py`'s token-at-rest encryption already
    uses, never plaintext).

    `ai_token_balances`/`ai_token_transactions` were `services/token_
    ledger.py`'s OWN minimal, parallel premium-AI-tokens ledger, kept
    deliberately distinct from the `community_token_balances`/
    `token_transactions` names the metered-token-billing spec
    (`docs/plans/2026-08-31-metered-token-billing-design.md`) reserved
    for the eventual multi-consumable ledger -- exactly so the two
    migrations wouldn't collide before that follow-on PR (#234) landed.
    It has: `token_ledger.py` now delegates to `services/token_billing_
    service.py`'s real, atomic ledger (migration 076) instead, so these
    two tables are unused dead schema going forward -- left defined
    (not dropped) rather than a destructive migration against any data
    that may already exist in them.

    Called from `app.py::_bind_reference_tables()` (this PR's one
    additive line there) and lazily from `services/ai_routing/
    config_service.py` itself -- idempotent either way, same "safe to
    call from more than one place" property every other `bind_*_tables()`
    in this module already relies on (see `bind_community_authz_tables()`'s
    own docstring).
    """
    if "ai_model_config" in dal.tables:
        return

    dal.define_table(
        "ai_model_config",
        Field("community_id", "integer", notnull=True),
        Field("preferred_tier", "string", length=20, default="free"),
        Field("byok_provider", "string", length=20),
        Field("on_insufficient_balance", "string", length=20, default="fallback_free"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("updated_by_user_id", "integer"),
        migrate=migrate,
    )

    dal.define_table(
        "ai_byok_keys",
        Field("community_id", "integer", notnull=True),
        Field("provider", "string", length=20, notnull=True),
        Field("encrypted_key", "text", notnull=True),
        Field("key_last4", "string", length=8, notnull=True),
        Field("is_active", "boolean", default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        Field("rotated_at", "datetime"),
        Field("created_by_user_id", "integer"),
        migrate=migrate,
    )

    dal.define_table(
        "ai_token_balances",
        Field("community_id", "integer", notnull=True),
        Field("consumable_type", "string", length=50, default="ai_premium_tokens"),
        Field("balance_tokens", "bigint", default=0),
        Field("lifetime_consumed", "bigint", default=0),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "ai_token_transactions",
        Field("community_id", "integer", notnull=True),
        Field("consumable_type", "string", length=50, notnull=True),
        Field("amount_tokens", "bigint", notnull=True),
        Field("balance_after", "bigint", notnull=True),
        Field("idempotency_key", "string", length=255, notnull=True, unique=True),
        Field("source_ref", "string", length=255),
        Field("actor_user_id", "integer"),
        Field("metadata", "json"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_token_billing_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the metered-token-billing ledger tables (migration 076).

    The third metering axis alongside node/seat licensing (critical-
    rules.md "Licensing Model: Nodes & Seats") -- premium metered
    consumables (e.g. AI-routing calls) sold as pre-paid token packs.
    `token_products` is a global catalog (no `community_id`/`tenant_id`
    column, matching the literal migration 076 schema); the per-community
    `balance`/`transactions` tables below are tenant-isolated
    transitively via `community_id -> communities.tenant_id`, the same
    pattern every other community-scoped table in this schema already
    uses (`inventory_items`, `community_members`, ...) -- enforced at the
    API layer by `services/community_authz.py::authorize_community()`,
    not a redundant `tenant_id` column here.

    Called unconditionally from `app.py::_bind_reference_tables` (this
    group's own PORTING.md instruction: "one call at END of
    app.py::_bind_reference_tables") -- no idempotency guard needed since
    that is the ONLY call site (contrast `bind_community_authz_tables`,
    which guards against being called lazily from multiple request
    paths).
    """
    dal.define_table(
        "token_products",
        Field("key", "string", length=100, notnull=True, unique=True),
        Field("name", "string", length=255, notnull=True),
        Field("unit", "string", length=50, notnull=True, default="token"),
        Field("price_cents", "integer", notnull=True),
        Field("tokens_granted", "integer", notnull=True),
        Field("active", "boolean", notnull=True, default=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "community_token_balances",
        Field("community_id", "integer", notnull=True),
        Field("product_id", "integer", notnull=True),
        Field("balance", "integer", notnull=True, default=0),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    # Append-only -- no service-layer code ever UPDATEs or DELETEs a row
    # here (see the migration's own docstring: the ledger alone can
    # reconstruct the balance history via `balance_after`).
    dal.define_table(
        "token_transactions",
        Field("community_id", "integer", notnull=True),
        Field("product_id", "integer", notnull=True),
        Field("delta", "integer", notnull=True),
        Field("reason", "string", length=255, notnull=True),
        Field("ref", "string", length=255),
        Field("balance_after", "integer", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_music_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define every table the Music Station queue feature queries.

    New-feature schema (`config/postgres/migrations/072_music_station.sql`),
    not a Node port -- backs `services/community_music_queue_service.py` +
    `blueprints/v1/community_music_queue.py`. `migrate=False` in production
    (schema owned by the numbered migration, same contract as every other
    `bind_<group>_tables()` in this file); tests pass `migrate=True` against
    a throwaway sqlite file (`hub_api/PORTING.md` Gotcha #2).

    `music_station_queue` (not `music_queue`) -- see 072's own migration
    file header comment: `012_add_music_providers.sql` already created a
    real, deployed `music_queue` table with a different, denormalized
    column set that no application code queries today; reusing that name
    here would silently bind the WRONG columns in any environment where
    012 already ran (`dal.define_table` maps onto whatever really exists),
    so this feature owns its own table name instead.

    Called from `app.py::_bind_reference_tables()` (this is new-feature
    schema this app owns outright, unlike the M7 Streaming group's
    schema-gap tables, which is why -- unlike `bind_streaming_tables()`
    above -- this function follows `hub_api/PORTING.md`'s normal checklist
    step 2 instead of that group's per-request lazy-bind workaround).
    """
    if "music_tracks" in dal.tables:
        return

    dal.define_table(
        "music_tracks",
        Field("tenant_id", "integer", notnull=True),
        Field("provider", "string", length=20, notnull=True),
        Field("external_id", "string", length=512, notnull=True),
        Field("title", "string", length=500, notnull=True),
        Field("artist", "string", length=500, notnull=True),
        Field("duration_ms", "integer", notnull=True, default=0),
        Field("artwork_url", "text"),
        Field("url", "text", notnull=True),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "music_station_queue",
        Field("tenant_id", "integer", notnull=True),
        Field("community_id", "integer", notnull=True),
        Field("track_id", "integer", notnull=True),
        Field("position", "integer", notnull=True, default=0),
        Field("status", "string", length=20, notnull=True, default="queued"),
        Field("source", "string", length=20, notnull=True, default="request"),
        Field("playlist_id", "string", length=64),
        Field("requested_by", "integer"),
        Field("added_at", "datetime"),
        Field("started_at", "datetime"),
        Field("ended_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "music_policy",
        Field("tenant_id", "integer", notnull=True),
        Field("community_id", "integer", notnull=True, unique=True),
        Field("song_requests_allowed", "boolean", notnull=True, default=True),
        Field("requests_category_restricted", "boolean", notnull=True, default=False),
        # gh-313: JSON-encoded list of lowercase YouTube content labels
        # allowed for song requests; NULL/empty = unrestricted. Added by
        # 0013_music_policy_yt_labels.
        Field("youtube_allowed_labels", "text"),
        Field("updated_by", "integer"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "music_moderation_log",
        Field("tenant_id", "integer", notnull=True),
        Field("community_id", "integer", notnull=True),
        Field("actor_user_id", "integer"),
        Field("action", "string", length=30, notnull=True),
        Field("target_queue_id", "integer"),
        Field("target_playlist_id", "string", length=64),
        Field("reason", "text"),
        Field("created_at", "datetime"),
        migrate=migrate,
    )


def bind_loyalty_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the loyalty MVP core-currency tables (gh-317).

    New-feature schema (`alembic/versions/0015_loyalty_core_tables.py`),
    not a Node port -- backs the eventual Python-native loyalty service;
    `hub_api/services/community_loyalty.py`/`blueprints/v1/
    community_loyalty.py` remain a pure reverse-proxy to the separate
    `loyalty-interaction` deployment today and do not query these tables
    directly (see the migration's own docstring for the full column-
    naming provenance -- modeled on this file's `bind_token_billing_
    tables()`/`bind_ai_routing_tables()` ledger precedent, `delta`/
    `ref`/`balance_after`/`bigint`, not the disused legacy Python
    module's `transaction_type`/`amount` naming).

    `migrate=False` in production (schema owned by the numbered Alembic
    migration, same contract as every other `bind_<group>_tables()` in
    this file); tests pass `migrate=True` against a throwaway sqlite
    file (`hub_api/PORTING.md` Gotcha #2). Idempotency guard mirrors
    `bind_music_tables()` above -- not yet wired into `app.py::
    _bind_reference_tables()` since no v1 blueprint queries hub-api's
    own DB for loyalty data yet; callers bind lazily once that lands.
    """
    if "loyalty_config" in dal.tables:
        return

    dal.define_table(
        "loyalty_config",
        Field("community_id", "integer", notnull=True, unique=True),
        Field("currency_name", "string", length=50, notnull=True, default="Points"),
        Field("currency_symbol", "string", length=16, notnull=True, default="\U0001fa99"),
        Field("earn_chat_points", "integer", notnull=True, default=1),
        Field("earn_chat_cooldown_s", "integer", notnull=True, default=60),
        Field("earn_watch_points_per_min", "integer", notnull=True, default=1),
        Field("earn_watch_enabled", "boolean", notnull=True, default=False),
        Field("max_balance", "bigint"),
        Field("enabled", "boolean", notnull=True, default=True),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "loyalty_balances",
        Field("community_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("balance", "bigint", notnull=True, default=0),
        Field("lifetime_earned", "bigint", notnull=True, default=0),
        Field("lifetime_spent", "bigint", notnull=True, default=0),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    # Append-only -- balance is reconstructable from balance_after, same
    # ledger contract as bind_token_billing_tables()'s token_transactions.
    dal.define_table(
        "loyalty_transactions",
        Field("community_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("delta", "bigint", notnull=True),
        Field("balance_after", "bigint", notnull=True),
        Field("kind", "string", length=32, notnull=True),
        Field("ref", "string", length=255),
        Field("actor_platform_user_id", "string", length=255),
        Field("created_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "loyalty_shop_items",
        Field("community_id", "integer", notnull=True),
        Field("sku", "string", length=64, notnull=True),
        Field("name", "string", length=120, notnull=True),
        Field("description", "text"),
        Field("cost", "bigint", notnull=True),
        Field("stock", "integer"),
        Field("enabled", "boolean", notnull=True, default=True),
        Field("requires_mod_approval", "boolean", notnull=True, default=False),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "loyalty_redemptions",
        Field("community_id", "integer", notnull=True),
        Field("item_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("platform_user_id", "string", length=255, notnull=True),
        Field("cost", "bigint", notnull=True),
        Field("status", "string", length=16, notnull=True, default="pending"),
        Field("note", "text"),
        Field("created_at", "datetime"),
        Field("fulfilled_at", "datetime"),
        Field("fulfilled_by", "integer"),
        migrate=migrate,
    )


def bind_bar_citizen_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the Bar Citizen guild-pairing + role-sync group's own tables (migration 0034).

    Schema is owned by `alembic/versions/0034_bar_citizen_guild_pairing.py`
    (raw SQL, not this process -- `backend-database.md`: "NO automatic
    Alembic migrations on startup") -- `migrate=False` in production,
    this function only maps pydal onto the already-migrated tables, same
    split every other `bind_*` function in this file follows.
    `flask_core.models.guild_pairing` mirrors the same columns
    column-for-column for SQLAlchemy-side FK resolution; this is the
    pydal-side mapping the service layer (`services/guild_pairing.py`,
    `services/credential_resolver.py`) actually queries through.

    Depends on `bind_auth_tables()` for `tenants`/`communities`/
    `hub_users` (this group's FK targets), same dependency-first pattern
    `bind_platform_tables()` uses. Idempotent per-DAL-instance guard.
    """
    if "tenant_platform_credentials" in dal.tables:
        return

    bind_auth_tables(dal, migrate=migrate)

    dal.define_table(
        "tenant_platform_credentials",
        Field("tenant_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("credentials_ciphertext", "text", notnull=True),
        Field("key_ref", "string", length=255),
        Field("installed_by_user_id", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "guild_tenant_pairings",
        Field("community_id", "integer", notnull=True),
        Field("discord_guild_id", "string", length=255, notnull=True),
        Field("direction", "string", length=20, notnull=True),
        Field("sync_enabled", "boolean", notnull=True, default=False),
        Field("role_name_prefix", "string", length=50, notnull=True),
        Field("created_by_user_id", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )

    dal.define_table(
        "community_role_sync_bindings",
        Field("pairing_id", "integer", notnull=True),
        Field("sync_scope", "string", length=20, notnull=True),
        Field("subscriber_tier", "integer"),
        # `community_role` (migration 0036) -- Discord -> platform direction's own
        # binding type, mutually exclusive with `subscriber_tier`/`moderator` (Twitch ->
        # Discord). See 0036's own docstring for the structural loop-prevention argument.
        Field("community_role", "string", length=20),
        Field("discord_role_id", "string", length=255, notnull=True),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
        migrate=migrate,
    )
