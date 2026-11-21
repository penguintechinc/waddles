-- Migration 084: flip the process-stage entrypoint from the echo demo bundle
-- to the real command+keyword bot for both bot apps.
--
-- Migration 083 seeded `penguintechinc.waddles.discord-pipeline` and
-- `penguintechinc.waddles.twitch-pipeline` with `stages.process.entrypoint =
-- "bundles.echo_process:transform"` (071's demo uppercase round-trip --
-- explicitly "no connector-specific process bundle exists yet for the demo"
-- at the time). `core/svc_process/bundles/bot_process.py` is that real
-- bundle: a platform-agnostic command (!ping/!hello/!help/!echo/!waddle/
-- !roll/!flip) + keyword/greeting responder that returns `None` ("no
-- reply") for ordinary chatter instead of echoing it back -- the process
-- runner (`core/svc_process/runner.py`) now supports a `None` transform
-- result as a first-class "skip, don't enqueue" outcome.
--
-- `jsonb_set` touches ONLY `stages.process.entrypoint`, leaving each
-- app_id's `ingest`/`action` stage entries (and `process.config`/
-- `process.spec`) untouched. The WHERE clause additionally requires the
-- CURRENT value to be the echo bundle, so re-applying this migration (or
-- applying it after some other change already flipped the entrypoint) is a
-- no-op -- idempotent across re-applies, matching every other migration in
-- this directory.
--
-- `bundles.echo_process:transform` itself is NOT deleted or altered by
-- this migration -- migration 071's own `penguintechinc.waddles.echo-pipeline` app_id
-- keeps referencing it unchanged, so it remains a valid, live demo/test
-- bundle.
--
-- Deploy order (per this migration's own author): rebuild + deploy the new
-- svc-process image (it must have `bundles/bot_process.py` importable via
-- `importlib` before this migration's entrypoint value is live), THEN
-- apply this migration. Applying this migration first would point the
-- runner at an entrypoint the running image can't import yet, and
-- `EntrypointLoadError` would skip every event for both bot apps until the
-- image catches up.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

UPDATE app_catalog
SET stages = jsonb_set(
    stages,
    '{process,entrypoint}',
    '"bundles.bot_process:transform"'::jsonb
)
WHERE app_id IN ('penguintechinc.waddles.discord-pipeline', 'penguintechinc.waddles.twitch-pipeline')
    AND stages -> 'process' ->> 'entrypoint' = 'bundles.echo_process:transform';
