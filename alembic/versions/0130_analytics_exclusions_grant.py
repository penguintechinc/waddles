"""hub-api is the ONLY writer of `public.data_use_exclusions`: grant it SELECT, INSERT, DELETE.

0129 created the suppression list and revoked PUBLIC, granting the read-only analytics roles
SELECT and hub-api nothing on it. hub-api must record exclusions at act time (consent / erasure /
DSAR, same transaction) and clear only its own consent-reversible reasons, so it needs exactly
SELECT (idempotence checks, uuid lookups), INSERT and DELETE. No UPDATE, and no other role is
widened. Guarded by role existence (the minimal CI chain has no `hub_api` group role) and
idempotent (re-GRANT is a no-op). Downgrade revokes only what this revision granted.

Revision ID: 0130_analytics_exclusions_grant
Revises: 0129_analytics_foundation
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0130_analytics_exclusions_grant"
down_revision = "0129_analytics_foundation"
branch_labels = None
depends_on = None

_GRANT = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN\n"
    "    GRANT SELECT, INSERT, DELETE ON public.data_use_exclusions TO hub_api;\n"
    "  END IF;\n"
    "END $$"
)
_REVOKE = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN\n"
    "    REVOKE SELECT, INSERT, DELETE ON public.data_use_exclusions FROM hub_api;\n"
    "  END IF;\n"
    "END $$"
)


def upgrade() -> None:
    """Grant hub-api SELECT, INSERT, DELETE on the exclusions list (role-guarded, idempotent)."""
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_GRANT)


def downgrade() -> None:
    """Revoke exactly the privileges `upgrade()` granted."""
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_REVOKE)
