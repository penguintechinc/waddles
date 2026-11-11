"""Delete the demo-seed legacy `hub_modules` rows (discord_bot / twitch_bot) idempotently.

`config/postgres/migrations/095_demo_seed.sql` (an applied SQL migration, not edited) seeded
`discord_bot` and `twitch_bot` rows into the legacy `hub_modules` table plus demo installations
and reviews. They are demo data for the removed "modules" model (superseded by app bundles), so
this removes them. Dependent `hub_module_installations` / `hub_module_reviews` rows go first;
only the two named seed rows are touched, and the whole step is a no-op when a table is absent
or the rows are already gone. The legacy tables themselves are retained.

Revision ID: 0093_drop_demo_legacy_mods
Revises: 0092_catalog_bundles_only
Create Date: 2026-11-11

Rebase risk: chained after 0092 (this PR). Re-point `down_revision` if another migration lands
ahead of it on the release branch.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0093_drop_demo_legacy_mods"
down_revision = "0092_catalog_bundles_only"
branch_labels = None
depends_on = None


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    """True when `public.<name>` exists (the legacy tables are absent on minimal bootstraps)."""
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{name}"}
        ).scalar()
    )


def upgrade() -> None:
    """Remove the demo discord_bot/twitch_bot legacy module rows and their dependents."""
    conn = op.get_bind()
    if not _has_table(conn, "hub_modules"):
        return
    ids_sql = "SELECT id FROM hub_modules WHERE name IN ('discord_bot', 'twitch_bot')"
    for dependent in ("hub_module_installations", "hub_module_reviews"):
        if _has_table(conn, dependent):
            op.execute(f"DELETE FROM {dependent} WHERE module_id IN ({ids_sql})")
    op.execute("DELETE FROM hub_modules WHERE name IN ('discord_bot', 'twitch_bot')")


def downgrade() -> None:
    """Irreversible data cleanup: demo rows are not restored (re-run 095_demo_seed.sql if needed)."""
