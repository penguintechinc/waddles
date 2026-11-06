"""`!feed` -> READ-ONLY community events and public info (`waddles.core.community.feed`).

Commands (all answer from a per-community cached snapshot of the community's public JSON feed):

- `!feed` / `!feed events`   -- the next upcoming events (soonest first).
- `!feed event <n>`        -- one event by its list position (1 = soonest) or its feed id.
- `!feed info [topic]`     -- public informative entries; with no topic, the available topics.
- `!feed sync`             -- (mod) force a refresh now (skip the TTL wait).
- `!feed set url <https-url>` / `!feed reset url` -- (mod) per-community feed url config.

Read-only by construction (issue #484): GET only, no credentials; the bundle never writes
anything back to the source, never posts except the reply to the invoking channel, and requests
no write/admin scope. `kv` is a CACHE of the last good snapshot (plus the configured url), never a
source of truth.

**Per-community feed url, not a hardcoded host.** A moderator sets it with `!feed set url`. The
bundle validates it (https, hostname only, no credentials/port/fragment/IP literal/local suffix)
before storing it, but the hard boundary is the host's egress allowlist: wildcard fqdn grants are
rejected by the capability gate, so the manifest declares no fixed host and the platform operator
grants `net.http.fqdn:<host>` for the community's chosen feed host. Without a grant the host denies
the call and the reply is an explicit "unavailable" (fail closed, never an open fetch).

Feed contract (schema validated strictly so a different source fails loudly rather than rendering
garbage):

    {"events": [{"id": str, "title": str, "starts_at": RFC3339, "location"?: str, "url"?: str}],
     "info":   [{"id": str, "title": str, "body": str}]}

Untrusted-text hygiene: every feed string is length-capped, control-char/egress-token/mention
sanitised (`waddle_sdk.safe_text`) and the final reply is lead-guarded, so a hostile or compromised
feed cannot inject mentions, commands or oversized replies. Items that fail validation are dropped
and counted (logged as a count), never rendered half-parsed; a structurally invalid document is a
loud failure.

Refresh is lazy (the platform has no tick event): a read finds the snapshot older than
`CACHE_TTL_SECONDS` and refreshes before answering. If the refresh fails and an older snapshot
exists, it is served with an explicit "cached, may be out of date" marker (never silently); with no
snapshot the reply is an explicit "unavailable".

PII: event/info data is public community content; no user identity is read, stored or logged.
Logs carry only the command, counts, HTTP status and exception class names -- never feed text.

State (`waddle_sdk.community_kv`, community-scoped; `community=None` is the host's valid
tenant-wide sentinel, passed through). Keys use `.` only (gh-631):

- `feed.url`      -- the validated https feed url
- `feed.snapshot` -- JSON `{"synced_ms": int, "events": [...], "info": [...]}` (<< 64 KiB cap)

Activation-gated only -- the platform's installed -> enabled -> activated check is the gate, so the
module is OFF until a community admin activates it (the repo's command-bundle convention,
`docs/reference/command-bundles.md`; no in-bundle flag read).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient
from waddle_sdk.safe_text import guard_leading_command, sanitize_user_text

COMMAND = "!feed"

SNAPSHOT_KEY = "feed.snapshot"
URL_KEY = "feed.url"
MAX_FEED_URL_LEN = 200
_HOST_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
CACHE_TTL_SECONDS = 300
MAX_EVENTS = 25
MAX_INFO = 12
LIST_SHOW = 5
MAX_TITLE_LEN = 80
MAX_LOCATION_LEN = 60
MAX_URL_LEN = 120
MAX_BODY_LEN = 300
MAX_ID_LEN = 40
MAX_TOPIC_LEN = 40

_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")
_USAGE = (
    "Usage: !feed [events] | !feed event <n> | !feed info [topic] | "
    "!feed sync | !feed set url <https-url> | !feed reset url (last three: mod)"
)
_NOT_CONFIGURED_MSG = (
    "No feed is configured - a moderator can set one with !feed set url <https-url>"
)
_BAD_URL_MSG = "the feed url must be a public https URL (a hostname, no credentials, no port)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can do that"
_UNAVAILABLE_MSG = "Community feed is unavailable right now - please try again later."
_STALE_MARK = " (cached - may be out of date)"
_KNOWN_COMMANDS = frozenset({"events", "event", "info", "sync", "set_url", "reset_url", "usage"})


class _FeedError(Exception):
    """Internal-only: the feed fetch/validation failed (already logged); always caught."""


class _NotConfiguredError(Exception):
    """Internal-only: the community has not set a feed url yet. Always caught."""


class _KvFailure(Exception):
    """Internal-only: a kv host-call failed or the cached snapshot was corrupt. Always caught."""


def _clean(value: Any, limit: int) -> str | None:
    """Sanitised, length-capped, single-line text, or `None` if `value` is not a usable string."""
    if not isinstance(value, str):
        return None
    text = " ".join(sanitize_user_text(value).split())
    if not text:
        return None
    return text[:limit]


def _parse_starts(value: Any) -> int | None:
    """Epoch ms for an RFC 3339 timestamp (must carry a UTC offset), else `None`."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        log.debug("feed.bad_timestamp", error_type=type(exc).__name__)
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def _valid_event(raw: Any) -> dict[str, Any] | None:
    """Validate + sanitise one feed event into the cached shape, or `None` to drop it."""
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("id")
    if not isinstance(event_id, str) or _ID_RE.fullmatch(event_id) is None:
        return None
    title = _clean(raw.get("title"), MAX_TITLE_LEN)
    starts_ms = _parse_starts(raw.get("starts_at"))
    if title is None or starts_ms is None:
        return None
    event: dict[str, Any] = {"id": event_id, "title": title, "starts_ms": starts_ms}
    location = _clean(raw.get("location"), MAX_LOCATION_LEN)
    if location is not None:
        event["location"] = location
    url = raw.get("url")
    if isinstance(url, str) and url.startswith("https://") and len(url) <= MAX_URL_LEN:
        cleaned_url = _clean(url, MAX_URL_LEN)
        if cleaned_url is not None and " " not in cleaned_url:
            event["url"] = cleaned_url
    return event


def _valid_info(raw: Any) -> dict[str, str] | None:
    """Validate + sanitise one feed info entry, or `None` to drop it."""
    if not isinstance(raw, dict):
        return None
    info_id = raw.get("id")
    if not isinstance(info_id, str) or _ID_RE.fullmatch(info_id) is None:
        return None
    title = _clean(raw.get("title"), MAX_TITLE_LEN)
    body = _clean(raw.get("body"), MAX_BODY_LEN)
    if title is None or body is None:
        return None
    return {"id": info_id, "title": title, "body": body}


def _parse_feed(body: bytes, now_ms: int) -> dict[str, Any]:
    """Decode + validate a feed document into a snapshot dict.

    Raises:
        _FeedError: Not JSON, wrong top-level shape, or no usable data at all.
    """
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        log.error("feed.feed_bad_json", error_type=type(exc).__name__)
        raise _FeedError("bad json") from exc
    if not isinstance(doc, dict):
        log.error("feed.feed_bad_shape")
        raise _FeedError("bad shape")
    raw_events, raw_info = doc.get("events", []), doc.get("info", [])
    if not isinstance(raw_events, list) or not isinstance(raw_info, list):
        log.error("feed.feed_bad_shape")
        raise _FeedError("bad shape")

    events = [e for e in (_valid_event(r) for r in raw_events[: MAX_EVENTS * 2]) if e is not None]
    info = [i for i in (_valid_info(r) for r in raw_info[: MAX_INFO * 2]) if i is not None]
    dropped = (len(raw_events) + len(raw_info)) - (len(events) + len(info))
    if (raw_events or raw_info) and not events and not info:
        log.error("feed.feed_all_invalid", dropped=dropped)
        raise _FeedError("no valid items")
    events.sort(key=lambda e: cast("int", e["starts_ms"]))
    log.info("feed.feed_parsed", events=len(events), info=len(info), dropped=dropped)
    return {"synced_ms": now_ms, "events": events[:MAX_EVENTS], "info": info[:MAX_INFO]}


def _valid_feed_url(raw: str) -> str | None:
    """The url if it is a public https URL (hostname only, no credentials/port/fragment), else None.

    The manifest egress allowlist is the hard boundary; this is the bundle-side first filter so a
    mod cannot even store `http://`, an IP literal, `localhost`, userinfo or a custom port.
    """
    if not raw or len(raw) > MAX_FEED_URL_LEN or not raw.isascii() or _has_ws_or_ctl(raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        log.debug("feed.bad_feed_url", error_type=type(exc).__name__)
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if parts.fragment or "." not in host or host.endswith("."):
        return None
    if not all(_HOST_LABEL_RE.fullmatch(label) for label in host.split(".")):
        return None
    if host.rsplit(".", 1)[1].isdigit() or host.endswith((".local", ".internal", ".localhost")):
        return None
    return raw


def _has_ws_or_ctl(value: str) -> bool:
    """True if `value` has whitespace or any ASCII control character."""
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


async def _load_url(community: str | None) -> str:
    """The community's configured feed url.

    Raises:
        _NotConfiguredError: None stored.
        _KvFailure: kv failed or the stored url no longer validates.
    """
    try:
        raw = await community_kv.get(community, URL_KEY)
    except Exception as exc:
        raise _KvFailure(f"kv.get failed: {type(getattr(exc, 'value', exc)).__name__}") from exc
    if raw is None:
        raise _NotConfiguredError
    url = _valid_feed_url(raw.decode("utf-8", "replace"))
    if url is None:
        raise _KvFailure("stored feed url invalid")
    return url


async def _fetch_snapshot(url: str) -> dict[str, Any]:
    """GET the configured feed url and return a validated snapshot.

    Raises:
        _FeedError: Transport failure, non-200, truncated, or invalid document (already logged).
    """
    try:
        response = await HttpClient().get(url)
    except Exception as exc:  # noqa: BLE001 - SDK raises Retryable/NonRetryable transport errors
        log.error("feed.feed_unavailable", error_type=type(exc).__name__)
        raise _FeedError("transport") from exc
    status = response["status"]
    if status != 200 or response["truncated"]:
        log.error("feed.feed_bad_status", status=status, truncated=bool(response["truncated"]))
        raise _FeedError("status")
    return _parse_feed(cast("bytes", response["body"]), clock.now_millis())


async def _load_snapshot(community: str | None) -> dict[str, Any] | None:
    """The cached snapshot, or `None` if none yet; a corrupt snapshot is a `_KvFailure`."""
    try:
        raw = await community_kv.get(community, SNAPSHOT_KEY)
    except Exception as exc:
        raise _KvFailure(f"kv.get failed: {type(getattr(exc, 'value', exc)).__name__}") from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt snapshot") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("synced_ms"), int)
        or not isinstance(data.get("events"), list)
        or not isinstance(data.get("info"), list)
    ):
        raise _KvFailure("malformed snapshot")
    return cast("dict[str, Any]", data)


async def _store_snapshot(community: str | None, snapshot: dict[str, Any]) -> None:
    """Write the snapshot with no TTL (age is judged from `synced_ms`, so stale can be served)."""
    payload = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    try:
        await community_kv.set(community, SNAPSHOT_KEY, payload, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure(f"kv.set failed: {type(getattr(exc, 'value', exc)).__name__}") from exc


async def _current(community: str | None, *, force: bool) -> tuple[dict[str, Any], bool]:
    """Return `(snapshot, is_stale)`, refreshing when forced or older than the TTL.

    Raises:
        _FeedError: Refresh failed and there is no cached snapshot to fall back on.
        _KvFailure: The cache could not be read or written.
        _NotConfiguredError: No feed url has been set for this community.
    """
    url = await _load_url(community)
    cached = await _load_snapshot(community)
    now_ms = clock.now_millis()
    fresh = cached is not None and 0 <= now_ms - cached["synced_ms"] < CACHE_TTL_SECONDS * 1000
    if fresh and not force:
        return cast("dict[str, Any]", cached), False
    try:
        snapshot = await _fetch_snapshot(url)
    except _FeedError:
        if cached is None:
            raise
        log.warn("feed.serving_stale", age_s=max(0, (now_ms - cached["synced_ms"]) // 1000))
        return cached, True
    await _store_snapshot(community, snapshot)
    return snapshot, False


def _fmt_when(starts_ms: int) -> str:
    """Render an epoch-ms start as `YYYY-MM-DD HH:MM UTC`."""
    return datetime.fromtimestamp(starts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _upcoming(snapshot: dict[str, Any], now_ms: int) -> list[dict[str, Any]]:
    """Events that have not started yet, soonest first."""
    events = cast("list[dict[str, Any]]", snapshot["events"])
    return [e for e in events if isinstance(e.get("starts_ms"), int) and e["starts_ms"] >= now_ms]


def _render_events(snapshot: dict[str, Any], now_ms: int) -> str:
    """One-line summary of the next few upcoming events."""
    upcoming = _upcoming(snapshot, now_ms)
    if not upcoming:
        return "No upcoming community events right now."
    parts = [
        f"{i}. {e['title']} ({_fmt_when(e['starts_ms'])})"
        for i, e in enumerate(upcoming[:LIST_SHOW], 1)
    ]
    more = f" (+{len(upcoming) - LIST_SHOW} more)" if len(upcoming) > LIST_SHOW else ""
    return "Community events: " + " | ".join(parts) + more + " - !feed event <n> for details"


def _render_event(snapshot: dict[str, Any], now_ms: int, ref: str) -> str:
    """Details for one event, addressed by 1-based upcoming position or by feed id."""
    upcoming = _upcoming(snapshot, now_ms)
    chosen: dict[str, Any] | None = None
    if ref.isascii() and ref.isdigit() and len(ref) <= 3 and 1 <= int(ref) <= len(upcoming):
        chosen = upcoming[int(ref) - 1]
    else:
        chosen = next((e for e in upcoming if e["id"] == ref), None)
    if chosen is None:
        return "No such upcoming event - try !feed events"
    out = f"{chosen['title']} - {_fmt_when(chosen['starts_ms'])}"
    if "location" in chosen:
        out += f" @ {chosen['location']}"
    if "url" in chosen:
        out += f" - {chosen['url']}"
    return out


def _render_info(snapshot: dict[str, Any], topic: str) -> str:
    """The info entry matching `topic` (id or title, case-insensitive), or the topic list."""
    info = cast("list[dict[str, str]]", snapshot["info"])
    if not info:
        return "No community info published yet."
    if not topic:
        return "Info topics: " + ", ".join(i["id"] for i in info) + " - !feed info <topic>"
    wanted = topic.lower()
    for entry in info:
        if wanted in (entry["id"].lower(), entry["title"].lower()):
            return f"{entry['title']}: {entry['body']}"
    return "No such topic - try !feed info"


def _parse(rest: str) -> dict[str, Any]:
    """Parse the command grammar into a normalized payload; `usage` if malformed."""
    verb, _, tail = rest.strip().partition(" ")
    verb, tail = verb.lower(), tail.strip()
    if not verb or verb == "events":
        return {"command": "events"} if not tail else {"command": "usage"}
    if verb == "sync":
        return {"command": "sync"} if not tail else {"command": "usage"}
    if verb in ("set", "reset"):
        sub, _, arg = tail.partition(" ")
        if sub.lower() != "url":
            return {"command": "usage"}
        if verb == "reset":
            return {"command": "reset_url"} if not arg.strip() else {"command": "usage"}
        return {"command": "set_url", "url": arg.strip()}
    if verb == "event" and tail and " " not in tail and len(tail) <= MAX_ID_LEN:
        return {"command": "event", "ref": tail}
    if verb == "info" and len(tail) <= MAX_TOPIC_LEN:
        return {"command": "info", "topic": tail}
    return {"command": "usage"}


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!feed ...` and parse its grammar.

    Returns `None` for non-matching text. A recognized-but-malformed command still yields a
    `usage` reply -- the caller did invoke it, never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    payload = _parse(rest)
    payload["channel_id"] = event.payload.get("channel_id")
    if "is_mod" in event.payload:
        payload["is_mod"] = event.payload["is_mod"] is True
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = event.payload["is_broadcaster"] is True
    log.info("feed.transform matched", command=payload["command"])
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
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


async def _fail(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud path: log the reason, reply an explicit error to chat, then raise."""
    log.error("feed.failed", reason=reason)
    await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"community feed read failed: {reason}")


def _is_privileged(payload: dict[str, Any]) -> bool:
    """True only for a real boolean `True` mod/broadcaster badge (absent badges = denied)."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


async def _configure(command: str, raw_url: str, community: str | None) -> str:
    """`set url` / `reset url`: validate + store (or clear) the url, dropping the stale cache."""
    try:
        if command == "set_url":
            url = _valid_feed_url(raw_url)
            if url is None:
                return _BAD_URL_MSG
            await community_kv.set(community, URL_KEY, url.encode("utf-8"), ttl_seconds=0)
            reply = "Feed url saved. Use !feed sync to load it."
        else:
            await community_kv.delete(community, URL_KEY)
            reply = "Feed url cleared."
        await community_kv.delete(community, SNAPSHOT_KEY)
    except Exception as exc:
        raise _KvFailure(f"kv op failed: {type(getattr(exc, 'value', exc)).__name__}") from exc
    log.info("feed.configured", command=command)
    return reply


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: refresh/read the cached snapshot, then relay the reply.

    Raises:
        ValueError: No `channel_id`, or an unrecognized `command` (defensive).
        RuntimeError: The feed is down with no cache, or kv failed (an explicit chat error reply
            and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("community feed reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized community feed command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community

    if command == "usage":
        reply = _USAGE
    elif command in ("sync", "set_url", "reset_url") and not _is_privileged(payload):
        log.info("feed.permission_denied", command=command)
        reply = _PERMISSION_DENIED_MSG
    elif command in ("set_url", "reset_url"):
        try:
            reply = await _configure(command, str(payload.get("url", "")), community)
        except _KvFailure as exc:
            await _fail(str(exc), provider=provider, channel_id=channel_id)
    else:
        try:
            snapshot, stale = await _current(community, force=command == "sync")
        except _NotConfiguredError:
            snapshot, stale = None, False
        except (_FeedError, _KvFailure) as exc:
            await _fail(str(exc), provider=provider, channel_id=channel_id)
        now_ms = clock.now_millis()
        if snapshot is None:
            reply = _NOT_CONFIGURED_MSG
        elif command == "sync":
            counts = f"{len(snapshot['events'])} events, {len(snapshot['info'])} info entries"
            reply = (
                f"Sync failed - still serving cached data: {counts}"
                if stale
                else f"Synced: {counts}"
            )
        else:
            if command == "events":
                reply = _render_events(snapshot, now_ms)
            elif command == "event":
                reply = _render_event(snapshot, now_ms, str(payload["ref"]))
            else:
                reply = _render_info(snapshot, str(payload["topic"]))
            reply += _STALE_MARK if stale else ""

    await relay.push(provider, {"channel": channel_id, "text": guard_leading_command(reply)})
    log.info("feed.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=str(command))
