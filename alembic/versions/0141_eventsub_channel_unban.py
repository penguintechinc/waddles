"""Allow the `channel.unban` EventSub type (code 8) in the desired-state ledger.

Twitch-native unbans lift the matching Waddles floor ban, so hub-api now keeps a `channel.unban`
subscription per eligible Twitch connection. The ledger's `sub_type` CHECK only allowed 1..7.

Revision ID: 0141_eventsub_channel_unban
Revises: 0140_floor_ban_unban_observed
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "0141_eventsub_channel_unban"
down_revision = "0140_floor_ban_unban_observed"
branch_labels = None
depends_on = None

_TABLE = "twitch_eventsub_subscriptions"
_CONSTRAINT = f"{_TABLE}_sub_type_check"


def upgrade() -> None:
    """Widen the `sub_type` CHECK to 1..8 (idempotent)."""
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (sub_type BETWEEN 1 AND 8)"
    )


def downgrade() -> None:
    """Drop `channel.unban` rows and restore the 1..7 CHECK."""
    op.execute(f"DELETE FROM {_TABLE} WHERE sub_type = 8")  # noqa: S608
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (sub_type BETWEEN 1 AND 7)"
    )
