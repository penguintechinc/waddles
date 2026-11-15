"""Least privilege: svc-process must not reach `communities` (revokes part of 0107).

0107_bundle_config_db_reads gave `waddles_svc_process` column-level `SELECT (id, tenant_id)` on
`communities` for the reader's community-belongs-to-tenant check. The per-service least-privilege
negative test (`test_0055_per_service_db_roles.py::test_a_role_cannot_reach_another_services_tables`)
says svc-process must NOT read `communities`, and the check is not needed: the `(tenant, community)`
pair is the stage's own verified envelope scope and every other table the reader touches
(`app_active_versions`, `bundle_community_config`) is filtered by that `tenant_id`, so a scope can
only ever see its own tenant's defaults and saved config (see
`core/bundle_config_db/src/lib.rs`, "Tenant isolation without `communities`"). The reader statement
no longer joins `communities`, and this revision removes the now-unused grant.

0107 is already applied on alpha, so this is a NEW revision rather than an edit of 0107. svc-action is
untouched: its table-level `communities: SELECT` predates 0107 (catalog, 0055).

Downgrade restores exactly the column grants this revision removed (back to 0107's state); a further
downgrade to 0106 revokes them again via 0107, leaving no column ACL behind so `DROP ROLE` works.
Idempotent and order-independent: a missing role or table is a no-op.

Revision ID: 0108_revoke_cfg_communities
Revises: 0107_bundle_config_db_reads
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0108_revoke_cfg_communities"
down_revision = "0107_bundle_config_db_reads"
branch_labels = None
depends_on = None

#: role -> table -> columns whose SELECT is revoked. Fixed literals, never user input.
REVOKED: dict[str, dict[str, tuple[str, ...]]] = {
    "waddles_svc_process": {"communities": ("id", "tenant_id")},
}


def _guarded(role: str, table: str, body: str) -> str:
    """Run `body` only if both `role` and `table` exist (fresh-replay / order safe)."""
    return (
        "DO $$ BEGIN\n"  # nosec B608 -- fixed literals only
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')\n"
        f"     AND to_regclass('public.{table}') IS NOT NULL THEN\n"
        f"    {body}\n"
        "  END IF;\n"
        "END $$;"
    )


def upgrade() -> None:
    """Revoke svc-process's column grants on `communities` (the reader no longer needs them)."""
    for role, tables in REVOKED.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {role};",
                )
            )


def downgrade() -> None:
    """Restore the column grants this revision removed (back to 0107's state)."""
    for role, tables in REVOKED.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {role};",
                )
            )
