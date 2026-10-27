"""The Slack ingest handler's manifest.

Registered into svc-ingest's own in-process `flask_core.app_registry.
AppRegistry` at startup (`app.py`), separate from -- and NOT loaded via --
hub-api's distribution HTTP endpoint the way the poll-drain loop
(`runner.py`) loads its handlers. Mirrors `builtin_handlers/discord_gateway_
manifest.py`'s own precedent exactly -- see that module's docstring for
why `is_default=True` + no real `InstallationLookup` wiring is a
deliberate, documented MVP scope choice.

`app_id` is `waddles.bot.slack.default` -- the SAME app_id the action
stage uses (`core/svc_action/builtin_handlers/slack_send_action.py`) per T8
convergence's "one app_id across all stages" precedent
(`083_discord_twitch_demo_convergence.sql`). The pipeline keys every
Valkey stream by `(tenant, community, app_id, stage)`
(`flask_core.stream_pipeline.bundle_stream_key`): the Slack receiver's
fan-out (`receivers/slack_socket.py`'s `CONSUMES_TAG`, `app.py`'s
`_register_slack_receiver`) LPUSHes onto `...:app:{app_id}:ingest` using
THIS manifest's `app_id` -- without this manifest, `fanout.fan_out_event`
finds zero consumers for `slack.message` and every inbound Slack event
is silently dropped (`gateway.fanout_no_consumers`), exactly the
documented gap `builtin_handlers/slack_ingest.py`'s own docstring calls out.

`receivers/slack_socket.py`'s `CONSUMES_TAG = "slack.message"` is the
SINGLE tag this manifest declares -- `SlackSocketReceiver._normalize_event`
normalizes `message`/`app_mention`/`member_joined_channel` events (its own
`_HANDLED_EVENT_TYPES`) into one common raw-dict shape before fan-out,
same one-tag-for-multiple-event-types shape `builtin_handlers/discord_ingest.py`'s
own `discord.message` tag already uses for Discord's own several message
subtypes -- there is no separate tag per Slack event type.

This manifest does NOT set `stages.ingest.communication_model` -- that
field is thirdparty-vendor-only (`webhook_push`/`rest_pull`,
`hub_api/services/marketplace_execution_service.py`), not a place to
classify a native/builtin handler's own transport. The receiver's transport
shape (a persistent inbound socket) is declared in CODE instead --
`receivers/slack_socket.py`'s `SlackSocketReceiver` subclasses the shared
`waddle_transports.Transport` ABC (`name = "slack_socket"`,
`directions = frozenset({Direction.INBOUND})`), implementing
`receive(config) -> AsyncIterator[Mapping]` per that library's own
contract rather than a bespoke `run()`/`stop()` shape.
"""

from __future__ import annotations

from typing import Any

from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.
#: parse_manifest` at registration time, never constructed as an
#: `AppManifest` directly (see that module's own docstring on why).
SLACK_GATEWAY_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.slack.default",
    "name": "Slack Gateway Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.slack",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            # Run by the poll-drain loop (runner.py), NOT the socket
            # receiver directly -- the receiver only fans the raw event
            # out onto this handler's `:ingest` Valkey key
            # (`bundle_stream_key`); `runner.py`'s own poll loop RPOPs it
            # and calls this entrypoint exactly like every other ingest
            # handler.
            "entrypoint": "builtin_handlers.slack_ingest:normalize",
            "consumes": ["slack.message"],
        }
    },
}


def register_default_bundles(registry: AppRegistry) -> AppManifest:
    """Load + register `SLACK_GATEWAY_MANIFEST` into `registry`. Returns the parsed manifest."""
    return registry.load(SLACK_GATEWAY_MANIFEST)
