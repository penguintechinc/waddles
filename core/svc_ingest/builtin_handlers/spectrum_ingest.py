"""RSI Spectrum ingest bundle -- normalizes a raw fanned-out Spectrum item (gh #101).

Fanned out by `receivers/spectrum_poll.py`'s `SpectrumPollReceiver` (one-way,
Spectrum -> Waddles; there is intentionally NO action stage and no outbound
path, see that module's docstring). Mirrors `builtin_handlers/youtube_live_ingest.py`
exactly: this module holds both the manifest (registered into svc-ingest's
in-process `AppRegistry` by `app.py`) and the `normalize()` entrypoint the
poll-drain loop (`runner.py`) calls.

Two raw shapes arrive, told apart by `kind`:

* `forum` / `lobby` -- a message/thread/reply (`consumes` tag `spectrum.message`),
  normalized to `message` / `thread` / `reply`.
* `roster` / `events` -- an org-sync CHANGE (`consumes` tag `spectrum.org`),
  normalized to `member_joined` / `member_left` / `member_roles_changed` and
  `org_event_created` / `_updated` / `_cancelled` / `_removed` / `_rsvp_changed`.

`app_id` is `waddles.bot.spectrum.default`; `feature` is `waddles.bot.spectrum`.
Ingestion itself is additionally gated by the `waddles.spectrum-integration`
flag (ENV baseline `FLAG_WADDLES_SPECTRUM_INTEGRATION`, default OFF) inside
the receiver -- a flag-OFF deployment fans out nothing -- and org sync by the
second flag `waddles.spectrum-org-sync` (`FLAG_WADDLES_SPECTRUM_ORG_SYNC`, default OFF).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent
from flask_core.app_manifest import AppManifest
from flask_core.app_registry import AppRegistry

#: The `consumes` tags `receivers/spectrum_poll.py` fans out under (mirror that module's tags).
CONSUMES_TAG = "spectrum.message"
ORG_CONSUMES_TAG = "spectrum.org"

#: Raw manifest dict -- validated + parsed via `flask_core.app_manifest.parse_manifest`
#: at registration time (see `builtin_handlers/twitch_gateway_manifest.py` on why it is
#: not constructed directly).
SPECTRUM_MANIFEST: dict[str, Any] = {
    "app_id": "waddles.bot.spectrum.default",
    "name": "Star Citizen Spectrum Ingest",
    "version": "1.1.0",
    "feature": "waddles.bot.spectrum",
    "module": "bot",
    "provider": "builtin",
    "is_default": True,
    "stages": {
        "ingest": {
            "entrypoint": "builtin_handlers.spectrum_ingest:normalize",
            "consumes": [CONSUMES_TAG, ORG_CONSUMES_TAG],
        }
    },
}

_MESSAGE_KINDS = frozenset({"forum", "lobby"})
_ROSTER_EVENT_TYPES = {
    "joined": "member_joined",
    "left": "member_left",
    "roles_changed": "member_roles_changed",
}
_ORG_EVENT_TYPES = {
    "created": "org_event_created",
    "updated": "org_event_updated",
    "cancelled": "org_event_cancelled",
    "removed": "org_event_removed",
    "rsvp_changed": "org_event_rsvp_changed",
}


def register_default_bundles(registry: AppRegistry) -> AppManifest:
    """Load + register `SPECTRUM_MANIFEST` into `registry`. Returns the parsed manifest."""
    return registry.load(SPECTRUM_MANIFEST)


def _as_str_or_none(value: object) -> str | None:
    """Non-empty `str` or `None` -- shared guard for every optional string field below."""
    return value if isinstance(value, str) and value else None


def _as_str_list(value: object) -> list[str]:
    """`value` as a list of non-empty strings; anything else is an empty list."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


def _normalize_roster(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one roster change (`joined` / `left` / `roles_changed`)."""
    change = raw.get("change")
    event_type = _ROSTER_EVENT_TYPES.get(change) if isinstance(change, str) else None
    member_id = _as_str_or_none(raw.get("member_id"))
    source_id = _as_str_or_none(raw.get("source_id"))
    if event_type is None:
        raise ValueError("raw Spectrum roster event 'change' must be joined|left|roles_changed")
    if member_id is None:
        raise ValueError("raw Spectrum roster event missing required 'member_id' string field")
    if source_id is None:
        raise ValueError("raw Spectrum event missing required 'source_id' string field")
    observed_at = _as_str_or_none(raw.get("observed_at"))
    return PlatformEvent(
        platform=raw.get("platform", "spectrum"),
        event_type=event_type,
        actor=member_id,
        payload={
            "kind": "roster",
            "source_id": source_id,
            "member_id": member_id,
            "display_name": _as_str_or_none(raw.get("display_name")),
            "roles_added": _as_str_list(raw.get("roles_added")),
            "roles_removed": _as_str_list(raw.get("roles_removed")),
            "role_names": _as_str_list(raw.get("role_names")),
        },
        occurred_at=observed_at or datetime.now(UTC).isoformat(),
    )


def _normalize_org_event(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one org-event change (`created` / `updated` / `cancelled` / ...)."""
    change = raw.get("change")
    event_type = _ORG_EVENT_TYPES.get(change) if isinstance(change, str) else None
    event_id = _as_str_or_none(raw.get("event_id"))
    source_id = _as_str_or_none(raw.get("source_id"))
    if event_type is None:
        raise ValueError(
            "raw Spectrum event 'change' must be created|updated|cancelled|removed|rsvp_changed"
        )
    if event_id is None:
        raise ValueError("raw Spectrum org event missing required 'event_id' string field")
    if source_id is None:
        raise ValueError("raw Spectrum event missing required 'source_id' string field")
    rsvp_count = raw.get("rsvp_count")
    rsvp_previous = raw.get("rsvp_previous")
    observed_at = _as_str_or_none(raw.get("observed_at"))
    return PlatformEvent(
        platform=raw.get("platform", "spectrum"),
        event_type=event_type,
        actor=_as_str_or_none(raw.get("organizer_id")),
        payload={
            "kind": "events",
            "source_id": source_id,
            "event_id": event_id,
            "title": _as_str_or_none(raw.get("title")),
            "description": _as_str_or_none(raw.get("description")),
            "starts_at": _as_str_or_none(raw.get("starts_at")),
            "ends_at": _as_str_or_none(raw.get("ends_at")),
            "location": _as_str_or_none(raw.get("location")),
            "organizer_id": _as_str_or_none(raw.get("organizer_id")),
            "status": _as_str_or_none(raw.get("status")),
            "rsvp_count": rsvp_count if isinstance(rsvp_count, int) else None,
            "rsvp_previous": rsvp_previous if isinstance(rsvp_previous, int) else None,
        },
        occurred_at=observed_at or datetime.now(UTC).isoformat(),
    )


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw Spectrum item or org-sync change to a `PlatformEvent`.

    Real transform (not a stub). Messages: requires `text`, `source_id` and a
    valid `kind` (`forum`|`lobby`); `event_type` is `message` for a lobby
    message, `thread` for a new forum thread, `reply` for a forum reply. Org
    sync (`kind` = `roster`|`events`): see the module docstring for the
    `event_type` set. Raises `ValueError` on a malformed raw event -- the
    ingest runner catches this per-event so one bad item never kills the poll
    loop (`runner.py`).
    """
    kind = raw.get("kind")
    if kind == "roster":
        return _normalize_roster(raw)
    if kind == "events":
        return _normalize_org_event(raw)

    text = raw.get("text")
    source_id = raw.get("source_id")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("raw Spectrum event missing required 'text' string field")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("raw Spectrum event missing required 'source_id' string field")
    if kind not in _MESSAGE_KINDS:
        raise ValueError("raw Spectrum event 'kind' must be forum|lobby|roster|events")

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
