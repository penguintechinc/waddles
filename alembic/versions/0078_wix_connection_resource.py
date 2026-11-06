"""Allow the ``wix_site`` resource type on ``platform_connections``.

The Wix provider adapter (``hub_api/services/provider_connections/wix.py``) stores a community's
Wix site (app instance) as the connection's resource. The ``resource_type`` allow-list CHECK (0035,
extended by 0064 and 0073) must name it or every Wix insert is refused by the database. Pure CHECK
swap -- no column, no data change. The new constraint is added ``NOT VALID`` then validated, so the
table is never scanned under an ACCESS EXCLUSIVE lock.

Revision ID: 0078_wix_connection_resource
Revises: 0077_stream_keys_destinations
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op

revision = "0078_wix_connection_resource"
down_revision = "0077_stream_keys_destinations"
branch_labels = None
depends_on = None

_RESOURCE_CHECK = "platform_connections_resource_type_check"
_BASE_TYPES = (
    "'discord_guild', 'twitch_channel', 'external_http', 'aws_lambda', 'gcp_function', "
    "'youtube_channel'"
)


def _swap_check(allowed: str) -> None:
    """Replace the resource_type CHECK with one allowing exactly ``allowed``."""
    op.execute(
        f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} "
        f"CHECK (resource_type IN ({allowed})) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE platform_connections VALIDATE CONSTRAINT {_RESOURCE_CHECK}"
    )


def upgrade() -> None:
    """Extend the allow-list with ``wix_site``."""
    _swap_check(f"{_BASE_TYPES}, 'wix_site'")


def downgrade() -> None:
    """Revert -- refusing (loudly) while a Wix connection exists, since it would be stranded."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections WHERE resource_type = 'wix_site') THEN
                RAISE EXCEPTION
                    '0078 downgrade refused: wix_site connections exist and the previous '
                    'release cannot represent them';
            END IF;
        END
        $$
        """
    )
    _swap_check(_BASE_TYPES)
