"""Bundle object-storage audit ledger + INSERT-only DB role.

Spec `docs/superpowers/specs/2026-11-06-bundle-object-storage.md` Sec4.
`core/bundle_host_storage::pg_audit` records every cross-prefix storage access
(allowed and denied) and every owner tag change in `bundle_storage_audit`. The role
`waddles_bundle_storage_audit` may only INSERT that table. LOGIN only when
`DB_BUNDLE_STORAGE_AUDIT_PASSWORD` is set; otherwise NOLOGIN (inert, fail-closed).
DDL: `scripts/db/bundle_storage_audit.sql`.

Revision ID: 0080_bundle_storage_audit
Revises: 0079_bundle_platform_table
Create Date: 2026-11-06
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0080_bundle_storage_audit"
down_revision = "0079_bundle_platform_table"
branch_labels = None
depends_on = None

_ROLE = "waddles_bundle_storage_audit"
_PASSWORD_ENV = "DB_BUNDLE_STORAGE_AUDIT_PASSWORD"
_PASSWORD_GUC = f"waddles.{_ROLE}_pw"
_SQL_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_storage_audit.sql"
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
    op.execute("DROP TABLE IF EXISTS bundle_storage_audit")
    op.execute(
        f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN
        REVOKE USAGE ON SCHEMA public FROM {_ROLE};
        DROP ROLE {_ROLE};
    END IF;
END $$;
"""
    )
