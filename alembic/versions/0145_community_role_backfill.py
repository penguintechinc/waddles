"""Data fix: seed every community's system roles and link members to their role rows.

Community authority lives in `community_roles`; authz resolves a member's scopes ONLY through
`community_members.community_role_id`. Community create never seeded the roles nor linked the
creator, so affected communities have no role rows and their members a NULL link (no scopes).
This revision repairs existing data (new communities are seeded in hub-api's create transaction):

1. `seed_community_system_roles(id)` for every community -- the role/scope definitions stay in that
   one SQL function (alembic 0002; kinds/moderator scopes via the 0117/0118 triggers). It is
   idempotent (`ON CONFLICT DO NOTHING`), so communities that already have roles are unchanged.
2. Backfill `community_role_id` from the legacy `role` string wherever it is NULL and a role of
   that name exists in the member's community; `claims_cache` is cleared so scopes re-resolve.
   Rows whose role string names no role row stay NULL (fail closed: no scopes) and are counted.

Fresh/minimal DBs without `community_roles` are a no-op. A partial schema that has the table but
lacks the seed function skips step 1 with a printed WARNING (step 2 still runs). Downgrade is a
no-op: the seeded rows and links are valid data that cannot be told apart from app-written rows.

Revision id <= 32 chars; `down_revision` is the release head at authoring time (re-point at merge
if another revision lands first -- single-head CI gate).

Revision ID: 0145_community_role_backfill
Revises: 0144_ingest_owner_public_ids
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0145_community_role_backfill"
down_revision = "0144_ingest_owner_public_ids"
branch_labels = None
depends_on = None

_SEED_ALL = "SELECT seed_community_system_roles(id) FROM communities"

_BACKFILL = """
    UPDATE community_members m
       SET community_role_id = r.id, claims_cache = NULL
      FROM community_roles r
     WHERE m.community_role_id IS NULL
       AND r.community_id = m.community_id
       AND r.name = m.role
"""

_UNRESOLVED = "SELECT count(*) FROM community_members WHERE community_role_id IS NULL"


def upgrade() -> None:
    """Seed missing system roles, then link members to the role row their role string names."""
    conn = op.get_bind()
    if not conn.execute(
        sa.text("SELECT to_regclass('public.community_roles') IS NOT NULL")
    ).scalar():
        # Minimal CI bootstrap (no legacy community tables): nothing to repair.
        return
    if conn.execute(
        sa.text(
            "SELECT to_regprocedure('seed_community_system_roles(integer)') IS NOT NULL"
        )
    ).scalar():
        seeded = conn.execute(sa.text(_SEED_ALL)).rowcount
    else:
        # Only a partial schema (e.g. a hand-built test table) lacks the function; say so loudly.
        # The backfill below still links every member whose role row already exists.
        seeded = 0
        print(
            "[0145] WARNING: seed_community_system_roles(integer) is missing; "
            "system roles NOT seeded"
        )
    linked = conn.execute(sa.text(_BACKFILL)).rowcount
    unresolved = conn.execute(sa.text(_UNRESOLVED)).scalar()
    print(
        f"[0145] seeded system roles for {seeded} communities; linked {linked} members; "
        f"{unresolved} members still have no role row (no scopes)"
    )


def downgrade() -> None:
    """No-op: seeded roles and links are valid data (see module docstring)."""
