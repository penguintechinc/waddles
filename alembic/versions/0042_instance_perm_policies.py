"""Instance-wide permission policy layer (bundle permissions & capability gate spec).

`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
adds a policy layer ABOVE the existing 3 tiers (instance -> global catalog
approval -> tenant -> community, 2026-09-28 decision): a GLOBAL admin
(`platform:admin`) can explicitly `allow`/`deny` a permission id OR an
entire family (e.g. `net.http.private-ip`) instance-wide, with an
optional `param_scope` narrowing the policy to one parameter value (e.g.
a single CIDR under `net.http.private-ip`). One row per
`(permission_key, param_scope)` -- `permission_key` is either a full
catalog permission id (`storage.objects`) or a family prefix
(`net.http.private-ip`); `param_scope IS NULL` means "the whole key".

Seeded default: `net.http.private-ip` = `deny` (opt-in only) -- both
`net.http` IP families are `dangerous`-risk (2026-09-28 refinement), but
`private-ip` additionally defaults CLOSED at the instance level since it
is the family capable of naming this platform's own internal network.

`instance_permission_policy_audit` is the append-only audit trail every
`set_instance_policy()` call writes to (who changed what, when, and
whether the change cascaded a grant revocation) -- kept as its own table
rather than folding into the generic `bundle_audit` table so a policy
change's `previous_action`/`new_action`/`cascaded_revocations` shape
doesn't need a JSON blob to query.

Note: this migration does not (yet) register these two tables in the
RBAC matrix (`config/postgres/rbac-matrix.yaml`, migration 0041's own
pattern) -- a follow-up, tracked alongside this table's own rollout,
should add per-role grants there instead of the blanket `hub_api` grant
below.

Revision ID: 0042_instance_perm_policies
Revises: 0041_bundle_permission_grants
Create Date: 2026-09-28

Note: revision id is intentionally abbreviated to `instance_perm_policies`
rather than the fuller `instance_permission_policies` -- alembic's default
`alembic_version.version_num` column is `VARCHAR(32)`, and the longer id
overflowed it (`psycopg2.errors.StringDataRightTruncation`), breaking
`alembic upgrade head`/`downgrade` for every real-Postgres test in this
suite from this migration onward.

Renumbered 0033 -> 0042 during the #432 resurrection onto `release/v3.0.X`
(2026-10-08) to chain after 0041 (itself retargeted to release's real
head, 0040_bundle_artifact_signature -- see that migration's docstring).
"""

from __future__ import annotations

from alembic import op

revision = "0042_instance_perm_policies"
down_revision = "0041_bundle_permission_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS instance_permission_policies (
            id BIGSERIAL PRIMARY KEY,
            permission_key VARCHAR(255) NOT NULL,
            param_scope VARCHAR(255),
            action VARCHAR(10) NOT NULL CHECK (action IN ('allow', 'deny')),
            set_by INTEGER REFERENCES hub_users(id),
            set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (permission_key, param_scope)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instance_permission_policies_key "
        "ON instance_permission_policies (permission_key)"
    )
    op.execute(
        "COMMENT ON TABLE instance_permission_policies IS "
        "'Instance policy layer (spec: instance policy, above the 3 consent tiers): a "
        "GLOBAL admin allow/deny for a permission id or family, optionally scoped to one "
        "param_scope value (e.g. a single CIDR)'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS instance_permission_policy_audit (
            id BIGSERIAL PRIMARY KEY,
            permission_key VARCHAR(255) NOT NULL,
            param_scope VARCHAR(255),
            previous_action VARCHAR(10),
            new_action VARCHAR(10) NOT NULL CHECK (new_action IN ('allow', 'deny')),
            cascaded_revocations INTEGER NOT NULL DEFAULT 0,
            set_by INTEGER REFERENCES hub_users(id),
            set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE instance_permission_policy_audit IS "
        "'Append-only audit trail for every instance_permission_policies change, incl. how "
        "many existing community grants a deny-enable cascaded a revocation for'"
    )

    op.execute("GRANT SELECT, INSERT, UPDATE ON instance_permission_policies TO hub_api")
    op.execute("GRANT SELECT, INSERT ON instance_permission_policy_audit TO hub_api")
    op.execute(
        "GRANT USAGE ON SEQUENCE instance_permission_policies_id_seq TO hub_api, migration_runner"
    )
    op.execute(
        "GRANT USAGE ON SEQUENCE instance_permission_policy_audit_id_seq "
        "TO hub_api, migration_runner"
    )

    # Seeded default (spec: "net.http.private-ip is DENIED by default at instance
    # level, opt-in by a global admin"). `set_by` is NULL -- a system default, not a
    # human decision, mirrors migration 0041's `approval_source` convention.
    op.execute(
        "INSERT INTO instance_permission_policies (permission_key, param_scope, action) "
        "VALUES ('net.http.private-ip', NULL, 'deny') "
        "ON CONFLICT (permission_key, param_scope) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS instance_permission_policy_audit")
    op.execute("DROP TABLE IF EXISTS instance_permission_policies")
