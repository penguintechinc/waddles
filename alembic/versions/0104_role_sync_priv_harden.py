"""Role-sync privilege hardening (v3.0 security fix, user decision 2026-11-12).

Three changes, all on the role-sync tables 0034/0036 created:

1. **Inbound (Discord -> Waddles) targets are a non-privileged allowlist.** A `community_role`
   binding may only target `vip` or `member`. Before this, anyone holding Manage Roles in a
   paired guild could give themselves the bound Discord role and be promoted to Waddles
   `community-admin` / `moderator`. Existing privileged inbound bindings are DELETED here (they
   are the vulnerability; there is no safe way to keep them), then a CHECK constraint makes the
   DB refuse them for good. `services/guild_pairing.py` refuses them at write time too.

2. **Grant ledgers.** Sync now records what it granted and removes only its own grants:
   - `role_sync_community_grants` -- one row per (community, hub user) whose member role
     role-sync raised, with the role it replaced (`prior_role`) so a revoke restores it.
   - `role_sync_discord_grants` -- Discord roles the Twitch -> Discord pass added; that pass only
     ever removes a role it has a row for (same pattern as 0099's `discord_role_mirror_grants`).

3. **`guild_tenant_pairings.ownership_verified_at`** -- set when a NEW pairing passes the guild
   control proof; NULL for pre-existing pairings, which are grandfathered (WARN + metric).

4. **`guild_tenant_pairings.propagate_moderators`** (default FALSE). Twitch moderators are copied
   onto the Discord `moderator` binding only when a pairing explicitly opts in -- one platform's
   moderator must not affect the community by default.

**Rechain note:** open PRs at the time of writing use 0100-0103; whichever lands first, the
others must rechain `down_revision` so heads == 1.

Revision ID: 0104_role_sync_priv_harden
Revises: 0099_discord_role_mirror
Create Date: 2026-11-12
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0104_role_sync_priv_harden"
down_revision = "0099_discord_role_mirror"
branch_labels = None
depends_on = None

#: Mirrors `services/guild_pairing.py::INBOUND_COMMUNITY_ROLES` (guarded by a test).
INBOUND_COMMUNITY_ROLES = ("vip", "member")

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"role_sync_community_grants", "role_sync_discord_grants"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0104", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0104"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Purge + forbid privileged inbound bindings, add the grant ledgers and the mod opt-in."""
    allowed = ", ".join(f"'{r}'" for r in INBOUND_COMMUNITY_ROLES)
    op.execute(  # constant allowlist, no user input
        "DELETE FROM community_role_sync_bindings "
        f"WHERE sync_scope = 'community_role' AND community_role NOT IN ({allowed})"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "chk_role_sync_binding_inbound_nonpriv CHECK ("
        f"sync_scope <> 'community_role' OR community_role IN ({allowed}))"
    )

    op.execute(
        "ALTER TABLE guild_tenant_pairings "
        "ADD COLUMN IF NOT EXISTS propagate_moderators BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Pairings created before this migration were never ownership-proven: they are
    # GRANDFATHERED (NULL here -> role-sync keeps running them, logs WARN + counts a metric);
    # only NEW pairings must pass `services/guild_ownership.verify_guild_control()`.
    op.execute(
        "ALTER TABLE guild_tenant_pairings "
        "ADD COLUMN IF NOT EXISTS ownership_verified_at TIMESTAMPTZ"
    )
    op.execute(
        "COMMENT ON COLUMN guild_tenant_pairings.propagate_moderators IS "
        "'Opt-in: copy Twitch moderators onto the Discord moderator binding. Default off.'"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS role_sync_community_grants (
            id BIGSERIAL PRIMARY KEY,
            pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
            granted_role VARCHAR(20) NOT NULL,
            prior_role VARCHAR(20),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_role_sync_grant_nonpriv CHECK (granted_role IN ({allowed})),
            CONSTRAINT uq_role_sync_community_grant UNIQUE (community_id, hub_user_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE role_sync_community_grants IS "
        "'community_members.role values role-sync raised; only these are ever revoked by sync.'"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS role_sync_discord_grants (
            id BIGSERIAL PRIMARY KEY,
            pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
            discord_role_id VARCHAR(255) NOT NULL,
            platform VARCHAR(50) NOT NULL DEFAULT 'discord',
            platform_user_id VARCHAR(255) NOT NULL,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_role_sync_discord_grant
                UNIQUE (pairing_id, discord_role_id, platform_user_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE role_sync_discord_grants IS "
        "'Discord roles the Twitch->Discord pass added; only these are ever removed by it.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_role_sync_discord_grants_user "
        "ON role_sync_discord_grants (platform, platform_user_id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)
    op.execute("GRANT USAGE ON SEQUENCE role_sync_community_grants_id_seq TO hub_api;")
    op.execute("GRANT USAGE ON SEQUENCE role_sync_discord_grants_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the ledgers, the opt-in column and the CHECK (purged bindings are NOT restored)."""
    op.execute("DROP TABLE IF EXISTS role_sync_discord_grants")
    op.execute("DROP TABLE IF EXISTS role_sync_community_grants")
    op.execute("ALTER TABLE guild_tenant_pairings DROP COLUMN IF EXISTS propagate_moderators")
    op.execute("ALTER TABLE guild_tenant_pairings DROP COLUMN IF EXISTS ownership_verified_at")
    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "chk_role_sync_binding_inbound_nonpriv"
    )
