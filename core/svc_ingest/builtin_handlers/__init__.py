"""Built-in ingest-stage handlers compiled into the svc-ingest image.

These are NOT installable app bundles. Each module is plain Python that
svc-ingest imports in-process: an `app_catalog.stages.ingest.entrypoint` of
`builtin_handlers.<module>:<fn>` is resolved by
`flask_core.stage_runner.load_entrypoint`, and the code ships and versions
with this service image. Installable (WASI) app bundles live under the
top-level `bundles/` tree and run in the bundle executor instead.

The package is named `builtin_handlers`, not `builtins`: a package called
`builtins` can never be imported (it collides with Python's own `builtins`
module).

Includes the demo `echo_ingest` (`waddles.core.demo.echo`, seeded by migration
071) that proves the ingest -> process -> action pipeline executes end to end.
"""
