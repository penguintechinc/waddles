"""Tamper-evident enterprise audit log: `audit_events` hash-chain table (GRC audit finding #3).

The legacy `audit_log` table (config/postgres `000_create_base_schema.sql`) is a plain
mutable table: any row can be edited or deleted with no trace, it records only a
handful of bundle-lifecycle events, and its writer swallowed failures (`except: pass`).
`audit_events` is its tamper-evident, Enterprise-tier successor; `audit_log` is left in
place (it remains the all-tier basic trail and the `/api/v1/platform/audit-log` source).

**Chain model.** One independent chain per `chain_id` (`tenant:<id>` or `platform`).
Each row has a gapless `seq` (1, 2, 3 ...) and `prev_hash` = the previous row's
`record_hash` (64 zeros for `seq = 1`); `record_hash` = SHA-256 over a canonical
serialisation of every other audited column (see `hub_api/services/audit_chain.py`).
Editing a row changes its hash; deleting one leaves a `seq` gap and a dangling
`prev_hash`; the verifier (`GET /api/v1/compliance/audit/verify`, `make verify-audit-chain`)
walks the chain and reports the first break.

**DB-level guarantees (defence in depth -- the hash chain, not these, is the proof):**

- `PRIMARY KEY (chain_id, seq)` -- two writers can never both commit `head + 1`;
  `UNIQUE (chain_id, prev_hash)` -- a chain can never fork.
- `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers raise, so ordinary SQL
  (including a compromised application role) cannot rewrite history.
- `hub_api` is granted `SELECT, INSERT` only -- never `UPDATE`/`DELETE`.
- CHECK constraints pin `actor_kind`/`outcome` vocabularies and the hex shape of the hashes.

A superuser can still `DROP TRIGGER`; that is precisely the case the hash chain exists
for -- the edit is then *detectable*, and the head hash can be pinned off-box.

**PII.** `actor_uuid` is `hub_users.uuid` (migration 0043), deliberately with **no foreign
key**: erasing/anonymising a user must never cascade into (and so break) an immutable
record, and the UUID alone identifies no one once the identity row is anonymised.
`details` is validated PII-free at write time (`services/audit_events.py`). No IP address
or user agent is stored.

**Numbering note:** parallel migrations are landing on `release/v3.0.X`; chained off
`0049_sso_connections` (the head when this was last re-sequenced; renumbered 0048 -> 0053
to sit after the other in-flight migrations) -- re-point `down_revision` at the then-current
head if another migration merges first.

Revision ID: 0053_audit_events_hash_chain
Revises: 0049_sso_connections
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "0053_audit_events_hash_chain"
down_revision = "0049_sso_connections"
branch_labels = None
depends_on = None

_TABLE = "audit_events"
_FN = "audit_events_reject_mutation"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            chain_id     VARCHAR(64)  NOT NULL,
            seq          BIGINT       NOT NULL CHECK (seq >= 1),
            event_id     UUID         NOT NULL,
            occurred_at  TIMESTAMPTZ  NOT NULL,
            actor_uuid   UUID,
            actor_kind   VARCHAR(16)  NOT NULL
                CHECK (actor_kind IN ('user', 'service', 'system', 'external', 'unresolved')),
            category     VARCHAR(16)  NOT NULL,
            action       VARCHAR(100) NOT NULL,
            outcome      VARCHAR(16)  NOT NULL
                CHECK (outcome IN ('success', 'denied', 'failure')),
            target_type  VARCHAR(50),
            target_id    VARCHAR(128),
            details      JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            prev_hash    VARCHAR(64)  NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{{64}}$'),
            record_hash  VARCHAR(64)  NOT NULL CHECK (record_hash ~ '^[0-9a-f]{{64}}$'),
            hash_version VARCHAR(16)  NOT NULL DEFAULT 'sha256-v1',
            PRIMARY KEY (chain_id, seq),
            CONSTRAINT audit_events_event_id_key UNIQUE (event_id),
            CONSTRAINT audit_events_chain_prev_hash_key UNIQUE (chain_id, prev_hash),
            CONSTRAINT audit_events_record_hash_key UNIQUE (record_hash)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_chain_time ON {_TABLE} (chain_id, occurred_at)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_chain_action ON {_TABLE} (chain_id, action, seq)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_chain_actor ON {_TABLE} (chain_id, actor_uuid, seq) "
        "WHERE actor_uuid IS NOT NULL"
    )
    op.execute(
        f"COMMENT ON TABLE {_TABLE} IS "
        "'Append-only, hash-chained (tamper-evident) enterprise audit log; one chain per "
        "tenant plus a platform chain. UPDATE/DELETE/TRUNCATE are rejected by trigger; "
        "integrity is proven by recomputing the chain, not by trusting these guards.'"
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FN}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_append_only ON {_TABLE}")
    op.execute(
        f"CREATE TRIGGER {_TABLE}_append_only BEFORE UPDATE OR DELETE ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {_FN}()"
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_truncate ON {_TABLE}")
    op.execute(
        f"CREATE TRIGGER {_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {_FN}()"
    )

    # Append + read only. Deliberately no UPDATE/DELETE for any role.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO hub_api")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_truncate ON {_TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_append_only ON {_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_FN}()")
