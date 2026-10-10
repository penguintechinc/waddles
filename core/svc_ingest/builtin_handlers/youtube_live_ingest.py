"""YouTube Live chat ingest handler + in-process manifest for `waddles.bot.youtube.default`.

Fanned out by this container's own YouTube Live poll receiver
(`receivers/youtube_live_poll.py`) via `fanout.fan_out_event` -- consumes
the raw event shape that receiver's `receive()` yields (`{platform,
channel_id, video_id, live_chat_id, author_id, display_name, is_mod,
is_owner, is_sponsor, text, message_id, published_at}`) and produces a
`flask_core.PlatformEvent`, the frozen stage-to-stage contract
(`libs/flask_core/flask_core/stream_pipeline.py`) -- mirrors `builtin_handlers/
twitch_ingest.py`'s own entrypoint/contract exactly (`normalize(raw) ->
PlatformEvent`, `ValueError` on a malformed raw event, caught per-event by
the ingest runner so one bad event never kills the poll loop).

Combines what Twitch splits across two files (`builtin_handlers/twitch_ingest.py` +
`builtin_handlers/twitch_gateway_manifest.py`) into this one module -- this PR's
own file-creation scope only covers a single ingest handler module for
YouTube, and `fanout.fan_out_event`'s `resolve_consuming_apps` (see that
module's own docstring) needs a manifest registered in svc-ingest's
in-process `flask_core.app_registry.AppRegistry` (`app.py`'s startup) to
find ANY app_id for `CONSUMES_TAG` at all -- without it, every YouTube
chat message would fan out to zero consumers and silently drop, the same
class of gap `builtin_handlers/twitch_gateway_manifest.py`'s own docstring
documents for Twitch.

`app_id` is `waddles.bot.youtube.default`, `feature` is `waddles.bot.
youtube` (`waddles.<module>.<feature>`, three segments -- `flask_core.
app_manifest`'s `_FEATURE_RE` contract, same as every other builtin
handler). Does NOT set `stages.ingest.communication_model` -- that field is
thirdparty-vendor-only (`flask_core.app_manifest.
KNOWN_COMMUNICATION_MODELS` == `{webhook_push, rest_pull}`); an earlier
Twitch draft set an out-of-enum value there and crashed `parse_manifest`
at every startup (`builtin_handlers/twitch_gateway_manifest.py`'s own documented
regression) -- this receiver's transport shape (Data API v3 polling) is
declared in CODE instead, via `receivers/youtube_live_poll.py`'s
`YouTubeLivePollReceiver` subclassing the shared `waddle_transports.
Transport` ABC. A future `action` stage (`builtin_handlers.youtube_send_action:
send_message`, replying into `live_chat_id` via `liveChatMessages.
insert`) is out of this PR's scope -- not registered here, see the PR's
own report for the combined `app_catalog` seed-migration JSON shape this
manifest must stay loosely coupled to (same `app_id`, same `ingest.
entrypoint`/`consumes`), matching `builtin_handlers/twitch_gateway_manifest.py`'s
own DB-row loose-coupling precedent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent
from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: The `consumes` tag `receivers/youtube_live_poll.py` fans out under --
#: this handler's half of that contract (mirrors `CONSUMES_TAG` in that
#: module).
CONSUMES_TAG = "youtube.message"

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.
#: parse_manifest` at registration time, never constructed as an
#: `AppManifest` directly (see `builtin_handlers/twitch_gateway_manifest.py`'s own
#: docstring on why).
YOUTUBE_LIVE_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.youtube.default",
    "name": "YouTube Live Chat Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.youtube",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            # Run by the poll-drain loop (runner.py), NOT the YouTube
            # receiver directly -- the receiver only fans the raw event
            # out onto this handler's `:ingest` Valkey key
            # (`bundle_stream_key`); `runner.py`'s own poll loop RPOPs it
            # and calls this entrypoint exactly like every other ingest
            # handler.
            "entrypoint": "builtin_handlers.youtube_live_ingest:normalize",
            "consumes": [CONSUMES_TAG],
        }
    },
}


def register_default_bundles(registry: AppRegistry) -> AppManifest:
    """Load + register `YOUTUBE_LIVE_MANIFEST` into `registry`. Returns the parsed manifest."""
    return registry.load(YOUTUBE_LIVE_MANIFEST)


def _as_str_or_none(value: object) -> str | None:
    """Non-empty `str` or `None` -- shared guard for every optional string field below."""
    return value if isinstance(value, str) and value else None


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw YouTube Live chat message event to a `PlatformEvent`.

    Real, working transform (not a stub): requires `text`/`live_chat_id`
    on the raw event (the two fields a downstream reply-capable action
    stage would need at minimum -- `live_chat_id` is YouTube's own
    equivalent of Twitch's `channel_name` for that purpose), trims `text`,
    and stamps `occurred_at` from the raw event's own `published_at`
    (falling back to a UTC "now" only when neither is present). Raises
    `ValueError` on a malformed raw event -- the ingest runner catches
    this per-event so one bad event never kills the poll loop
    (`core/svc_ingest/runner.py`).
    """
    text = raw.get("text")
    live_chat_id = raw.get("live_chat_id")
    if not isinstance(text, str) or not text:
        raise ValueError("raw YouTube Live event missing required 'text' string field")
    if not live_chat_id or not isinstance(live_chat_id, str):
        raise ValueError("raw YouTube Live event missing required 'live_chat_id' string field")

    actor = raw.get("author_id") or "unknown"
    return PlatformEvent(
        platform=raw.get("platform", "youtube"),
        event_type="message",
        actor=actor,
        payload={
            "text": text.strip(),
            "video_id": _as_str_or_none(raw.get("video_id")),
            "live_chat_id": live_chat_id,
            "author_id": _as_str_or_none(raw.get("author_id")),
            "display_name": _as_str_or_none(raw.get("display_name")),
            "is_mod": bool(raw.get("is_mod")),
            "is_owner": bool(raw.get("is_owner")),
            "is_sponsor": bool(raw.get("is_sponsor")),
            "message_id": _as_str_or_none(raw.get("message_id")),
            "published_at": _as_str_or_none(raw.get("published_at")),
        },
        occurred_at=raw.get("occurred_at")
        or raw.get("published_at")
        or datetime.now(UTC).isoformat(),
    )
