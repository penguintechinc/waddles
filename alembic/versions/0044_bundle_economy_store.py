"""Bundle economy store (issue #714): balances, ledger, least-privilege DB role.

The data-plane `economy` host capability (`core/bundle_host_economy`,
`wit/waddle-bundle/stage.wit` `interface economy`) needs a hub-owned,
community-scoped currency store with atomic debit/credit:

- `economy_balances (tenant, community, user_uuid) -> balance` with a
  `CHECK (balance >= 0)` backstop -- mutated by single-statement guarded
  UPDATEs (`SET balance = balance - stake + payout WHERE balance >= stake`),
  never read-modify-write.
- `economy_ledger` -- append-only audit rows written in the SAME statement
  as the balance move they record.
- `waddles_economy_runtime` -- the data-plane's least-privilege role for this
  store: DML on the two economy tables (ledger append-only) plus
  column-scoped SELECT on the membership tables, nothing else. Provisioned
  LOGIN with the password in `DB_ECONOMY_PASSWORD` when that env var is set;
  otherwise created NOLOGIN (inert -- it cannot authenticate, and the
  capability stays unwired/fail-closed in that deployment), so a missing
  secret never breaks the migration chain.
- Re-states 0043's nullable `community_members.user_uuid` identity column
  idempotently (IF NOT EXISTS) so the DDL file stands alone. It is NULL until
  hub-api's IdentityService (#429) mints it; NULL rows never match, so the
  capability is fail-closed until populated.

The DDL itself lives in `scripts/db/bundle_economy_store.sql` (copied into the
migrations image, and `include_str!`'d by the Rust crate's integration test) so
the shipped schema and the tested schema cannot drift.

Chain note: this repo's live schema chain is the Alembic one;
`config/postgres/migrations/*.sql` is only replayed by the 0001 baseline on a
brand-new database, so a new SQL-only file there would never reach an
existing deployment.

Revision ID: 0044_bundle_economy_store
Revises: 0043_bundle_reputation_store
Create Date: 2026-10-09
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0044_bundle_economy_store"
down_revision = "0043_bundle_reputation_store"
branch_labels = None
depends_on = None

_ROLE = "waddles_economy_runtime"
_PASSWORD_ENV = "DB_ECONOMY_PASSWORD"  # noqa: S105 -- env var NAME, not a secret
_PASSWORD_GUC = f"waddles.{_ROLE}_pw"
_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_economy_store.sql"

#: Created/refreshed BEFORE the DDL file's grants block so its guarded GRANTs
#: find the role. The password reaches SQL only through a session GUC bound by
#: a parameterized query (same pattern as 0030/0032/0043) -- never interpolated.
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
    """Provision the role, then apply the shipped DDL file."""
    conn = op.get_bind()
    conn.execute(
        sa.text("SELECT set_config(:key, :v, false)"),
        {"key": _PASSWORD_GUC, "v": os.environ.get(_PASSWORD_ENV, "")},
    )
    op.execute(_ROLE_SQL)
    # exec_driver_sql: the file is a multi-statement script with `$$` blocks
    # and quotes; bypass SQLAlchemy `text()` bind-parameter parsing entirely.
    conn.exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop the economy tables and role; leave 0043's shared `user_uuid` column alone."""
    op.execute("DROP TABLE IF EXISTS economy_ledger")
    op.execute("DROP TABLE IF EXISTS economy_balances")
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
