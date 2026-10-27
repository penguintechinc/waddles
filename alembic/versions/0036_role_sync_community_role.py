"""Bar Citizen role-sync (Unit F): adds the `community_role` binding scope for the
deferred Discord -> platform direction (owner ask, 2026-10-05: "advance ROLE SYNC
toward beta ... add the deferred Discord->platform direction").

**Evolves 0034's `community_role_sync_bindings` forward -- ALTER, not a new table.**
0034 shipped two `sync_scope` values (`subscriber_tier`, `moderator`), both
exclusively `twitch_to_discord` in practice (0034's own docstring: "Twitch sub tiers
are read-only so this direction is always twitch_to_discord"). This migration adds a
third value, `community_role`, which is exclusively the OTHER direction: a Discord
role id mapped to one of this hub platform's own community-member roles
(`services/admin_service.py::VALID_MEMBER_ROLES` -- `community-admin`, `moderator`,
`vip`, `member`; `community-owner` is deliberately never assignable here, matching
`admin_service.update_member_role()`'s own owner-protection invariant).

**Why a new scope value on the same table, not a new table.** `community_role_sync_
bindings` is already "one pairing's platform-role concept -> Discord role id"; a
Discord-role -> community-role mapping is the exact same shape (pairing, discord_role_id,
one additional concept column), not a different entity. Adding `community_role` plays
the same mutually-exclusive-with-`subscriber_tier` role the `moderator` scope already
does -- `subscriber_tier` stays `SMALLINT` (1/2/3), `community_role` is a new nullable
`VARCHAR(20)` column, and the existing `chk_role_sync_binding_scope_tier` three-way-ish
CHECK grows a third branch rather than being replaced by a different constraint shape.

**This is how loop-prevention (deferred at 0034 time, see role_sync_service.py's old
module docstring: "discord_to_twitch/bidirectional ... additionally needs loop-
prevention: never re-applying a change this worker just made") is actually resolved --
structurally, not with a last-applied-state table.** Each binding's `sync_scope` fixes
which single direction ever writes through it:
  - `subscriber_tier` / `moderator` bindings: Twitch is authoritative, Discord is the
    target. The worker only ever calls Discord's add/remove-role API for these roles.
  - `community_role` bindings: Discord is authoritative, this hub platform's
    `community_members.role` is the target. The worker NEVER calls Discord's role API
    for a `discord_role_id` bound this way -- it only reads the guild member's current
    Discord roles and writes `community_members`.
A single `discord_role_id` is never bound as both `subscriber_tier`/`moderator` AND
`community_role` for the same pairing (enforced by the existing `uq_role_sync_binding_tier`
partial index only applying to `subscriber_tier`, PLUS this migration's new
`uq_role_sync_binding_community_role` partial index on `(pairing_id, discord_role_id)
WHERE sync_scope = 'community_role'` -- an admin *could* still assign the same role id
under both scopes for the same pairing via two separate binding rows, which the DB
schema alone cannot forbid without a cross-partial-index UNIQUE Postgres doesn't support
cleanly; `services/guild_pairing.py::create_binding()` is the actual enforcement point,
rejecting a `community_role` binding whose `discord_role_id` already has a
`subscriber_tier`/`moderator` binding under the same pairing, and vice versa). Because
each binding's scope is fixed at creation and each direction's write target is disjoint
(Discord roles vs. `community_members.role`), there is no code path where this worker's
own write becomes a read that triggers another write of its own -- no cycle, no state to
remember across reconcile passes.

Revision ID: 0036_role_sync_community_role
Revises: 0035_connection_model_layers
Create Date: 2026-10-05
"""

from __future__ import annotations

from alembic import op

revision = "0036_role_sync_community_role"
down_revision = "0035_connection_model_layers"
branch_labels = None
depends_on = None

#: `services/admin_service.py::VALID_MEMBER_ROLES` -- kept in sync by
#: `test_0036_schema_drift.py::test_community_role_check_matches_valid_member_roles`.
_VALID_COMMUNITY_ROLES = ("community-admin", "moderator", "vip", "member")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE community_role_sync_bindings "
        "ADD COLUMN IF NOT EXISTS community_role VARCHAR(20)"
    )
    op.execute(
        "COMMENT ON COLUMN community_role_sync_bindings.community_role IS "
        "'Target hub-platform community_members.role when sync_scope=community_role "
        "(Discord -> platform direction). NULL for subscriber_tier/moderator bindings "
        "(Twitch -> Discord direction). Never community-owner -- see this migration''s "
        "own docstring on owner-protection.'"
    )

    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "community_role_sync_bindings_sync_scope_check"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "community_role_sync_bindings_sync_scope_check "
        "CHECK (sync_scope IN ('subscriber_tier', 'moderator', 'community_role'))"
    )

    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "chk_role_sync_binding_community_role CHECK ("
        f"community_role IS NULL OR community_role IN {_VALID_COMMUNITY_ROLES}"
        ")"
    )

    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "chk_role_sync_binding_scope_tier"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "chk_role_sync_binding_scope_tier CHECK ("
        "(sync_scope = 'subscriber_tier' AND subscriber_tier IS NOT NULL AND community_role IS NULL) "
        "OR (sync_scope = 'moderator' AND subscriber_tier IS NULL AND community_role IS NULL) "
        "OR (sync_scope = 'community_role' AND subscriber_tier IS NULL AND community_role IS NOT NULL)"
        ")"
    )

    # One `community_role` binding per (pairing, discord_role_id) -- unlike
    # subscriber_tier/moderator (capped at one binding per CONCEPT per pairing via
    # 0034's own partial indexes), a pairing may map many distinct Discord roles to
    # (possibly repeated) community roles, so the uniqueness axis here is the Discord
    # role id itself, not the scope.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_sync_binding_community_role "
        "ON community_role_sync_bindings (pairing_id, discord_role_id) "
        "WHERE sync_scope = 'community_role'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_role_sync_binding_community_role")
    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "chk_role_sync_binding_scope_tier"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "chk_role_sync_binding_scope_tier CHECK ("
        "(sync_scope = 'subscriber_tier' AND subscriber_tier IS NOT NULL) "
        "OR (sync_scope = 'moderator' AND subscriber_tier IS NULL)"
        ")"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "chk_role_sync_binding_community_role"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP CONSTRAINT IF EXISTS "
        "community_role_sync_bindings_sync_scope_check"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings ADD CONSTRAINT "
        "community_role_sync_bindings_sync_scope_check "
        "CHECK (sync_scope IN ('subscriber_tier', 'moderator'))"
    )
    op.execute(
        "ALTER TABLE community_role_sync_bindings DROP COLUMN IF EXISTS community_role"
    )
