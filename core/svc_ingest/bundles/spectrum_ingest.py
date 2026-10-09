"""RSI Spectrum ingest bundle -- normalizes a raw fanned-out Spectrum item (gh #101).

Fanned out by `receivers/spectrum_poll.py`'s `SpectrumPollReceiver` (one-way,
Spectrum -> Waddles; there is intentionally NO action stage and no outbound
path, see that module's docstring). Mirrors `bundles/youtube_live_ingest.py`
exactly: this module holds both the manifest (registered into svc-ingest's
in-process `AppRegistry` by `app.py`) and the `normalize()` entrypoint the
poll-drain loop (`runner.py`) calls.

`app_id` is `waddles.bot.spectrum.default`; `feature` is `waddles.bot.spectrum`.
Ingestion itself is additionally gated by the `waddles.spectrum-integration`
flag (ENV baseline `FLAG_WADDLES_SPECTRUM_INTEGRATION`, default OFF) inside
the receiver -- a flag-OFF deployment fans out nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent
from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: The `consumes` tag `receivers/spectrum_poll.py` fans out under (mirrors that module's tag).
CONSUMES_TAG = "spectrum.message"

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.parse_manifest`
#: at registration time (see `bundles/twitch_gateway_manifest.py` on why not constructed directly).
SPECTRUM_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.spectrum.default",
    "name": "Star Citizen Spectrum Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.spectrum",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            "entrypoint": "bundles.spectrum_ingest:normalize",
            "consumes": [CONSUMES_TAG],
        }
    },
}

_VALID_KINDS = frozenset({"forum", "lobby"})


def register_default_bundles(registry: AppRegistry) -> AppManifest:
    """Load + register `SPECTRUM_MANIFEST` into `registry`. Returns the parsed manifest."""
    return registry.load(SPECTRUM_MANIFEST)


def _as_str_or_none(value: object) -> str | None:
    """Non-empty `str` or `None` -- shared guard for every optional string field below."""
    return value if isinstance(value, str) and value else None


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Spectrum item to a `PlatformEvent`.

    Real transform (not a stub): requires `text`, `source_id` and a valid
    `kind` (`forum`|`lobby`); `event_type` is `message` for a lobby message,
    `thread` for a new forum thread, `reply` for a forum reply. Raises
    `ValueError` on a malformed raw event -- the ingest runner catches this
    per-event so one bad item never kills the poll loop (`runner.py`).
    """
    text = raw.get("text")
    source_id = raw.get("source_id")
    kind = raw.get("kind")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("raw Spectrum event missing required 'text' string field")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("raw Spectrum event missing required 'source_id' string field")
    if kind not in _VALID_KINDS:
        raise ValueError("raw Spectrum event 'kind' must be 'forum' or 'lobby'")

    if kind == "lobby":
        event_type = "message"
    else:
        event_type = "reply" if raw.get("is_reply") else "thread"

    created_at = _as_str_or_none(raw.get("created_at"))
    return PlatformEvent(
        platform=raw.get("platform", "spectrum"),
        event_type=event_type,
        actor=_as_str_or_none(raw.get("author_id")) or "unknown",
        payload={
            "text": text.strip(),
            "kind": kind,
            "source_id": source_id,
            "thread_id": _as_str_or_none(raw.get("thread_id")),
            "message_id": _as_str_or_none(raw.get("message_id")),
            "author_id": _as_str_or_none(raw.get("author_id")),
            "display_name": _as_str_or_none(raw.get("display_name")),
            "created_at": created_at,
        },
        occurred_at=raw.get("occurred_at") or created_at or datetime.now(UTC).isoformat(),
    )
