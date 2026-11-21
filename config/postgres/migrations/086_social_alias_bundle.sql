-- Migration 086: seed the social alias bundle (process + action stages).
--
-- Ported from action/interactive/alias_interaction_module (v2). Follows the
-- `bundles.<module>:<function>` entrypoint convention established by earlier
-- migrations. The process stage handles alias resolution and management commands;
-- the action stage sends responses back to Discord/Twitch.
--
-- Uses the existing command_aliases table (created in migration 013) to store
-- alias definitions. No token or secret is stored in app_catalog config.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.alias-pipeline',
    '1.0.0',
    'social',
    'penguintechinc.waddles',
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
ON CONFLICT (app_id) DO NOTHING;

INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.alias-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
