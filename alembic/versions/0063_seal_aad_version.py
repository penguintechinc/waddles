"""Row-bound (AAD) sealing: `seal_version` on both connection-secret tables + `public_id`.

Closes security finding M4 (a connection's ciphertext was not bound to its row, so a DB-write
primitive could swap tokens between rows). New writes seal with AES-GCM AAD derived from the
row's identity; this migration adds the columns the readers need to tell the two layouts apart:

- ``platform_integrations.seal_version`` / ``platform_connections.seal_version`` --
  ``SMALLINT NOT NULL DEFAULT 0``: 0 = legacy no-AAD ciphertext (every pre-existing row, and
  every row `credential_manager_module` writes), 1 = AAD-bound. Adding a column with a constant
  default is metadata-only on PG 11+ (no table rewrite). Legacy rows keep opening and are
  re-sealed to 1 lazily on read and in bulk by `make reseal-connections`.
- ``platform_connections.public_id`` -- ``UUID NOT NULL DEFAULT gen_random_uuid()``, unique: the
  immutable identity those rows' AAD is built from (`conn:<public_id>:<platform>`) -- they are
  tenant-owned and shared across communities via grants, so `community_id` is not a stable
  identity there. (`platform_integrations` rows bind `<community_id>:<provider>` instead.) This
  one volatile default does rewrite `platform_connections` once; the table holds one row per
  installed bot, so it is tiny.

Revision ID: 0063_seal_aad_version
Revises: 0062_app_commands_registry
Create Date: 2026-10-24
"""

from __future__ import annotations

from alembic import op

revision = "0063_seal_aad_version"
down_revision = "0062_app_commands_registry"
branch_labels = None
depends_on = None

_PUBLIC_ID_INDEX = "uq_platform_connections_public_id"


def upgrade() -> None:
    """Add `seal_version` to both tables and `public_id` to `platform_connections`."""
    op.execute(
        "ALTER TABLE platform_integrations "
        "ADD COLUMN IF NOT EXISTS seal_version SMALLINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE platform_connections "
        "ADD COLUMN IF NOT EXISTS seal_version SMALLINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE platform_connections "
        "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_PUBLIC_ID_INDEX} ON platform_connections (public_id)"
    )
    op.execute(
        "COMMENT ON COLUMN platform_integrations.seal_version IS "
        "'0 = legacy ciphertext sealed with no AAD; 1 = AES-GCM AAD bound to community_id:platform "
        "(community_oauth rows). All new writes are 1.'"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.seal_version IS "
        "'0 = legacy ciphertext sealed with no AAD; 1 = AES-GCM AAD bound to "
        "conn:public_id:platform. All new writes are 1.'"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.public_id IS "
        "'Immutable row identity: the AAD of this row''s sealed tokens and the only identifier "
        "allowed across a trust boundary (never the numeric id).'"
    )


def downgrade() -> None:
    """Drop the columns -- refusing (loudly) while any AAD-bound row exists.

    The previous release cannot open a `seal_version = 1` ciphertext, so dropping the column
    under such rows would silently strand their secrets. Return every row to legacy form first
    (there is deliberately no tool for that -- restore from backup instead).
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_integrations WHERE seal_version <> 0)
               OR EXISTS (SELECT 1 FROM platform_connections WHERE seal_version <> 0) THEN
                RAISE EXCEPTION
                    '0063 downgrade refused: AAD-bound (seal_version=1) rows exist and the '
                    'previous release cannot open them';
            END IF;
        END
        $$
        """
    )
    op.execute(f"DROP INDEX IF EXISTS {_PUBLIC_ID_INDEX}")
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS public_id")
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS seal_version")
    op.execute("ALTER TABLE platform_integrations DROP COLUMN IF EXISTS seal_version")
