"""`bundle_disabled_apps` -- a narrow view of globally disabled bundles for the data plane.

A super-admin global disable sets `app_catalog.status = 'disabled'`
(`hub_api/services/bundle_global_state_service.py`). The Rust active-set reads
(`core/bundle_active_set`) must exclude those apps, but they run as the restricted
`waddles_bundle_reader` role, which has (deliberately) no SELECT on `app_catalog`.
This view exposes ONLY the `app_id` of disabled apps -- no manifest, author or other
catalog column -- and the reader gets SELECT on the view alone.

Chain note: down_revision is the current release head (0092). Rebase this onto
whatever head is on `release/v3.0.X` when it merges (single alembic head).
"""

from alembic import op

revision = "0095_bundle_disabled_apps_view"
down_revision = "0092_leaderboard_config_toggles"
branch_labels = None
depends_on = None

_VIEW = "bundle_disabled_apps"
_ROLE = "waddles_bundle_reader"


def upgrade() -> None:
    """Create the view and grant it to the bundle reader (when that role already exists)."""
    op.execute(
        f"CREATE OR REPLACE VIEW {_VIEW} AS "  # nosec B608 -- fixed literal
        "SELECT app_id FROM app_catalog WHERE status = 'disabled'"
    )
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') "
        f"THEN GRANT SELECT ON {_VIEW} TO {_ROLE}; END IF; END $$;"
    )


def downgrade() -> None:
    """Drop the view."""
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
