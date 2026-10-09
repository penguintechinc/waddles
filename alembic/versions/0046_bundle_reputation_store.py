"""Bundle reputation store (issue #726): scores table, member identity column, DB role.

The data-plane `reputation` host capability (`core/bundle_host_reputation`,
`wit/waddle-bundle/stage.wit` `interface reputation`) needs a hub-owned,
community-scoped balance store with an atomic adjust and a durable rolling-24h
per-(community, user) cap:

- `bundle_reputation_scores (community_id, user_uuid) -> balance` -- the
  store the adjust transaction mutates under a row lock.
- `community_members.user_uuid` (nullable) -- the stable, PII-free token a
  bundle names as the target of a call. It is minted inside hub-api's PII
  boundary (IdentityService, #429) and is NULL until that lands; NULL rows
  never match a lookup, so the capability is fail-closed until populated.
- A partial index on the existing 0041 `bundle_reputation_adjustments`
  audit ledger for the rolling-window cap query (the ledger row for an
  applied adjustment is inserted in the same transaction as the score
  update).
- `waddles_bundle_reputation` -- the data-plane's least-privilege role for
  this store: DML on the two reputation tables plus column-scoped SELECT on
  the membership tables, nothing else. Provisioned LOGIN with the password in
  `DB_REPUTATION_PASSWORD` when that env var is set; otherwise created
  NOLOGIN (inert -- it cannot authenticate, and the capability stays
  unwired/fail-closed in that deployment), so a missing secret never breaks
  the migration chain.

The DDL itself lives in `scripts/db/bundle_reputation_store.sql` (copied into
the migrations image, and `include_str!`'d by the Rust crate's integration
test) so the shipped schema and the tested schema cannot drift.

Chain note: this repo's live schema chain is the Alembic one (head was
`0045_identity_resolution` when this was renumbered from 0043 -> 0046 to chain
after the identity migrations 0043-0045); `config/postgres/migrations/*.sql` is
only replayed by the 0001 baseline on a brand-new database, so a new SQL-only
file there would never reach an existing deployment.

`community_members.user_uuid` and its `(community_id, user_uuid)` unique index
are now first created by 0045_identity_resolution; the DDL file's `IF NOT
EXISTS` forms make them a no-op here (and keep the file runnable standalone for
the Rust integration tests). 0045 therefore OWNS them: this migration's
downgrade must not drop them.

Revision ID: 0046_bundle_reputation_store
Revises: 0045_identity_resolution
Create Date: 2026-10-09
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0046_bundle_reputation_store"
down_revision = "0045_identity_resolution"
branch_labels = None
depends_on = None

_ROLE = "waddles_bundle_reputation"
_PASSWORD_ENV = "DB_REPUTATION_PASSWORD"  # noqa: S105 -- env var NAME, not a secret
_PASSWORD_GUC = f"waddles.{_ROLE}_pw"
_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_reputation_store.sql"

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
    # exec_driver_sql: the file is a multi-statement script with `$$` blocks
    # and quotes; bypass SQLAlchemy `text()` bind-parameter parsing entirely.
    conn.exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_bundle_reputation_adjustments_user_window")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_scores")
    # `community_members.user_uuid` + its unique index are owned by 0045
    # (see module docstring) -- left in place so downgrading to 0045 keeps the
    # identity layer intact.
    op.execute(
        f"""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN
        REVOKE ALL ON bundle_reputation_adjustments FROM {_ROLE};
        REVOKE ALL ON SEQUENCE bundle_reputation_adjustments_id_seq FROM {_ROLE};
        REVOKE ALL ON community_members FROM {_ROLE};
        REVOKE ALL ON communities FROM {_ROLE};
        REVOKE USAGE ON SCHEMA public FROM {_ROLE};
        DROP ROLE {_ROLE};
    END IF;
END $$;
"""
    )
