"""marketplace_catalog: surface the bundle registry and derive a vendor (GitHub org) per row.

Until now the `marketplace_catalog` view (059) merged only `hub_modules` and approved
`marketplace_modules`; the real bundle registry (`app_catalog` + `app_versions`, written by the
core-bundle seeder and the third-party install path) never reached it, so the marketplace showed
a handful of demo rows while ~160 bundles existed. This migration:

1. Backfills `app_catalog.author`/`source_url` from the newest `app_versions` row for catalog rows
   that predate the seeder writing attribution (existing alpha DBs). Idempotent -- only NULLs are
   filled, never an existing value.
2. Re-creates the view with a third `bundle` branch (active `app_catalog` rows; the version is the
   newest `app_versions.version`, `source_id` is NULL -- bundles are addressed by `source_ref` =
   `app_id`) and four appended columns on EVERY branch:
   `source_url`, `source_ref`, `vendor_key`, `source_repo`.
3. `vendor_key` = lower-cased GitHub org: from a canonical `github:<org>` author, else from a
   `https://github.com/<org>/...` source URL. `source_repo` = `<org>/<repo>` from the source URL.
   A vendor marketplace is one GitHub org; the repo is a sub-grouping (type) inside it. Rows with
   neither stay NULL (legacy/third-party rows with a plain-name author) -- the read path treats a
   NULL vendor on a `bundle` row as an error, never a silent bucket.

The view is dropped and recreated (not `CREATE OR REPLACE`): UNION branch type resolution may widen
column types, which `REPLACE` refuses. Existing SELECT grants are captured and re-issued so a role
never loses read access mid-migration. A DB without the legacy catalog view (minimal CI bootstrap)
is skipped with a WARNING, same precedent as 0062.

Revision ID: 0091_marketplace_vendor_view
Revises: 0090_kick_connection
Create Date: 2026-11-11
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0091_marketplace_vendor_view"
down_revision = "0090_kick_connection"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")

# Org from a canonical `github:<org>` author, else from a github.com source URL.
_VENDOR_KEY = """
    COALESCE(
        CASE WHEN {author} ~* '^github:[A-Za-z0-9-]+$' THEN lower(substr({author}, 8)) END,
        lower(substring({url} from '^https://github\\.com/([A-Za-z0-9-]+)(?:/|$)'))
    )
"""
_SOURCE_REPO = """
    regexp_replace(
        lower(substring({url} from '^https://github\\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)')),
        '\\.git$', ''
    )
"""

_COMMON_TAIL = """
    {url} AS source_url,
    {ref} AS source_ref,
    {vendor} AS vendor_key,
    {repo} AS source_repo
"""


def _tail(url: str, ref: str, author: str) -> str:
    """Appended columns for one UNION branch (`author`/`url` are SQL expressions)."""
    return _COMMON_TAIL.format(
        url=url,
        ref=ref,
        vendor=_VENDOR_KEY.format(author=author, url=url),
        repo=_SOURCE_REPO.format(url=url),
    )


def _view_sql() -> str:
    """The full new `marketplace_catalog` definition (059's two branches + `bundle`)."""
    core_tail = _tail("NULL::text", "NULL::text", "m.author")
    market_tail = _tail(
        "NULL::text", "NULL::text", "COALESCE(ms.display_name, 'Independent')"
    )
    bundle_tail = _tail("ac.source_url", "ac.app_id::text", "ac.author")
    return f"""
    CREATE VIEW marketplace_catalog AS
      SELECT
        'core'::text AS source, m.id AS source_id, m.name,
        COALESCE(m.display_name, m.name) AS display_name, m.description, m.category,
        m.icon_url, m.is_core, 'free'::text AS pricing_type, 0 AS price_cents,
        'flat'::text AS pricing_model, m.version, m.author,
        NULL::text AS webhook_url, NULL::text AS communication_model,
        NULL::text AS integration_type,
        COALESCE(AVG(r.rating), 0) AS avg_rating,
        COUNT(DISTINCT r.id)::int AS review_count,
        COUNT(DISTINCT inst.id)::int AS install_count,
        m.created_at, m.updated_at, NULL::int AS tenant_id,
        {core_tail}
      FROM hub_modules m
      LEFT JOIN hub_module_reviews r ON r.module_id = m.id
      LEFT JOIN hub_module_installations inst ON inst.module_id = m.id
      WHERE m.is_published = true
      GROUP BY m.id

      UNION ALL

      SELECT
        'marketplace'::text, mm.id, mm.name, mm.name, mm.description, mm.category,
        mm.icon_url, false, mm.pricing_type, mm.price_cents, mm.pricing_model, mm.version,
        COALESCE(ms.display_name, 'Independent'), mm.webhook_url, mm.communication_model,
        mm.integration_type,
        CASE WHEN mm.rating_count > 0
          THEN mm.rating_sum::numeric / mm.rating_count ELSE 0 END,
        mm.rating_count, mm.install_count, mm.created_at, mm.updated_at, mm.tenant_id,
        {market_tail}
      FROM marketplace_modules mm
      LEFT JOIN marketplace_sellers ms ON ms.id = mm.seller_id
      WHERE mm.status = 'approved' AND mm.deleted_at IS NULL

      UNION ALL

      SELECT
        'bundle'::text, NULL::int, ac.app_id::text, COALESCE(ac.name, ac.app_id)::text,
        NULL::text, ac.category, NULL::text, false, 'free'::text, 0, 'flat'::text,
        COALESCE(v.version, ac.manifest_version)::text, ac.author,
        NULL::text, NULL::text, NULL::text,
        0::numeric, 0, COALESCE(act.n, 0)::int, ac.installed_at, ac.installed_at,
        NULL::int,
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


_OLD_VIEW_SQL = """
    CREATE VIEW marketplace_catalog AS
      SELECT
        'core'::text AS source, m.id AS source_id, m.name,
        COALESCE(m.display_name, m.name) AS display_name, m.description, m.category,
        m.icon_url, m.is_core, 'free'::text AS pricing_type, 0 AS price_cents,
        'flat'::text AS pricing_model, m.version, m.author,
        NULL::text AS webhook_url, NULL::text AS communication_model,
        NULL::text AS integration_type,
        COALESCE(AVG(r.rating), 0) AS avg_rating,
        COUNT(DISTINCT r.id)::int AS review_count,
        COUNT(DISTINCT inst.id)::int AS install_count,
        m.created_at, m.updated_at, NULL::int AS tenant_id
      FROM hub_modules m
      LEFT JOIN hub_module_reviews r ON r.module_id = m.id
      LEFT JOIN hub_module_installations inst ON inst.module_id = m.id
      WHERE m.is_published = true
      GROUP BY m.id
      UNION ALL
      SELECT
        'marketplace'::text, mm.id, mm.name, mm.name, mm.description, mm.category,
        mm.icon_url, false, mm.pricing_type, mm.price_cents, mm.pricing_model, mm.version,
        COALESCE(ms.display_name, 'Independent'), mm.webhook_url, mm.communication_model,
        mm.integration_type,
        CASE WHEN mm.rating_count > 0
          THEN mm.rating_sum::numeric / mm.rating_count ELSE 0 END,
        mm.rating_count, mm.install_count, mm.created_at, mm.updated_at, mm.tenant_id
      FROM marketplace_modules mm
      LEFT JOIN marketplace_sellers ms ON ms.id = mm.seller_id
      WHERE mm.status = 'approved' AND mm.deleted_at IS NULL
"""


def _view_exists(conn: sa.engine.Connection) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass('public.marketplace_catalog') IS NOT NULL")
        ).scalar()
    )


def _recreate(conn: sa.engine.Connection, sql: str) -> None:
    """Drop + create the view, re-issuing the SELECT grants it carried."""
    grantees = [
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND table_name = 'marketplace_catalog' "
                "AND privilege_type = 'SELECT' AND grantee <> current_user"
            )
        )
    ]
    op.execute("DROP VIEW marketplace_catalog")
    op.execute(sql)
    for grantee in grantees:
        op.execute(f'GRANT SELECT ON marketplace_catalog TO "{grantee}"')


def upgrade() -> None:
    """Backfill bundle attribution, then rebuild the view with the bundle branch + vendor cols."""
    conn = op.get_bind()
    if not _view_exists(conn):
        _LOG.warning(
            "marketplace_catalog absent (CI bootstrap); skipping 0091 view rebuild"
        )
        return
    op.execute(
        """
        UPDATE app_catalog ac
           SET author = COALESCE(ac.author, lv.author),
               source_url = COALESCE(ac.source_url, lv.source_url)
          FROM (
            SELECT DISTINCT ON (app_id) app_id, author, source_url
              FROM app_versions
             WHERE author IS NOT NULL OR source_url IS NOT NULL
             ORDER BY app_id, created_at DESC, id DESC
          ) lv
         WHERE lv.app_id = ac.app_id
           AND (ac.author IS NULL OR ac.source_url IS NULL)
        """
    )
    _recreate(conn, _view_sql())


def downgrade() -> None:
    """Restore 059's two-branch view (drops the bundle branch and the four appended columns)."""
    conn = op.get_bind()
    if not _view_exists(conn):
        return
    _recreate(conn, _OLD_VIEW_SQL)
