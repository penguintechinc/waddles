"""DSAR request inbox: `dsar_requests` + append-only `dsar_request_events`.

Level 1 of the two-level DSAR system (statutory, every tier): data-subject requests are
received, stored, and acted on by tenant admins. Design notes the code depends on:

- BIGINT surrogate PK for joins; `public_id` UUID is the ONLY id that crosses the API boundary.
- `request_type` / `status` / `regime` / `source` are SMALLINT enum codes with CHECK ranges
  (meanings documented in `services/dsar_service.py` and `openapi/v1.yaml`).
- Requester contact data for non-users (`contact_email_*`, `message_*`) is PII: sealed by the repo vault
  (AES-256-GCM, base64 TEXT, `services/dsar_crypto.py`), plus a keyed digest (`contact_email_hash`) used
  for per-tenant+email duplicate detection (indexed: `ix_dsar_requests_tenant_email`).
  All of it is blanked by the purge
  job (`contact_purged_at`) after completion + `DSAR_CONTACT_RETENTION_DAYS`, or on expiry.
- `verification_token_hash` is hex SHA-256 of the single-use emailed token (never the token).
- `dsar_request_events` is append-only: a trigger refuses UPDATE and direct DELETE (a cascade
  from the parent request/tenant is the only permitted removal).

Revision ID: 0121_dsar_requests
Revises: 0118_room_scopes_moderator
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0121_dsar_requests"
down_revision = "0118_room_scopes_moderator"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
CREATE TABLE IF NOT EXISTS dsar_requests (
    id                       BIGSERIAL PRIMARY KEY,
    public_id                UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id                INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    community_id             INTEGER REFERENCES communities(id) ON DELETE SET NULL,
    request_type             SMALLINT NOT NULL,
    status                   SMALLINT NOT NULL,
    regime                   SMALLINT NOT NULL,
    source                   SMALLINT NOT NULL,
    requested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at              TIMESTAMPTZ,
    due_at                   TIMESTAMPTZ,
    extended_until           TIMESTAMPTZ,
    extension_reason         TEXT,
    completed_at             TIMESTAMPTZ,
    expires_at               TIMESTAMPTZ,
    assignee_user_id         INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    subject_user_id          INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    response_notes           TEXT,
    contact_email_enc        TEXT,
    contact_email_hash       TEXT,
    message_enc              TEXT,
    contact_purged_at        TIMESTAMPTZ,
    verification_token_hash  TEXT,
    verification_expires_at  TIMESTAMPTZ,
    verification_sent_at     TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dsar_requests_type CHECK (request_type BETWEEN 1 AND 6),
    CONSTRAINT ck_dsar_requests_status CHECK (status BETWEEN 1 AND 8),
    CONSTRAINT ck_dsar_requests_regime CHECK (regime BETWEEN 1 AND 3),
    CONSTRAINT ck_dsar_requests_source CHECK (source BETWEEN 1 AND 3)
)
""",
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_requests_tenant_status "
        "ON dsar_requests (tenant_id, status, requested_at DESC, id DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_requests_tenant_due "
        "ON dsar_requests (tenant_id, due_at) WHERE completed_at IS NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_requests_tenant_email "
        "ON dsar_requests (tenant_id, contact_email_hash) WHERE contact_email_hash IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dsar_requests_token_hash "
        "ON dsar_requests (verification_token_hash) WHERE verification_token_hash IS NOT NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_requests_unverified_expiry "
        "ON dsar_requests (expires_at) WHERE status = 2"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_requests_subject "
        "ON dsar_requests (tenant_id, subject_user_id)"
    ),
    """
CREATE TABLE IF NOT EXISTS dsar_request_events (
    id            BIGSERIAL PRIMARY KEY,
    public_id     UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    request_id    BIGINT NOT NULL REFERENCES dsar_requests(id) ON DELETE CASCADE,
    event_type    SMALLINT NOT NULL,
    actor_kind    SMALLINT NOT NULL,
    actor_user_id INTEGER,
    from_status   SMALLINT,
    to_status     SMALLINT,
    detail        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dsar_events_actor_kind CHECK (actor_kind BETWEEN 1 AND 4)
)
""",
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_events_request "
        "ON dsar_request_events (tenant_id, request_id, id)"
    ),
    """
CREATE OR REPLACE FUNCTION dsar_request_events_append_only() RETURNS trigger AS $$
BEGIN
    -- A cascade from the parent request/tenant runs this trigger at depth > 1: allowed.
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'dsar_request_events is append-only (% refused)', TG_OP;
END;
$$ LANGUAGE plpgsql
""",
    "DROP TRIGGER IF EXISTS trg_dsar_request_events_append_only ON dsar_request_events",
    (
        "CREATE TRIGGER trg_dsar_request_events_append_only "
        "BEFORE UPDATE OR DELETE ON dsar_request_events "
        "FOR EACH ROW EXECUTE FUNCTION dsar_request_events_append_only()"
    ),
)

_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS trg_dsar_request_events_append_only ON dsar_request_events",
    "DROP FUNCTION IF EXISTS dsar_request_events_append_only()",
    "DROP TABLE IF EXISTS dsar_request_events",
    "DROP TABLE IF EXISTS dsar_requests",
)


def upgrade() -> None:
    """Create the two DSAR tables, their indexes and the append-only trigger."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop both tables (events first). This discards stored requests: dev/rollback only."""
    for statement in _DOWNGRADE:
        op.execute(statement)
