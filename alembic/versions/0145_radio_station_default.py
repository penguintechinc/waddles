"""Add `is_default` + `last_tested_at` to `community_radio_stations`.

The admin webui's Radio Stations page sets one default station per community
(`PUT .../music/radio-stations/<uuid>/default`) and records when a stream URL was
last reachability-tested (`POST .../music/radio-stations/<uuid>/test`). Neither
column existed on the table 0012 created.

- `is_default BOOLEAN NOT NULL DEFAULT FALSE` -- existing rows backfill to FALSE.
- `last_tested_at TIMESTAMP` -- nullable; never tested = NULL.
- `uq_radio_default_per_community` -- partial unique index so at most ONE station
  per community is default even under concurrent `set default` calls (the service
  clears the old default and sets the new one; the index makes a race fail loudly
  instead of leaving two defaults).

Idempotent (`IF NOT EXISTS`) and fresh-replay safe: a missing table is a no-op.

Revision ID: 0145_radio_station_default
Revises: 0144_ingest_owner_public_ids
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0145_radio_station_default"
down_revision = "0144_ingest_owner_public_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the default-station flag, last-tested timestamp and one-default-per-community index."""
    op.execute(
        """
        DO $$ BEGIN
          IF to_regclass('public.community_radio_stations') IS NOT NULL THEN
            ALTER TABLE community_radio_stations
              ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE community_radio_stations
              ADD COLUMN IF NOT EXISTS last_tested_at TIMESTAMP;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_radio_default_per_community
              ON community_radio_stations (community_id) WHERE is_default;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the index and both columns added by `upgrade()`."""
    op.execute("DROP INDEX IF EXISTS uq_radio_default_per_community")
    op.execute(
        """
        DO $$ BEGIN
          IF to_regclass('public.community_radio_stations') IS NOT NULL THEN
            ALTER TABLE community_radio_stations DROP COLUMN IF EXISTS last_tested_at;
            ALTER TABLE community_radio_stations DROP COLUMN IF EXISTS is_default;
          END IF;
        END $$;
        """
    )
