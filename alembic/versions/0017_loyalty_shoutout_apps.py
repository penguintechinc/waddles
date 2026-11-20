"""Register + tenant-wide activate the loyalty and shoutout action bundles (gh-316, gh-317).

`libs/community_module/features.py` (`community.loyalty` Feature, flag
`waddles.community.loyalty`, default App `waddles.community.loyalty.
default`) and `libs/bot_module/features.py` (`bot.shoutout` Feature, flag
`waddles.bot.shoutout`, default App `penguintechinc.waddles.shoutout-pipeline`) both
already declare their default App manifests -- but without a matching
`app_catalog` row, svc_process/svc_action never subscribe to either
app_id's own `:action` Valkey key and neither app's action stage is ever
actually dispatched, mirroring `0016_moderation_enforce_app`'s own gap.

Both Features list `surfaces = ("process", "action")`, but neither gets a
catalog `process` stage here: `!<command>` dispatch for both runs
in-process inside `core/svc_process/bundles/bot_process.py`'s own
`_FEATURE_MODULES` router (`bundles.community_loyalty_process` /
`bundles.social_shoutout_process` -- the latter already registered under
`"so"`/`"shoutout"`/`"vso"`), which stamps `PROCESS_TARGET_APP_ID_KEY`
onto the routed event so it lands on this app_id's own `:action` key (see
`bundles.social_shoutout_process`'s own docstring for the exact mechanism,
already live and ported by this same convention). This is the identical
action-only precedent `0016`/`093_streaming_stream_bundle` (ported by
`0014_wave1a_bundle_seeds`) already establish -- the "process" surface is
real but not a catalog-registered stage.

Both action entrypoints share `moderation_enforce_action`'s /
`social_music_action`'s reply shape: Discord Bot API
(`https://discord.com/api/v10`) via a `bot_token_ref`-resolved token,
reusing the same shared Discord bot connection/secret
(`DISCORD_BOT_TOKEN`) already live for `penguintechinc.waddles.discord-pipeline`/
`penguintechinc.waddles.music-pipeline`/`penguintechinc.waddles.moderation-pipeline`'s
own `global`-tenant rows (`bundles.twitch_shoutout_action:shoutout`
itself only reaches this Discord path when the reply channel's platform
resolves to non-Twitch -- see that bundle's own module docstring; its
Twitch-platform reply path uses the relay outbound IRC transport instead,
not this catalog row's `config`).

Verified against the LIVE `app_catalog` table (`kubectl exec` psql,
read-only): no row exists yet for either app_id, so both are plain
upserts, not an update-in-place onto pre-existing catalog state.

Both INSERTs are idempotent upserts (`ON CONFLICT ... DO UPDATE`), same
convention `0016_moderation_enforce_app` established, so a partially-
seeded or drifted row self-heals back to this migration's exact
`stages`/`config_defaults` on re-run. `app_tenant_availability`'s
`config_defaults` merge (`COALESCE(...) || EXCLUDED...`) never clobbers an
admin-set key, matching `0016`'s own rationale for a brand-new app_id.

Revision ID: 0017_loyalty_shoutout_apps
Revises: 0016_moderation_enforce_app
Create Date: 2026-09-11
"""

from alembic import op

revision = "0017_loyalty_shoutout_apps"
down_revision = "0016_moderation_enforce_app"
branch_labels = None
depends_on = None

LOYALTY_APP_ID = "penguintechinc.waddles.loyalty-pipeline"
SHOUTOUT_APP_ID = "penguintechinc.waddles.shoutout-pipeline"
TENANT_SLUG = "global"
BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching 0009_music_catalog's/0016_moderation_enforce_app's
    # own rationale: `alembic upgrade --sql`'s offline literal_binds
    # renderer can silently emit NULL for a bind param cast into `::jsonb`).
    # The full `(... || ...)::jsonb` wrap is required even for this
    # shorter, single-stage blob -- a bare `... || '...'::jsonb` casts ONLY
    # the last literal, not the full concatenation (the exact regression
    # 0014_wave1a_bundle_seeds' own test suite guards against).
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{LOYALTY_APP_ID}',
            '1.0.0',
            'community',
            'waddles.community.loyalty',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"action": {{"entrypoint": "bundles.community_loyalty_action:loyalty", ' ||
                '"config": {{"api_base": "https://discord.com/api/v10"}}, "spec": ' ||
                '{{"required_config": ["bot_token_ref"]}}}}}}'
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
        SELECT t.id, '{LOYALTY_APP_ID}', TRUE, '{{"bot_token_ref": "{BOT_TOKEN_REF}"}}'::jsonb
        FROM tenants t
        WHERE t.slug = '{TENANT_SLUG}'
        ON CONFLICT (tenant_id, app_id) DO UPDATE SET
            config_defaults = COALESCE(app_tenant_availability.config_defaults, '{{}}'::jsonb)
                || EXCLUDED.config_defaults
        """
    )

    # `penguintechinc.waddles.shoutout-pipeline` -- if a catalog row already exists
    # for this app_id (none does live, verified above), this upsert folds
    # the action entrypoint into it rather than inserting a duplicate.
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{SHOUTOUT_APP_ID}',
            '1.0.0',
            'bot',
            'waddles.bot.shoutout',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"action": {{"entrypoint": "bundles.twitch_shoutout_action:shoutout", ' ||
                '"config": {{"api_base": "https://discord.com/api/v10"}}, "spec": ' ||
                '{{"required_config": ["bot_token_ref"]}}}}}}'
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
        SELECT t.id, '{SHOUTOUT_APP_ID}', TRUE, '{{"bot_token_ref": "{BOT_TOKEN_REF}"}}'::jsonb
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
        WHERE app_id = '{SHOUTOUT_APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{SHOUTOUT_APP_ID}'")

    op.execute(
        f"""
        DELETE FROM app_tenant_availability
        WHERE app_id = '{LOYALTY_APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{LOYALTY_APP_ID}'")
