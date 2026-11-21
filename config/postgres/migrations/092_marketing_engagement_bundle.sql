-- Migration 092: seed the marketing engagement bundle (polls/forms).
--
-- Ported from `core/engagement_module/app.py` (v2) -- the REST API for
-- polls and forms with visibility controls. The v3 bundle implements process +
-- action stages for event-driven engagement workflows: the process stage
-- validates and passes through engagement events (poll creation, voting, form
-- submission), and the action stage sends engagement notifications to a
-- platform channel.
--
-- Both stages use the `penguintechinc.waddles.engagement-pipeline` app_id, following
-- the bundle naming convention established by migrations 071 (demo echo),
-- 082 (Discord send), and 083 (bot convergence).
--
-- Process stage (`bundles.marketing_engagement_process:transform`) validates
-- engagement event payloads (polls, forms, voting, submissions) and passes them
-- through to the action stage. Config carries no secrets, and requires no
-- runtime configuration.
--
-- Action stage (`bundles.marketing_engagement_action:send_engagement_notification`)
-- sends engagement notifications (poll announcements, form confirmations) to a
-- platform channel via HTTP API call, SSRF-guarded. Config carries only non-
-- secret defaults (`api_base`); per-activation channel_id and notification_token_ref
-- are supplied via app_activations.config or app_tenant_availability.config_defaults
-- (migration 069's 3-tier precedence), never seeded here. notification_token_ref
-- is an env-var *name*, resolved at dispatch time via
-- waddle_transports.signing.resolve_secret -- no token is ever stored in this
-- table or any app-bundle config JSON.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.engagement-pipeline',
    '1.0.0',
    'marketing',
    'penguintechinc.waddles',
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
ON CONFLICT (app_id) DO NOTHING;

INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.engagement-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
