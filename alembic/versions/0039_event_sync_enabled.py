"""Event-Discord-sync: `guild_tenant_pairings.event_sync_enabled` + `calendar_event_discord_syncs`.

**Migration-number collision note (2026-10-05, updated post-rebase).** At
the time this was written, `release/v3.0.X`'s true head was
`0035_connection_model_layers`, but two numbers an owner-confirmed
instruction explicitly flagged as "already taken" -- `0036` (PR #639,
`feature/role-sync-bidirectional`) and config/postgres `097` (PR #640,
`feature/reputation-community-tenant-scope`) -- were claimed by still-open
PRs, along with several OTHER open PRs independently claiming `0036`-
`0038` / `097` for their own still-unmerged work (`#442` keystore, `#464`
connector-pii-reader, `#501` guild-pairing, `#622` poll-bundle).
Concurrent branches racing for the same next-free Alembic number is
expected and resolved at merge/rebase time, not avoidable by checking
once up front; `0039`/config `098` were picked as the highest free
numbers observed at write time to minimize (not guarantee) a second
collision.

PR #639 (`0036_role_sync_community_role`) has since merged to
`release/v3.0.X` ahead of this branch, making `0036` -- not `0035` --
the current single alembic head. This migration's `down_revision` was
re-parented from `0035_connection_model_layers` to
`0036_role_sync_community_role` to resolve the resulting multi-head
chain (gh-643); `098` remained free (`097` was consumed by reputation,
PR #640) so no renumbering was needed on the config/postgres side.

**Scope.** Two independent additions for the Discord event-sync push
engine (`hub_api/services/event_discord_sync_service.py`):

1. `guild_tenant_pairings.event_sync_enabled` -- a SECOND, independent
   opt-in column alongside migration 0034's `sync_enabled` (role-sync).
   A pairing may have role-sync on with event-sync off, or vice versa,
   or both -- these are two unrelated features sharing one guild<->
   community pairing row, not a shared toggle.

2. `calendar_event_discord_syncs` -- new table, per-`(event_id,
   pairing_id)` sync state. `calendar_events` (legacy, NOT owned by this
   migration -- see `hub_api/services/schema.py::bind_calendar_sync_
   tables()`'s own docstring) has only ONE `discord_event_id`/
   `sync_status`/`sync_error` column set, which cannot represent "this
   event is live on guild A's Discord but still pending on guild B's" --
   the multi-guild fan-out this engine's `run_event_sync_reconcile_batch`
   and `sync_event()` both require (one community may pair with N
   guilds, each independently `event_sync_enabled`). This table is the
   per-guild source of truth; `calendar_events`' own three columns are
   kept in sync as a single-value aggregate (first successful guild's
   id; `sync_error` if ANY guild failed) for `calendar_service.py::
   EventInfo` callers that only know about one Discord event per
   WaddleBot event.

RBAC: `hub_api` is the sole writer for `calendar_event_discord_syncs`
(control-plane table, same convention as 0034's three tables) -- every
other role gets an explicit empty-privilege row per spec D28.

Revision ID: 0039_event_sync_enabled
Revises: 0036_role_sync_community_role
Create Date: 2026-10-05
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0039_event_sync_enabled"
down_revision = "0036_role_sync_community_role"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"calendar_event_discord_syncs"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0039", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0039"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    # -- guild_tenant_pairings.event_sync_enabled --------------------------
    op.execute(
        "ALTER TABLE guild_tenant_pairings "
        "ADD COLUMN IF NOT EXISTS event_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "COMMENT ON COLUMN guild_tenant_pairings.event_sync_enabled IS "
        "'Opt-in for the Discord event-sync push engine, independent of sync_enabled "
        "(role-sync) on the same pairing row.'"
    )

    # -- calendar_event_discord_syncs ---------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_event_discord_syncs (
            id BIGSERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
            discord_guild_id VARCHAR(255) NOT NULL,
            discord_event_id VARCHAR(255),
            sync_status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (sync_status IN ('pending', 'synced', 'sync_error')),
            sync_error TEXT,
            last_sync_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (event_id, pairing_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE calendar_event_discord_syncs IS "
        "'Per-guild Discord scheduled-event sync state for one calendar_events row -- "
        "the multi-guild fan-out state calendar_events own single discord_event_id/ "
        "sync_status/sync_error columns cannot represent.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_event_discord_syncs_event "
        "ON calendar_event_discord_syncs (event_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_event_discord_syncs_pending "
        "ON calendar_event_discord_syncs (sync_status) "
        "WHERE sync_status IN ('pending', 'sync_error')"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE calendar_event_discord_syncs_id_seq TO hub_api;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calendar_event_discord_syncs")
    op.execute("ALTER TABLE guild_tenant_pairings DROP COLUMN IF EXISTS event_sync_enabled")
