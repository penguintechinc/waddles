"""Register + tenant-wide activate the Slack and YouTube bot connector apps (gh-318).

`core/svc_ingest/bundles/slack_ingest.py` / `youtube_live_ingest.py` and
`core/svc_action/bundles/slack_send_action.py` / `youtube_send_action.py`
already ship working ingest normalize + action send_message entrypoints
for both platforms, ported alongside the Discord/Twitch connectors, but
neither app_id has an `app_catalog` row -- without one, svc_ingest never
subscribes either platform's own `:ingest` Valkey key and svc_action never
subscribes either app_id's own `:action` key, mirroring the exact
routability gap `0014_wave1a_bundle_seeds`/`0016_moderation_enforce_app`/
`0017_loyalty_shoutout_apps` each close for their own app_ids.

Both `stages` blobs mirror the live `waddles.bot.discord.default` row's
own key shape (verified via `kubectl exec` psql, read-only: `ingest` uses
`entrypoint`/`consumes`/`config`/`spec`, `action` uses `entrypoint`/
`config`/`spec` -- discord's own row carries no `communication_model` key
on either stage). YouTube's `ingest` stage adds `communication_model:
"rest_pull"` -- unlike Discord/Slack, which both push events over a
persistent gateway/socket connection, the YouTube Live Chat API has no
push transport and svc_ingest must poll it, so this key flags that
polling behavior to the ingest runner rather than mirroring a key discord
doesn't have. Neither app declares a `process` stage: `!<command>` dispatch
for both routes in-process inside `core/svc_process/bundles/bot_process.py`
the same way discord/twitch already do, so this migration is action+ingest
only, not a missing-stage gap.

Verified against the LIVE `app_catalog` table (`kubectl exec` psql,
read-only): no row exists yet for either app_id, so both are plain
upserts, not updates onto pre-existing catalog state.

Both INSERTs are idempotent upserts (`ON CONFLICT ... DO UPDATE`), same
convention `0016_moderation_enforce_app`/`0017_loyalty_shoutout_apps`
establish, so a partially-seeded or drifted row self-heals back to this
migration's exact `stages`/`config_defaults` on re-run. Slack's
`config_defaults` seeds both `bot_token_ref` (Bot User OAuth Token, for
Web API calls) and `app_token_ref` (App-Level Token, for Socket Mode --
`slack_gateway_manifest.py`'s own connection needs both). YouTube's seeds
all four OAuth2 refresh-flow secret refs `youtube_send_action.py` needs to
mint short-lived access tokens (`api_key_ref` covers the read-only Data
API quota path `youtube_live_ingest.py` polls with). `app_tenant_
availability`'s `config_defaults` merge (`COALESCE(...) || EXCLUDED...`)
never clobbers an admin-set key, matching `0017`'s own rationale.

Revision ID: 0018_slack_youtube_apps
Revises: 0017_loyalty_shoutout_apps
Create Date: 2026-09-11
"""

from alembic import op

revision = "0018_slack_youtube_apps"
down_revision = "0017_loyalty_shoutout_apps"
branch_labels = None
depends_on = None

SLACK_APP_ID = "waddles.bot.slack.default"
YOUTUBE_APP_ID = "waddles.bot.youtube.default"
TENANT_SLUG = "global"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching 0009_music_catalog's/0016_moderation_enforce_app's/
    # 0017_loyalty_shoutout_apps' own rationale: `alembic upgrade --sql`'s
    # offline literal_binds renderer can silently emit NULL for a bind
    # param cast into `::jsonb`). The full `(... || ...)::jsonb` wrap is
    # required even for these two-stage blobs -- a bare `... || '...'::jsonb`
    # casts ONLY the last literal, not the full concatenation (the exact
    # regression 0014_wave1a_bundle_seeds' own test suite guards against).
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{SLACK_APP_ID}',
            '1.0.0',
            'bot',
            'waddles.bot.slack',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"ingest": {{"entrypoint": "bundles.slack_ingest:normalize", ' ||
                '"consumes": ["slack.message"], "config": {{}}, "spec": {{}}}}, ' ||
                '"action": {{"entrypoint": "bundles.slack_send_action:send_message", ' ||
                '"spec": {{"required_config": ["channel_id", "bot_token_ref"]}}, ' ||
                '"config": {{"api_base": "https://slack.com/api"}}}}}}'
            )::jsonb
        )
        ON CONFLICT (app_id) DO UPDATE SET
            manifest_version = EXCLUDED.manifest_version,
            module = EXCLUDED.module,
            feature = EXCLUDED.feature,
            provider = EXCLUDED.provider,
            execution_model = EXCLUDED.execution_model,
            is_default = EXCLUDED.is_default,
            platform_compatibility = EXCLUDED.platform_compatibility,
            status = EXCLUDED.status,
            stages = EXCLUDED.stages
        """
    )

    op.execute(
        f"""
        INSERT INTO app_tenant_availability (tenant_id, app_id, available, config_defaults)
        SELECT t.id, '{SLACK_APP_ID}', TRUE,
            '{{"bot_token_ref": "SLACK_BOT_TOKEN", "app_token_ref": "SLACK_APP_TOKEN"}}'::jsonb
        FROM tenants t
        WHERE t.slug = '{TENANT_SLUG}'
        ON CONFLICT (tenant_id, app_id) DO UPDATE SET
            config_defaults = COALESCE(app_tenant_availability.config_defaults, '{{}}'::jsonb)
                || EXCLUDED.config_defaults
        """
    )

    # `waddles.bot.youtube.default` -- if a catalog row already exists for
    # this app_id (none does live, verified above), this upsert folds the
    # ingest+action entrypoints into it rather than inserting a duplicate.
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{YOUTUBE_APP_ID}',
            '1.0.0',
            'bot',
            'waddles.bot.youtube',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"ingest": {{"entrypoint": "bundles.youtube_live_ingest:normalize", ' ||
                '"consumes": ["youtube.message"], "config": {{}}, "spec": ' ||
                '{{"required_config": ["channel_id"]}}, "communication_model": "rest_pull"}}, ' ||
                '"action": {{"entrypoint": "bundles.youtube_send_action:send_message", ' ||
                '"spec": {{"required_config": ["refresh_token_ref"]}}, ' ||
                '"config": {{"api_base": "https://www.googleapis.com/youtube/v3"}}}}}}'
            )::jsonb
        )
        ON CONFLICT (app_id) DO UPDATE SET
            manifest_version = EXCLUDED.manifest_version,
            module = EXCLUDED.module,
            feature = EXCLUDED.feature,
            provider = EXCLUDED.provider,
            execution_model = EXCLUDED.execution_model,
            is_default = EXCLUDED.is_default,
            platform_compatibility = EXCLUDED.platform_compatibility,
            status = EXCLUDED.status,
            stages = EXCLUDED.stages
        """
    )

    op.execute(
        f"""
        INSERT INTO app_tenant_availability (tenant_id, app_id, available, config_defaults)
        SELECT t.id, '{YOUTUBE_APP_ID}', TRUE,
            '{{"api_key_ref": "YOUTUBE_API_KEY",
               "client_id_ref": "YOUTUBE_CLIENT_ID",
               "client_secret_ref": "YOUTUBE_CLIENT_SECRET",
               "refresh_token_ref": "YOUTUBE_REFRESH_TOKEN"}}'::jsonb
        FROM tenants t
        WHERE t.slug = '{TENANT_SLUG}'
        ON CONFLICT (tenant_id, app_id) DO UPDATE SET
            config_defaults = COALESCE(app_tenant_availability.config_defaults, '{{}}'::jsonb)
                || EXCLUDED.config_defaults
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM app_tenant_availability
        WHERE app_id = '{YOUTUBE_APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{YOUTUBE_APP_ID}'")

    op.execute(
        f"""
        DELETE FROM app_tenant_availability
        WHERE app_id = '{SLACK_APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{SLACK_APP_ID}'")
