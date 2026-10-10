"""The Twitch chat + EventSub ingest handlers' manifests.

Registered into svc-ingest's own in-process `flask_core.app_registry.
AppRegistry` at startup (`app.py`), separate from -- and NOT loaded via --
hub-api's distribution HTTP endpoint the way the poll-drain loop
(`runner.py`) loads its handlers. Mirrors `builtin_handlers/discord_gateway_
manifest.py`'s own precedent -- see that module's docstring for why
`is_default=True` + no real `InstallationLookup` wiring is a deliberate,
documented MVP scope choice.

Two Apps registered, under two DIFFERENT Features -- `flask_core.
app_manifest`'s `_FEATURE_RE`/`_APP_ID_RE` require `feature` to be exactly
`waddles.<module>.<feature>` (3 segments) and `app_id`'s own first three
segments to equal it, so a `feature` value cannot itself contain a
further `.`-separated qualifier the way an earlier draft of this module
tried (`waddles.bot.twitch.chat` is a valid 4-segment *app_id* shape, not
a valid 3-segment *feature* shape -- and `_APP_ID_RE` itself caps app_id
at exactly 4 segments total, so `waddles.bot.twitch.chat.default` is not
a legal app_id either):

- `waddles.bot.twitch.default` -- the IRC chat receiver
  (`receivers/twitch_irc.py`), feature `waddles.bot.twitch` (matches
  Discord's own `waddles.bot.discord` built-in app convention exactly). Same
  app_id the action stage uses (`core/svc_action/builtin_handlers/
  twitch_send_action.py`) and the unified 3-stage `app_catalog` row
  (`083_discord_twitch_demo_convergence.sql`, on the merged `feature/
  v3-svc-gateway-discord` branch) describes -- T8 convergence: an earlier
  draft used a separate `waddles.bot.twitch.gateway` app_id for ingest
  alone, which never connected to the action-only DB row seeded under
  `waddles.bot.twitch.default` (the pipeline keys every Valkey stream by
  `(tenant, community, app_id, stage)`, `flask_core.stream_pipeline.
  bundle_stream_key` -- the receiver's fan-out and the poll-drain loop
  must agree on app_id or the ingest event never reaches the poll loop).
  Does NOT set `stages.ingest.communication_model` -- that field is
  thirdparty-vendor-only (`webhook_push`/`rest_pull`,
  `flask_core.app_manifest.KNOWN_COMMUNICATION_MODELS`, `hub_api/
  services/marketplace_execution_service.py`), not a place to classify a
  native/builtin handler's own transport; `parse_manifest` rejects any
  other value outright (`ManifestError`), which an earlier draft of this
  manifest hit at every svc-ingest startup by setting `"gateway_socket"`
  here -- confirmed via a live `register_default_bundles()` call, fixed
  by dropping the key, matching `builtin_handlers/discord_gateway_manifest.py`'s
  own precedent exactly. The receiver's transport shape (a persistent
  inbound socket) is declared in CODE instead -- `receivers/twitch_irc.py`'s
  `TwitchIrcReceiver` subclasses the shared `waddle_transports.Transport`
  ABC (`name = "twitch_irc"`, `directions = frozenset({Direction.
  INBOUND})`).
- `waddles.bot.twitchevents.eventsub` -- the EventSub webhook handler
  (`eventsub.py`), feature `waddles.bot.twitchevents` (a single token,
  deliberately NOT `waddles.bot.twitch` -- `AppRegistry.register` rejects
  a second `is_default=True` App per feature, and this handler needs to be
  its own feature's default so `fanout.resolve_consuming_apps`'s
  `resolve_apps` fallback finds IT, not the chat gateway, when no
  `InstallationLookup` row exists yet), `communication_model=
  "webhook_push"` (the existing StageSpec value for a thirdparty-push-fed
  stage -- EventSub genuinely IS a webhook Twitch calls into this
  service, unlike the IRC socket the chat gateway holds). Out of the v3
  demo's scope (Twitch chat ingest/process/action only) -- registered
  in-process same as before, but no `app_catalog` seed row as of this PR,
  so it is not yet discoverable by the poll-drain loop; deferred, not a
  regression (it was never wired to a process/action stage either).
"""

from __future__ import annotations

from typing import Any

from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.
#: parse_manifest` at registration time, never constructed as an
#: `AppManifest` directly (see that module's own docstring on why).
TWITCH_GATEWAY_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.twitch.default",
    "name": "Twitch Chat Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.twitch",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            # Run by the poll-drain loop (runner.py), NOT the IRC
            # receiver directly -- the receiver only fans the raw event
            # out onto this handler's `:ingest` Valkey key
            # (`bundle_stream_key`); `runner.py`'s own poll loop RPOPs it
            # and calls this entrypoint exactly like every other ingest
            # handler.
            "entrypoint": "builtin_handlers.twitch_ingest:normalize",
            "consumes": ["twitch.message"],
        }
    },
}

TWITCH_EVENTSUB_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.twitchevents.eventsub",
    "name": "Twitch EventSub Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.twitchevents",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            "entrypoint": "builtin_handlers.twitch_eventsub_ingest:normalize",
            "consumes": ["twitch.eventsub"],
            "communication_model": "webhook_push",
        }
    },
}


def register_default_bundles(registry: AppRegistry) -> tuple[AppManifest, AppManifest]:
    """Load + register both Twitch manifests into `registry`. Returns `(gateway, eventsub)`."""
    gateway = registry.load(TWITCH_GATEWAY_MANIFEST)
    eventsub = registry.load(TWITCH_EVENTSUB_MANIFEST)
    return gateway, eventsub
