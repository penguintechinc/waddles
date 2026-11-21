-- Migration 082: seed a real (not demo) ACTION-stage bundle -- Discord send-message.
--
-- Proof-of-work port of one real connector (action/pushing/
-- discord_action_module) into the App Bundle SDK's action-stage
-- script-entrypoint model, alongside the existing generic action_target
-- adapters (webhook/rest_api/message_queue/overlay/email,
-- core/svc_action/services/adapters/). `stages.action.entrypoint` follows
-- the exact `bundles.<module>:<function>` convention migration 071's demo
-- echo bundles established for ingest/process --
-- `bundles.discord_send_action:send_message`
-- (core/svc_action/bundles/discord_send_action.py).
--
-- `stages.action.config` carries only the bundle's own non-secret
-- defaults (`api_base`) -- `channel_id`/`bot_token_ref` are per-activation
-- config (app_activations.config / app_tenant_availability.config_defaults,
-- migration 069's 3-tier precedence) supplied when a tenant/community
-- actually activates this bundle, never seeded here. `bot_token_ref` is an
-- env-var *name*, resolved at dispatch time by
-- core/svc_action/services/signing.py::resolve_secret -- no bot token is
-- ever stored in this table or any app-bundle config JSON.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.discord-pipeline',
    '1.0.0',
    'bot',
    'penguintechinc.waddles',
    'builtin',
    'native',
    FALSE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"action": {"entrypoint": "bundles.discord_send_action:send_message", ' ||
        '"config": {"api_base": "https://discord.com/api/v10"}, "spec": ' ||
        '{"required_config": ["channel_id", "bot_token_ref"]}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;
