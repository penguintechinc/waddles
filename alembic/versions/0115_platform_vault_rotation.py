"""Platform secret store T8: operator reason codes + replay-protection ledger for rotate/retire.

Three additions to `platform_vault` (migration 0109), both inside the same deny-by-default schema:

1. Reason codes. `secrets.reason` and `secret_events.reason` accepted 1..7 (the *kind* of change:
   generated, imported, restored, rotated, retired, destroyed, rewrapped). A rotation or retirement
   is operator-initiated and an audit reader needs the operator's WHY, so three codes are added:

     8 scheduled rotation / retirement, 9 suspected compromise, 10 maintenance / other

   (API codes 1/2/3 map to 8/9/10 in `services/platform_secrets/rotation.py`). Codes only, never
   free text: nothing an operator types can enter the audit chain.

2. `platform_vault.rotation_requests` -- the replay ledger (security review H7). Every rotate /
   retire request carries an `Idempotency-Key` UUID; the key is claimed by an INSERT that fails on
   the unique index, so a replayed request (a captured request re-sent, a double click, a retry
   that already succeeded) is refused and never executes twice. Append-only like the event table:
   UPDATE / DELETE / TRUNCATE raise for every role (the same `reject_mutation` trigger), the grant
   is SELECT + INSERT for `waddles_hub_api` only, and everyone else is revoked. Int codes only
   (`action` 1 rotate, 2 retire); the actor is the `hub_users.uuid`, never a name.

3. The escrow FRESHNESS ANCHOR (security review MEDIUM, escrow rollback). The S3 escrow manifest
   is KEK-signed, but an attacker with bucket write could replay an OLDER validly-signed one and
   roll a restore back to retired or compromised keys. `root_marker` (which survives a wipe of the
   `secrets` table and is not in the bucket) now also records the highest manifest generation ever
   written, the digest of that manifest, and a keyed MAC over both
   (`vault.sign_escrow_anchor`). A restore refuses a manifest older than the anchor, or the same
   generation with another digest. A trigger makes the generation monotonic: it can never go down
   and, at the same generation, the digest can never change (hub-api already holds UPDATE on the
   marker; it gets no new privilege).

Downgrade drops the ledger and restores the 1..7 checks, and refuses when a row already uses a
code above 7 (a replay of the chain must not silently rewrite audit history).

Revision ID: 0115_platform_vault_rotation
Revises: 0110_merge_precompile_vault
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0115_platform_vault_rotation"
#: A plain revision on top of `0110_merge_precompile_vault` (PR #1354), the merge that joins the
#: two 0108 heads; 0111-0114 are claimed by other in-flight PRs, hence 0115.
down_revision = "0110_merge_precompile_vault"
branch_labels = None
depends_on = None

SCHEMA = "platform_vault"
HUB_API_ROLE = "waddles_hub_api"
TABLE = "rotation_requests"
#: Exact privileges `waddles_hub_api` holds on the ledger (no UPDATE, DELETE or TRUNCATE).
HUB_API_GRANTS: dict[str, tuple[str, ...]] = {TABLE: ("SELECT", "INSERT")}

_LEDGER = f"""
CREATE TABLE {SCHEMA}.{TABLE} (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key UUID NOT NULL,
    secret_type     SMALLINT NOT NULL,
    action          SMALLINT NOT NULL,
    actor_uuid      UUID NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rotation_requests_key_uq UNIQUE (idempotency_key),
    CONSTRAINT rotation_requests_type_ck CHECK (secret_type >= 1),
    CONSTRAINT rotation_requests_action_ck CHECK (action IN (1, 2))
)
"""

_TRIGGERS = (
    (
        f"CREATE TRIGGER rotation_requests_append_only BEFORE UPDATE OR DELETE "
        f"ON {SCHEMA}.{TABLE} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER rotation_requests_no_truncate BEFORE TRUNCATE ON {SCHEMA}.{TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
)

_REVOKE_FROM_OTHERS = (
    "DO $do$ DECLARE r record; BEGIN "  # nosec B608
    "FOR r IN SELECT rolname FROM pg_roles "
    "WHERE NOT rolsuper AND rolname <> current_user AND rolname !~ '^pg_' LOOP "
    f"EXECUTE format('REVOKE ALL ON {SCHEMA}.{TABLE} FROM %I', r.rolname); "
    "END LOOP; END $do$"
)


_ANCHOR_COLUMNS = f"""
ALTER TABLE {SCHEMA}.root_marker
    ADD COLUMN escrow_generation INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN escrow_manifest_hash BYTEA,
    ADD COLUMN escrow_anchor_kek_id TEXT,
    ADD COLUMN escrow_anchor_mac BYTEA,
    ADD CONSTRAINT root_marker_anchor_ck CHECK (
        (escrow_generation = 0 AND escrow_manifest_hash IS NULL
            AND escrow_anchor_kek_id IS NULL AND escrow_anchor_mac IS NULL)
        OR (escrow_generation >= 1
            AND octet_length(escrow_manifest_hash) = 32
            AND escrow_anchor_kek_id ~ '^[0-9a-f]{{16}}$'
            AND octet_length(escrow_anchor_mac) = 32)
    )
"""

_ANCHOR_GUARD_FN = f"""
CREATE FUNCTION {SCHEMA}.root_marker_anchor_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $fn$
BEGIN
    IF NEW.escrow_generation < OLD.escrow_generation THEN
        RAISE EXCEPTION 'the escrow anchor generation only moves forward' USING ERRCODE = '23000';
    END IF;
    IF NEW.escrow_generation = OLD.escrow_generation
        AND NEW.escrow_generation > 0
        AND NEW.escrow_manifest_hash IS DISTINCT FROM OLD.escrow_manifest_hash THEN
        RAISE EXCEPTION 'the escrow anchor digest is fixed once a generation is written'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END
$fn$
"""

_ANCHOR_TRIGGER = (
    f"CREATE TRIGGER root_marker_anchor_guard BEFORE UPDATE ON {SCHEMA}.root_marker "
    f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.root_marker_anchor_guard()"
)


def upgrade() -> None:
    """Widen the reason checks, create the replay ledger, guard it, and grant hub-api only."""
    for table, constraint in (
        ("secrets", "secrets_reason_ck"),
        ("secret_events", "secret_events_reason_ck"),
    ):
        op.execute(f"ALTER TABLE {SCHEMA}.{table} DROP CONSTRAINT {constraint}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.secrets ADD CONSTRAINT secrets_reason_ck "
        "CHECK (reason BETWEEN 1 AND 10)"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.secret_events ADD CONSTRAINT secret_events_reason_ck "
        "CHECK (reason IS NULL OR reason BETWEEN 1 AND 10)"
    )
    op.execute(_ANCHOR_COLUMNS)
    op.execute(_ANCHOR_GUARD_FN)
    op.execute(_ANCHOR_TRIGGER)
    op.execute(_LEDGER)
    for statement in _TRIGGERS:
        op.execute(statement)
    op.execute(f"REVOKE ALL ON {SCHEMA}.{TABLE} FROM PUBLIC")
    op.execute(_REVOKE_FROM_OTHERS)
    grants = "".join(
        f"GRANT {', '.join(privs)} ON {SCHEMA}.{table} TO {HUB_API_ROLE}; "
        for table, privs in HUB_API_GRANTS.items()
    )
    op.execute(
        "DO $do$ BEGIN "  # nosec B608
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{HUB_API_ROLE}') THEN "
        f"{grants}"
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {HUB_API_ROLE}; "
        "END IF; END $do$"
    )


def downgrade() -> None:
    """Drop the ledger and restore the 1..7 reason checks, refusing if a wider code is in use."""
    op.execute(
        "DO $do$ BEGIN "  # nosec B608
        f"IF EXISTS (SELECT 1 FROM {SCHEMA}.secrets WHERE reason > 7) "
        f"OR EXISTS (SELECT 1 FROM {SCHEMA}.secret_events WHERE reason > 7) THEN "
        "RAISE EXCEPTION 'refusing to downgrade: platform_vault rows use operator reason codes'; "
        "END IF; END $do$"
    )
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{TABLE}")
    op.execute(
        f"DROP TRIGGER IF EXISTS root_marker_anchor_guard ON {SCHEMA}.root_marker"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.root_marker_anchor_guard()")
    op.execute(
        f"ALTER TABLE {SCHEMA}.root_marker DROP CONSTRAINT IF EXISTS root_marker_anchor_ck, "
        "DROP COLUMN IF EXISTS escrow_generation, DROP COLUMN IF EXISTS escrow_manifest_hash, "
        "DROP COLUMN IF EXISTS escrow_anchor_kek_id, DROP COLUMN IF EXISTS escrow_anchor_mac"
    )
    op.execute(f"ALTER TABLE {SCHEMA}.secrets DROP CONSTRAINT secrets_reason_ck")
    op.execute(
        f"ALTER TABLE {SCHEMA}.secret_events DROP CONSTRAINT secret_events_reason_ck"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.secrets ADD CONSTRAINT secrets_reason_ck "
        "CHECK (reason BETWEEN 1 AND 7)"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.secret_events ADD CONSTRAINT secret_events_reason_ck "
        "CHECK (reason IS NULL OR reason BETWEEN 1 AND 7)"
    )
