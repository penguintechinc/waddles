-- Migration 071: per-stage {entrypoint, config, spec} on app_catalog.
--
-- The App Bundle SDK App Bundle concept is the packaged combination of
-- {config + spec + script} for each stage (ingest/process/action). Migration
-- 069 (app_catalog/app_tenant_availability/app_activations) tracks WHICH
-- bundles are installed/available/activated, but never persisted the
-- per-stage entrypoint/config/spec triple itself -- there was no column for
-- it. The svc-ingest/svc-process stage-runners (docs/plans/2026-08-31-
-- app-bundle-sdk-design.md) need exactly that triple, served from the read
-- replica via GET /api/v1/distribution/bundles, without depending on the
-- (not-yet-wired-into-hub-api) in-process flask_core.app_registry.
--
-- `stages` mirrors flask_core.app_manifest.StageSpec's own field names
-- (entrypoint/config/spec) so the DB row and the in-memory manifest shape
-- never drift into two different vocabularies:
--   {"ingest":  {"entrypoint": "module:function", "config": {...}, "spec": {...}},
--    "process": {"entrypoint": "module:function", "config": {...}, "spec": {...}},
--    "action":  {...}}
--
-- `entrypoint` is a dotted `module:function` path resolved via importlib by
-- the stage-runner's own loader (flask_core.stage_runner.load_entrypoint) --
-- never raw code stored in the DB and never exec()'d, per security.md (no
-- injection surface: the module must already be an installed, vetted
-- package inside the stage-runner's own container image).
--
-- Encryption at rest: engine-level (PostgreSQL volume/TDE per security.md
-- Encryption -- Storage baseline). No column-level action needed here.

ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS stages JSONB DEFAULT '{}'::jsonb;

-- Demo bundle (global tier only -- hub-api is app_catalog's sole writer).
-- Activating it for a real (tenant, community) is a separate, explicit
-- app_activations/app_tenant_availability insert -- not seeded here, since
-- neither table is guaranteed to have a real community row in every
-- environment this migration runs against (unlike 058's seeded 'global'
-- tenant). ON CONFLICT DO NOTHING keeps this idempotent across re-applies.
INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.echo-pipeline',
    '1.0.0',
    'core',
    'waddles.core.demo',
    'builtin',
    'native',
    TRUE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"ingest": {"entrypoint": "bundles.echo_ingest:normalize", "config": {}, "spec": {}}, ' ||
        '"process": {"entrypoint": "bundles.echo_process:transform", "config": {}, "spec": {}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;
