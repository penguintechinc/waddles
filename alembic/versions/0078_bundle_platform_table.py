"""Bundle platform-table capability: audit ledger + least-privilege DB role.

Spec `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md` Sec5.
`core/bundle_host_platform_table` writes allowlisted platform-table columns
(initially `community_members.role`) on behalf of an approved bundle and records
one `bundle_platform_table_audit` row per write in the same transaction. The role
`waddles_platform_table` may only SELECT/UPDATE the allowlisted `community_members`
columns, look up `communities.tenant_id`, and INSERT the audit row. LOGIN only when
`DB_PLATFORM_TABLE_PASSWORD` is set; otherwise NOLOGIN (inert, capability unwired,
fail-closed). DDL: `scripts/db/bundle_platform_table.sql`.

Revision ID: 0078_bundle_platform_table
Revises: 0077_stream_keys_destinations
Create Date: 2026-11-06
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0078_bundle_platform_table"
down_revision = "0077_stream_keys_destinations"
branch_labels = None
depends_on = None

_ROLE = "waddles_platform_table"
_PASSWORD_ENV = "DB_PLATFORM_TABLE_PASSWORD"
_PASSWORD_GUC = f"waddles.{_ROLE}_pw"
_SQL_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_platform_table.sql"
)

#: Created/refreshed BEFORE the DDL file's grants block so its guarded GRANTs
#: find the role. The password reaches SQL only through a session GUC bound by
#: a parameterized query (same pattern as 0030/0032) -- never interpolated.
_ROLE_SQL = f"""
DO $$
DECLARE
    pw text := NULLIF(current_setting('{_PASSWORD_GUC}', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN
        IF pw IS NULL THEN
            CREATE ROLE {_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSE
            EXECUTE format(
                'CREATE ROLE {_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
                pw
            );
        END IF;
    ELSIF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE {_ROLE} LOGIN PASSWORD %L', pw);
    END IF;
END $$;
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("SELECT set_config(:key, :v, false)"),
        {"key": _PASSWORD_GUC, "v": os.environ.get(_PASSWORD_ENV, "")},
    )
    op.execute(_ROLE_SQL)
    conn.exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bundle_platform_table_audit")
    op.execute(
        f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN
        REVOKE ALL ON community_members FROM {_ROLE};
        REVOKE ALL ON communities FROM {_ROLE};
        REVOKE USAGE ON SCHEMA public FROM {_ROLE};
        DROP ROLE {_ROLE};
    END IF;
END $$;
"""
    )
