"""Multi-tenant Discord guild<->community pairing + per-tenant bot credentials (#500, spec #499 Sec3.0/3.10).

**Gap this closes.** `ingest_sources` (migration 0020) enforces
`UNIQUE (tenant_id, platform, source_id)` with no `community_id` in the
key, so a second community -- same tenant or a different one -- cannot
register the same guild; `ingest_source_service.create_source()`'s
existence check 409s. This migration adds the N:M schema #500/#499
Sec3.0 requires: a guild is a shared external resource that may be
paired with many tenants, and a paired guild may host many communities
via exclusive channel/guild-default bindings.

**Per-tenant bot install, not a shared bot (owner correction 2026-09-29).**
Discord allows one application per guild, so consent is the tenant's
own OAuth2 bot-install callback (`bot` + `applications.commands`
scopes), not a Manage-Server check against one shared bot. Every
non-global tenant therefore needs its own Discord application
(`tenant_platform_credentials`); the global/default tenant (`tenants.
is_global`) keeps using the cluster `waddlebot-platform-credentials`
Secret (#478) and never gets a row here -- enforced by a trigger, not
just documentation, since a stray row would silently defeat the
never-shared-for-non-global-tenants rule.

Tables:
- `tenant_platform_credentials` -- one row per (tenant, platform)
  holding that tenant's own encrypted Discord application/bot
  credentials. Ciphertext + IV columns only (same shape as
  `ingest_sources.secret_ciphertext`/`secret_iv`, migration 0020); no
  plaintext secret ever appears in a migration or a grant. `key_ref`
  names the encryption key used -- today a placeholder pointing at
  this same pattern; once PR #442's per-tenant DEK broker merges, a
  follow-up migration repoints `key_ref` at a `tenant_keystore` key id
  (documented in the schema contract doc, not implemented here --
  #442 is an open, unmerged PR and this migration must not assume its
  schema exists).
- `guild_tenant_pairings` -- one row per (platform, guild, tenant).
  `status` starts `pending` when a tenant admin initiates the OAuth
  install and flips to `active` only from that tenant's own install
  callback succeeding (never from a Manage-Server check alone).
  Revocable from either side: `revoked_by` records whether a tenant
  admin revoked it, the guild authority removed the bot
  (`integration_removed`/guild-delete webhook), or periodic
  re-verification found the bot removed from the guild.
- `community_channel_bindings` -- binds a community to a channel
  (`channel_id` set) or to the whole guild as its default
  (`channel_id NULL`). Both binding kinds are exclusive **across every
  tenant paired with the guild**, enforced by partial unique indexes,
  not just a service-layer check. Requires an `active` pairing for the
  same tenant, enforced by a `BEFORE INSERT/UPDATE` trigger (a DB
  constraint alone can't reach across tables).
- `managed_roles` -- one owning community per external Discord role id
  per guild, scoped to the tenant whose bot can actually see/manage
  that role (each tenant's bot has its own role-hierarchy position,
  bounding cross-tenant blast radius per the owner's correction).

**RO views for the data plane, granted instead of the base tables**
(`v_guild_routing`, `v_managed_roles_active`) -- see the schema
contract doc (`docs/superpowers/specs/2026-09-29-guild-binding-contract.md`)
for the routing precedence and grant rationale. `tenant_platform_credentials`
is granted to `hub_api` only -- the data plane never reads bot
credentials directly off a table; it resolves them through hub-api's
own internal credential-resolution path (fail-closed for any non-global
tenant without its own app configured), analogous to PR #442's
`internal_keys.py` pattern.

**Backward compatibility.** Every pre-existing `ingest_sources` row
with a `community_id` gets a synthesized `active` pairing (this data
predates any consent flow, so it is grandfathered active rather than
demoted to `pending`) and a guild-default `community_channel_bindings`
row (`channel_id IS NULL`), preserving today's 1:1 routing exactly.
Grandfathered non-global-tenant pairings do **not** get a
`tenant_platform_credentials` row -- there is no secret to migrate --
so any actual Discord bot action for one fails closed until that
tenant configures its own app; this is a real operational gap called
out in the contract doc, not silently bridged with the platform bot.

Revision ID: 0038_guild_tenant_pairing
Revises: 0030_bundle_app_schemas
Create Date: 2026-09-29
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0038_guild_tenant_pairing"
down_revision = "0030_bundle_app_schemas"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset(
    {
        "tenant_platform_credentials",
        "guild_tenant_pairings",
        "community_channel_bindings",
        "managed_roles",
        "v_guild_routing",
        "v_managed_roles_active",
    }
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0038", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0038"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -- tenant_platform_credentials ---------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_platform_credentials (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            application_id VARCHAR(255) NOT NULL,
            client_secret_ciphertext BYTEA NOT NULL,
            client_secret_iv BYTEA NOT NULL,
            bot_token_ciphertext BYTEA NOT NULL,
            bot_token_iv BYTEA NOT NULL,
            key_ref VARCHAR(255) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, platform)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE tenant_platform_credentials IS "
        "'Per-tenant Discord application/bot credentials (#500 owner correction: "
        "one bot per tenant, Discord allows one app install per guild). Never "
        "populated for tenants.is_global=TRUE -- that tenant uses the cluster "
        "waddlebot-platform-credentials Secret (#478). Enforced by trigger below.'"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_global_tenant_credentials() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM tenants WHERE id = NEW.tenant_id AND is_global = TRUE) THEN
                RAISE EXCEPTION
                    'tenant_platform_credentials: tenant % is the global tenant and must use '
                    'the cluster waddlebot-platform-credentials Secret, not a DB-stored app',
                    NEW.tenant_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_reject_global_tenant_credentials ON tenant_platform_credentials;
        CREATE TRIGGER trg_reject_global_tenant_credentials
            BEFORE INSERT OR UPDATE ON tenant_platform_credentials
            FOR EACH ROW EXECUTE FUNCTION reject_global_tenant_credentials()
        """
    )

    # -- guild_tenant_pairings ----------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_tenant_pairings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            guild_id VARCHAR(255) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'revoked')),
            installed_by_user_id INTEGER REFERENCES hub_users(id),
            install_state_nonce VARCHAR(255),
            granted_permissions BIGINT,
            oauth_scopes VARCHAR(255),
            consent_at TIMESTAMPTZ,
            last_verified_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            revoked_by VARCHAR(30)
                CHECK (revoked_by IS NULL OR revoked_by IN
                    ('tenant_admin', 'guild_removed_bot', 'integration_removed')),
            revoked_by_user_id INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (platform, guild_id, tenant_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE guild_tenant_pairings IS "
        "'One row per (guild, tenant). active only from that tenant''s own OAuth2 "
        "bot-install callback (bot+applications.commands), never a shared-bot "
        "Manage-Server check. Revocable by tenant admin or by the bot being removed "
        "from the guild (guild delete / integration-removed webhook).'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_tenant_pairings_guild "
        "ON guild_tenant_pairings (platform, guild_id) WHERE status = 'active'"
    )

    # -- community_channel_bindings -------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS community_channel_bindings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            guild_id VARCHAR(255) NOT NULL,
            channel_id VARCHAR(255),
            community_id INTEGER NOT NULL REFERENCES communities(id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            pairing_id UUID NOT NULL REFERENCES guild_tenant_pairings(id),
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            created_by INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE community_channel_bindings IS "
        "'channel_id IS NULL means a guild-default binding. Both a channel bind and "
        "the guild default are exclusive across ALL tenants paired with the guild "
        "(partial unique indexes below), not just within one tenant.'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_binding_exclusive "
        "ON community_channel_bindings (platform, guild_id, channel_id) "
        "WHERE channel_id IS NOT NULL AND status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_guild_default_binding_exclusive "
        "ON community_channel_bindings (platform, guild_id) "
        "WHERE channel_id IS NULL AND status = 'active'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_community_channel_bindings_tenant "
        "ON community_channel_bindings (tenant_id, community_id)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION require_active_pairing_for_binding() RETURNS trigger AS $$
        DECLARE
            pairing_tenant INTEGER;
            pairing_status VARCHAR(20);
        BEGIN
            SELECT tenant_id, status INTO pairing_tenant, pairing_status
            FROM guild_tenant_pairings WHERE id = NEW.pairing_id;

            IF pairing_tenant IS NULL THEN
                RAISE EXCEPTION 'community_channel_bindings: pairing % not found', NEW.pairing_id;
            END IF;
            IF pairing_tenant != NEW.tenant_id THEN
                RAISE EXCEPTION
                    'community_channel_bindings: pairing % belongs to a different tenant',
                    NEW.pairing_id;
            END IF;
            IF pairing_status != 'active' AND NEW.status = 'active' THEN
                RAISE EXCEPTION
                    'community_channel_bindings: pairing % is not active (status=%)',
                    NEW.pairing_id, pairing_status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_require_active_pairing_for_binding ON community_channel_bindings;
        CREATE TRIGGER trg_require_active_pairing_for_binding
            BEFORE INSERT OR UPDATE ON community_channel_bindings
            FOR EACH ROW EXECUTE FUNCTION require_active_pairing_for_binding()
        """
    )

    # -- managed_roles --------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            guild_id VARCHAR(255) NOT NULL,
            role_id VARCHAR(255) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            pairing_id UUID NOT NULL REFERENCES guild_tenant_pairings(id),
            owning_community_id INTEGER NOT NULL REFERENCES communities(id),
            registered_via VARCHAR(20) NOT NULL
                CHECK (registered_via IN ('created', 'adopted')),
            approved_by_user_id INTEGER REFERENCES hub_users(id),
            approved_at TIMESTAMPTZ,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'pending_cleanup', 'removed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (platform, guild_id, role_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE managed_roles IS "
        "'One owning community per external role id per guild -- registering an "
        "already-owned role is rejected at creation, never a silent overwrite. "
        "adopted rows require approved_by_user_id (guild-authority approval); "
        "created rows need none. status=pending_cleanup marks rows a revoked "
        "pairing left behind for the data plane''s best-effort unwind pass.'"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_managed_roles_adopted_approval'
            ) THEN
                ALTER TABLE managed_roles ADD CONSTRAINT chk_managed_roles_adopted_approval
                    CHECK (registered_via = 'created' OR approved_by_user_id IS NOT NULL);
            END IF;
        END $$
        """
    )

    # -- RO views for the data plane (never the base tables) ------------
    op.execute(
        """
        CREATE OR REPLACE VIEW v_guild_routing AS
        SELECT
            b.id AS binding_id,
            b.platform,
            b.guild_id,
            b.channel_id,
            b.community_id,
            b.tenant_id,
            b.pairing_id,
            p.status AS pairing_status
        FROM community_channel_bindings b
        JOIN guild_tenant_pairings p ON p.id = b.pairing_id
        WHERE b.status = 'active' AND p.status = 'active'
        """
    )
    op.execute(
        "COMMENT ON VIEW v_guild_routing IS "
        "'Ingest routing precedence (contract doc): channel bind -> tag/prefix -> "
        "guild default (channel_id IS NULL) -> fail closed. Only active bindings "
        "under an active pairing are visible; granted to svc_ingest/svc_action "
        "instead of the base tables.'"
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW v_managed_roles_active AS
        SELECT
            r.id, r.platform, r.guild_id, r.role_id, r.tenant_id, r.pairing_id,
            r.owning_community_id, r.registered_via, r.status
        FROM managed_roles r
        JOIN guild_tenant_pairings p ON p.id = r.pairing_id
        WHERE r.status = 'active' AND p.status = 'active'
        """
    )
    op.execute(
        "COMMENT ON VIEW v_managed_roles_active IS "
        "'Role-ownership read path for every sync decision (spec #499 Sec3.0 "
        "read-state authorization gap) -- resolve by role_id through this view, "
        "never trust a native Discord role presence alone.'"
    )

    # -- Backward-compat data migration from ingest_sources --------------
    op.execute(
        """
        INSERT INTO guild_tenant_pairings
            (platform, guild_id, tenant_id, status, consent_at, last_verified_at, created_at)
        SELECT DISTINCT s.platform, s.source_id, s.tenant_id, 'active', s.created_at, s.created_at, s.created_at
        FROM ingest_sources s
        WHERE s.community_id IS NOT NULL
        ON CONFLICT (platform, guild_id, tenant_id) DO NOTHING
        """
    )
    op.execute(
        "COMMENT ON COLUMN guild_tenant_pairings.consent_at IS "
        "'Grandfathered rows from pre-#500 ingest_sources are backdated to "
        "created_at and marked active with no OAuth install -- they predate this "
        "consent model. A real Discord action still fails closed for a "
        "non-global tenant with no tenant_platform_credentials row (see contract doc).'"
    )
    op.execute(
        """
        INSERT INTO community_channel_bindings
            (platform, guild_id, channel_id, community_id, tenant_id, pairing_id, status, created_at)
        SELECT s.platform, s.source_id, NULL, s.community_id, s.tenant_id, p.id, 'active', s.created_at
        FROM ingest_sources s
        JOIN guild_tenant_pairings p
            ON p.platform = s.platform AND p.guild_id = s.source_id AND p.tenant_id = s.tenant_id
        WHERE s.community_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    roles = sorted(matrix_module.matrix_roles(matrix_path))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_create_roles_sql(roles):
        op.execute(statement)
    for statement in matrix_module.render_revoke_public_sql(
        sorted(t for t in _MATRIX_TABLES if not t.startswith("v_"))
    ):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE tenant_platform_credentials_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_managed_roles_active")
    op.execute("DROP VIEW IF EXISTS v_guild_routing")
    op.execute("DROP TABLE IF EXISTS managed_roles")
    op.execute("DROP TRIGGER IF EXISTS trg_require_active_pairing_for_binding ON community_channel_bindings")
    op.execute("DROP FUNCTION IF EXISTS require_active_pairing_for_binding()")
    op.execute("DROP TABLE IF EXISTS community_channel_bindings")
    op.execute("DROP TABLE IF EXISTS guild_tenant_pairings")
    op.execute("DROP TRIGGER IF EXISTS trg_reject_global_tenant_credentials ON tenant_platform_credentials")
    op.execute("DROP FUNCTION IF EXISTS reject_global_tenant_credentials()")
    op.execute("DROP TABLE IF EXISTS tenant_platform_credentials")
