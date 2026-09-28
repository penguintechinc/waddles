"""hub_users.uuid -- the real-user UUID the data plane needs (PII rule hard blocker).

**Problem this fixes.** `critical-rules.md` PII Tokenization requires a
single identity table where every other table/service references users by
UUID only. `hub_users.id` (`config/postgres/migrations/000_create_base_schema.sql`)
is a plain `SERIAL` -- no UUID column exists anywhere on the identity
table. Two in-flight PRs already assume one exists and are written as
tolerant no-ops until it lands:

- PR #429 (`feature/ingest-pii-tokenization`)'s `{user:<uuid>}` chat
  tokenization mints a token from `community_members.user_id`, which today
  holds `str(hub_users.id)` (confirmed against `hub_api/services/
  admin_service.py` etc's own `dal.community_members.user_id ==
  str(user_id)` convention) -- a stringified integer, not a UUID. No
  `{user:<uuid>}` token has actually been UUID-shaped in practice until
  this migration lands.
- PR #427 (`feature/egress-detokenizer`)'s `internal_identity_service.py`
  already queries `dal.hub_users.uuid`, feature-checked
  (`"uuid" in dal.hub_users.fields`) so it stays a safe no-op until this
  column exists.

**Column contract (for #429/#427 to align on):**

- `hub_users.uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()` --
  `gen_random_uuid()` is core Postgres as of PG13 (this repo's baseline is
  PG17, `backend-database.md`), no `pgcrypto` extension needed.
- Backfilled for every pre-existing row before the `NOT NULL` constraint
  is applied (three-step: add nullable -> backfill -> set NOT NULL +
  default -- never a bare `ADD COLUMN ... NOT NULL DEFAULT` on a
  populated table under load).
- `hub_users_uuid_key` UNIQUE constraint (Postgres creates the backing
  btree index implicitly -- no separate `CREATE INDEX` needed).
- New view `community_member_identities` -- the query #429's
  `bundle_active_set::identity::resolve_linked_user_id` should read
  instead of `community_members.user_id` directly, once that crate is
  updated to consume it: joins `community_members` to `hub_users` via the
  existing `user_id::text = hub_users.id::text` convention and projects
  only `(community_id, platform, platform_user_id, display_name,
  hub_user_uuid)` -- no PII column (`email`, `username`, `password_hash`,
  `avatar_url`, ...) is exposed.
- `waddles_bundle_reader` (the Rust data-plane's existing RO role, see
  `0025_app_source_bindings.py`) is granted **column-scoped** `SELECT
  (uuid, id)` on `hub_users` directly (the join key plus the UUID itself,
  nothing else) and table-level `SELECT` on the new view -- guarded by the
  same `IF EXISTS (SELECT 1 FROM pg_roles ...)` pattern 0025 established,
  since this migration must be a safe no-op wherever that role doesn't
  exist yet.

Downgrade is the exact inverse, in reverse order: revoke both grants,
drop the view, drop the unique constraint, drop the column.

Revision ID: 0027_hub_users_identity_uuid
Revises: 0026_app_install_approval_source
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "0027_hub_users_identity_uuid"
down_revision = "0026_app_install_approval_source"
branch_labels = None
depends_on = None

#: Not part of the hub-api-owned RBAC matrix (config/postgres/rbac-matrix.yaml)
#: -- this is the Rust data-plane's own reader role, provisioned by a
#: separate (parallel) migration. Guarded by IF EXISTS below so this
#: migration never depends on ordering against that one (same convention
#: as 0025_app_source_bindings.py).
_BUNDLE_READER_ROLE = "waddles_bundle_reader"

_VIEW_NAME = "community_member_identities"


def upgrade() -> None:
    # 1. Nullable first -- hub_users already has rows in every real
    #    environment; a bare `ADD COLUMN ... NOT NULL` fails outright, and
    #    `... NOT NULL DEFAULT gen_random_uuid()` on a populated table
    #    rewrites every row under an ACCESS EXCLUSIVE lock without giving
    #    us a chance to verify the backfill first.
    op.execute("ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS uuid UUID")

    # 2. Backfill every pre-existing row that doesn't have one yet.
    op.execute("UPDATE hub_users SET uuid = gen_random_uuid() WHERE uuid IS NULL")

    # 3. Now safe to enforce NOT NULL and attach the default for future inserts.
    op.execute("ALTER TABLE hub_users ALTER COLUMN uuid SET NOT NULL")
    op.execute("ALTER TABLE hub_users ALTER COLUMN uuid SET DEFAULT gen_random_uuid()")

    # 4. Uniqueness -- also creates the backing btree index implicitly.
    op.execute(
        "ALTER TABLE hub_users ADD CONSTRAINT hub_users_uuid_key UNIQUE (uuid)"
    )

    # 5. The join the data plane actually needs: community_members (keyed
    #    by platform identity) -> hub_users.uuid (the real identity), with
    #    zero PII columns projected.
    op.execute(
        f"CREATE OR REPLACE VIEW {_VIEW_NAME} AS\n"  # nosec B608 -- view name is a fixed module-level literal, never user input
        "SELECT\n"
        "    cm.community_id,\n"
        "    cm.platform,\n"
        "    cm.platform_user_id,\n"
        "    cm.display_name,\n"
        "    hu.uuid AS hub_user_uuid\n"
        "FROM community_members cm\n"
        "LEFT JOIN hub_users hu ON hu.id::text = cm.user_id"
    )
    op.execute(
        f"COMMENT ON VIEW {_VIEW_NAME} IS "
        "'Data-plane read contract for PR #429/#427: community_members "
        "platform identity joined to its linked hub_users.uuid (NULL if "
        "unlinked). No PII column from either table is projected.'"
    )

    # 6. Column-scoped grant on hub_users -- uuid plus the join key (id),
    #    never username/email/password_hash/avatar_url/etc. Guarded: role
    #    may not exist yet in every environment (same posture as 0025).
    op.execute(
        "DO $$ BEGIN\n"  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}') THEN\n"
        f"    GRANT SELECT (uuid, id) ON hub_users TO {_BUNDLE_READER_ROLE};\n"
        f"    GRANT SELECT ON {_VIEW_NAME} TO {_BUNDLE_READER_ROLE};\n"
        "  END IF;\n"
        "END $$;"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN\n"  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}') THEN\n"
        f"    REVOKE SELECT ON {_VIEW_NAME} FROM {_BUNDLE_READER_ROLE};\n"
        f"    REVOKE SELECT (uuid, id) ON hub_users FROM {_BUNDLE_READER_ROLE};\n"
        "  END IF;\n"
        "END $$;"
    )
    op.execute(f"DROP VIEW IF EXISTS {_VIEW_NAME}")
    op.execute("ALTER TABLE hub_users DROP CONSTRAINT IF EXISTS hub_users_uuid_key")
    op.execute("ALTER TABLE hub_users DROP COLUMN IF EXISTS uuid")
