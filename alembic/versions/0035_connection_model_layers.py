"""Waddles platform-connection model: three separate layers (owner-confirmed 2026-10-03).

**Evolves 0034 forward -- no revert.** 0034 shipped `tenant_platform_
credentials`, a single table that conflates two distinct concepts: a
tenant's own platform *app* credentials (client_id/secret, bot_token)
and the *connection* of that app into a specific Discord guild or Twitch
channel (the OAuth install, holding per-resource access/refresh tokens).
PRs #565/#566 (`feature/bar-citizen-pairing-api`, still stacked behind
open PR #563 -- not yet on this release branch) built their bot-install
flows against that conflated shape via `services.credential_resolver.
store_tenant_credentials()`. This migration splits the conflated table
into the owner-confirmed three-layer model, documented in full at
`docs/CONNECTION_MODEL.local.md`:

1. **`tenant_platform_apps`** (renamed from `tenant_platform_credentials`,
   same columns, same tenant-0-reject trigger logic) -- one row per
   `(tenant_id, platform)`: that tenant's own app credentials
   (client_id/secret + bot_token), AES-256-GCM encrypted via
   `hub_api/services/platform_integrations_crypto.py`'s wire format
   (unchanged from 0034). Tenant 0 (global/SaaS) still never gets a row
   here -- enforced by the same trigger, renamed to match the table.

2. **`platform_connections`** (new) -- the actual Discord-guild or
   Twitch-channel install: `(tenant_id, platform, resource_type,
   resource_id)` plus the per-resource `access_token`/`refresh_token`
   (same AES-256-GCM wire format). **Tokens live ONLY here**, never in
   layer 1 or layer 3. `UNIQUE (tenant_id, platform, resource_id)` --
   installed once per resource under a tenant's app. `resource_type` is
   a `CHECK`-constrained VARCHAR (`discord_guild`, `twitch_channel`),
   same extensible-enum convention 0034 used for `guild_tenant_pairings.
   direction` -- a new platform's resource kind is a new allowed value,
   not a schema change shape. No FK to `tenant_platform_apps`: tenant 0
   legitimately has connections (via SaaS creds) with no app row at all,
   so the relationship is resolved at the application layer (the
   `resolve()` function described in `docs/CONNECTION_MODEL.local.md`),
   not a DB constraint.

3. **`community_connection_access`** (new) -- which communities may
   leverage a given connection, and under what approval state. **No
   tokens or credentials here at all** -- purely a grant: `community_id`,
   `connection_id` (FK to `platform_connections`, `ON DELETE CASCADE`),
   `status` (`pending` / `approved` / `revoked`), `requested_by_user_id`,
   `approved_by_user_id`. `UNIQUE (community_id, connection_id)` -- one
   access row per community per connection. This is the "install once,
   reuse with server-admin approval" mechanic: the first community to
   connect a resource does the real OAuth install (creates layer 2 + an
   already-approved layer 3 row); a second community wanting the same
   resource creates a `pending` row here instead of re-installing, and a
   guild/server admin must flip it to `approved` before that community's
   traffic can use the shared connection.

**Backward-compatibility shim for PR #563 (open, stacked on
`feature/bar-citizen-pairing-api`).** That branch's `credential_resolver.
py`, `discord_install_service.py`, `twitch_install_credentials.py`, and
`hub_api/services/schema.py` all reference `tenant_platform_credentials`
by name (via `dal.tenant_platform_credentials`, penguin-dal's PyDAL-style
accessor, resolved by table name at runtime -- not a hardcoded FK/OID).
Renaming the physical table out from under that branch would break it at
import/run the moment #563 is rebased onto a release branch that has
this migration. Rather than block this schema PR on #563 landing first
(explicitly out of scope here -- the stacked backend-rework PR owns that
rework, not this migration), this migration leaves a **compatibility
VIEW** named `tenant_platform_credentials` over the renamed table, with
`INSTEAD OF INSERT/UPDATE/DELETE` triggers that forward to
`tenant_platform_apps` (the real `BEFORE INSERT/UPDATE` tenant-0-reject
trigger on the base table still fires normally, since the instead-of
trigger performs a real INSERT/UPDATE against it). `dal.
tenant_platform_credentials`-shaped code keeps working unmodified. The
`libs/flask_core/flask_core/models/guild_pairing.py` `TenantPlatformCredential`
SQLAlchemy model is left untouched (still mapped to the
`tenant_platform_credentials` name, now the view) for the same reason,
and to keep `test_0034_schema_drift.py` -- which statically compares
0034's own migration SQL text against that model's columns, unrelated to
this migration -- green without touching 0034.

**Follow-up required (tracked, not done here):** the stacked
backend-rework PR must (a) port `credential_resolver.py` et al. to read/
write `tenant_platform_apps` directly for app credentials and
`platform_connections` for install tokens, (b) rework the Discord/Twitch
install flows and the role-sync worker (#567) to create/resolve
`platform_connections` + `community_connection_access` rows with the
reuse+approval flow instead of one row per community, and (c) drop this
compatibility view and the `TenantPlatformCredential` model once no
caller references the old name. Until then, both names resolve to the
same underlying data -- release is never left broken by either merge
order.

RBAC: `hub_api` remains sole writer for all four relations (the renamed
table, the compatibility view, and both new tables) -- same D28
"explicit row per role x table" convention 0034 used, extended in
`config/postgres/rbac-matrix.yaml`.

Revision ID: 0035_connection_model_layers
Revises: 0034_bar_citizen_guild_pairing
Create Date: 2026-10-03
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0035_connection_model_layers"
down_revision = "0034_bar_citizen_guild_pairing"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset(
    {
        "tenant_platform_apps",
        "tenant_platform_credentials",
        "platform_connections",
        "community_connection_access",
    }
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0035", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0035"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    # -- layer 1: rename tenant_platform_credentials -> tenant_platform_apps ---
    op.execute("ALTER TABLE tenant_platform_credentials RENAME TO tenant_platform_apps")
    op.execute(
        "ALTER SEQUENCE tenant_platform_credentials_id_seq RENAME TO tenant_platform_apps_id_seq"
    )
    op.execute(
        "ALTER TABLE tenant_platform_apps RENAME CONSTRAINT "
        "tenant_platform_credentials_tenant_id_platform_key TO "
        "tenant_platform_apps_tenant_id_platform_key"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_reject_global_tenant_credentials ON tenant_platform_apps"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_global_tenant_credentials()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_global_tenant_app_credentials() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM tenants WHERE id = NEW.tenant_id AND is_global = TRUE) THEN
                RAISE EXCEPTION
                    'tenant_platform_apps: tenant % is the global tenant and must use '
                    'the platform''s own SaaS credentials, not a DB-stored app',
                    NEW.tenant_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reject_global_tenant_app_credentials
            BEFORE INSERT OR UPDATE ON tenant_platform_apps
            FOR EACH ROW EXECUTE FUNCTION reject_global_tenant_app_credentials()
        """
    )
    op.execute(
        "COMMENT ON TABLE tenant_platform_apps IS "
        "'Layer 1 of the three-layer connection model (docs/CONNECTION_MODEL.local.md): "
        "per-tenant platform app credentials (client_id/secret + bot_token), AES-256-GCM "
        "encrypted. Tenant 0/global excluded (uses SaaS creds), enforced by trigger. "
        "Renamed from tenant_platform_credentials by 0035 -- see that migration''s "
        "compatibility view of the old name.'"
    )

    # -- compatibility view: old name keeps working for PR #563's stacked code ---
    op.execute(
        """
        CREATE VIEW tenant_platform_credentials AS
        SELECT id, tenant_id, platform, credentials_ciphertext, key_ref,
               installed_by_user_id, created_at, updated_at
        FROM tenant_platform_apps
        """
    )
    op.execute(
        "COMMENT ON VIEW tenant_platform_credentials IS "
        "'Compatibility shim (0035) over the renamed tenant_platform_apps table -- "
        "temporary, for the still-open stacked PR #563 chain. Drop once that chain "
        "is rebased onto tenant_platform_apps directly.'"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_platform_credentials_view_write() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO tenant_platform_apps
                    (tenant_id, platform, credentials_ciphertext, key_ref, installed_by_user_id)
                VALUES
                    (NEW.tenant_id, NEW.platform, NEW.credentials_ciphertext, NEW.key_ref,
                     NEW.installed_by_user_id)
                RETURNING id, created_at, updated_at INTO NEW.id, NEW.created_at, NEW.updated_at;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                UPDATE tenant_platform_apps SET
                    tenant_id = NEW.tenant_id,
                    platform = NEW.platform,
                    credentials_ciphertext = NEW.credentials_ciphertext,
                    key_ref = NEW.key_ref,
                    installed_by_user_id = NEW.installed_by_user_id,
                    updated_at = NOW()
                WHERE id = OLD.id;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                DELETE FROM tenant_platform_apps WHERE id = OLD.id;
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tenant_platform_credentials_view_write
            INSTEAD OF INSERT OR UPDATE OR DELETE ON tenant_platform_credentials
            FOR EACH ROW EXECUTE FUNCTION tenant_platform_credentials_view_write()
        """
    )

    # -- layer 2: platform_connections -------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_connections (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            platform VARCHAR(50) NOT NULL,
            resource_type VARCHAR(20) NOT NULL
                CHECK (resource_type IN ('discord_guild', 'twitch_channel')),
            resource_id VARCHAR(255) NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked', 'expired')),
            installed_by_user_id INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, platform, resource_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE platform_connections IS "
        "'Layer 2 of the three-layer connection model: the actual bot install/"
        "authorization of a tenant''s app into one Discord guild or Twitch channel. "
        "access_token/refresh_token (AES-256-GCM encrypted) live ONLY here -- never in "
        "tenant_platform_apps or community_connection_access. No FK to "
        "tenant_platform_apps: tenant 0 has connections via SaaS creds with no app row.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_connections_resource "
        "ON platform_connections (platform, resource_id)"
    )

    # -- layer 3: community_connection_access -------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS community_connection_access (
            id BIGSERIAL PRIMARY KEY,
            community_id INTEGER NOT NULL REFERENCES communities(id),
            connection_id BIGINT NOT NULL REFERENCES platform_connections(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'revoked')),
            requested_by_user_id INTEGER REFERENCES hub_users(id),
            approved_by_user_id INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (community_id, connection_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE community_connection_access IS "
        "'Layer 3 of the three-layer connection model: which communities may leverage "
        "a given platform_connections row, and its approval state. NO tokens/credentials "
        "here -- grant only. First connect to a resource creates an already-approved row; "
        "a second community reusing the same connection creates a pending row requiring "
        "server/guild-admin approval.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_community_connection_access_connection "
        "ON community_connection_access (connection_id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE tenant_platform_apps_id_seq, "
        "platform_connections_id_seq, community_connection_access_id_seq TO hub_api;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS community_connection_access")
    op.execute("DROP TABLE IF EXISTS platform_connections")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_tenant_platform_credentials_view_write "
        "ON tenant_platform_credentials"
    )
    op.execute("DROP FUNCTION IF EXISTS tenant_platform_credentials_view_write()")
    op.execute("DROP VIEW IF EXISTS tenant_platform_credentials")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_reject_global_tenant_app_credentials ON tenant_platform_apps"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_global_tenant_app_credentials()")

    op.execute(
        "ALTER TABLE tenant_platform_apps RENAME CONSTRAINT "
        "tenant_platform_apps_tenant_id_platform_key TO "
        "tenant_platform_credentials_tenant_id_platform_key"
    )
    op.execute(
        "ALTER SEQUENCE tenant_platform_apps_id_seq RENAME TO tenant_platform_credentials_id_seq"
    )
    op.execute("ALTER TABLE tenant_platform_apps RENAME TO tenant_platform_credentials")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_global_tenant_credentials() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM tenants WHERE id = NEW.tenant_id AND is_global = TRUE) THEN
                RAISE EXCEPTION
                    'tenant_platform_credentials: tenant % is the global tenant and must use '
                    'the platform''s own SaaS credentials, not a DB-stored app',
                    NEW.tenant_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reject_global_tenant_credentials
            BEFORE INSERT OR UPDATE ON tenant_platform_credentials
            FOR EACH ROW EXECUTE FUNCTION reject_global_tenant_credentials()
        """
    )
