"""Cross-guild Discord role mirror: mapping table + grant ledger (hub-api only).

`discord_role_mirror_mappings` -- one row per ONE-WAY mapping
`(community_id, source_guild_id, source_role_id) -> (target_guild_id, target_role_id)`. A reverse
mapping is a separate row; the API layer rejects cycles. The `last_*` columns hold the most recent
sync pass's counters so the webui can show them without a metrics backend.

`discord_role_mirror_grants` -- ledger of target roles THIS mapping actually granted to a member.
The sync pass only ever removes a target role it has a grant row for, so a role an admin assigned
by hand is never taken away. `platform`/`platform_user_id` follow the erasure-registry convention
(deleted on erasure of a linked identity); no hub user FK, since the same Discord id may be
unlinked.

Revision ID: 0099_discord_role_mirror
Revises: 0098_system_tenant_global_admin
Create Date: 2026-11-11
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0099_discord_role_mirror"
down_revision = "0098_system_tenant_global_admin"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset(
    {"discord_role_mirror_mappings", "discord_role_mirror_grants"}
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location(
        "waddles_rbac_matrix_0099", _MATRIX_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0099"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create both tables, indexes, and apply the RBAC grants."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_role_mirror_mappings (
            id BIGSERIAL PRIMARY KEY,
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            source_guild_id VARCHAR(255) NOT NULL,
            source_role_id VARCHAR(255) NOT NULL,
            target_guild_id VARCHAR(255) NOT NULL,
            target_role_id VARCHAR(255) NOT NULL,
            last_synced_at TIMESTAMPTZ,
            last_roles_added INTEGER NOT NULL DEFAULT 0,
            last_roles_removed INTEGER NOT NULL DEFAULT 0,
            last_skipped_not_member INTEGER NOT NULL DEFAULT 0,
            last_error VARCHAR(80),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_discord_role_mirror_distinct_guilds
                CHECK (source_guild_id <> target_guild_id),
            CONSTRAINT uq_discord_role_mirror_mapping
                UNIQUE (community_id, source_guild_id, source_role_id,
                        target_guild_id, target_role_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE discord_role_mirror_mappings IS "
        "'One-way cross-guild Discord role mirror mappings for one community (same tenant). "
        "Hub-api is the only writer; role-sync CronJob reconciles them.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_role_mirror_mappings_community "
        "ON discord_role_mirror_mappings (community_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_role_mirror_grants (
            id BIGSERIAL PRIMARY KEY,
            mapping_id BIGINT NOT NULL
                REFERENCES discord_role_mirror_mappings(id) ON DELETE CASCADE,
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            platform_user_id VARCHAR(255) NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_discord_role_mirror_grant UNIQUE (mapping_id, platform_user_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE discord_role_mirror_grants IS "
        "'Target roles the mirror itself granted; only these are ever removed by the sync pass.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_role_mirror_grants_user "
        "ON discord_role_mirror_grants (platform, platform_user_id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE discord_role_mirror_mappings_id_seq TO hub_api;"
    )
    op.execute("GRANT USAGE ON SEQUENCE discord_role_mirror_grants_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop both tables (grants first -- FK)."""
    op.execute("DROP TABLE IF EXISTS discord_role_mirror_grants")
    op.execute("DROP TABLE IF EXISTS discord_role_mirror_mappings")
