"""Register + tenant-wide activate the Kick bot connector app (gh-318).

`core/svc_ingest/bundles/kick_ingest.py` and
`core/svc_action/bundles/kick_send_action.py`
already ship working ingest normalize + action send_message entrypoints
for the Kick platform, ported alongside the Discord/Twitch connectors, but
the app_id has no `app_catalog` row -- without one, svc_ingest never
subscribes the platform's own `:ingest` Valkey key and svc_action never
subscribes the app_id's own `:action` key, mirroring the exact
routability gap `0014_wave1a_bundle_seeds`/`0016_moderation_enforce_app`/
`0017_loyalty_shoutout_apps`/`0018_slack_youtube_apps` each close for their own app_ids.

The `stages` blob mirrors the live `waddles.bot.discord.default` row's
own key shape (verified via `kubectl exec` psql, read-only: `ingest` uses
`entrypoint`/`consumes`/`config`/`spec`, `action` uses `entrypoint`/
`config`/`spec` -- discord's own row carries no `communication_model` key
on either stage). Kick's ingest stage includes `required_config:
["channel_slug"]` to specify the channel being monitored; action stage
requires `access_token_ref` for API authentication. This migration is
ingest+action only, not a missing-stage gap (no process stage: `!<command>`
dispatch for Kick routes in-process inside `core/svc_process/bundles/bot_process.py`
the same way discord/twitch already do).

Verified against the LIVE `app_catalog` table (`kubectl exec` psql,
read-only): no row exists yet for the app_id, so this is a plain
upsert, not an update onto pre-existing catalog state.

The INSERT is idempotent upsert (`ON CONFLICT ... DO UPDATE`), same
convention `0016_moderation_enforce_app`/`0017_loyalty_shoutout_apps`/
`0018_slack_youtube_apps` establish, so a partially-seeded or drifted
row self-heals back to this migration's exact `stages`/`config_defaults`
on re-run. Kick's `config_defaults` seeds all four secret refs
`kick_send_action.py` needs: `access_token_ref` (for chat sending),
`client_id_ref` and `client_secret_ref` (for OAuth2 credential exchange),
and `webhook_secret_ref` (for webhook signature validation on inbound
events). `app_tenant_availability`'s `config_defaults` merge
(`COALESCE(...) || EXCLUDED...`) never clobbers an admin-set key,
matching `0017`'s own rationale.

Revision ID: 0019_kick_app
Revises: 0018_slack_youtube_apps
Create Date: 2026-09-11
"""

from alembic import op

revision = "0019_kick_app"
down_revision = "0018_slack_youtube_apps"
branch_labels = None
depends_on = None

KICK_APP_ID = "waddles.bot.kick.default"
TENANT_SLUG = "global"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching 0009_music_catalog's/0016_moderation_enforce_app's/
    # 0017_loyalty_shoutout_apps'/0018_slack_youtube_apps' own rationale:
    # `alembic upgrade --sql`'s offline literal_binds renderer can silently
    # emit NULL for a bind param cast into `::jsonb`). The full `(... || ...)::jsonb`
    # wrap is required even for this two-stage blob -- a bare `... || '...'::jsonb`
    # casts ONLY the last literal, not the full concatenation (the exact
    # regression 0014_wave1a_bundle_seeds' own test suite guards against).
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{KICK_APP_ID}',
            '1.0.0',
            'bot',
            'waddles.bot.kick',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"ingest": {{"entrypoint": "bundles.kick_ingest:normalize", ' ||
                '"consumes": ["kick.message"], "config": {{}}, "spec": ' ||
                '{{"required_config": ["channel_slug"]}}}}, ' ||
                '"action": {{"entrypoint": "bundles.kick_send_action:send_message", ' ||
                '"spec": {{"required_config": ["access_token_ref"]}}, ' ||
                '"config": {{"api_base": "https://kick.com/api/v2"}}}}}}'
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
        SELECT t.id, '{KICK_APP_ID}', TRUE,
            '{{"access_token_ref": "KICK_ACCESS_TOKEN",
               "client_id_ref": "KICK_CLIENT_ID",
               "client_secret_ref": "KICK_CLIENT_SECRET",
               "webhook_secret_ref": "KICK_WEBHOOK_SECRET"}}'::jsonb
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
        WHERE app_id = '{KICK_APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{KICK_APP_ID}'")
