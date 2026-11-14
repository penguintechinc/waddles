"""Column-scoped read-only access to bundle community config for svc-process and svc-action.

User decision 2026-11-12 (issue #1306): hub-api is the control-plane writer; the data plane reads
the database directly -- from the read-only replica -- and never calls hub-api on the hot path.
svc-process and svc-action load per-bundle community config (`get-context().config_json`) with
ONE batched statement per warm-up (`core/bundle_config_db/src/lib.rs::CONFIG_SQL`) under
their own per-service roles (`waddles_svc_process`, `waddles_svc_action`; created by 0055 and
already each pod's only database identity), exactly the shape of svc-ingest's display-name reader
(0101). No role is created here, so downgrade has no role to drop: it only has to REVOKE exactly
the column grants below, after which the roles carry no dependency on these tables.

Grants (SELECT only, column-scoped; never `updated_by`, `schema_sha256`, `created_at`, the
upload staging keys, `artifact_digest` or any other column; no INSERT/UPDATE/DELETE anywhere):

  - `bundle_community_config (tenant_id, community_id, app_id, version, config)` -- the saved,
    schema-validated per-community settings (highest version live).
  - `app_active_versions (app_id, tenant_id, community_id, version_id)` -- which version is
    active for the scope (a community activation beats the tenant-wide `community_id = 0` one).
  - `app_versions (id)` -- join key only.
  - `app_version_uploads (id, app_version_id, manifest_json)` -- the published manifest, read for
    `community_config_schema.properties.*.default` (public bundle metadata, no secrets).
  - `communities (id, tenant_id)` -- the tenant gate (svc-process only: svc-action already holds
    table-level `communities: SELECT` in `config/postgres/service-roles.yaml`).

The same grants are declared in `config/postgres/service-roles.yaml` (`columns:`), which
`migrations/run-alembic.sh` re-asserts with `service_roles.py reconcile --strict` after every
upgrade (it REVOKEs any column grant the catalog does not list, so the catalog must stay in
step); this migration records the change in the chain and applies it immediately. Idempotent and
order-independent: a missing role or table is a no-op (the minimal CI chain does not create every
table, and a fresh replay creates the roles later).

Revision ID: 0106_bundle_config_db_reads
Revises: 0105_twitch_handle_freshness
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0106_bundle_config_db_reads"
down_revision = "0105_twitch_handle_freshness"
branch_labels = None
depends_on = None

#: table -> columns granted SELECT, shared by both roles. Fixed literals, never user input.
_CORE: dict[str, tuple[str, ...]] = {
    "bundle_community_config": (
        "tenant_id",
        "community_id",
        "app_id",
        "version",
        "config",
    ),
    "app_active_versions": ("app_id", "tenant_id", "community_id", "version_id"),
    "app_versions": ("id",),
    "app_version_uploads": ("id", "app_version_id", "manifest_json"),
}

#: role -> table -> columns. svc-action keeps its table-level `communities` SELECT instead.
GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "waddles_svc_process": {**_CORE, "communities": ("id", "tenant_id")},
    "waddles_svc_action": dict(_CORE),
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
    """Grant column-scoped SELECT on the bundle-config read columns to the two stage roles."""
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
    """Revoke exactly the column grants this revision added (back to 0105's state).

    Column-level REVOKE removes the `pg_attribute.attacl` entries, so nothing is left behind that
    would make a later `DROP ROLE` fail with "privileges for column ... of table ...".
    """
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {role};",
                )
            )
