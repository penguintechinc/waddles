-- Migration 085: seed the social quote process + action bundle.
--
-- Ports the v2 quote_interaction_module (action/interactive/) into the v3
-- App Bundle SDK with process + action stages. Ported from:
-- - process logic: action/interactive/quote_interaction_module/
--   (command parsing, quote lookup, random selection)
-- - action logic: DB writes (add quote) and platform reply send
--
-- The bundle follows the `bundles.<module>:<function>` entrypoint convention
-- (migrations 071, 082, 084):
--   - `bundles.social_quote_process:transform` — process stage
--   - `bundles.social_quote_action:send_message` — action stage
--
-- Process stage: parses !quote commands (add/get/<id>/random), does read
-- operations (quote lookup), and builds reply text. Write intentions (quote
-- add) are stored in event payload for action stage to execute.
--
-- Action stage: executes any quote writes (INSERT), resolves channel
-- (reply-in-place or config fallback), and sends via Discord/Twitch.
--
-- Config carries non-secret defaults (api_base for Discord). Per-activation
-- values (channel_id, bot_token_ref) belong in app_activations.config or
-- app_tenant_availability.config_defaults (migration 069's 3-tier precedence),
-- never seeded here. `bot_token_ref` is an env-var *name*, resolved at
-- dispatch time via waddle_transports.signing.resolve_secret -- no token
-- is ever stored in this table or any app-bundle config JSON.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

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
ON CONFLICT (app_id) DO NOTHING;

-- Activate bundle tenant-wide (every community under the global tenant can use it)
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.quote-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
