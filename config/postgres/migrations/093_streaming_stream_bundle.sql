-- Migration 093: seed the streaming live-stream listings action bundle.
--
-- Ported from `hub_api/services/stream_service.py` (v2). Provides three
-- query operations for live-stream listings:
--   - get_live_streams: all live streams ordered by viewer count
--   - get_featured_streams: top 5 live streams by viewer count
--   - get_stream_details: one stream by entity_id
--
-- All three queries read pre-existing `coordination` and `community_servers`
-- tables. No schema changes needed. The action bundle operates read-only
-- and returns formatted DTOs (JSON) via TransportResult.detail for audit
-- logging.
--
-- Follows the `bundles.<module>:<function>` entrypoint convention
-- (migration 071/082/084). Config carries no secrets; queries are
-- scoped to the caller's community_id from the event payload.
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
ON CONFLICT (app_id) DO NOTHING;

-- Activate for all communities in the global tenant (tenant-wide availability).
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.stream-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
