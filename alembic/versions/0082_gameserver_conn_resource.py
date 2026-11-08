"""Allow the ``gameserver`` resource type on ``platform_connections``.

The game-server provider adapter (``hub_api/services/provider_connections/gameserver.py``) stores a
registered game server (opaque server id) as the connection resource. The ``resource_type``
allow-list CHECK (0035, extended by 0064, 0073, 0078_wix and 0081) must name it or every
game-server insert
is refused by the database. Pure CHECK swap -- no column, no data change. The new constraint
is added
``NOT VALID`` then validated, so the table is never scanned under an ACCESS EXCLUSIVE lock.

Revision ID: 0082_gameserver_conn_resource
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0082_gameserver_conn_resource"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None

_RESOURCE_CHECK = "platform_connections_resource_type_check"
_PRIOR_TYPES = (
    "'discord_guild', 'twitch_channel', 'external_http', 'aws_lambda', 'gcp_function', "
    "'youtube_channel', 'wix_site', 'spectrum_community'"
)


def _swap_check(allowed: str) -> None:
    """Replace the resource_type CHECK with one allowing exactly ``allowed``."""
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}")
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} "
        f"CHECK (resource_type IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE platform_connections VALIDATE CONSTRAINT {_RESOURCE_CHECK}")


def upgrade() -> None:
    """Extend the allow-list with ``gameserver``."""
    _swap_check(f"{_PRIOR_TYPES}, 'gameserver'")


def downgrade() -> None:
    """Revert, refusing (loudly) while a game-server connection exists (it would be stranded)."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections WHERE resource_type = 'gameserver') THEN
                RAISE EXCEPTION
                    '0082 downgrade refused: gameserver connections exist and the previous '
                    'release cannot represent them';
            END IF;
        END
        $$
        """
    )
    _swap_check(_PRIOR_TYPES)
