"""Wix sealed API credential column + ``soundcloud_app`` / ``youtube_api_key`` resource types.

Also extends the ``resource_type`` allow-list (0035 ... 0081) with the two keyed music providers
(``hub_api/services/provider_connections/music_keys.py``).

Adds ``platform_connections.sealed_api_credential`` -- a sealed outbound API credential.

A Wix connection row (kind 1) holds the app PUBLIC key as its ``access_token`` (webhook verification)
but outbound Wix API calls need a separate secret (API key + site id). That second credential is
AES-256-GCM sealed under the row-bound AAD (``conn:<public_id>:<platform>``) into this column. It is
write-only through the admin API and reaches the data plane only via the ``connections:resolve:static``
resolver path. The existing ``secret_shape`` CHECK keeps ``sealed_secret`` NULL on kind 1, so a new
column (rather than overloading ``sealed_secret``) preserves that invariant.

Revision ID: 0082_wix_api_credential
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0082_wix_api_credential"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None

_CHECK = "platform_connections_api_credential_kind_check"
_RESOURCE_CHECK = "platform_connections_resource_type_check"
_PRIOR_TYPES = (
    "'discord_guild', 'twitch_channel', 'external_http', 'aws_lambda', 'gcp_function', "
    "'youtube_channel', 'wix_site', 'spectrum_community'"
)
_NEW_TYPES = ("soundcloud_app", "youtube_api_key")


def _swap_check(allowed: str) -> None:
    """Replace the resource_type CHECK (NOT VALID then VALIDATE: no long exclusive lock)."""
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
    """Add the nullable sealed column (kind-1 only) and extend the resource-type allow-list."""
    new = ", ".join(f"'{name}'" for name in _NEW_TYPES)
    _swap_check(f"{_PRIOR_TYPES}, {new}")
    op.execute(
        "ALTER TABLE platform_connections ADD COLUMN IF NOT EXISTS sealed_api_credential TEXT"
    )
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_CHECK} "
        "CHECK (sealed_api_credential IS NULL OR connection_kind = 1)"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.sealed_api_credential IS "
        "'AES-256-GCM (row-bound AAD) JSON {api_key, site_id} of an outbound API credential; "
        "never returned by an admin route'"
    )


def downgrade() -> None:
    """Revert, refusing loudly while a row that the previous release cannot represent exists."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections
                       WHERE resource_type IN ('soundcloud_app', 'youtube_api_key')
                          OR sealed_api_credential IS NOT NULL) THEN
                RAISE EXCEPTION
                    '0082 downgrade refused: soundcloud/youtube_api connections or sealed Wix '
                    'API credentials exist and the previous release cannot represent them';
            END IF;
        END
        $$
        """
    )
    _swap_check(_PRIOR_TYPES)
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(
        "ALTER TABLE platform_connections DROP COLUMN IF EXISTS sealed_api_credential"
    )
