"""`!sr`, `!music`/`!queue`, `!songqueue`/`!sq` over the shared music-requests service.

The song-request queue, now-playing state and history live in ONE service bundle
(`waddles.core.service.music-requests`); this app is only the chat front end. It owns no queue:
every command is a tool call (`waddle_sdk.tool.invoke`) made in the process stage, where the host
resolves the verified actor from the triggering event. The action stage relays the reply and
publishes the overlay surfaces from the service state the process stage fetched.

Privilege is never decided from chat badges. A moderator-only operation is attempted and the
service's host-verified scopes (`music:requests.manage`) answer; a typed `forbidden` ToolError is
mapped to a friendly reply. `!music remove <id>` tries `requests.remove` (moderator) first and falls
back to `requests.cancel_own` (the requester's own row) on `forbidden`.

No requester identity ever reaches a reply, an overlay payload or a log line: the service never
returns one, and logs carry only the command, tool name, error code and counts.

The AI DJ is an optional layer on top of the same service; this bundle never references it and works
fully with it absent.

Migration: bundles <= 1.0.9 kept a per-community kv queue (`music.queue`). On the first command per
community, legacy items are handed to `requests.import` (title/artist/url only -- requester ids are
dropped), then the legacy keys are deleted and `music.migrated` is set (one-shot, no ':' in keys).

Attribution: net-new Waddles content, inspired by superpenguintv (Psychoboy)'s PenguinTwitchBot
song-request feature (https://github.com/Psychoboy/PenguinTwitchBot); no original source reused.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from waddle_sdk import clock, community_kv, event_dedupe, log, relay, tool
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import guard_leading_command, sanitize_user_text

#: The shared queue service every command delegates to.
SERVICE = "waddles.core.service.music-requests"

#: PostHog flag gating the `!songqueue`/`!sq` link command (default OFF).
FLAG_QUEUE_PAGE = "waddles.music.queue-page"

_REQUEST_HEADS = frozenset({"!sr", "!songrequest"})
_QUEUE_HEADS = frozenset({"!music", "!queue"})
_LINK_HEADS = frozenset({"!songqueue", "!sq"})

#: No sub-modules declared -- `!music` has no opt-in feature toggles.
SPEC = CommandSpec(name="music")

#: Bare keywords handled ahead of the formal grammar (none is in `waddle_sdk.command.VERBS`).
_ADVANCE_VERBS = frozenset({"next", "skip"})

MIN_MAX_PER_USER = 1
MAX_MAX_PER_USER = 10

#: Longest free-text request, per-field cap the service stores, and URL cap.
MAX_REQUEST_TEXT_LEN = 300
_FIELD_MAX = 200
_URL_MAX = 500

#: How many "up next" entries a chat reply lists, and the per-entry display width.
SHOW_LIMIT = 5
_DISPLAY_MAX = 60
_HISTORY_LIMIT = 5

#: Overlay surfaces this bundle owns (`bundles/contracts/surfaces.v1.json` `public_track` shape).
_SURFACE_NOW_PLAYING = "waddles.core.example.music.now_playing"
_SURFACE_QUEUE = "waddles.core.example.music.queue"
_SURFACE_QUEUE_MAX = 50

#: kv keys. Legacy keys are only ever read once, to migrate; no ':' anywhere.
_LEGACY_QUEUE_KEY = "music.queue"
_LEGACY_SEQ_KEY = "music.seq"
_LEGACY_CONFIG_KEY = "music.config.max-per-user"
_MIGRATED_KEY = "music.migrated"
_IMPORT_MAX = 100

_URL_RE = re.compile(r"https://\S+", re.IGNORECASE)

_USAGE = (
    "Usage: !sr <title - artist [https-link]> | !music | !music now | !music history | "
    "!music remove <id> | !music top [artists|songs] | !music dmca | !music next|skip (mod) | "
    "!music set max-per-user <n> | !music set dmca-safe on|off (mod) | !songqueue"
)
_DENIED_MSG = "only moderators can do that"
_UNAVAILABLE_MSG = "the song queue is temporarily unavailable, try again shortly."
_NOT_CONFIGURED_MSG = "song queue page is not configured"
_QUEUE_EMPTY_MSG = "the queue is empty."

#: Service error keys (the contract's typed errors) -> friendly replies.
_SERVICE_ERRORS = {
    "queue_full": "the queue is full -- try again once it drains",
    "per_user_limit": "you already have the maximum number of pending requests",
    "duplicate": "that song is already in the queue",
    "invalid_url": "only https links are accepted",
    "not_dmca_safe": (
        "DMCA-safe mode is on: only royalty-free (RFM) and NCS tracks can be requested"
    ),
}


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _scope(community: str | None) -> str:
    """Return the community to key kv under (`None` = the host's tenant-wide sentinel)."""
    if community == "":
        log.error("music.missing_community")
        raise ValueError("music requires a community context (got an empty community id)")
    return community if community is not None else TENANT_WIDE_SENTINEL


def _clean(value: str, limit: int) -> str:
    """Printable-only, single-spaced, length-capped text."""
    return " ".join("".join(ch for ch in value if ch.isprintable()).split())[:limit]


def parse_request(text: str) -> tuple[dict[str, str], str | None]:
    """Split free text `title - artist [https://link]` into `requests.add` fields.

    Returns `(fields, error)`; `error` is a user-facing message when the text is unusable. The
    first `https://` token is the source URL; the rest is `title - artist` (artist optional).
    """
    url = ""
    match = _URL_RE.search(text)
    if match:
        url = match.group(0)
        if len(url) > _URL_MAX:
            return {}, "that link is too long"
        text = f"{text[: match.start()]} {text[match.end() :]}"
    cleaned = _clean(text, MAX_REQUEST_TEXT_LEN)
    title, _, artist = cleaned.partition(" - ")
    title, artist = title.strip()[:_FIELD_MAX], artist.strip()[:_FIELD_MAX]
    if not title:
        if not url:
            return {}, _USAGE
        title = "Requested link"
    fields = {"title": title, "artist": artist}
    if url:
        fields["source_url"] = url
    return fields, None


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!music `/`!queue ` onto this bundle's command set.

    Bare keywords (`now`, `history`, `next`, `skip`) are handled first; every other shape is
    normalized onto `!music ...` and delegated to `parse_command` unchanged.
    """
    if rest:
        first_tok, _, remainder = rest.partition(" ")
        word = first_tok.lower()
        if word in _ADVANCE_VERBS:
            return ("usage", None) if remainder.strip() else ("advance", None)
        if word in {"now", "history", "dmca"}:
            return ("usage", None) if remainder.strip() else (word, None)
        if word == "top":
            return "top", remainder.strip().lower() or "songs"

    normalized = f"!music {rest}" if rest else "!music"
    try:
        parsed = parse_command(normalized, SPEC)
    except CommandUsageError as exc:
        log.debug("music.invalid_command", error_type=type(exc).__name__)
        return "usage", None
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a parsed `ParsedCommand` onto `show`/`remove`/`config_set`; anything else is usage."""
    if parsed.option is None:
        return "show", None
    if parsed.option == "list":
        return ("usage", None) if parsed.args is not None else ("show", None)
    if parsed.option == "remove":
        return ("usage", None) if parsed.args is None else ("remove", parsed.args)
    if parsed.option == "set":
        return ("usage", None) if parsed.args is None else ("config_set", parsed.args)
    return "usage", None


def _idem(event: PlatformEvent, op: str) -> str:
    """Replay-stable idempotency key from the platform message id (raises `EventIdError`)."""
    message_id = event_dedupe.event_id_from_payload(event.payload, platform=event.platform)
    return hashlib.sha256(f"music:{op}:{message_id}".encode()).hexdigest()


def _service_error(exc: tool.ToolError) -> str | None:
    """Friendly reply for one of the contract's typed service errors, else `None`."""
    blob = f"{exc.code} {exc.message}".lower()
    for key, reply in _SERVICE_ERRORS.items():
        if key in blob:
            return reply
    return None


def _show(item: dict[str, Any]) -> str:
    """One item as `Title - Artist`, sanitized and truncated -- never a requester."""
    title = _clean(str(item.get("title", "")), _DISPLAY_MAX)
    artist = _clean(str(item.get("artist", "")), _DISPLAY_MAX)
    return sanitize_user_text(f"{title} - {artist}" if artist else title)


def _public_track(item: dict[str, Any]) -> dict[str, Any]:
    """One service item as the contract's `public_track`: text only, no requester, no URLs."""
    duration_s = item.get("duration_ms")
    seconds = duration_s // 1000 if isinstance(duration_s, int) and duration_s > 0 else 0
    item_id = item.get("id")
    ident = item_id if isinstance(item_id, int) and item_id >= 0 else 0
    return {
        "id": ident,
        "provider": "request",
        "track_id": str(ident),
        "title": _clean(str(item.get("title", "")), _FIELD_MAX),
        "artist": _clean(str(item.get("artist", "")), _FIELD_MAX),
        "duration_s": min(seconds, 86400),
    }


def _overlay_state(listing: dict[str, Any]) -> dict[str, Any]:
    """Overlay frames' content derived from a `requests.list` result (service state only)."""
    now = listing.get("now_playing")
    queue = listing.get("queue")
    items = queue if isinstance(queue, list) else []
    return {
        "now_playing": _public_track(now) if isinstance(now, dict) else None,
        "queue": [_public_track(i) for i in items[:_SURFACE_QUEUE_MAX] if isinstance(i, dict)],
    }


def _format_listing(listing: dict[str, Any]) -> str:
    """`Now playing: X. Up next (n): #id A; #id B (+k more)` from a `requests.list` result."""
    now = listing.get("now_playing")
    queue = [i for i in (listing.get("queue") or []) if isinstance(i, dict)]
    total = listing.get("total_queued")
    total = total if isinstance(total, int) else len(queue)
    parts = [f"Now playing: {_show(now)}" if isinstance(now, dict) else "Nothing is playing"]
    if queue:
        shown = "; ".join(f"#{i.get('id')} {_show(i)}" for i in queue[:SHOW_LIMIT])
        more = f" (+{total - SHOW_LIMIT} more)" if total > SHOW_LIMIT else ""
        parts.append(f"Up next ({total}): {shown}{more}")
    else:
        parts.append(_QUEUE_EMPTY_MSG.rstrip("."))
    return ". ".join(parts) + "."


async def _migrate(scope: str) -> None:
    """One-shot import of the legacy per-community kv queue into the service (see module doc).

    A kv or service failure leaves the legacy keys untouched so the next command retries; a
    service REFUSAL (`invalid_args`: already imported / queue not empty) clears the stale legacy
    data, since retrying could never succeed.
    """
    try:
        if await community_kv.get(scope, _MIGRATED_KEY) is not None:
            return
        raw = await community_kv.get(scope, _LEGACY_QUEUE_KEY)
    except Exception as exc:  # noqa: BLE001 -- loud log; migration retried on the next command
        log.error("music.migrate_kv_failed", op="read", error_type=type(exc).__name__)
        return
    if raw is not None:
        items = _legacy_items(raw)
        if items:
            key = hashlib.sha256(f"music:import:{scope}".encode()).hexdigest()
            try:
                result = await tool.invoke(
                    SERVICE, "requests.import", {"items": items}, idempotency_key=key
                )
                log.info("music.migrated", count=_imported_count(result))
            except tool.ToolError as exc:
                if exc.code != "invalid_args":
                    log.error("music.migrate_import_failed", code=exc.code)
                    return
                log.warn("music.migrate_import_refused", code=exc.code)
    try:
        for legacy in (_LEGACY_QUEUE_KEY, _LEGACY_SEQ_KEY, _LEGACY_CONFIG_KEY):
            await community_kv.delete(scope, legacy)
        await community_kv.set(scope, _MIGRATED_KEY, b"1")
    except Exception as exc:  # noqa: BLE001 -- loud log; the import is idempotent on retry
        log.error("music.migrate_kv_failed", op="clear", error_type=type(exc).__name__)


def _imported_count(result: Any) -> int:
    """`imported` from a `requests.import` result, 0 when absent."""
    value = result.get("imported") if isinstance(result, dict) else None
    return value if isinstance(value, int) else 0


def _legacy_items(raw: bytes) -> list[dict[str, Any]]:
    """Convert the legacy JSON queue into import items (title/artist/url/duration only)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warn("music.migrate_corrupt_legacy")
        return []
    if not isinstance(data, list):
        log.warn("music.migrate_corrupt_legacy")
        return []
    items: list[dict[str, Any]] = []
    for entry in data[:_IMPORT_MAX]:
        text = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(text, str):
            continue
        fields, error = parse_request(text)
        if error is not None:
            continue
        items.append(
            {
                "title": fields["title"],
                "artist": fields["artist"],
                "source_url": fields.get("source_url", ""),
                "duration_ms": 0,
            }
        )
    return items


async def _list(limit: int = 50) -> dict[str, Any]:
    """`requests.list` after `requests.advance_due`, so time-based advances show up first."""
    await tool.invoke(SERVICE, "requests.advance_due", {})
    result = await tool.invoke(SERVICE, "requests.list", {"limit": limit})
    if not isinstance(result, dict):
        raise tool.ToolError("invalid_args", "unexpected requests.list result")
    return result


async def _handle_add(event: PlatformEvent, arg: str | None) -> str:
    """`!sr <title - artist [link]>` -> `requests.add` (idempotent per chat message)."""
    if not arg or not arg.strip():
        return _USAGE
    if len(arg.strip()) > MAX_REQUEST_TEXT_LEN:
        return f"song requests must be {MAX_REQUEST_TEXT_LEN} characters or fewer"
    fields, error = parse_request(arg)
    if error is not None:
        return error
    result = await tool.invoke(SERVICE, "requests.add", fields, idempotency_key=_idem(event, "add"))
    item = result.get("item") if isinstance(result, dict) else None
    position = result.get("position") if isinstance(result, dict) else None
    label = f"{_show(item)} (#{item.get('id')})" if isinstance(item, dict) else "your song"
    where = f" at position {position}" if isinstance(position, int) else ""
    return f"added {label} to the queue{where}"


async def _handle_remove(event: PlatformEvent, arg: str | None) -> str:
    """`!music remove <id>`: try the moderator tool, fall back to the requester's own row."""
    if not arg:
        return _USAGE
    cleaned = arg.strip()
    if not cleaned.isdigit() or int(cleaned) < 1:
        return "that isn't a valid request id"
    target = int(cleaned)
    try:
        result = await tool.invoke(
            SERVICE, "requests.remove", {"id": target}, idempotency_key=_idem(event, "remove")
        )
    except tool.ToolError as exc:
        if exc.code != "forbidden":
            raise
        result = await tool.invoke(
            SERVICE, "requests.cancel_own", {"id": target}, idempotency_key=_idem(event, "cancel")
        )
        if not (isinstance(result, dict) and result.get("removed") is True):
            return "you can only remove your own requests"
    if isinstance(result, dict) and result.get("removed") is True:
        return f"removed request #{target}"
    return f"no request #{target} found"


async def _handle_advance(event: PlatformEvent) -> str:
    """`!music next|skip` -> `requests.advance` (service enforces `music:requests.manage`)."""
    result = await tool.invoke(
        SERVICE, "requests.advance", {}, idempotency_key=_idem(event, "advance")
    )
    now = result.get("now_playing") if isinstance(result, dict) else None
    finished = result.get("finished") if isinstance(result, dict) else None
    if isinstance(now, dict):
        return f"now playing: {_show(now)}"
    if isinstance(finished, dict):
        return f"finished: {_show(finished)} (queue is now empty)"
    return _QUEUE_EMPTY_MSG


async def _handle_config_set(event: PlatformEvent, arg: str | None) -> str:
    """`!music set max-per-user <n>` -> `config.set` (service enforces the manage scope)."""
    parts = (arg or "").split()
    if len(parts) == 2 and parts[0].lower() == "dmca-safe":
        return await _handle_dmca_set(event, parts[1].lower())
    if len(parts) != 2 or parts[0].lower() != "max-per-user":
        return _USAGE
    if not parts[1].isdigit():
        return "that isn't a whole number"
    value = int(parts[1])
    if not (MIN_MAX_PER_USER <= value <= MAX_MAX_PER_USER):
        return f"max-per-user must be between {MIN_MAX_PER_USER} and {MAX_MAX_PER_USER}"
    result = await tool.invoke(
        SERVICE, "config.set", {"max_per_user": value}, idempotency_key=_idem(event, "config")
    )
    stored = result.get("max_per_user") if isinstance(result, dict) else None
    return f"max-per-user set to {stored if isinstance(stored, int) else value}"


async def _handle_dmca_set(event: PlatformEvent, value: str) -> str:
    """`!music set dmca-safe on|off` -> `settings.set` (service enforces `music:settings.write`)."""
    if value not in {"on", "off"}:
        return _USAGE
    result = await tool.invoke(
        SERVICE,
        "settings.set",
        {"dmca_safe": value == "on"},
        idempotency_key=_idem(event, "dmca"),
    )
    stored = result.get("dmca_safe") if isinstance(result, dict) else None
    on = stored if isinstance(stored, bool) else value == "on"
    return f"DMCA-safe mode is now {'on' if on else 'off'}"


async def _handle_dmca_status() -> str:
    """`!music dmca` -> DMCA-safe status from `config.get`."""
    result = await tool.invoke(SERVICE, "config.get", {})
    on = isinstance(result, dict) and result.get("dmca_safe") is True
    return f"DMCA-safe mode is {'on' if on else 'off'}"


async def _handle_top(arg: str | None) -> str:
    """`!music top [artists|songs]` -> top 5 from `music.stats.top` (no requester data)."""
    word = (arg or "songs").lower()
    kinds = {"artist": "artist", "artists": "artist", "song": "song", "songs": "song"}
    kind = kinds.get(word)
    if kind is None:
        return _USAGE
    result = await tool.invoke(SERVICE, "music.stats.top", {"kind": kind, "limit": 5})
    items = (
        [i for i in (result.get("items") or []) if isinstance(i, dict)]
        if isinstance(result, dict)
        else []
    )
    if not items:
        return "no plays recorded yet."
    rows = []
    for rank, item in enumerate(items[:5], 1):
        name = _clean(str(item.get("artist", "")), _DISPLAY_MAX)
        if kind == "song":
            title = _clean(str(item.get("title", "")), _DISPLAY_MAX)
            name = f"{title} - {name}" if name else title
        plays = item.get("plays") if isinstance(item.get("plays"), int) else 0
        rows.append(f"{rank}. {sanitize_user_text(name)} ({plays})")
    return f"Top {'artists' if kind == 'artist' else 'songs'}: " + "; ".join(rows)


async def _handle_history() -> str:
    """`!music history` -> the last few finished songs from `history.list`."""
    result = await tool.invoke(SERVICE, "history.list", {"limit": _HISTORY_LIMIT})
    items = (
        [i for i in (result.get("items") or []) if isinstance(i, dict)]
        if isinstance(result, dict)
        else []
    )
    if not items:
        return "no songs have been played yet."
    return "Recently played: " + "; ".join(_show(i) for i in items[:_HISTORY_LIMIT])


async def _handle_link() -> str:
    """`!songqueue`/`!sq` -> `page.link`; a not-configured platform gets a plain notice."""
    try:
        result = await tool.invoke(SERVICE, "page.link", {})
    except tool.ToolError as exc:
        if "link_not_configured" in f"{exc.code} {exc.message}".lower():
            return _NOT_CONFIGURED_MSG
        raise
    url = result.get("url") if isinstance(result, dict) else None
    if not isinstance(url, str) or not url:
        return _NOT_CONFIGURED_MSG
    return f"Song queue: {url}"


async def _execute(
    event: PlatformEvent, command: str, arg: str | None
) -> tuple[str, dict[str, Any] | None]:
    """Run one command; return `(reply, overlay_state_or_None)` (overlay = fresh service state)."""
    if command == "usage":
        return _USAGE, None
    if command == "link":
        return await _handle_link(), None
    if command == "history":
        return await _handle_history(), None
    if command == "dmca":
        return await _handle_dmca_status(), None
    if command == "top":
        return await _handle_top(arg), None
    if command in {"show", "now"}:
        listing = await _list()
        if command == "show":
            return _format_listing(listing), _overlay_state(listing)
        now = listing.get("now_playing")
        reply = f"Now playing: {_show(now)}" if isinstance(now, dict) else "Nothing is playing."
        return reply, _overlay_state(listing)
    handlers = {
        "add": lambda: _handle_add(event, arg),
        "remove": lambda: _handle_remove(event, arg),
        "advance": lambda: _handle_advance(event),
        "config_set": lambda: _handle_config_set(event, arg),
    }
    reply = await handlers[command]()
    try:
        return reply, _overlay_state(await _list())
    except tool.ToolError as exc:
        log.warn("music.overlay_refresh_failed", code=exc.code)
        return reply, None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize the command heads and run the tool calls.

    Cheap-skip first (no matching head -- `None`), then the one-shot migration, then the command.
    Every recognized command yields a reply; a service failure becomes a friendly chat line plus
    an ERROR log (code only), never a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    head_lower = head.lower()
    arg: str | None = None
    if head_lower in _REQUEST_HEADS:
        arg = rest.strip() or None
        command = "add" if arg is not None else "usage"
    elif head_lower in _QUEUE_HEADS:
        command, arg = _resolve(rest.strip())
    elif head_lower in _LINK_HEADS:
        if not await feature_enabled(FLAG_QUEUE_PAGE, default=False):
            return None
        command = "link"
    else:
        return None
    channel_id = event.payload.get("channel_id")
    if not channel_id:
        raise ValueError("music reply requires a channel_id from the inbound chat.message")

    log.info("music.transform matched", command=command)
    scope = _scope(get_bundle_context().community)
    await _migrate(scope)
    overlay: dict[str, Any] | None = None
    try:
        reply, overlay = await _execute(event, command, arg)
    except event_dedupe.EventIdError:
        log.warn("music.missing_message_id", command=command)
        reply = "couldn't process that message, please try again."
    except tool.ToolError as exc:
        reply = _tool_error_reply(exc, command)
    payload: dict[str, Any] = {"channel_id": channel_id, "reply": guard_leading_command(reply)}
    if overlay is not None:
        payload["overlay"] = overlay
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


def _tool_error_reply(exc: tool.ToolError, command: str) -> str:
    """Map a typed ToolError to a chat reply; unknown failures log loudly and apologize."""
    if exc.code == "forbidden":
        return _DENIED_MSG
    friendly = _service_error(exc)
    if friendly is not None:
        return friendly
    log.error("music.tool_failed", command=command, code=exc.code)
    return _UNAVAILABLE_MSG


async def _publish_overlay(overlay: dict[str, Any]) -> None:
    """Publish the service-derived state to this bundle's own overlay surfaces.

    A refused or failed publish must not fail the chat command; it is logged loudly (surface and
    error code only, never the payload) and the next command republishes the full state.
    """
    import wit_world

    now_ms = int(clock.now_millis())
    frames = (
        (_SURFACE_NOW_PLAYING, {"now_playing": overlay.get("now_playing"), "updated_ms": now_ms}),
        (_SURFACE_QUEUE, {"queue": overlay.get("queue") or [], "updated_ms": now_ms}),
    )
    for surface_id, payload in frames:
        try:
            wit_world.imports.overlay.publish_surface(surface_id, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 -- loud log, never a silent drop
            wit_error = getattr(exc, "value", exc)
            code = getattr(wit_error, "code", type(wit_error).__name__)
            log.error("music.surface_publish_failed", surface=surface_id, error=str(code))


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: publish the overlay (if any), then relay the reply.

    Raises:
        ValueError: the payload has no `channel_id` or no `reply` string (defensive -- `transform`
            always emits both).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    reply = payload.get("reply")
    if not channel_id or not isinstance(reply, str) or not reply:
        raise ValueError("music reply requires a channel_id and a reply from process-stage")
    overlay = payload.get("overlay")
    if isinstance(overlay, dict):
        await _publish_overlay(overlay)
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("music.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="reply")
