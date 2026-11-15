"""Grant the moderator role group today's room moderation scopes (kick, mute, lock, hands).

Room actions now have their own scopes (`rooms:kick|mute|lock|hands` for moderation,
`rooms:create|update|close|guests` for management). The admin kind is COMPUTED from the scope
registry, so it needs no data change. The moderator kind (`community_roles.role_kind = 2`) stores
its scopes, so this migration appends exactly the four moderation scopes to every existing
moderator row and installs a BEFORE INSERT trigger that does the same for rows created later by
`seed_community_system_roles()` -- both key on the int `role_kind`, never on a role name. Moderators
do NOT gain any management scope (`rooms:update` etc.).

Revision id <= 32 chars; `down_revision` is re-pointed to the release alembic head at merge
(single-head CI gate).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0118_room_scopes_moderator"
down_revision = "0117_community_role_kind"
branch_labels = None
depends_on = None

#: What a moderator can do in rooms today (kick/mute/lock/hand moderation).
MODERATOR_ROOM_SCOPES = ("rooms:kick", "rooms:mute", "rooms:lock", "rooms:hands")

_SCOPES_JSON = "[" + ",".join(f'"{s}"' for s in MODERATOR_ROOM_SCOPES) + "]"

_MERGE_SQL = f"""
    jsonb_set(
      {{col}}::jsonb,
      '{{{{scopes}}}}',
      (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]'::jsonb)
         FROM jsonb_array_elements_text(
                COALESCE({{col}}::jsonb -> 'scopes', '[]'::jsonb) || '{_SCOPES_JSON}'::jsonb
              ) AS x),
      TRUE
    )
"""


def upgrade() -> None:
    """Backfill existing moderator-kind rows and install the seeding trigger."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE community_roles SET base_claims = "
            + _MERGE_SQL.format(col="base_claims")
            + " WHERE role_kind = 2 AND is_system = TRUE"
        )
    )
    conn.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION community_roles_add_room_scopes() RETURNS trigger AS $$
            BEGIN
              IF NEW.is_system AND NEW.role_kind = 2 THEN
                NEW.base_claims := {_MERGE_SQL.format(col="NEW.base_claims")};
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    conn.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_community_roles_room_scopes ON community_roles")
    )
    # Fires after trg_community_roles_kind (alphabetical), which sets role_kind from the name once.
    conn.execute(
        sa.text(
            "CREATE TRIGGER trg_community_roles_room_scopes BEFORE INSERT ON community_roles "
            "FOR EACH ROW EXECUTE FUNCTION community_roles_add_room_scopes()"
        )
    )


def downgrade() -> None:
    """Remove the trigger and the scopes this migration added to moderator-kind rows."""
    conn = op.get_bind()
    conn.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_community_roles_room_scopes ON community_roles")
    )
    conn.execute(sa.text("DROP FUNCTION IF EXISTS community_roles_add_room_scopes()"))
    conn.execute(
        sa.text(
            """
            UPDATE community_roles SET base_claims = jsonb_set(
              base_claims::jsonb, '{scopes}',
              (SELECT COALESCE(jsonb_agg(x), '[]'::jsonb)
                 FROM jsonb_array_elements_text(base_claims::jsonb -> 'scopes') AS x
                WHERE x NOT IN ('rooms:kick', 'rooms:mute', 'rooms:lock', 'rooms:hands')),
              TRUE)
            WHERE role_kind = 2 AND is_system = TRUE AND base_claims::jsonb ? 'scopes'
            """
        )
    )
