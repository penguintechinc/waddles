"""Allow the ``youtube_channel`` resource type on ``platform_connections``.

The YouTube provider adapter (``hub_api/services/provider_connections/youtube.py``) stores a
community's authorised YouTube channel as the connection's resource. The ``resource_type``
allow-list CHECK (0035, extended by 0064) must name it or every YouTube insert is refused by the
database. Pure CHECK swap -- no column, no data change. The new constraint is added ``NOT VALID``
then validated, so the table is never scanned under an ACCESS EXCLUSIVE lock.

Revision ID: 0073_youtube_connection_resource
Revises: 0072_reputation_host_ops
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision = "0073_youtube_connection_resource"
down_revision = "0072_reputation_host_ops"
branch_labels = None
depends_on = None

_RESOURCE_CHECK = "platform_connections_resource_type_check"
_BASE_TYPES = "'discord_guild', 'twitch_channel', 'external_http', 'aws_lambda', 'gcp_function'"


def _swap_check(allowed: str) -> None:
    """Replace the resource_type CHECK with one allowing exactly ``allowed``."""
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}")
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} "
        f"CHECK (resource_type IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE platform_connections VALIDATE CONSTRAINT {_RESOURCE_CHECK}")


def upgrade() -> None:
    """Extend the allow-list with ``youtube_channel``."""
    _swap_check(f"{_BASE_TYPES}, 'youtube_channel'")


def downgrade() -> None:
    """Revert -- refusing (loudly) while a YouTube connection exists, since it would be stranded."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections WHERE resource_type = 'youtube_channel') THEN
                RAISE EXCEPTION
                    '0073 downgrade refused: youtube_channel connections exist and the previous '
                    'release cannot represent them';
            END IF;
        END
        $$
        """
    )
    _swap_check(_BASE_TYPES)
