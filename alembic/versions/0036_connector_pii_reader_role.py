"""`waddles_connector_pii_reader` RO role -- column-scoped identity reads for `identity.lookup`.

Spec `docs/superpowers/specs/2026-09-28-connector-bundles.md` S3.3/S3.4
(design landed on `docs/connector-bundles-design`, not yet merged into
this branch's base): the `identity.lookup` host capability (spec S1,
gated by `connector.pii.read`, `core/bundle_executor/src/manifest.rs`'s
`may_link_identity`) needs a dedicated RO-replica Postgres role that can
read ONLY the identity columns a connector needs -- never any other PII
column (email, IP, payment, address) and never a non-identity table.

**Real tables, not the spec table's literal names.** The spec's S3.3
comparison table describes the target shape ("hub_users.uuid",
"community_members/the identity view") in terms of a parallel, not-yet-
merged design; this schema's actual tables (`config/postgres/migrations/
000_create_base_schema.sql`) are:
  - `hub_users` -- the one identity table (PII Tokenization rule). It has
    no `uuid` column today (`0023_bundle_install_schema`'s own docstring:
    "hub_users is this codebase's one identity table and uses an integer
    SERIAL key"). A stable, external-safe token is exactly what
    `identity.lookup`'s WIT contract needs (`identity-record.uuid: string`,
    `wit/waddle-connector/connector.wit`) -- this migration adds it
    (`UUID UNIQUE NOT NULL DEFAULT gen_random_uuid()`), backfilling
    existing rows, rather than reusing the raw SERIAL id (sequential,
    enumerable) as the token a WASM guest receives.
  - `hub_user_identities` -- the platform-identity mapping
    (`hub_user_id -> (platform, platform_user_id, platform_username)`)
    inbound tokenization resolves through.
  - `community_members` -- per-community `display_name` (outbound mention
    rendering wants a display name, not just a bare handle).

Grants are column-scoped `GRANT SELECT (...)` statements, hand-written in
this migration rather than routed through `scripts/db/rbac_matrix.py` --
that generator's `GrantSpec`/`render_grant_sql` only knows table-level
privileges (`ALL_PRIVILEGES` applied to a whole table), so it cannot
express "SELECT on these three columns only". This mirrors the existing,
already-established precedent for this exact class of role:
`waddles_bundle_reader` (PR #434) is likewise NOT part of
`config/postgres/rbac-matrix.yaml`'s `roles:` list and is granted via
hand-written, idempotent `DO $$ ... $$` blocks in its own migration
(`0028_bundle_active_set_changelog._bundle_reader_grant_sql`) -- this
migration follows that same convention for the new role.

Revision ID: 0036_connector_pii_reader_role
Revises: 0028_bundle_active_set_changelog
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "0036_connector_pii_reader_role"
down_revision = "0028_bundle_active_set_changelog"
branch_labels = None
depends_on = None

#: The new RO role. NOLOGIN -- like every other role in this schema, an
#: application process authenticates as it via a Kubernetes Secret
#: (helm auto-provision pattern, PR #445) that maps a login role's
#: password to this NOLOGIN role's membership, never a directly-login-able
#: role with a hardcoded password in this migration.
_ROLE = "waddles_connector_pii_reader"


def _role_exists_guard(body: str) -> str:
    """Wrap `body` in a `DO $$ ... $$` block that only runs if `_ROLE` exists.

    Same idempotent-and-order-independent posture as migration 0028's
    `_bundle_reader_grant_sql`/`_bundle_reader_revoke_sql`: this migration
    may run before or after whatever future migration provisions the
    login role that becomes a member of `_ROLE` (helm auto-provision,
    PR #445) -- both orders must be safe no-ops on the missing side.
    """
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN\n"
        f"    {body}\n"
        f"  END IF;\n"
        f"END $$;"
    )


def upgrade() -> None:
    # hub_users gets a real external-safe identity token. `gen_random_uuid()`
    # needs pgcrypto's extension in some Postgres builds pre-13; this schema
    # already targets Postgres 17 (backend-database.md), which ships
    # gen_random_uuid() in core (pgcrypto not required).
    op.execute(
        "ALTER TABLE hub_users "
        "ADD COLUMN IF NOT EXISTS uuid UUID NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        "DO $$ BEGIN\n"
        "  IF NOT EXISTS (\n"
        "    SELECT 1 FROM pg_constraint WHERE conname = 'hub_users_uuid_key'\n"
        "  ) THEN\n"
        "    ALTER TABLE hub_users ADD CONSTRAINT hub_users_uuid_key UNIQUE (uuid);\n"
        "  END IF;\n"
        "END $$;"
    )

    op.execute(
        f"DO $$ BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN\n"
        f"    CREATE ROLE {_ROLE} NOLOGIN;\n"
        f"  END IF;\n"
        f"END $$;"
    )

    # Column-scoped SELECT ONLY -- no INSERT/UPDATE/DELETE anywhere, no
    # other column on any of these three tables (never email, ip, payment,
    # address, or any hub_users/community_members column beyond what's
    # listed here). `id` is granted alongside `uuid` on hub_users because
    # the join predicate (hub_user_identities.hub_user_id = hub_users.id)
    # references it -- Postgres requires column-level SELECT on every
    # column a query touches, including join keys, not just selected ones.
    op.execute(_role_exists_guard(f"GRANT SELECT (id, uuid) ON hub_users TO {_ROLE};"))
    op.execute(
        _role_exists_guard(
            "GRANT SELECT (hub_user_id, platform, platform_user_id, platform_username) "
            f"ON hub_user_identities TO {_ROLE};"
        )
    )
    op.execute(
        _role_exists_guard(
            "GRANT SELECT (community_id, platform, platform_user_id, display_name) "
            f"ON community_members TO {_ROLE};"
        )
    )

    # Belt-and-suspenders default-deny: explicit REVOKE ALL from PUBLIC on
    # the touched tables, matching rbac_matrix.py's `render_revoke_public_sql`
    # baseline for every other identity/PII-adjacent table in this schema.
    op.execute("REVOKE ALL ON hub_users FROM PUBLIC;")
    op.execute("REVOKE ALL ON hub_user_identities FROM PUBLIC;")
    op.execute("REVOKE ALL ON community_members FROM PUBLIC;")


def downgrade() -> None:
    op.execute(_role_exists_guard(f"REVOKE ALL PRIVILEGES ON hub_users FROM {_ROLE};"))
    op.execute(
        _role_exists_guard(
            f"REVOKE ALL PRIVILEGES ON hub_user_identities FROM {_ROLE};"
        )
    )
    op.execute(
        _role_exists_guard(f"REVOKE ALL PRIVILEGES ON community_members FROM {_ROLE};")
    )
    op.execute(
        f"DO $$ BEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN\n"
        f"    DROP ROLE {_ROLE};\n"
        f"  END IF;\n"
        f"EXCEPTION WHEN dependent_objects_still_exist THEN\n"
        f"  NULL; -- role still owns objects from a later migration; leave it\n"
        f"END $$;"
    )
    op.execute(
        "DO $$ BEGIN\n"
        "  IF EXISTS (\n"
        "    SELECT 1 FROM pg_constraint WHERE conname = 'hub_users_uuid_key'\n"
        "  ) THEN\n"
        "    ALTER TABLE hub_users DROP CONSTRAINT hub_users_uuid_key;\n"
        "  END IF;\n"
        "END $$;"
    )
    op.execute("ALTER TABLE hub_users DROP COLUMN IF EXISTS uuid")
