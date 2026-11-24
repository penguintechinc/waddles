-- Migration 088: seed the community chat process bundle.
--
-- Ported from `hub_api/services/community_chat.py` (v2 read-only chat
-- history + channel list queries). Implemented as a process-stage bundle
-- (`core/svc_process/bundles/community_chat_process.py`) that responds to
-- `!chat-history` and `!channels` commands and returns formatted replies.
--
-- **FLAG: DB Access Pattern Unclear.** This bundle's implementation is
-- currently minimal (returns placeholder responses) because process stages
-- in the runner do not receive a database connection. A full port would
-- need to resolve how process stages should access the DB if needed. See
-- the bundle's docstring for details.
--
-- Registered as `penguintechinc.waddles.chat-pipeline` (module=community,
-- feature=waddles.community.chat) with only a process stage. An action
-- stage (sending the reply back to the channel) uses existing platform
-- send infrastructure and is not duplicated here. No ingest stage needed
-- (community chat queries are command-driven, not webhook-triggered).
--
-- Migration 069's 3-tier config precedence applies: activation-scoped
-- config (app_activations or app_tenant_availability.config_defaults) can
-- override bundle defaults. No secrets/tokens in this bundle yet.
--
-- Activation is tenant-wide via app_tenant_availability (matching the
-- demo/bot convention) -- `hub_api/services/distribution_service.py`
-- requires an available row before a bundle appears in any stage's list.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.chat-pipeline',
    '1.0.0',
    'community',
    'penguintechinc.waddles',
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
ON CONFLICT (app_id) DO NOTHING;

INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.chat-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
