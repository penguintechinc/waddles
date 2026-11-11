"""marketplace_catalog: drop the legacy hub_modules / marketplace_modules UNION branches.

The legacy "modules" model is superseded by app bundles (`app_catalog` + `app_versions`), so the
catalog view no longer merges `hub_modules` ('core') or approved `marketplace_modules`
('marketplace'). It keeps only the `bundle` branch introduced by 0091, with the exact same column
names, order and types (including the four 0091 vendor columns), so the vendors/catalog API
contract and the pydal view binding are unchanged. The legacy tables and their data are left
untouched (GDPR erasure still covers them); only the view stops reading them.

The view is dropped and recreated (a column-type-preserving `CREATE OR REPLACE` is not possible
when the UNION is removed). Existing SELECT grants are captured and re-issued, as 0091 does. A DB
without the catalog view (minimal CI bootstrap) is skipped with a WARNING, same precedent as 0091.

Revision ID: 0092_catalog_bundles_only
Revises: 0091_marketplace_vendor_view
Create Date: 2026-11-11

Rebase risk: chained directly after the release-branch head 0091. If another PR lands a 0092
first, re-point `down_revision` at that head (and renumber this file) so the chain keeps a single
head.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0092_catalog_bundles_only"
down_revision = "0091_marketplace_vendor_view"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")


def _load_0091() -> ModuleType:
    """Load the 0091 migration module (its file name starts with a digit, so not importable)."""
    path = Path(__file__).with_name("0091_marketplace_vendor_view.py")
    spec = importlib.util.spec_from_file_location(
        "_mig_0091_marketplace_vendor_view", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle_only_view_sql(prev: ModuleType) -> str:
    """The `marketplace_catalog` definition with only the bundle branch (0091 column shape)."""
    bundle_tail = prev._tail("ac.source_url", "ac.app_id::text", "ac.author")
    return f"""
    CREATE VIEW marketplace_catalog AS
      SELECT
        'bundle'::text AS source, NULL::int AS source_id, ac.app_id::text AS name,
        COALESCE(ac.name, ac.app_id)::text AS display_name,
        NULL::text AS description, ac.category, NULL::text AS icon_url, false AS is_core,
        'free'::text AS pricing_type, 0 AS price_cents, 'flat'::text AS pricing_model,
        COALESCE(v.version, ac.manifest_version)::text AS version, ac.author,
        NULL::text AS webhook_url, NULL::text AS communication_model,
        NULL::text AS integration_type,
        0::numeric AS avg_rating, 0 AS review_count, COALESCE(act.n, 0)::int AS install_count,
        ac.installed_at AS created_at, ac.installed_at AS updated_at,
        NULL::int AS tenant_id,
        {bundle_tail}
      FROM app_catalog ac
      LEFT JOIN LATERAL (
        SELECT av.version FROM app_versions av
        WHERE av.app_id = ac.app_id ORDER BY av.created_at DESC, av.id DESC LIMIT 1
      ) v ON TRUE
      LEFT JOIN (
        SELECT app_id, COUNT(*) AS n FROM app_active_versions GROUP BY app_id
      ) act ON act.app_id = ac.app_id
      WHERE ac.status = 'active'
    """


def upgrade() -> None:
    """Rebuild the view with the bundle branch only (legacy module branches removed)."""
    prev = _load_0091()
    conn = op.get_bind()
    if not prev._view_exists(conn):
        _LOG.warning(
            "marketplace_catalog absent (CI bootstrap); skipping 0092 view rebuild"
        )
        return
    prev._recreate(conn, _bundle_only_view_sql(prev))


def downgrade() -> None:
    """Restore 0091's three-branch view (legacy branches read the retained legacy tables)."""
    prev = _load_0091()
    conn = op.get_bind()
    if not prev._view_exists(conn):
        return
    prev._recreate(conn, prev._view_sql())
