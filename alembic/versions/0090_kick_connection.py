"""Kick provider connections: allow ``kick_channel`` and enforce one Kick connection per community.

The Kick adapter (``hub_api/services/provider_connections/kick.py``) stores a community's Kick
channel as the connection resource. Two schema changes:

1. The ``resource_type`` allow-list CHECK (0035 -> 0064 -> 0073 -> 0078 -> 0081 -> 0086 -> 0089)
   must name ``kick_channel`` or every Kick insert is refused. Pure CHECK swap, added ``NOT VALID`` then
   validated so the table is never scanned under ACCESS EXCLUSIVE.
2. The Kick data plane (inbound Pusher, webhook route, outbound sender) resolves its binding *by
   community*, so a second live Kick connection makes every one of them ambiguous and refused.
   ``community_connection_access`` carries no platform column (it joins to
   ``platform_connections``), so a plain UNIQUE index cannot express "one Kick grant per
   community". A BEFORE INSERT/UPDATE trigger does, serialised per community by a transaction-scoped advisory lock so two concurrent
   connects cannot both pass the check. A violation is raised as ``unique_violation`` (SQLSTATE
   23505) on the named constraint ``uq_one_kick_connection_per_community`` -- exactly the error
   class a real UNIQUE index would give, which the store already maps to a 409. Revoked grants do not
   count, so a community can reconnect after revoking.

Upgrade refuses (loudly) when a community ALREADY holds more than one live Kick grant: silently
keeping an ambiguous state is the defect this migration exists to remove.

Revision ID: 0090_kick_connection
Revises: 0089_gameserver_conn_resource
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op

revision = "0090_kick_connection"
down_revision = "0089_gameserver_conn_resource"
branch_labels = None
depends_on = None

_RESOURCE_CHECK = "platform_connections_resource_type_check"
_PRIOR_TYPES = (
    "'discord_guild', 'twitch_channel', 'external_http', 'aws_lambda', 'gcp_function', "
    "'youtube_channel', 'wix_site', 'spectrum_community', 'soundcloud_app', 'youtube_api_key', "
    "'gameserver'"
)
_FUNCTION = "enforce_one_kick_connection_per_community"
_TRIGGER = "trg_one_kick_connection_per_community"
_CONSTRAINT = "uq_one_kick_connection_per_community"


def _swap_check(allowed: str) -> None:
    """Replace the resource_type CHECK with one allowing exactly ``allowed``."""
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_RESOURCE_CHECK}")
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_RESOURCE_CHECK} "
        f"CHECK (resource_type IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE platform_connections VALIDATE CONSTRAINT {_RESOURCE_CHECK}")


def upgrade() -> None:
    """Allow ``kick_channel`` and install the one-Kick-connection-per-community trigger."""
    op.execute(
        """
        DO $$
        DECLARE offenders INTEGER;
        BEGIN
            SELECT count(*) INTO offenders FROM (
                SELECT a.community_id
                FROM community_connection_access a
                JOIN platform_connections c ON c.id = a.connection_id
                WHERE c.platform = 'kick' AND a.status <> 'revoked'
                GROUP BY a.community_id HAVING count(*) > 1
            ) dup;
            IF offenders > 0 THEN
                RAISE EXCEPTION
                    '0090 upgrade refused: % communit(ies) hold more than one live Kick '
                    'connection; revoke the extras (community_connection_access.status = '
                    '''revoked'') and re-run', offenders;
            END IF;
        END
        $$
        """
    )
    _swap_check(f"{_PRIOR_TYPES}, 'kick_channel'")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'revoked' THEN
                RETURN NEW;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM platform_connections
                WHERE id = NEW.connection_id AND platform = 'kick'
            ) THEN
                RETURN NEW;
            END IF;
            -- Serialise concurrent connects for ONE community; the second waits, then re-reads.
            PERFORM pg_advisory_xact_lock(
                hashtextextended('kick-connection-community:' || NEW.community_id::text, 0)
            );
            IF EXISTS (
                SELECT 1
                FROM community_connection_access a
                JOIN platform_connections c ON c.id = a.connection_id
                WHERE a.community_id = NEW.community_id
                  AND a.status <> 'revoked'
                  AND c.platform = 'kick'
                  AND a.id IS DISTINCT FROM NEW.id
            ) THEN
                RAISE EXCEPTION 'community % already has a live Kick connection', NEW.community_id
                    USING ERRCODE = 'unique_violation', CONSTRAINT = '{_CONSTRAINT}';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON community_connection_access")
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE INSERT OR UPDATE OF community_id, connection_id, status
        ON community_connection_access
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
        """
    )


def downgrade() -> None:
    """Drop the trigger and revert the allow-list -- refusing while a Kick connection exists."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_connections WHERE resource_type = 'kick_channel') THEN
                RAISE EXCEPTION
                    '0090 downgrade refused: kick_channel connections exist and the previous '
                    'release cannot represent them';
            END IF;
        END
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON community_connection_access")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    _swap_check(_PRIOR_TYPES)
