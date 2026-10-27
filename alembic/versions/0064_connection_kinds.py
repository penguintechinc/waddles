"""Connection kinds + sealed structured secrets on `platform_connections` (Chunk 2, Task 2).

Extends the connection store beyond OAuth installs with three static-secret kinds -- an external
https endpoint, an AWS Lambda function and a GCP Cloud Function -- whose credential is a structured
JSON document rather than an OAuth token pair.

- ``connection_kind SMALLINT NOT NULL DEFAULT 1`` -- the int type authority: 1 = platform OAuth
  (every pre-existing row, backfilled by the default), 2 = external_http, 3 = aws_lambda, 4 = gcp_function.
  ``CHECK (connection_kind BETWEEN 1 AND 4)``. The codes are mirrored by the Rust enum in the data
  plane; never renumber.
- ``sealed_secret TEXT NULL`` -- AES-256-GCM of the secret JSON document, AAD bound to
  ``conn:<public_id>:<platform>`` (migration 0063's row identity). NULL for OAuth rows.
- ``access_token`` becomes nullable: a static row has no OAuth token. Two CHECKs keep the invariant
  honest at the DB level -- an OAuth row must have ``access_token``, a static row must have
  ``sealed_secret``.
- the ``resource_type`` allow-list CHECK (0035) is extended with the three provider ids.

``public_id`` already exists (0063) and is NOT touched here.

Revision ID: 0064_connection_kinds
Revises: 0063_seal_aad_version
Create Date: 2026-10-24
"""

from __future__ import annotations

from alembic import op

revision = "0064_connection_kinds"
down_revision = "0063_seal_aad_version"
branch_labels = None
depends_on = None

_RESOURCE_CHECK = "platform_connections_resource_type_check"
_KIND_CHECK = "platform_connections_connection_kind_check"
_SECRET_SHAPE_CHECK = "platform_connections_secret_shape_check"


def upgrade() -> None:
    """Add the kind + sealed-secret columns (kind backfilled to 1), tighten, extend the allow-list."""
    # NOT NULL DEFAULT 1 backfills every existing row to 1 (metadata-only on PG 11+) and keeps any
    # pre-0064 writer (which only ever wrote OAuth rows) valid without naming the column.
    op.execute(
        "ALTER TABLE platform_connections "
        "ADD COLUMN IF NOT EXISTS connection_kind SMALLINT NOT NULL DEFAULT 1"
    )
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_KIND_CHECK} "
        "CHECK (connection_kind BETWEEN 1 AND 4)"
    )
    op.execute(
        "ALTER TABLE platform_connections ADD COLUMN IF NOT EXISTS sealed_secret TEXT"
    )
    op.execute(
        "ALTER TABLE platform_connections ALTER COLUMN access_token DROP NOT NULL"
    )
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_SECRET_SHAPE_CHECK} CHECK ("
        "(connection_kind = 1 AND access_token IS NOT NULL AND sealed_secret IS NULL) OR "
        "(connection_kind <> 1 AND access_token IS NULL AND refresh_token IS NULL "
        "AND sealed_secret IS NOT NULL))"
    )
    op.execute(
        f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} CHECK "
        "(resource_type IN ('discord_guild', 'twitch_channel', "
        "'external_http', 'aws_lambda', 'gcp_function'))"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.connection_kind IS "
        "'Int type authority: 1 platform_oauth, 2 external_http, 3 aws_lambda, 4 gcp_function.'"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.sealed_secret IS "
        "'AES-256-GCM of the static-secret JSON document, AAD conn:public_id:platform. "
        "NULL for OAuth rows. Never returned by any admin route.'"
    )


def downgrade() -> None:
    """Revert -- refusing (loudly) while any static-kind row exists, since it would be stranded."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections WHERE connection_kind <> 1) THEN
                RAISE EXCEPTION
                    '0064 downgrade refused: static-secret connections (connection_kind <> 1) '
                    'exist and the previous release cannot represent them';
            END IF;
        END
        $$
        """
    )
    op.execute(
        f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_SECRET_SHAPE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} CHECK "
        "(resource_type IN ('discord_guild', 'twitch_channel'))"
    )
    op.execute(
        "ALTER TABLE platform_connections ALTER COLUMN access_token SET NOT NULL"
    )
    op.execute(
        f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_KIND_CHECK}"
    )
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS sealed_secret")
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS connection_kind")
