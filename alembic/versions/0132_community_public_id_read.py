"""Column-scoped read of `communities.public_id` for the svc-process bundle context.

Owner decision 2026-11-18: the data plane keys on the NUMERIC community id (enumerable, never
exposed outside the platform) but a bundle building a USER-FACING link needs the community's
PUBLIC UUID (the webui rejects numeric ids in URLs). The host therefore passes the public id to
bundles in the context it already hands them, looked up from the read REPLICA by numeric id
(`core/bundle_config_db/src/community_public_id.rs`) -- the data plane reads, hub-api writes.

Grants (SELECT only, column-scoped; no write anywhere):

  - `waddles_svc_process`: `communities (id, tenant_id, public_id)` -- the lookup is filtered by
    BOTH the verified `tenant_id` and `id`, so a mismatched pair resolves to nothing. NO name,
    owner, billing or any other column: the 0055 least-privilege negative still holds for
    everything but these three. (svc-action already holds table-level `communities: SELECT`,
    pre-dating 0107, so it needs nothing here.)

The same grant is declared in `config/postgres/service-roles.yaml` (`columns:`), which
`migrations/run-alembic.sh` re-asserts with `service_roles.py reconcile --strict`; this revision
records the change in the chain and applies it immediately. Idempotent and order-independent: a
missing role or table is a no-op. Downgrade revokes exactly these column grants (no column ACL
entry is left behind, so a later `DROP ROLE` works).

Revision ID: 0132_community_public_id_read
Revises: 0131_analytics_applied_entries
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0132_community_public_id_read"
down_revision = "0131_analytics_applied_entries"
branch_labels = None
depends_on = None

#: role -> table -> columns granted SELECT. Fixed literals, never user input.
GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "waddles_svc_process": {"communities": ("id", "tenant_id", "public_id")},
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
    """Grant column-scoped SELECT on the community public-id lookup columns."""
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {role};",
                )
            )


def downgrade() -> None:
    """Revoke exactly the column grants this revision added (back to 0131's state)."""
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {role};",
                )
            )
