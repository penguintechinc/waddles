"""The Kick ingest handler's manifest.

Mirrors `builtin_handlers/slack_gateway_manifest.py`'s own precedent exactly (see
that module's own docstring for why `is_default=True` + no real
`InstallationLookup` wiring is a deliberate, documented MVP scope choice)
-- registered into svc-ingest's own in-process `flask_core.app_registry.
AppRegistry` at startup (a future `app.py::_register_kick_receivers`
wiring, out of this PR's scope; see `receivers/kick_pusher.py`'s own
module docstring for why).

`app_id` is `waddles.bot.kick.default` -- the SAME app_id the action stage
uses (`core/svc_action/builtin_handlers/kick_send_action.py`), matching T8
convergence's "one app_id across all stages" precedent
(`083_discord_twitch_demo_convergence.sql`). The pipeline keys every
Valkey stream by `(tenant, community, app_id, stage)`
(`flask_core.stream_pipeline.bundle_stream_key`): the Kick receiver's
fan-out (`receivers/kick_pusher.py`'s `CONSUMES_TAG`, a future `app.py`'s
`_register_kick_receivers`) would LPUSH onto `...:app:{app_id}:ingest`
using THIS manifest's `app_id` -- without this manifest,
`fanout.fan_out_event` finds zero consumers for `kick.message` and every
inbound Kick chat event is silently dropped (`gateway.fanout_no_
consumers`), the exact same documented gap `builtin_handlers/slack_ingest.py`'s own
docstring calls out for Slack.

`receivers/kick_pusher.py`'s `CONSUMES_TAG = "kick.message"` is the SINGLE
tag this manifest declares -- one Pusher connection per channel slug,
`community=<slug>`, matching `TwitchIrcReceiver`'s own per-channel lease
precedent (`builtin_handlers/twitch_gateway_manifest.py`'s own docstring). A future
`app.py` wiring would build one `KickPusherReceiver` per
`Config.KICK_CHANNELS` entry (a comma-separated channel-slug list env var,
mirroring `Config.TWITCH_CHANNELS`'s identical "no DB-backed channel list
yet" MVP posture -- see `core/svc_ingest/config.py`'s own `TWITCH_CHANNELS`
docstring), one channel slug per socket-leased receiver instance -- out of
this PR's scope (CREATE-ONLY boundary; `config.py`/`app.py` are not
touched beyond appending the env var *names* this PR's own task spec
calls for).

This manifest does NOT set `stages.ingest.communication_model` -- that
field is thirdparty-vendor-only (`webhook_push`/`rest_pull`, `hub_api/
services/marketplace_execution_service.py`), not a place to classify a
native/builtin handler's own transport. The receiver's transport shape (a
persistent inbound socket) is declared in CODE instead -- `receivers/
kick_pusher.py`'s `KickPusherReceiver` subclasses the shared
`waddle_transports.Transport` ABC (`name = "kick_pusher"`, `directions =
frozenset({Direction.INBOUND})`), implementing `receive(config) ->
AsyncIterator[Mapping]` per that library's own contract rather than a
bespoke `run()`/`stop()` shape.
"""

from __future__ import annotations

from typing import Any

from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.
#: parse_manifest` at registration time, never constructed as an
#: `AppManifest` directly (see that module's own docstring on why).
KICK_GATEWAY_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.kick.default",
    "name": "Kick Gateway Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.kick",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            # Run by the poll-drain loop (runner.py), NOT the Pusher
            # receiver directly -- the receiver only fans the raw event
            # out onto this handler's `:ingest` Valkey key
            # (`bundle_stream_key`); `runner.py`'s own poll loop RPOPs it
            # and calls this entrypoint exactly like every other ingest
            # handler.
            "entrypoint": "builtin_handlers.kick_ingest:normalize",
            "consumes": ["kick.message"],
        }
    },
}


def register_default_bundles(registry: AppRegistry) -> AppManifest:
    """Load + register `KICK_GATEWAY_MANIFEST` into `registry`. Returns the parsed manifest."""
    return registry.load(KICK_GATEWAY_MANIFEST)
