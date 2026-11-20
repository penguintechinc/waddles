"""Wave 1a: port the bundle catalog/activation seeds the legacy SQL runner
never applied on alpha (gh-298).

The legacy psql runner (`config/postgres/migrations/run-migrations.sh`)
last recorded `083_discord_twitch_demo_convergence` in alpha's
`schema_migrations` table. Files `084`-`096` exist in the repo but were
never applied there, so nine coded+tested App Bundles have no
`app_catalog`/`app_tenant_availability` rows and can't route:
085 social_quote, 086 social_alias, 087 social_welcome, 088 community_chat,
089 community_polls, 090 community_announcements, 092 marketing_engagement,
093 streaming_stream, 094 integrations_waddleai. Every entrypoint this
migration seeds resolves to a real, already-shipped module file under
`core/svc_process/bundles/` / `core/svc_action/bundles/` (verified by this
revision's own test, and by direct listing during authoring) -- this closes
a routability gap, not a missing-code gap.

Four of the thirteen numbered files (084, 091, 095, 096) are NOT re-ported
here -- live-DB inspection (`kubectl exec` psql, read-only) showed their
content already applied on alpha, most likely via the same T8/convergence
work referenced in this branch's recent commit history (`d22af3a4`,
`ef1b6465`, `b2191e81`) rather than through `schema_migrations`:
  - 084 (bot_process_entrypoint): `app_catalog.stages->process->entrypoint`
    for both `penguintechinc.waddles.discord-pipeline`/`penguintechinc.waddles.twitch-pipeline`
    is already `bundles.bot_process:transform`, not the echo-demo bundle
    084's UPDATE targets.
  - 091 (community_forums_bundle): `penguintechinc.waddles.forums-pipeline`
    already exists in `app_catalog`.
  - 095 (demo_seed): the `waddlebot` demo community (id=4), its Discord
    (Club Penguinz)/Twitch (penguinzplays) `community_servers` rows,
    `discord_bot`/`twitch_bot` `hub_modules`, and both
    `hub_module_installations` rows are already live -- nothing left to
    seed, so per this issue's instruction it is skipped outright rather
    than re-ported.
  - 096 (live_activity_events): the table (and its
    `idx_live_activity_events_community_id` index) already exists live.

All four are still legacy-`schema_migrations`-bookkept below (alongside the
nine actually ported) so the legacy runner can never attempt to re-apply
any of 084-096 against this or any other already-migrated DB again.

Every catalog INSERT is an upsert (`ON CONFLICT (app_id) DO UPDATE`) so a
partially-seeded or drifted `app_catalog` row self-heals back to the exact
`stages`/config the corresponding numbered SQL file defines, rather than
silently keeping stale data (`DO NOTHING` would). `app_tenant_availability`
activation rows stay `DO NOTHING` on conflict, matching every one of 085-094's
own source files -- an existing activation's `available`/`config_defaults`
is community/tenant-owned state this migration must never clobber.

Revision ID: 0014_wave1a_bundle_seeds
Revises: 0013_music_policy_yt_labels
Create Date: 2026-09-11
"""

from alembic import op

revision = "0014_wave1a_bundle_seeds"
down_revision = "0013_music_policy_yt_labels"
branch_labels = None
depends_on = None


# app_ids this migration's upgrade() seeds into app_catalog +
# app_tenant_availability. Ordered to match the source SQL files
# (085, 086, 087, 088, 089, 090, 092, 093, 094) -- 084/091/095/096 are
# bookkeeping-only (see module docstring), not in this list.
SEEDED_APP_IDS = (
    "penguintechinc.waddles.quote-pipeline",
    "penguintechinc.waddles.alias-pipeline",
    "penguintechinc.waddles.welcome-pipeline",
    "penguintechinc.waddles.chat-pipeline",
    "penguintechinc.waddles.polls-pipeline",
    "penguintechinc.waddles.announcements-pipeline",
    "penguintechinc.waddles.engagement-pipeline",
    "penguintechinc.waddles.stream-pipeline",
    "penguintechinc.waddles.waddleai",
)

# Every numbered legacy SQL file this revision accounts for -- ported
# (the 9 above) or bookkeeping-only (084/091/095/096, already live). A row
# per version stops `config/postgres/migrations/run-migrations.sh` from
# ever attempting any of these again, matching the exact `schema_migrations`
# shape `0001_baseline_from_sql_migrations.py` itself writes
# (`version` PRIMARY KEY, `applied_at` defaults to `CURRENT_TIMESTAMP`).
LEGACY_VERSIONS = (
    "084_bot_process_entrypoint",
    "085_social_quote_bundle",
    "086_social_alias_bundle",
    "087_social_welcome_bundle",
    "088_community_chat_bundle",
    "089_community_polls_bundle",
    "090_community_announcements_bundle",
    "091_community_forums_bundle",
    "092_marketing_engagement_bundle",
    "093_streaming_stream_bundle",
    "094_integrations_waddleai_bundle",
    "095_demo_seed",
    "096_live_activity_events",
)


def upgrade() -> None:
    # -- 085: social.quote (process + action), ported verbatim from
    # 085_social_quote_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.quote-pipeline',
            '1.0.0',
            'social',
            'waddles.social.quote',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.social_quote_process:transform", ' ||
                '"config": {}, "spec": {"required_config": []}}, ' ||
                '"action": {"entrypoint": "bundles.social_quote_action:send_message", ' ||
                '"config": {"api_base": "https://discord.com/api/v10"}, "spec": ' ||
                '{"required_config": ["channel_id", "bot_token_ref"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.quote-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 086: social.alias (process + action), ported verbatim from
    # 086_social_alias_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.alias-pipeline',
            '1.0.0',
            'social',
            'waddles.social.alias',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.social_alias_process:transform", ' ||
                '"config": {}, "spec": {"required_config": []}}, ' ||
                '"action": {"entrypoint": "bundles.social_alias_action:send_message", ' ||
                '"config": {}, "spec": {"required_config": ["channel_id", "bot_token_ref"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.alias-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 087: social.welcome (process + action), ported verbatim from
    # 087_social_welcome_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.welcome-pipeline',
            '1.0.0',
            'social',
            'waddles.social.welcome',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.social_welcome_process:transform", ' ||
                '"config": {}, "spec": {"required_config": []}}, ' ||
                '"action": {"entrypoint": "bundles.social_welcome_action:send_welcome", ' ||
                '"config": {}, "spec": {"required_config": ["channel_id", "api_token_ref"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.welcome-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 088: community.chat (process only -- no action stage; replies use
    # existing platform send infrastructure), ported verbatim from
    # 088_community_chat_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.chat-pipeline',
            '1.0.0',
            'community',
            'waddles.community.chat',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.community_chat_process:transform", ' ||
                '"config": {}, "spec": {"required_config": []}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.chat-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 089: community.polls (process + action), ported verbatim from
    # 089_community_polls_bundle.sql. Reads/writes existing
    # community_polls/poll_options/poll_votes tables (migration 028).
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.polls-pipeline',
            '1.0.0',
            'community',
            'waddles.community.polls',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.community_polls_process:transform", ' ||
                '"config": {}, "spec": {"required_config": []}}, ' ||
                '"action": {"entrypoint": "bundles.community_polls_action:send_poll_reply", ' ||
                '"config": {}, "spec": {"required_config": ["channel_id"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.polls-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 090: community.announcements (process + action), ported verbatim
    # from 090_community_announcements_bundle.sql. Reads/writes existing
    # announcements/announcement_broadcasts/community_servers tables
    # (migration 000).
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.announcements-pipeline',
            '1.0.0',
            'community',
            'waddles.community.announcements',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{'
                || '"process": {'
                || '  "entrypoint": "bundles.community_announcements_process:transform",'
                || '  "config": {},'
                || '  "spec": {"required_config": []}'
                || '},'
                || '"action": {'
                || '  "entrypoint": "bundles.community_announcements_action:broadcast_announcement",'
                || '  "config": {'
                || '    "discord_endpoint": "http://localhost:8070",'
                || '    "twitch_endpoint": "http://localhost:8072",'
                || '    "slack_endpoint": "http://localhost:8071",'
                || '    "youtube_endpoint": "http://localhost:8073"'
                || '  },'
                || '  "spec": {"required_config": []}'
                || '}'
                || '}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.announcements-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 092: marketing.engagement (process + action), ported verbatim from
    # 092_marketing_engagement_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.engagement-pipeline',
            '1.0.0',
            'marketing',
            'waddles.marketing.engagement',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"process": {"entrypoint": "bundles.marketing_engagement_process:transform", ' ||
                '"config": {}, "spec": {}}, ' ||
                '"action": {"entrypoint": "bundles.marketing_engagement_action:send_engagement_notification", ' ||
                '"config": {"api_base": "https://api.example/v1"}, ' ||
                '"spec": {"required_config": ["channel_id", "notification_token_ref"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.engagement-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 093: streaming.stream (action only -- read-only stream listings),
    # ported verbatim from 093_streaming_stream_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.stream-pipeline',
            '1.0.0',
            'streaming',
            'waddles.streaming.stream',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"action": {"entrypoint": "bundles.streaming_stream_action:list_streams", ' ||
                '"config": {}, "spec": {"required_config": []}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.stream-pipeline', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- 094: integrations.waddleai (action only -- thin HTTP client onto
    # hub-api's AI completions endpoint; policy/license enforcement lives in
    # hub-api itself), ported verbatim from 094_integrations_waddleai_bundle.sql.
    op.execute(
        """
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider, execution_model,
            is_default, platform_compatibility, status, stages
        ) VALUES (
            'penguintechinc.waddles.waddleai',
            '1.0.0',
            'integrations',
            'waddles.integrations.waddleai',
            'builtin',
            'native',
            FALSE,
            '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
            'active',
            (
                '{"action": {"entrypoint": "bundles.integrations_waddleai_action:waddleai_completion", ' ||
                '"config": {"max_tokens": 512, "temperature": 0.7}, ' ||
                '"spec": {"required_config": ["hub_api_base"]}}}'
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
        """
        INSERT INTO app_tenant_availability (tenant_id, app_id, available)
        SELECT t.id, 'penguintechinc.waddles.waddleai', TRUE
        FROM tenants t
        WHERE t.slug = 'global'
        ON CONFLICT (tenant_id, app_id) DO NOTHING
        """
    )

    # -- legacy schema_migrations bookkeeping for all thirteen numbered
    # files (084-096) -- see module docstring for which were actually
    # ported above vs. already live. Same shape 0001_baseline writes
    # (`version` PRIMARY KEY) so `ON CONFLICT DO NOTHING` needs no target.
    values = ", ".join(f"('{version}')" for version in LEGACY_VERSIONS)
    op.execute(
        f"INSERT INTO schema_migrations (version) VALUES {values} "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    # Reverse legacy bookkeeping first (no FK dependency either way, but
    # keeps upgrade/downgrade symmetric in the same top-to-bottom order).
    versions = ", ".join(f"'{version}'" for version in LEGACY_VERSIONS)
    op.execute(f"DELETE FROM schema_migrations WHERE version IN ({versions})")

    # app_tenant_availability has an FK onto app_catalog.app_id -- delete
    # activations before catalog rows.
    app_ids = ", ".join(f"'{app_id}'" for app_id in SEEDED_APP_IDS)
    op.execute(f"DELETE FROM app_tenant_availability WHERE app_id IN ({app_ids})")
    op.execute(f"DELETE FROM app_catalog WHERE app_id IN ({app_ids})")
