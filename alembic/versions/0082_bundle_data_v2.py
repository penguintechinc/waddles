"""bundle_data schema: db v2 table registry + per-community storage-usage counters.

Backs the `db` v2 host capability (`docs/bundle-storage-v2.md`): a bundle may
declare several tables. Two platform-owned tables live in a third schema,
`bundle_data` (NOT `app_core`/`app_community`, so no bundle-derived table name
can ever collide with them):

- `bundle_data.table_registry` -- one row per (app_id, logical table): the
  validated declaration (typed columns + indexes as JSON) and the physical
  `(schema_kind, physical_table)` hub-api provisioned. The data plane reads it
  (SELECT only) to build its schema registry -- a guest can only ever name a
  logical table that this registry lists, and the physical table is resolved
  here, never taken from a host-call. Written by `waddles_bundle_migrator`
  (hub-api provisioning), which already owns the bundle DDL path.
- `bundle_data.usage` -- one row per (app_id, tenant_id, community_id): live row
  and byte counters aggregated across ALL of the bundle's v2 tables, maintained
  by the runtime role in the SAME transaction as each write (conditional upsert
  enforcing the quota). Same tenant+community RLS as the bundle tables
  themselves, so one community can never read or move another's counters.

Revision ID: 0082_bundle_data_v2
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0082_bundle_data_v2"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None

_MIGRATOR = "waddles_bundle_migrator"
_RUNTIME = "waddles_bundle_runtime"


def upgrade() -> None:
    """Create the `bundle_data` schema, table registry and usage counters (with RLS)."""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS bundle_data AUTHORIZATION {_MIGRATOR}")
    op.execute(f"GRANT USAGE ON SCHEMA bundle_data TO {_RUNTIME}")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_data.table_registry (
            app_id          text    NOT NULL,
            logical_name    text    NOT NULL,
            schema_kind     text    NOT NULL CHECK (schema_kind IN ('core', 'community')),
            physical_table  text    NOT NULL,
            columns         jsonb   NOT NULL,
            indexes         jsonb   NOT NULL DEFAULT '[]'::jsonb,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (app_id, logical_name),
            UNIQUE (schema_kind, physical_table)
        )
        """
    )
    op.execute(f"GRANT SELECT ON bundle_data.table_registry TO {_RUNTIME}")
    op.execute(f"GRANT ALL ON bundle_data.table_registry TO {_MIGRATOR}")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_data.usage (
            app_id        text    NOT NULL,
            tenant_id     integer NOT NULL,
            community_id  integer NOT NULL,
            row_count     bigint  NOT NULL DEFAULT 0 CHECK (row_count >= 0),
            byte_count    bigint  NOT NULL DEFAULT 0 CHECK (byte_count >= 0),
            PRIMARY KEY (app_id, tenant_id, community_id)
        )
        """
    )
    op.execute("ALTER TABLE bundle_data.usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bundle_data.usage FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY usage_scope ON bundle_data.usage TO {_RUNTIME}
        USING (
            tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer
            AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer
        )
        """
    )
    # Teardown/inspection by the DDL role is not scoped to one community.
    op.execute(
        f"CREATE POLICY usage_migrator ON bundle_data.usage TO {_MIGRATOR} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON bundle_data.usage TO {_RUNTIME}")
    op.execute(f"GRANT ALL ON bundle_data.usage TO {_MIGRATOR}")


def downgrade() -> None:
    """Drop the usage counters, registry and schema."""
    op.execute("DROP TABLE IF EXISTS bundle_data.usage")
    op.execute("DROP TABLE IF EXISTS bundle_data.table_registry")
    op.execute("DROP SCHEMA IF EXISTS bundle_data")
