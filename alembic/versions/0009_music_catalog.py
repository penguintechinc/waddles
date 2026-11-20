"""Register + tenant-wide activate the social music app bundle.

Registers `penguintechinc.waddles.music-pipeline` in `app_catalog` -- process
stage `bundles.social_music_process:transform` parses `!sr`/
`!songrequest`, action stage `bundles.social_music_action:
enqueue_song_request` enqueues into the hub-api Music Station
(`hub_api/blueprints/v1/community_music_queue.py`) via the service-key-
gated internal endpoint added alongside this migration, then replies
in-place. Without this row, svc_process/svc_action never subscribe to
`penguintechinc.waddles.music-pipeline`'s `:process`/`:action` Valkey keys and
`!sr`/`!songrequest` -- despite being wired into `bot_process.py`'s
command router -- is never actually dispatched. Mirrors
`0007_forum_catalog`'s exact shape (tenant-wide activation via
`app_tenant_availability`, not per-community `app_activations`).

Revision ID: 0009_music_catalog
Revises: 0008_moderation_config
Create Date: 2026-09-09
"""

from alembic import op

revision = "0009_music_catalog"
down_revision = "0008_moderation_config"
branch_labels = None
depends_on = None

APP_ID = "penguintechinc.waddles.music-pipeline"
TENANT_SLUG = "global"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching 0007_forum_catalog's own rationale: `alembic
    # upgrade --sql`'s offline literal_binds renderer can silently emit
    # NULL for a bind param cast into `::jsonb`).
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{APP_ID}',
            '1.0.0',
            'social',
            'waddles.social.music',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"process": {{"entrypoint": "bundles.social_music_process:transform", ' ||
                '"config": {{}}, "spec": {{"required_config": []}}}}, ' ||
                '"action": {{"entrypoint": "bundles.social_music_action:enqueue_song_request", ' ||
                '"config": {{"api_base": "https://discord.com/api/v10"}}, "spec": ' ||
                '{{"required_config": ["channel_id", "bot_token_ref"]}}}}}}'
            )::jsonb
        )
        ON CONFLICT (app_id) DO NOTHING
        """
    )

    # Activate for the global tenant (all communities) -- mirrors migration
    # 0007 (forums), 088 (community chat), 093 (streaming): tenant-wide via
    # app_tenant_availability, keyed off the 'global' tenant seeded in
    # migration 058.
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
