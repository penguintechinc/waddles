-- Migration 090: seed the community announcements process + action bundle.
--
-- Ported from `hub_api/services/community_announcements.py`'s publish +
-- broadcast logic into the App Bundle SDK's process + action stages
-- (migrations 082/083's pattern). Two-stage pipeline:
--
-- 1. Process (bundles.community_announcements_process:transform):
--    Parse the `!announce publish <id>` command, look up the announcement
--    from the database, validate it's publishable, enrich the event with
--    announcement data + target platforms, return the event or None
--    (no reply if not an announcement command).
--
-- 2. Action (bundles.community_announcements_action:broadcast_announcement):
--    Fan out to every active community_server matching the target platforms,
--    POST the announcement to each server's platform action endpoint
--    (discord/twitch/slack/youtube), record results in announcement_broadcasts,
--    return a typed TransportResult.
--
-- Tables announcements, announcement_broadcasts, community_servers already
-- exist (migration 000). This migration creates the app_catalog row and
-- activates it for the 'global' tenant (all communities can use it).
--
-- Config carries only platform endpoint defaults (from env vars). Per-
-- activation config (channel_id overrides, token refs) supplied when a
-- community/tenant activates the bundle (app_activations.config /
-- app_tenant_availability.config_defaults, migration 069's 3-tier
-- precedence). No secrets ever stored in app_catalog.
--
-- DB Access Pattern Note: Both process and action bundles create local
-- penguin-dal instances from environment variables (DB_TYPE, DB_HOST,
-- DB_PORT, DB_NAME, DB_USER, DB_PASS) to query announcements, community_
-- servers, and record announcement_broadcasts. This assumes DB credentials
-- are available in the svc-process and svc-action container environments
-- at runtime -- same pattern as the runner's own connection pooling.
-- (Flagged: action-stage entrypoint contract does not provide DAL parameter;
-- future refinement could pass pre-instantiated DAL via config.)
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

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
ON CONFLICT (app_id) DO NOTHING;

-- Activate for the 'global' tenant (all communities can use it)
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.announcements-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
