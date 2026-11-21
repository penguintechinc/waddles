-- Migration 089: seed the community polls App Bundle (process + action stages).
--
-- Ported from `hub_api/blueprints/v1/community_polls.py`'s REST API
-- (GET/POST/DELETE /polls endpoints) into a chat-command-driven App Bundle.
-- The port adds new chat-command interface (!poll create/vote/close/list/view)
-- alongside the existing REST API.
--
-- Process stage (`bundles.community_polls_process:transform`):
--   - Parses poll commands from chat text
--   - Executes state changes (create/vote/close) against existing
--     `community_polls`, `poll_options`, `poll_votes` tables (migration 028)
--   - Handles both read operations (list, view) and write coordination
--   - Returns formatted reply text or None for non-poll chatter
--
-- Action stage (`bundles.community_polls_action:send_poll_reply`):
--   - Sends formatted reply (built by process stage) to triggering channel
--   - Reply-in-place (event payload channel_id/channel_name) or config fallback
--   - Delegates actual send to runner's transport adapters
--
-- FLAGs (implementation uncertainty, noted but not blocking):
--   - [FLAG] User ID resolution: process stage uses placeholder `created_by=1`
--     instead of mapping `event.actor` (username string) to `hub_users.id`.
--     Proper implementation requires actor→user lookup table or UUID-based
--     actor field.
--   - [FLAG] Community context: process stage reads `event.payload["community_id"]`
--     which assumes it's populated by ingest stage. Real implementation should
--     extract community_id from StageEnvelope.community (tenant context) or
--     validate tenant/community membership.
--   - [FLAG] External API integration: action stage is intentionally minimal
--     (returns TransportResult without actual send). Full implementation would
--     either call platform-specific APIs (Discord/Twitch) or rely on
--     action_target transport adapters. Current pattern follows migration 082's
--     model where adapters handle the real send.
--
-- Database contract: uses existing tables from migration 028.
--   - community_polls (id, community_id, created_by, title, description,
--     view_visibility, submit_visibility, allow_multiple_choices, max_choices,
--     expires_at, is_active, created_at, updated_at)
--   - poll_options (id, poll_id, option_text, sort_order)
--   - poll_votes (id, poll_id, option_id, user_id, ip_hash, voted_at)
--
-- Activation: tenant-wide (every community can use it).
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.polls-pipeline',
    '1.0.0',
    'community',
    'penguintechinc.waddles',
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
ON CONFLICT (app_id) DO NOTHING;

-- Activate tenant-wide
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.polls-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
