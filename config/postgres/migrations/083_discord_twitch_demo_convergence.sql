-- Migration 083: T8 convergence -- unify Discord + Twitch onto ONE app_id
-- per connector across all three pipeline stages (ingest/process/action).
--
-- The pipeline keys every Valkey stream by (tenant, community, app_id,
-- stage) (`flask_core.stream_pipeline.bundle_stream_key`): ingest LPUSHes
-- onto `<app_id>:process`, process LPUSHes onto `<app_id>:action`, action
-- RPOPs `<app_id>:action`. A connector only connects end to end if its
-- ingest, process, AND action stages all resolve the SAME app_id.
--
-- Before this migration, three independently-developed branches each
-- seeded a split app_id per connector that never connected:
--   - migration 082 (this file's own predecessor, `feature/v3-bundle-
--     discord-action`, already merged to release) seeded
--     `penguintechinc.waddles.discord-pipeline` with ONLY an `action` stage.
--   - `feature/v3-svc-gateway-discord`'s own (now-deleted)
--     `082_discord_gateway_bundle.sql` seeded a SEPARATE
--     `penguintechinc.waddles.bot-discord-gateway` with only an `ingest` stage.
--   - `feature/v3-connector-twitch`'s own (now-deleted)
--     `082_twitch_gateway_bundle.sql`/`083_twitch_send_action_bundle.sql`
--     split Twitch the same way: `penguintechinc.waddles.bot-twitch-gateway` (ingest
--     only) vs `penguintechinc.waddles.twitch-pipeline` (action only).
--
-- This migration merges Discord's ingest+process stages onto the ONE
-- `penguintechinc.waddles.discord-pipeline` row migration 082 already created (an
-- UPSERT, since 082 is immutable -- already shipped on release), and
-- inserts ONE fresh `penguintechinc.waddles.twitch-pipeline` row carrying all three
-- Twitch stages at once (no pre-existing release-side row to merge onto).
-- Both app_ids' in-process ingest manifests
-- (`core/svc_ingest/bundles/{discord_gateway,twitch_gateway}_manifest.py`)
-- were realigned onto these SAME app_ids in this same PR -- the receiver
-- fan-out (manifest-driven) and the poll-drain loop (this table-driven)
-- must agree on app_id or the ingest event never reaches the poll loop.
--
-- `process` reuses migration 071's demo `bundles.echo_process:transform`
-- (a real "hello"->"HELLO" round-trip, not a stub) -- no connector-specific
-- process bundle exists yet for the demo, per the v3 demo reconciliation
-- roadmap's explicit "no new process bundle for demo" scope decision.
--
-- Also activates both app_ids for the 'global' tenant via
-- `app_tenant_availability` (migration 069/058) -- `hub_api/services/
-- distribution_service.py`'s `list_bundles_for_stage` (what the
-- ingest/process/action poll loops actually call) requires an available
-- row before a bundle appears in ANY stage's distribution response, even
-- though `app_catalog.stages` already declares the stage. Tenant-wide
-- (no `app_activations` community row) matches the demo's guild/channel
-- -> community MVP posture: receivers fan out with `community=None`.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

-- Discord: merge ingest+process into the action-only row migration 082
-- already inserted. `stages || EXCLUDED.stages` is a top-level JSONB
-- merge -- 082's row only has an "action" key, this adds "ingest"/
-- "process" alongside it with no overlap, and the merge is idempotent
-- across re-applies (same two keys, same values every time).
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
    TRUE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"ingest": {"entrypoint": "bundles.discord_ingest:normalize", "config": {}, ' ||
        '"spec": {}, "consumes": ["discord.message"]}, ' ||
        '"process": {"entrypoint": "bundles.echo_process:transform", "config": {}, "spec": {}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO UPDATE
    SET stages = app_catalog.stages || EXCLUDED.stages,
        is_default = EXCLUDED.is_default;

-- Twitch: no pre-existing release-side row -- one clean 3-stage INSERT.
INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.twitch-pipeline',
    '1.0.0',
    'bot',
    'penguintechinc.waddles',
    'builtin',
    'native',
    TRUE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"ingest": {"entrypoint": "bundles.twitch_ingest:normalize", "config": {}, ' ||
        '"spec": {}, "consumes": ["twitch.message"], "communication_model": "gateway_socket"}, ' ||
        '"process": {"entrypoint": "bundles.echo_process:transform", "config": {}, "spec": {}}, ' ||
        '"action": {"entrypoint": "bundles.twitch_send_action:send_message", ' ||
        '"config": {"channel": "waddlebot"}, "spec": {"required_config": ["channel"]}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;

-- Tenant-wide activation for 'global' (migration 058's seeded tenant) --
-- required for either app_id to appear in ANY stage's distribution
-- response (see module docstring above).
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.discord-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;

INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.twitch-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
