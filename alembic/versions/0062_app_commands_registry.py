"""Slash-command registry: `app_commands` (Phase 1, hub-api is the only writer).

One row per command an installed app bundle declares, scoped to where it was registered:

- ``tenant_id`` / ``community_id`` -- the owning scope. ``community_id = 0`` is the
  tenant-wide sentinel (no FK to communities -- there is no community row 0).
- ``app_id`` -- the owning bundle (FK to ``app_catalog``).
- ``command`` -- the command token (lowercase, ``[a-z0-9_-]``, max 32) or the ``*``
  wildcard (an app that handles every command it is routed).
- ``command_types`` -- int bitmask, 1 = standard, 2 = webhook (3 = both); never a string.
- ``platforms`` -- non-empty JSON array of platform names the command is offered on.
- ``description`` -- short user-facing text (<= 100 chars), display only.
- ``registered_at`` -- with ``id`` this orders ownership: when two apps claim the same
  command in the same scope the earliest ``(registered_at, id)`` wins.

Hub-api-only privileges (explicit empty rows for every other role in
`config/postgres/rbac-matrix.yaml`); Phase 2 widens `svc_ingest` to read.

Revision ID: 0062_app_commands_registry
Revises: 0061_port_legacy_sql_103_codec
Create Date: 2026-10-23
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0062_app_commands_registry"
down_revision = "0061_port_legacy_sql_103_codec"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"app_commands"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0062", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0062"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create `app_commands`, its owner index, and apply the RBAC matrix grants (hub-api only)."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_commands (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL DEFAULT 0,
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            command VARCHAR(32) NOT NULL
                CHECK (command = '*' OR command ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
            command_types SMALLINT NOT NULL DEFAULT 1
                CHECK (command_types BETWEEN 1 AND 3),
            platforms JSONB NOT NULL
                CHECK (jsonb_typeof(platforms) = 'array' AND jsonb_array_length(platforms) > 0),
            description VARCHAR(100),
            registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_app_commands_scope_app_command
                UNIQUE (tenant_id, community_id, app_id, command)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE app_commands IS "
        "'Slash-command registry: one row per command an app bundle declares. Hub-api is the "
        "only writer (Phase 1). community_id 0 = tenant-wide sentinel. Ownership of a contested "
        "command = earliest (registered_at, id) in the (tenant_id, community_id) scope. "
        "command_types is an int bitmask: 1 standard, 2 webhook.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_commands_owner "
        "ON app_commands (tenant_id, community_id, command, registered_at, id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE app_commands_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the registry (derived state: it is rebuilt from bundle manifests)."""
    op.execute("DROP TABLE IF EXISTS app_commands")
