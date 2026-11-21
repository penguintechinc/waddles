-- Migration 087: register social.welcome process+action bundle
--
-- Ported from action/interactive/welcome_interaction_module (v2.x). Implements
-- first-message welcome detection and dispatch using the v3 App Bundle pattern.
--
-- Two stages compose this bundle:
-- - process (social_welcome_process.transform): detects first-time users, marks
--   them as welcomed atomically, and generates a welcome message (template-based
--   for now; AI personalization deferred to feature flag integration).
-- - action (social_welcome_action.send_welcome): sends the welcome message as a
--   reply in the original channel, using the platform's native send-message API.
--
-- Database tables:
-- - activity_message_events (migration 044) -- cheap read to short-circuit repeat visitors
-- - community_welcomed_users (migration 068) -- atomic write-once-per-user guard with
--   UNIQUE(community_id, platform, platform_user_id) to enforce concurrency safety
--
-- Encryption at rest: engine-level (security.md Storage baseline). No column-level
-- action needed here; app_catalog.stages is JSONB, encrypted by the database engine.

BEGIN;

-- Register the social.welcome bundle in app_catalog
-- Stages: process (detect + mark welcome), action (send message)
-- Config: empty defaults; per-activation channel_id and token_ref come via
--   app_tenant_availability.config_defaults / app_activations.config
--   (migration 069's 3-tier precedence)
INSERT INTO app_catalog (
    app_id,
    manifest_version,
    module,
    feature,
    provider,
    execution_model,
    is_default,
    platform_compatibility,
    status,
    stages
) VALUES (
    'penguintechinc.waddles.welcome-pipeline',
    '1.0.0',
    'social',
    'penguintechinc.waddles',
    'builtin',
    'native',
    FALSE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{'
        '"process": {'
        '  "entrypoint": "bundles.social_welcome_process:transform",'
        '  "config": {},'
        '  "spec": {"required_config": []}'
        '},'
        '"action": {'
        '  "entrypoint": "bundles.social_welcome_action:send_welcome",'
        '  "config": {},'
        '  "spec": {'
        '    "required_config": ["channel_id", "api_token_ref"]'
        '  }'
        '}'
        '}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;

-- Activate the bundle tenant-wide (every community in 'global' tenant can use it)
-- The 'global' tenant is the reserved pseudo-tenant containing per-environment
-- defaults and test fixtures.
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.welcome-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;

COMMIT;
