"""`dsar_mail_outbox`: durable queue for DSAR verification emails.

The public DSAR routes used to await SMTP in-band, which (a) made `resend-verification` latency
reveal whether a pending request exists and (b) let a failing relay pin a worker for ~30 s per
public call. The routes now only enqueue a row here; a worker loop in every hub-api replica claims
due rows (a conditional UPDATE lease, safe across replicas), sends, and records the outcome.
`lease_token` is stamped per claim; every later write is guarded by it, so a worker whose lease
expired cannot overwrite a row another worker re-claimed.

- The raw verification token is sealed in `token_enc` (repo vault, row `public_id` as AAD) and
  nulled as soon as the row reaches a terminal state; the requester email is NOT copied here (the
  worker reads it from the request's sealed contact blob), so this table holds no PII.
- `status`: 1 pending, 2 sent, 3 failed (terminal), 4 discarded (request closed or
  token superseded).
- No foreign keys, on purpose: the public routes' "nothing to send" branches write a terminal
  decoy row (tenant_id/request_id 0, status 4) so their DB cost -- and therefore latency -- is the
  same as a real enqueue (a zero-row UPDATE is measurably cheaper than a committed write). A worker
  that finds no request for a row discards it; terminal rows are pruned after the retention window.

Revision ID: 0123_dsar_mail_outbox
Revises: 0122_ingest_sources_public_id
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0123_dsar_mail_outbox"
down_revision = "0122_ingest_sources_public_id"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
CREATE TABLE IF NOT EXISTS dsar_mail_outbox (
    id               BIGSERIAL PRIMARY KEY,
    public_id        UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id        INTEGER NOT NULL,
    request_id       BIGINT NOT NULL,
    tenant_slug      TEXT NOT NULL,
    kind             SMALLINT NOT NULL,
    token_enc        TEXT,
    status           SMALLINT NOT NULL DEFAULT 1,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_until    TIMESTAMPTZ,
    lease_token      TEXT,
    last_reason      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    CONSTRAINT ck_dsar_mail_outbox_kind CHECK (kind BETWEEN 1 AND 2),
    CONSTRAINT ck_dsar_mail_outbox_status CHECK (status BETWEEN 1 AND 4)
)
""",
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_mail_outbox_due "
        "ON dsar_mail_outbox (next_attempt_at, id) WHERE status = 1"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_mail_outbox_request ON dsar_mail_outbox (request_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_dsar_mail_outbox_done "
        "ON dsar_mail_outbox (completed_at) WHERE status <> 1"
    ),
)

_DOWNGRADE = ("DROP TABLE IF EXISTS dsar_mail_outbox",)


def upgrade() -> None:
    """Create the outbox table and its indexes."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the outbox. Queued, unsent mails are discarded (requesters can resend)."""
    for statement in _DOWNGRADE:
        op.execute(statement)
