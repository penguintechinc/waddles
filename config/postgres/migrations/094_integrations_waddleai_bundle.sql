-- Migration 094: seed the WaddleAI integration action bundle.
--
-- Ported from `hub_api/services/ai_routing/` (v2). This bundle wraps the
-- existing hub-api `/api/v1/community/<community_id>/ai/completions` endpoint
-- via HTTP, sending a prompt from the triggering event's text and returning
-- the AI-generated response text as a reply in the channel (reply-in-place).
--
-- WaddleAI is an Enterprise feature gated by PostHog flag + license tier
-- (`services/ai_routing/router.py`'s `FEATURE_AI_ROUTING` + `FEATURE_AI_PREMIUM`
-- + `FEATURE_AI_BYOK`), enforced by the hub-api service itself. This bundle
-- is the thin HTTP client; policy enforcement lives in hub-api/router.py.
--
-- Config carries only non-secret defaults; per-activation `hub_api_base`
-- supplied at activation time (migration 069's 3-tier precedence). No token
-- is ever stored in this table; credentials are resolved via hub-api's own
-- JWT middleware.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

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
ON CONFLICT (app_id) DO NOTHING;

-- Activate the bundle for the global tenant (tenant-wide, all communities).
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.waddleai', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
