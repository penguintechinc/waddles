"""Built-in action-stage handlers compiled into the svc-action image.

These are NOT installable app bundles. Each module is plain Python that
svc-action imports in-process: an `app_catalog.stages.action.entrypoint` of
`builtin_handlers.<module>:<fn>` is resolved by
`flask_core.stage_runner.load_entrypoint`, and the code ships and versions
with this service image. Installable (WASI) app bundles live under the
top-level `bundles/` tree and run in the bundle executor instead.

The package is named `builtin_handlers`, not `builtins`: a package called
`builtins` can never be imported (it collides with Python's own `builtins`
module).

Holds the platform send handlers (`discord_send_action.py`, `twitch_send_action.py`,
...) and the feature action handlers (`community_*_action.py`, `social_*_action.py`, ...).
"""
