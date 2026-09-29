"""`waddles_connector_pii_reader` RO role -- column-scoped identity reads for `identity.lookup`.

Spec `docs/superpowers/specs/2026-09-28-connector-bundles.md` S3.3/S3.4
(design landed on `docs/connector-bundles-design`, not yet merged into
this branch's base): the `identity.lookup` host capability (spec S1,
gated by `connector.pii.read`, `core/bundle_executor/src/manifest.rs`'s
`may_link_identity`) needs a dedicated RO-replica Postgres role that can
read ONLY the identity columns a connector needs -- never any other PII
column (email, IP, payment, address) and never a non-identity table.

**Depends on `0033_hub_users_identity_uuid` (PR #434, `feature/users-uuid-token`)
for `hub_users.uuid` -- this migration does NOT add or backfill that
column.** An earlier version of this file did add/backfill `hub_users.uuid`
directly; that duplicated PR #434's `0033_hub_users_identity_uuid`
(`ALTER TABLE hub_users ADD COLUMN ... uuid UUID`, backfill, `UNIQUE`
constraint) and would conflict with it at merge. This migration now only
creates the new role and grants column-scoped `SELECT` on the identity
columns those two migrations, taken together, already establish:

  - `hub_users` -- `id` (join key) and `uuid` (0033's external-safe
    token; `identity-record.uuid: string` in
    `wit/waddle-connector/connector.wit`).
  - `hub_user_identities` -- the platform-identity mapping
    (`hub_user_id -> (platform, platform_user_id, platform_username)`)
    inbound tokenization resolves through.
  - `community_members` -- per-community `display_name` (outbound mention
    rendering wants a display name, not just a bare handle). Note this is
    a **direct, PII-exposing** grant, deliberately distinct from 0033's
    own `community_member_identities` view (which excludes `display_name`
    on purpose, for the PII-free `waddles_bundle_reader` role) -- spec
    S3.3's whole point is that `waddles_connector_pii_reader` IS allowed
    to read PII, unlike `waddles_bundle_reader`.

Grants are column-scoped `GRANT SELECT (...)` statements, hand-written in
this migration rather than routed through `scripts/db/rbac_matrix.py` --
that generator's `GrantSpec`/`render_grant_sql` only knows table-level
privileges (`ALL_PRIVILEGES` applied to a whole table), so it cannot
express "SELECT on these three columns only". This mirrors the existing,
already-established precedent for this exact class of role:
`waddles_bundle_reader` (0033/0025) is likewise NOT part of
`config/postgres/rbac-matrix.yaml`'s `roles:` list and is granted via
hand-written, idempotent `DO $$ ... $$` blocks in its own migration -- this
migration follows that same convention for the new role.

**Down-revision chain / renumbering note.** This branch's own
`alembic/versions/` only goes up to `0028_bundle_active_set_changelog`
(0029-0035 are queued on separate, not-yet-merged branches: 0033
`feature/users-uuid-token` PR #434, 0034 `fix/seeder-stalled-upload-
recovery` PR #435, 0035 `feature/tenant-dek-broker` PR #442). `down_revision`
below is set to `0035_keystore_tenant_dek` per the intended merge order;
**this WILL need re-chaining if the actual merged order on
`release/v3.0.X` differs** -- same caveat `0035_keystore_tenant_dek`'s own
docstring already carries for its own position in this same queue.

Revision ID: 0036_connector_pii_reader_role
Revises: 0035_keystore_tenant_dek
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "0036_connector_pii_reader_role"
down_revision = "0035_keystore_tenant_dek"
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

    Same idempotent-and-order-independent posture as `0028_bundle_active_
    set_changelog`'s `_bundle_reader_grant_sql`/`_bundle_reader_revoke_sql`
    and `0033_hub_users_identity_uuid`'s own `waddles_bundle_reader` grant:
    this migration may run before or after whatever future migration
    provisions the login role that becomes a member of `_ROLE` (helm
    auto-provision, PR #445) -- both orders must be safe no-ops on the
    missing side.
    """
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN\n"
        f"    {body}\n"
        f"  END IF;\n"
        f"END $$;"
    )


def upgrade() -> None:
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
    # `uuid` itself is 0033_hub_users_identity_uuid's column, not this
    # migration's -- this migration only grants access to it.
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
    # Idempotent/harmless to repeat even though 0033 may already have run
    # an equivalent REVOKE for its own role's tables.
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
