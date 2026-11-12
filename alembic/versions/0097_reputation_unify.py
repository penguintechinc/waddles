"""Reputation unification: single store = the bundle store.

Adds the per-community policy table, the tenant-score aggregate, ledger columns
(actor / source_event / numeric reason code, nullable app_id) and the atomic
`reputation_apply()` write function (membership + input validation, SQL-enforced caps per kind
and per app, clamp 300-850, default 600, idempotent ledger insert, row lock, floor soft-ban
auto-clear), the soft-ban functions (manual set/clear hub_api-only; floor wrappers for the bundle
role) and `reputation_active_logic_app()`. Contract: REPUTATION_CONTRACT.md (PR #1236/#1238). DDL lives in
`scripts/db/bundle_reputation_unify.sql` (shared with the Rust crate's integration test).

Data steps (run exactly once, here -- not in the shared SQL):

1. Rebase: bundle scores were 0-based balances; they become `clamp(600 + balance, 300, 850)`.
2. Backfill (idempotent, `ON CONFLICT DO NOTHING`): legacy `community_members.reputation`
   (community scope) and `reputation_tenant.score` (tenant scope) into the bundle store where
   the user's `user_uuid` resolves; skipped when the legacy tables/columns are absent or empty.
   Legacy tables are NOT dropped here (separate, post-e2e step).

Revision ID: 0097_reputation_unify
Revises: 0095_bundle_disabled_apps_view

Chain note: numbered 0097 because #1234 owns 0096_system_tenant_global_admin. Until #1234
merges this chains after 0095; if #1234 merges first, re-chain `down_revision` onto 0096.
Create Date: 2026-11-11
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0097_reputation_unify"
down_revision = "0095_bundle_disabled_apps_view"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_reputation_unify.sql"
)

_REBASE_SQL = """
UPDATE bundle_reputation_scores
   SET balance = LEAST(GREATEST(600 + balance, 300), 850), updated_at = NOW();
"""

_BACKFILL_SQL = """
DO $$
BEGIN
    IF to_regclass('public.community_members') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'community_members'
                      AND column_name = 'reputation')
       AND EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'community_members'
                      AND column_name = 'user_uuid') THEN
        INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance)
        SELECT c.tenant_id, cm.community_id, cm.user_uuid,
               LEAST(GREATEST(ROUND(cm.reputation)::int, 300), 850)
          FROM community_members cm
          JOIN communities c ON c.id = cm.community_id
         WHERE cm.user_uuid IS NOT NULL AND cm.reputation IS NOT NULL
        ON CONFLICT (community_id, user_uuid) DO NOTHING;
    END IF;

    IF to_regclass('public.reputation_tenant') IS NOT NULL
       AND to_regclass('public.community_members') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'community_members'
                      AND column_name = 'user_uuid') THEN
        INSERT INTO bundle_reputation_tenant_scores
            (tenant_id, user_uuid, score, event_count, last_event_at)
        SELECT rt.tenant_id, m.user_uuid, LEAST(GREATEST(rt.score, 300), 850),
               COALESCE(rt.total_events, 0), rt.last_event_at
          FROM reputation_tenant rt
          JOIN LATERAL (
              SELECT cm.user_uuid
                FROM community_members cm
                JOIN communities c ON c.id = cm.community_id AND c.tenant_id = rt.tenant_id
               WHERE cm.user_id = rt.hub_user_id::text AND cm.user_uuid IS NOT NULL
               LIMIT 1
          ) m ON TRUE
        ON CONFLICT (tenant_id, user_uuid) DO NOTHING;
    END IF;
END
$$;
"""


def upgrade() -> None:
    """Apply the unify DDL, rebase 0-based bundle balances, then backfill legacy scores."""
    bind = op.get_bind()
    # exec_driver_sql: multi-statement script with `$$` blocks; bypass text() bind parsing.
    bind.exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))
    bind.exec_driver_sql(_REBASE_SQL)
    bind.exec_driver_sql(_BACKFILL_SQL)


def downgrade() -> None:
    """Drop the functions, new tables and ledger columns (the 0-based rebase is not reversed)."""
    for sig in (
        "reputation_apply(bigint, bigint, uuid, int, smallint, smallint, text, text, text, text)",
        "reputation_is_soft_banned(bigint, bigint, uuid)",
        "reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz)",
        "reputation_clear_soft_ban(bigint, bigint, uuid, text, bool)",
        "reputation_set_floor_soft_ban(bigint, bigint, uuid)",
        "reputation_clear_floor_soft_ban(bigint, bigint, uuid)",
        "reputation_active_logic_app(bigint)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_soft_bans")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_tenant_scores")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_policy")
    op.execute("DROP TABLE IF EXISTS bundle_reputation_tenant_policy")
    op.execute("DROP INDEX IF EXISTS idx_bundle_reputation_adjustments_kind_window")
    op.execute("DROP INDEX IF EXISTS uq_bundle_reputation_adjustments_idem_noapp")
    op.execute(
        "DELETE FROM bundle_reputation_adjustments WHERE kind IN ('soft_ban', 'soft_ban_clear', 'moderation')"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_adjustments "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_adjustments_kind"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_adjustments ADD CONSTRAINT "
        "ck_bundle_reputation_adjustments_kind CHECK (kind IN ('adjust', 'admin_override'))"
    )
    for col in ("reason_code_id", "source_event", "actor"):
        op.execute(
            f"ALTER TABLE bundle_reputation_adjustments DROP COLUMN IF EXISTS {col}"
        )
    op.execute("DELETE FROM bundle_reputation_adjustments WHERE app_id IS NULL")
    op.execute(
        "ALTER TABLE bundle_reputation_adjustments ALTER COLUMN app_id SET NOT NULL"
    )
