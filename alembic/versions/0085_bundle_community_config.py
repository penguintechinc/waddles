"""Per-community bundle config: versioned, append-only (generic bundle-admin gateway).

Every app bundle's admin UI reads/writes its per-community config through hub-api's generic
gateway (`docs/BUNDLE_ADMIN_GATEWAY.md`) -- no bundle-specific hub-api code. The accepted
config is validated against the bundle manifest's `community_config_schema` before it lands here.
Rows are never updated: a save INSERTs `version = previous + 1`; the highest version is the
live config and older versions stay as history (rollback = a new save of an older body). The
`UNIQUE (tenant_id, community_id, app_id, version)` constraint is the optimistic-concurrency
guard -- two racing saves of the same next version cannot both commit.

PII: `config` is bundle-schema-validated settings; the gateway refuses to log it. `updated_by`
is `hub_users.id` only (never a name/email).

Revision ID: 0085_bundle_community_config
Revises: 0084_user_active_context
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0085_bundle_community_config"
down_revision = "0084_user_active_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_community_config (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            community_id INTEGER NOT NULL REFERENCES communities(id),
            app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
            version INTEGER NOT NULL CHECK (version >= 1),
            config JSONB NOT NULL,
            schema_sha256 VARCHAR(64) NOT NULL,
            updated_by INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, community_id, app_id, version)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE bundle_community_config IS "
        "'Append-only per-community bundle config; highest version = live (gateway-validated)'"
    )
    op.execute(
        "COMMENT ON COLUMN bundle_community_config.updated_by IS "
        "'hub_users.id -- PII tokenization: never a name or email'"
    )


def downgrade() -> None:
    # Destroys every community's saved bundle config history -- an explicit operator choice.
    op.execute("DROP TABLE IF EXISTS bundle_community_config")
