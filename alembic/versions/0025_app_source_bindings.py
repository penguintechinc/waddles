"""app_source_bindings -- which ingest sources an installed app consumes (spec Sec5.1/Sec9.5).

**Problem this fixes.** `bundle_approval_service.approve_version()` had no
record of which `ingest_sources` rows an approved app should read from, so
the process-stage consumer-group it provisioned (`bundle_version_service.
process_prebuilt_component()`, via `flask_core.stream_pipeline.
bundle_stream_key(tenant, None, app_id, "process")`) pointed at
`waddles:t:{tenant}:c:_tenant:app:{app_id}:process` -- a key svc-process
never reads. svc-process actually consumes each *source's own* stream,
`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events`
(penguin-spine `Scope::source_stream`, `packages/rust-spine/src/scope.rs`),
with consumer group = `app_id`. `app_source_bindings` is the missing
join: one row per (tenant, community, app, platform, source) the app is
actually granted to read, populated by AUTO-BIND at approval time
(`services/app_source_binding_service.py::sync_bindings()`, called from
`bundle_approval_service._write_approval_and_activate()` inside the same
transaction as the approval + activation) and consumed by
`approve_version()` to `ensure_group` on each bound source stream --
`bundle_version_service.py`'s own `process`-stage provisioning loop is
fixed in the same change to stop provisioning the unused key and keep
only the (correct) `action`-stage group.

`community_id INTEGER NOT NULL DEFAULT 0` mirrors `app_active_versions`'
own sentinel (migration 0022: `communities.id` is a real SERIAL starting
at 1, so 0 never collides) -- 0 means "bound at the tenant-wide
activation", matching the same sentinel `_write_approval_and_activate()`
already uses for `app_active_versions.community_id`.

The primary key doubles as the natural uniqueness constraint and as the
"replace on re-approval" delete-then-insert target (`sync_bindings()`
deletes every existing `(tenant_id, community_id, app_id)` row before
inserting the freshly-resolved set, so a re-approval with a narrowed
manifest or reconfigured `ingest_sources` never leaves a stale binding
behind).

`waddles_bundle_reader` (the Rust data-plane's read-only role for this
table, provisioned by a parallel migration on the Rust side) is granted
`SELECT` here guarded by `IF EXISTS` -- this migration runs whichever
side lands first in a given environment, so the grant must be a safe
no-op when the role doesn't exist yet, never a hard failure.

svc-process also resolves the tenant slug and community name (the
`source_stream_key` segments) itself, via its own read-only connection --
it fails closed if it can't, so `waddles_bundle_reader` additionally gets
`SELECT` on `tenants` and `communities` (pre-existing tables owned by
earlier migrations), each guarded by both the role's `IF EXISTS` and a
`to_regclass()` check on the table -- never assumes either exists yet.
`downgrade()` revokes both grants (also guarded) before the reader's own
`app_source_bindings` grant implicitly disappears with the dropped table.

Revision ID: 0025_app_source_bindings
Revises: 0024_app_versions_component_key
Create Date: 2026-09-27
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0025_app_source_bindings"
down_revision = "0024_app_versions_component_key"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"app_source_bindings"})

#: Not part of the hub-api-owned RBAC matrix (config/postgres/rbac-matrix.yaml)
#: -- this is the Rust data-plane's own reader role, provisioned by a
#: separate (parallel) migration. Guarded by IF EXISTS below so this
#: migration never depends on ordering against that one.
_BUNDLE_READER_ROLE = "waddles_bundle_reader"

#: Pre-existing tables (owned by earlier migrations, not this one)
#: svc-process's read-only connection resolves the `source_stream_key`
#: segments from directly -- see module docstring.
_BUNDLE_READER_EXTRA_TABLES = ("tenants", "communities")


def _bundle_reader_table_grant_sql(table: str) -> str:
    """Guarded `GRANT SELECT ON {table} TO waddles_bundle_reader` -- role AND table must both exist."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}')\n"
        f"     AND to_regclass('{table}') IS NOT NULL THEN\n"
        f"    GRANT SELECT ON {table} TO {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _bundle_reader_table_revoke_sql(table: str) -> str:
    """Guarded `REVOKE SELECT ON {table} FROM waddles_bundle_reader` -- the `upgrade()` grant's inverse."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}')\n"
        f"     AND to_regclass('{table}') IS NOT NULL THEN\n"
        f"    REVOKE SELECT ON {table} FROM {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0025", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0025"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_source_bindings (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL DEFAULT 0,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, community_id, app_id, platform, source_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_source_bindings IS "
        "'Which ingest_sources an approved app actually consumes (spec Sec5.1/Sec9.5) -- "
        "AUTO-BIND populates this at approval time; svc-process consumer groups are "
        "provisioned on each bound source stream, keyed by app_id'"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    # Rust data-plane reader -- not in the hub-api RBAC matrix (see module
    # docstring); guarded so this is a no-op wherever the role doesn't
    # exist yet. Also grants SELECT on tenants/communities -- svc-process
    # resolves the source_stream_key tenant-slug/community-name segments
    # itself, via this same read-only connection.
    op.execute(
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}') THEN\n"
        f"    GRANT SELECT ON app_source_bindings TO {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )
    for table in _BUNDLE_READER_EXTRA_TABLES:
        op.execute(_bundle_reader_table_grant_sql(table))


def downgrade() -> None:
    # Inverse of the extra grants above -- revoked before the table (and
    # its own implicit app_source_bindings grant) is dropped.
    for table in reversed(_BUNDLE_READER_EXTRA_TABLES):
        op.execute(_bundle_reader_table_revoke_sql(table))
    op.execute("DROP TABLE IF EXISTS app_source_bindings")
