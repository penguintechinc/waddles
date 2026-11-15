"""Column-scoped read-only access for the command-latency label registries.

Owner decision 2026-11-15: the `command`, `bundle` and `bundle_lang` metric labels are sourced from
the registered commands / app bundles in the database READ REPLICA (not env lists), under each
stage's own per-service role, with no hub-api call and no primary read
(`core/bundle_config_db/src/labels.rs`, `core/command_latency/src/labels.rs`).

Grants (SELECT only, column-scoped; no INSERT/UPDATE/DELETE anywhere):

  - `waddles_svc_process`: `app_commands (command)` -- the registered command names. NO tenant,
    community, app, platform or description column; the reader never learns who registered what.
  - `waddles_svc_action`: `app_versions (app_id, language)` -- on top of 0107's `(id)`; the language
    of each bundle's newest version. NO digest, scan or builder column.

The same grants are declared in `config/postgres/service-roles.yaml` (`columns:`), which
`migrations/run-alembic.sh` re-asserts with `service_roles.py reconcile --strict`; this revision
records the change in the chain and applies it immediately. Idempotent and order-independent: a
missing role or table is a no-op. Downgrade revokes exactly these column grants (no column ACL
entry is left behind, so a later `DROP ROLE` works).

Revision ID: 0112_latency_label_reads
Revises: 0110_merge_precompile_vault
Create Date: 2026-11-15
"""

from __future__ import annotations

from alembic import op

revision = "0112_latency_label_reads"
down_revision = "0110_merge_precompile_vault"
branch_labels = None
depends_on = None

#: role -> table -> columns granted SELECT. Fixed literals, never user input.
GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "waddles_svc_process": {"app_commands": ("command",)},
    "waddles_svc_action": {"app_versions": ("app_id", "language")},
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
    """Grant column-scoped SELECT on the label-registry columns."""
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
    """Revoke exactly the column grants this revision added (back to 0110's state)."""
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {role};",
                )
            )
