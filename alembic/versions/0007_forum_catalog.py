"""Register + tenant-wide activate the community forums app bundle.

Ports `config/postgres/migrations/091_community_forums_bundle.sql` into
Alembic -- that raw SQL file was never applied (0001_baseline only replays
legacy SQL files against a genuinely fresh DB; this DB's `schema_migrations`
table already had entries when 0001 ran, so the file-by-file loop was
skipped entirely). Without this row in `app_catalog`, svc_action never
subscribes to `penguintechinc.waddles.forums-pipeline`'s `:action` Valkey key and
`community_forums_action.create_forum_post` is never invoked. Without the
matching `app_tenant_availability` row, the bundle is registered but not
activated for any tenant. Activation is tenant-wide via
`app_tenant_availability` (matching the currently-active community/social
bundles seeded by migrations 088 and 093), not per-community
`app_activations`.

Revision ID: 0007_forum_catalog
Revises: 0006_forum_nullable
Create Date: 2026-09-06
"""

from alembic import op

revision = "0007_forum_catalog"
down_revision = "0006_forum_nullable"
branch_labels = None
depends_on = None

APP_ID = "penguintechinc.waddles.forums-pipeline"
TENANT_SLUG = "global"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching the source-of-truth raw SQL in migrations 088/091/
    # 093) rather than passed as sa.text() bind params: SQLAlchemy's
    # literal_binds offline-SQL renderer (`alembic upgrade --sql`) cannot
    # reliably substitute untyped bind params -- e.g. into `::jsonb` casts --
    # and silently emits NULL, verified against this exact migration.
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{APP_ID}',
            '1.0.0',
            'community',
            'penguintechinc.waddles',
            'builtin',
            'native',
            TRUE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"process": {{"entrypoint": "bundles.community_forums_process:transform", ' ||
                '"config": {{}}, "spec": {{"required_config": []}}}}, ' ||
                '"action": {{"entrypoint": "bundles.community_forums_action:create_forum_post", ' ||
                '"config": {{}}, "spec": {{"required_config": ["channel_id"]}}}}}}'
            )::jsonb
        )
        ON CONFLICT (app_id) DO NOTHING
        """
    )

    # Activate for the global tenant (all communities) -- mirrors how
    # migrations 088 (community chat) and 093 (streaming) activate their
    # bundles: tenant-wide via app_tenant_availability, keyed off the
    # 'global' tenant seeded in migration 058.
    op.execute(
        f"""
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, '{APP_ID}', TRUE
        FROM tenants t
        WHERE t.slug = '{TENANT_SLUG}'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM app_tenant_availability
        WHERE app_id = '{APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )

    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{APP_ID}'")
