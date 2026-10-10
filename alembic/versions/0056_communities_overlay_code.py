"""Add `communities.overlay_code`: the unguessable public handle in overlay URLs.

svc-presentation served overlays at `/overlay/{community_id}/{surface}`, so
every overlay was enumerable by walking the sequential integer id. The new
scheme is `/{overlay_code}/{surface}` where `overlay_code` is a per-community
random 64-bit value rendered as 16 lowercase hex characters
(`encode(gen_random_bytes(8), 'hex')`, pgcrypto's CSPRNG-backed generator).
The code is only a *public path handle*: every internal key, scope, credential
and join stays on the real `communities.id`.

What this revision does, in order (every step re-runnable):

1. `CREATE EXTENSION IF NOT EXISTS pgcrypto` -- provides `gen_random_bytes`.
   Trusted extension on PG13+, already created by 0021, so no superuser needed.
2. `ADD COLUMN IF NOT EXISTS overlay_code VARCHAR(16)` -- nullable at first so
   the backfill can run before NOT NULL is enforced.
3. Backfill every row that has no code with its own random value. The
   `UPDATE` evaluates the volatile `gen_random_bytes` once per row, so codes
   are independent draws.
4. De-duplicate: a 64-bit collision is astronomically unlikely (about
   n^2 / 2^65 for n communities) but the UNIQUE constraint below would turn
   one into a failed migration, so any row that shares a code with a lower-id
   row is re-drawn (bounded retries, then the migration fails loud instead of
   looping forever).
5. `SET DEFAULT encode(gen_random_bytes(8), 'hex')` -- new communities get a
   code with no application change (hub-api's pydal inserts omit the column).
6. `SET NOT NULL`, a `CHECK` pinning the exact `[0-9a-f]{16}` shape, and a
   named `UNIQUE` constraint (whose backing index serves svc-presentation's
   `WHERE overlay_code = $1` lookup).

Downgrade drops the constraints and the column. That discards the codes, which
is the point of a downgrade to the integer-id URL scheme; a later re-upgrade
mints fresh codes (any URL that embedded the old code stops working).

Operational note: the svc-presentation database role needs `SELECT` on
`communities.overlay_code` (it already reads `communities.id`/`tenant_id`).
This revision does not issue the GRANT because the per-service role names are
provisioned outside Alembic.

Revision ID: 0056_communities_overlay_code
Revises: 0047_builtin_handler_paths
Create Date: 2026-10-10
"""

from alembic import op

revision = "0056_communities_overlay_code"
down_revision = "0047_builtin_handler_paths"
branch_labels = None
depends_on = None

UNIQUE_CONSTRAINT = "communities_overlay_code_key"
FORMAT_CONSTRAINT = "communities_overlay_code_format"

#: Re-draw rounds the de-duplication step may take before failing loud. One
#: round clears a collision with probability 1 - 2^-64; 5 is far past any
#: realistic need and still bounded.
_MAX_DEDUP_ROUNDS = 5

_RANDOM_CODE = "encode(gen_random_bytes(8), 'hex')"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        "ALTER TABLE communities ADD COLUMN IF NOT EXISTS overlay_code VARCHAR(16)"
    )
    op.execute(
        f"UPDATE communities SET overlay_code = {_RANDOM_CODE} WHERE overlay_code IS NULL"
    )
    op.execute(
        f"""
        DO $dedup$
        DECLARE
            round_no INTEGER := 0;
            dupes BIGINT;
        BEGIN
            LOOP
                SELECT count(*) INTO dupes FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY overlay_code ORDER BY id
                    ) AS rn
                    FROM communities
                ) ranked
                WHERE rn > 1;
                EXIT WHEN dupes = 0;
                round_no := round_no + 1;
                IF round_no > {_MAX_DEDUP_ROUNDS} THEN
                    RAISE EXCEPTION
                        'communities.overlay_code: % duplicate codes remain after % re-draw rounds',
                        dupes, {_MAX_DEDUP_ROUNDS};
                END IF;
                UPDATE communities c
                   SET overlay_code = {_RANDOM_CODE}
                  FROM (
                      SELECT id, row_number() OVER (
                          PARTITION BY overlay_code ORDER BY id
                      ) AS rn
                      FROM communities
                  ) ranked
                 WHERE c.id = ranked.id AND ranked.rn > 1;
            END LOOP;
        END
        $dedup$
        """
    )
    op.execute(
        f"ALTER TABLE communities ALTER COLUMN overlay_code SET DEFAULT {_RANDOM_CODE}"
    )
    op.execute("ALTER TABLE communities ALTER COLUMN overlay_code SET NOT NULL")
    op.execute(
        f"""
        DO $constraints$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = '{FORMAT_CONSTRAINT}'
                   AND conrelid = 'communities'::regclass
            ) THEN
                ALTER TABLE communities
                    ADD CONSTRAINT {FORMAT_CONSTRAINT}
                    CHECK (overlay_code ~ '^[0-9a-f]{{16}}$');
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = '{UNIQUE_CONSTRAINT}'
                   AND conrelid = 'communities'::regclass
            ) THEN
                ALTER TABLE communities
                    ADD CONSTRAINT {UNIQUE_CONSTRAINT} UNIQUE (overlay_code);
            END IF;
        END
        $constraints$
        """
    )
    op.execute(
        "COMMENT ON COLUMN communities.overlay_code IS "
        "'Public overlay URL handle: 16 lowercase hex chars (64 random bits, CSPRNG). "
        "Path handle only -- all internal keying uses communities.id. Rotating it "
        "invalidates a leaked overlay URL.'"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE communities DROP CONSTRAINT IF EXISTS {UNIQUE_CONSTRAINT}")
    op.execute(f"ALTER TABLE communities DROP CONSTRAINT IF EXISTS {FORMAT_CONSTRAINT}")
    op.execute("ALTER TABLE communities DROP COLUMN IF EXISTS overlay_code")
