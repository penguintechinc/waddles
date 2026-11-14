"""call_rooms -- hub-api's own record of the call rooms it created (and each community's Town Hall).

svc-streaming's `Room` carries only `locked`; the facts the access policy needs (kind, closed,
guest opt-in) are not visible to hub-api, and the old REST join treated every room as a plain
community room (fail-open). hub-api now records every room it creates here and the REST join
trusts ONLY these facts: a room not recorded is refused to non-staff (fail closed).

`kind` is an int code (0 community, 1 event, 2 breakout, 3 stage, 4 green_room; matches the
grants contract vocabulary order). `town_hall` marks the community's persistent default room;
the partial unique index allows at most ONE per community, which is what makes concurrent
Town Hall creation race-safe (the loser's insert conflicts and it deletes only its own room).
Nothing here is PII (opaque ids and flags only).

Revision ID: 0110_call_rooms
Revises: 0107_bundle_config_db_reads
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0110_call_rooms"
down_revision = "0107_bundle_config_db_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the table and its indexes (idempotent)."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS call_rooms (
            id            BIGSERIAL PRIMARY KEY,
            community_id  INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            room_id       UUID NOT NULL,
            kind          SMALLINT NOT NULL DEFAULT 0 CHECK (kind BETWEEN 0 AND 4),
            town_hall     BOOLEAN NOT NULL DEFAULT FALSE,
            closed        BOOLEAN NOT NULL DEFAULT FALSE,
            allow_guests  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_call_rooms_room UNIQUE (room_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_call_rooms_one_town_hall "
        "ON call_rooms (community_id) WHERE town_hall"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_call_rooms_community ON call_rooms (community_id)"
    )


def downgrade() -> None:
    """Drop the table (the rooms themselves live in svc-streaming and are untouched)."""
    op.execute("DROP TABLE IF EXISTS call_rooms")
