"""Twitch EventSub desired-state ledger (`twitch_eventsub_subscriptions`).

The control plane owns *which* EventSub subscriptions should exist (community x Twitch
connection x event type+version); the Twitch platform bundle's reconcile hook executes
create/delete against Twitch with an app token and reports results back. Rows reference
the broadcaster only through the Twitch connection (`platform_connections`), never a raw
platform id. Status / type / failure reason are int enum codes (see
`hub_api/services/eventsub_desired_state.py`). The per-subscription webhook secret is sealed
AES-256-GCM with row-bound AAD in `secret_enc`; `callback_key` is the opaque URL path segment
the ingest intake resolves a secret by.

Revision ID: 0082_eventsub_desired_state
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0082_eventsub_desired_state"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None

_TABLE = "twitch_eventsub_subscriptions"


def upgrade() -> None:
    """Create the desired-state table and its lookup indexes."""
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            public_id UUID NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL REFERENCES communities(id),
            connection_id BIGINT NOT NULL REFERENCES platform_connections(id) ON DELETE CASCADE,
            sub_type SMALLINT NOT NULL CHECK (sub_type BETWEEN 1 AND 7),
            sub_version SMALLINT NOT NULL CHECK (sub_version BETWEEN 1 AND 9),
            status SMALLINT NOT NULL DEFAULT 1 CHECK (status BETWEEN 1 AND 6),
            twitch_subscription_id VARCHAR(64),
            callback_key VARCHAR(64) NOT NULL UNIQUE,
            callback_url TEXT NOT NULL,
            secret_enc TEXT NOT NULL,
            failure_reason SMALLINT
                CHECK (failure_reason IS NULL OR failure_reason BETWEEN 1 AND 7),
            failure_count INTEGER NOT NULL DEFAULT 0,
            issued_action SMALLINT CHECK (issued_action IS NULL OR issued_action IN (1, 2)),
            issued_nonce UUID,
            next_attempt_at TIMESTAMPTZ,
            last_reconciled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (community_id, connection_id, sub_type, sub_version)
        )
        """
    )
    op.execute(
        f"COMMENT ON TABLE {_TABLE} IS "
        "'Desired state of Twitch EventSub webhook subscriptions (control plane owns it; "
        "the Twitch bundle reconcile hook executes it). sub_type/status/failure_reason are "
        "int enum codes; secret_enc is AES-256-GCM sealed (AAD eventsub:<public_id>), never "
        "logged or returned except to the machine-authenticated reconcile caller.'"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_status ON {_TABLE} (status, next_attempt_at)"
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_connection ON {_TABLE} (connection_id)")


def downgrade() -> None:
    """Drop the desired-state table (subscriptions on Twitch's side are not touched)."""
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
