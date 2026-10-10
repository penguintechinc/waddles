"""`!bookmark` -> a per-community saved-URL list, KV-backed (v2 port).

Commands: `!bookmark add <url>`, `!bookmark list`, `!bookmark remove <id>` (mod/broadcaster
only). Structure mirrors `bundles/python/count`: `transform` does all `kv` work (the command is
recognised by an exact first-token match, then the flag, then the grammar) and `dispatch` is a
pure relay of the reply text `transform` built.

State is community-scoped: the WIT `kv` host capability already scopes every key by
`(tenant, community, app_id)`, so the plain literal keys here (`bookmark.list`, `bookmark.seq`)
never cross communities. Keys are `.`-separated, never `:` (host-reserved, gh-631).

Data: `bookmark.list` is one JSON array of `{"id", "url"}` records (capped at `MAX_BOOKMARKS`
so the blob stays far below the host's 64 KiB value limit); `bookmark.seq` is an atomic
`kv.increment` counter handing out short, chat-typeable ids. No author is stored -- chat actors
are raw platform strings, never PII this bundle may persist.

URL validation: http/https only, a dotted host, no embedded credentials, no whitespace, at most
`MAX_URL_LEN` characters. Anyone may `add` (duplicates and the cap bound abuse); `remove` fails
CLOSED when `is_mod`/`is_broadcaster` are absent (same gap `count` documents for Discord).

Logs are PII-free: only the op, counts and exception-type names -- never a URL or chat text.
Gated behind the PostHog flag ``waddles.command-bookmark``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-bookmark"
COMMAND = "!bookmark"

LIST_KEY = "bookmark.list"
SEQ_KEY = "bookmark.seq"

#: Most bookmarks a community may hold; keeps the single JSON blob well under 64 KiB.
MAX_BOOKMARKS = 50
MAX_URL_LEN = 500
#: Chat reply budget (Twitch caps messages at 500 chars).
_REPLY_BUDGET = 400

_USAGE = "Usage: !bookmark add <url> | !bookmark list | !bookmark remove <id>"
_UNAVAILABLE_MSG = "Something went wrong accessing bookmark storage - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call or stored-data check failed; caught in `transform`."""

    def __init__(self, op: str, error: str) -> None:
        """Record the failing op and a PII-free error classification (exception type name)."""
        super().__init__(f"{op}: {error}")
        self.op = op
        self.error = error


@dataclass(slots=True)
class _Bookmark:
    """One saved URL and its short chat-visible id."""

    id: int
    url: str


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying a host `Err` into `_KvFailure` (fail-loud, never silent)."""
    try:
        return cast("bytes | None", await kv.get(key))
    except Exception as exc:
        raise _KvFailure("kv_get", type(getattr(exc, "value", exc)).__name__) from exc


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure("kv_set", type(getattr(exc, "value", exc)).__name__) from exc


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        return cast(int, await kv.increment(key, delta, ttl_seconds=0))
    except Exception as exc:
        raise _KvFailure(
            "kv_increment", type(getattr(exc, "value", exc)).__name__
        ) from exc


async def _load() -> list[_Bookmark]:
    """Return the community's bookmarks (`[]` if none yet); corrupt data raises `_KvFailure`."""
    raw = await _kv_get(LIST_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        return [_Bookmark(id=int(item["id"]), url=str(item["url"])) for item in data]
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise _KvFailure("corrupt_list", type(exc).__name__) from exc


async def _save(items: list[_Bookmark]) -> None:
    """Persist the full bookmark list."""
    payload = [{"id": b.id, "url": b.url} for b in items]
    await _kv_set(LIST_KEY, json.dumps(payload).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("bookmark.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def validate_url(raw: str) -> str | None:
    """Return an error message, or `None` when `raw` is an acceptable bookmark URL."""
    if not raw:
        return "a URL is required, e.g. `!bookmark add https://example.com`"
    if len(raw) > MAX_URL_LEN:
        return f"URLs must be {MAX_URL_LEN} characters or fewer"
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return "provide a single URL with no spaces"
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        log.debug("bookmark.invalid_url", error=type(exc).__name__)
        return "that doesn't look like a valid URL"
    if parts.scheme not in ("http", "https"):
        return "only http:// and https:// URLs can be bookmarked"
    host = parts.hostname or ""
    if "." not in host or host.startswith(".") or host.endswith("."):
        return "that URL needs a valid host name"
    if parts.username is not None or parts.password is not None:
        return "URLs with embedded credentials aren't allowed"
    return None


def _render(items: list[_Bookmark]) -> str:
    """Format the newest bookmarks first within the chat reply budget."""
    parts: list[str] = []
    used = 0
    for shown, item in enumerate(reversed(items)):
        piece = f"#{item.id} {item.url}"
        if used + len(piece) + 3 > _REPLY_BUDGET and shown > 0:
            parts.append(f"... and {len(items) - shown} more")
            break
        parts.append(piece)
        used += len(piece) + 3
    return f"Bookmarks ({len(items)}): " + " | ".join(parts)


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!bookmark`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if not verb:
        return _USAGE

    if verb == "list":
        items = await _load()
        log.info("bookmark.list", count=len(items))
        return "No bookmarks saved yet." if not items else _render(items)

    if verb == "add":
        error = validate_url(arg)
        if error:
            return f"Can't add: {error}."
        items = await _load()
        if any(b.url == arg for b in items):
            return "That URL is already bookmarked."
        if len(items) >= MAX_BOOKMARKS:
            return f"The bookmark list is full ({MAX_BOOKMARKS}); a mod must remove some first."
        new_id = await _kv_increment(SEQ_KEY, 1)
        items.append(_Bookmark(id=new_id, url=arg))
        await _save(items)
        log.info("bookmark.added", count=len(items))
        return f"Saved bookmark #{new_id}."

    if verb == "remove":
        if not _is_privileged(event):
            log.info("bookmark.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can remove bookmarks."
        if not arg.isdigit():
            return "Usage: !bookmark remove <id>"
        target = int(arg)
        items = await _load()
        kept = [b for b in items if b.id != target]
        if len(kept) == len(items):
            return f"No bookmark #{target}."
        await _save(kept)
        log.info("bookmark.removed", count=len(kept))
        return f"Removed bookmark #{target}."

    return f"Unknown !bookmark subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!bookmark` and build the reply.

    Cheap-skip (exact first-token match, so `!bookmarks` never matches) comes before the flag
    check; a `kv` failure is logged loudly (PII-free) and answered with an error reply, never
    swallowed into a default.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        reply = await _handle(rest.strip(), event)
    except _KvFailure as exc:
        log.error("bookmark.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("bookmark.transform matched")
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive -- `transform`
            always sets both).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "bookmark reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("bookmark reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("bookmark.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
