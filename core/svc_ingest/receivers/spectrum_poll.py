"""SpectrumPollReceiver -- a `waddle_transports.Transport` for one-way Spectrum ingest (gh #101).

Star Citizen's Spectrum has NO official public API. This receiver polls the
community-documented, reverse-engineered REST endpoints
(`https://robertsspaceindustries.com/api/spectrum/...`, JSON `POST`s) using a
per-deployment RSI session token. Everything below is built around that
instability:

* **One-way only (Spectrum -> Waddles).** There is deliberately NO outbound
  path -- no `Direction.OUTBOUND`, no posting helper, no action stage.
  Automating posts through undocumented endpoints risks RSI actioning the
  connected account/org (issue #101, "No outbound sync by design").
* **Provider abstraction.** All wire knowledge lives in `SpectrumProvider`
  / `RsiRestProvider`; the poll loop, dedupe, backoff, telemetry and event
  shape never touch a URL. An official API (or a websocket provider for
  real-time lobby streaming -- deferred, see below) swaps in without
  rewriting business logic.
* **Graceful degradation.** A 404/changed-envelope response is a loud
  `NonRetryableTransportError` naming the (non-secret) path, never a silent
  empty poll. Transient 429/5xx/network errors back off exponentially
  *inside* the loop and only escalate to the supervisor
  (`RetryableTransportError`) after `max_consecutive_errors`. 401/403 /
  RSI "login required" envelopes are NEVER retried (auth error).
* **Quick disable.** `flag_check` (the `waddles.spectrum-integration` flag,
  ENV baseline `FLAG_WADDLES_SPECTRUM_INTEGRATION`, default OFF; PostHog
  overrides when connected) is re-evaluated every `flag_recheck_s`; flag
  OFF idles the poller without dropping its lease or crashing.

Credentials: `config["token_ref"]` names an environment variable (never a
raw value), resolved via `waddle_transports.signing.resolve_secret` at
connect time -- same convention as `receivers/youtube_live_poll.py`. The
token is only ever placed in the `Cookie`/`X-Rsi-Token` request headers and
is never logged.

One poller per source (`kind` = `forum` channel, `lobby`, or -- for org sync --
the `roster` / `events` of one Spectrum community), lease-guarded by `app.py`
(`provider="spectrum", community="<kind>-<id>"`), matching
`receivers/youtube_live_poll.py`'s per-channel precedent.

**Org sync (`roster` / `events`).** Unlike forum threads and lobby messages
these are *snapshots*, so the loop diffs each poll against the last snapshot
(`receivers/spectrum_org.py`) and yields one raw event per CHANGE: member
joined / left / roles_changed, and event created / updated / cancelled /
removed / rsvp_changed. The snapshot lives in a `SnapshotStore` (Valkey in
production) so changes made while svc-ingest was down are still detected; the
roster is fetched completely or not at all (a truncated page set would read as
mass departure), and an implausible shrink is rejected, not emitted.

Logs are PII-free by construction: only source ids, counts, status codes
and exception types -- never message text, handles, or the token.

DEFERRED (documented in the PR, not stubbed): websocket real-time lobby
streaming, DM capture (opt-in), RSI-handle <-> profile linking, the scheduled
health probe and the admin connection-management API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx
from opentelemetry import metrics, trace
from waddle_transports import (
    Direction,
    NonRetryableTransportError,
    RetryableTransportError,
    Transport,
)
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request

from receivers.spectrum_org import (
    DEFAULT_GUARD_MIN_SIZE,
    DEFAULT_MAX_DEPARTURE_RATIO,
    KIND_EVENTS,
    KIND_ROSTER,
    MAX_DESCRIPTION_CHARS,
    ORG_KINDS,
    STATUS_CANCELLED,
    STATUS_SCHEDULED,
    EventChange,
    MemorySnapshotStore,
    RosterChange,
    SnapshotAnomalyError,
    SnapshotStore,
    SnapshotStoreError,
    SpectrumEvent,
    SpectrumMember,
    diff_events,
    diff_roster,
)

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Spectrum item declares
#: (`builtin_handlers/spectrum_ingest.py`'s `stages.ingest.consumes`).
CONSUMES_TAG = "spectrum.message"
#: Tag for org-sync changes (roster / events) -- same default app, separate tag so a
#: bundle can opt into messages without roster traffic or vice versa.
ORG_CONSUMES_TAG = "spectrum.org"

#: Flag key (ENV baseline `FLAG_WADDLES_SPECTRUM_INTEGRATION`, default OFF).
FLAG_KEY = "waddles.spectrum-integration"
#: Second gate for org sync (roster / events): ENV baseline `FLAG_WADDLES_SPECTRUM_ORG_SYNC`,
#: default OFF. Org sync runs only when BOTH this and `FLAG_KEY` are on (see `app.py`).
ORG_SYNC_FLAG_KEY = "waddles.spectrum-org-sync"

KIND_FORUM = "forum"
KIND_LOBBY = "lobby"
_KINDS = frozenset({KIND_FORUM, KIND_LOBBY}) | ORG_KINDS

DEFAULT_API_BASE = "https://robertsspaceindustries.com/api/spectrum"
_HTTP_TIMEOUT_S = 15.0
#: Floor under any configured poll interval -- respectful polling, never a tight loop.
MIN_POLL_INTERVAL_S = 5.0
DEFAULT_POLL_INTERVAL_S = 15.0
#: Org snapshots change slowly and a roster fetch is several paged requests, so they
#: poll far less often than chat: floor and default are much higher.
MIN_ORG_POLL_INTERVAL_S = 60.0
DEFAULT_ORG_POLL_INTERVAL_S = 300.0
#: Roster/event paging: bounded so a server ignoring `page` can never spin forever.
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 50
DEFAULT_PAGE_DELAY_S = 1.0
DEFAULT_MAX_CONSECUTIVE_ERRORS = 8
DEFAULT_BASE_BACKOFF_S = 2.0
DEFAULT_MAX_BACKOFF_S = 120.0
DEFAULT_FLAG_RECHECK_S = 30.0
#: Dedupe window: ids remembered per poller (bounded -- never grows unbounded).
_SEEN_CAP = 2000
_MAX_RETRY_AFTER_S = 300.0

#: RSI envelope `code` values meaning "your session is not valid" -- auth, never retried.
_AUTH_CODES = frozenset({"ErrApiLoginRequired", "ErrApiUnauthorized", "ErrApiForbidden"})

_meter = metrics.get_meter("waddles.svc_ingest.spectrum")
_tracer = trace.get_tracer("waddles.svc_ingest.spectrum")
_poll_duration = _meter.create_histogram(
    "waddles_spectrum_poll_duration_seconds",
    unit="s",
    description="Wall time of one Spectrum REST poll, by kind and outcome.",
)
_ingest_lag = _meter.create_histogram(
    "waddles_spectrum_ingest_lag_seconds",
    unit="s",
    description="Delay between a Spectrum item's creation and Waddles ingesting it.",
)
_items_counter = _meter.create_counter(
    "waddles_spectrum_items_ingested_total",
    description="Spectrum items yielded into the ingest pipeline, by kind.",
)
_errors_counter = _meter.create_counter(
    "waddles_spectrum_poll_errors_total",
    description="Spectrum poll failures, by kind and reason.",
)

#: `(seen-ids dedupe)` flag evaluator -- returns True when ingest is enabled.
FlagCheck = Callable[[], Awaitable[bool]]


class SpectrumAuthError(Exception):
    """RSI rejected the session (401/403/login-required) -- never retried."""


class SpectrumTransientError(Exception):
    """A retryable failure (429/5xx/network); `retry_after_s` honors RSI's own hint."""

    def __init__(self, reason: str, retry_after_s: float | None = None) -> None:
        """Record the machine-readable `reason` and optional server `Retry-After`."""
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


class SpectrumEndpointError(Exception):
    """The endpoint is gone or its response envelope changed -- RSI changed something."""


@dataclass(slots=True, frozen=True)
class SpectrumItem:
    """One normalized Spectrum forum thread/reply or lobby message."""

    item_id: str
    kind: str
    source_id: str
    text: str
    author_id: str | None
    display_name: str | None
    created_at: str | None
    created_epoch: float | None
    thread_id: str | None = None
    is_reply: bool = False


class SpectrumProvider(Protocol):
    """Transport-layer contract -- swap in an official API/websocket without touching the loop."""

    async def fetch(self, kind: str, source_id: str) -> list[SpectrumItem]:
        """Return the latest items for one source, oldest first. Raises the Spectrum*Error types."""
        ...

    async def fetch_roster(self, source_id: str) -> list[SpectrumMember]:
        """Return the COMPLETE member roster of one community (all pages, or raise)."""
        ...

    async def fetch_events(self, source_id: str) -> list[SpectrumEvent]:
        """Return the community's current event list (all pages, or raise)."""
        ...


def _s(value: object) -> str | None:
    """Non-empty `str` (ints coerced) or `None`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) and value else None


def _epoch(value: object) -> float | None:
    """RSI timestamps are epoch seconds (int/float/numeric str); anything else -> `None`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            # Length only -- the raw value is upstream (RSI) content, never logged.
            logger.debug("spectrum.epoch_non_numeric: length=%d, treated as absent", len(value))
            return None
    return None


def _iso(epoch: float | None) -> str | None:
    """Epoch seconds -> UTC ISO-8601, or `None`."""
    if epoch is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _item_from_mapping(raw: Mapping[str, Any], kind: str, source_id: str) -> SpectrumItem | None:
    """Best-effort map of one RSI message/thread dict; `None` if it has no id or text."""
    item_id = _s(raw.get("id"))
    text = _s(raw.get("plaintext")) or _s(raw.get("body")) or _s(raw.get("subject"))
    if item_id is None or text is None:
        return None
    member = raw.get("member")
    member = member if isinstance(member, Mapping) else {}
    created = _epoch(raw.get("time_created"))
    return SpectrumItem(
        item_id=item_id,
        kind=kind,
        source_id=source_id,
        text=text.strip(),
        author_id=_s(member.get("id")) or _s(raw.get("member_id")),
        display_name=_s(member.get("displayname")) or _s(member.get("nickname")),
        created_at=_iso(created),
        created_epoch=created,
        thread_id=_s(raw.get("thread_id")),
        is_reply=bool(raw.get("is_reply")) or _s(raw.get("parent_id")) is not None,
    )


def _role_pair(raw: object) -> tuple[str, str | None] | None:
    """One role entry -> `(id, name)`; accepts `{id, name}` dicts or bare ids; else `None`."""
    if isinstance(raw, Mapping):
        role_id = _s(raw.get("id"))
        return (role_id, _s(raw.get("name"))) if role_id else None
    role_id = _s(raw)
    return (role_id, None) if role_id else None


def _member_from_mapping(raw: Mapping[str, Any]) -> SpectrumMember | None:
    """Best-effort map of one RSI roster entry; `None` if it has no member id.

    Org rank (when present) is folded in as a pseudo-role `rank:<id>` so a promotion
    or demotion surfaces as a `roles_changed` event just like a role grant.
    """
    member_id = _s(raw.get("id")) or _s(raw.get("member_id"))
    if member_id is None:
        return None
    pairs: dict[str, str | None] = {}
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list):
        roles_raw = raw.get("role_ids")
    for entry in roles_raw if isinstance(roles_raw, list) else []:
        pair = _role_pair(entry)
        if pair is not None:
            pairs[pair[0]] = pair[1]
    rank = raw.get("rank")
    if isinstance(rank, Mapping) and (rank_id := _s(rank.get("id"))) is not None:
        pairs[f"rank:{rank_id}"] = _s(rank.get("name"))
    return SpectrumMember(
        member_id=member_id,
        display_name=_s(raw.get("displayname")) or _s(raw.get("nickname")) or _s(raw.get("handle")),
        roles=tuple(sorted(pairs.items())),
    )


def _count_or_none(value: object) -> int | None:
    """A non-negative integer count (numeric str ok) or `None`; bools are not counts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _event_from_mapping(raw: Mapping[str, Any]) -> SpectrumEvent | None:
    """Best-effort map of one RSI event entry; `None` if it has no event id.

    Field names vary across RSI revisions (`time_start`/`start_time`, nested `rsvp`
    vs flat counts), so each is read through a short alias list.
    """
    event_id = _s(raw.get("id")) or _s(raw.get("event_id"))
    if event_id is None:
        return None
    rsvp = raw.get("rsvp")
    rsvp = rsvp if isinstance(rsvp, Mapping) else {}
    attending = _count_or_none(rsvp.get("count_attending"))
    if attending is None:
        attending = _count_or_none(raw.get("rsvp_count"))
    if attending is None:
        attending = _count_or_none(raw.get("attending_count"))
    organizer = raw.get("member")
    organizer = organizer if isinstance(organizer, Mapping) else {}
    cancelled = bool(raw.get("cancelled")) or _s(raw.get("status")) == STATUS_CANCELLED
    description = _s(raw.get("description")) or _s(raw.get("body"))
    return SpectrumEvent(
        event_id=event_id,
        title=_s(raw.get("title")) or _s(raw.get("name")),
        description=description.strip() if description else None,
        starts_epoch=_epoch(raw.get("time_start"))
        or _epoch(raw.get("start_time"))
        or _epoch(raw.get("starts_at")),
        ends_epoch=_epoch(raw.get("time_end"))
        or _epoch(raw.get("end_time"))
        or _epoch(raw.get("ends_at")),
        location=_s(raw.get("location")),
        organizer_id=_s(organizer.get("id")) or _s(raw.get("member_id")),
        status=STATUS_CANCELLED if cancelled else STATUS_SCHEDULED,
        rsvp_count=attending,
    )


class RsiRestProvider:
    """Reverse-engineered RSI Spectrum REST provider (community-documented, may change).

    Paths are config-overridable because RSI can change them without notice;
    the defaults are the community-documented ones (unverified against live
    Spectrum -- an envelope that doesn't match fails loud, never as an empty
    result). Never logs the token.
    """

    #: kind -> request path (relative to the API base)
    DEFAULT_PATHS: ClassVar[dict[str, str]] = {
        KIND_FORUM: "/forum/channel/threads",
        KIND_LOBBY: "/lobby/messages",
        KIND_ROSTER: "/community/member/list",
        KIND_EVENTS: "/community/event/list",
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        paths: Mapping[str, str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_delay_s: float = DEFAULT_PAGE_DELAY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind the shared client + session token; resolves nothing at construction."""
        self._client = client
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._paths = {**self.DEFAULT_PATHS, **(paths or {})}
        self._page_size = page_size
        self._max_pages = max_pages
        self._page_delay_s = page_delay_s
        self._sleep = sleep

    async def fetch(self, kind: str, source_id: str) -> list[SpectrumItem]:
        """POST the source's list endpoint and map the response; see class docstring."""
        path = self._paths[kind]
        body: dict[str, Any] = (
            {"channel_id": source_id, "page": 1, "sort": "newest"}
            if kind == KIND_FORUM
            else {"lobby_id": source_id, "limit": 50}
        )
        data = await self._call(kind, body)
        key = "threads" if kind == KIND_FORUM else "messages"
        raw_items = data.get(key)
        if not isinstance(raw_items, list):
            raise SpectrumEndpointError(f"spectrum endpoint {path} has no {key!r} list")
        items = [
            mapped
            for raw in raw_items
            if isinstance(raw, Mapping)
            and (mapped := _item_from_mapping(raw, kind, source_id)) is not None
        ]
        # Oldest first so downstream ordering matches Spectrum's own timeline.
        items.sort(key=lambda i: (i.created_epoch or 0.0, i.item_id))
        return items

    async def fetch_roster(self, source_id: str) -> list[SpectrumMember]:
        """Page through the community's member list; returns ALL members or raises."""
        return await self._fetch_paged(
            KIND_ROSTER,
            source_id,
            list_keys=("members", "member_list"),
            parse=_member_from_mapping,
            key_of=lambda m: m.member_id,
        )

    async def fetch_events(self, source_id: str) -> list[SpectrumEvent]:
        """Page through the community's event list; returns ALL events or raises."""
        return await self._fetch_paged(
            KIND_EVENTS,
            source_id,
            list_keys=("events",),
            parse=_event_from_mapping,
            key_of=lambda e: e.event_id,
        )

    async def _fetch_paged[T](
        self,
        kind: str,
        source_id: str,
        *,
        list_keys: tuple[str, ...],
        parse: Callable[[Mapping[str, Any]], T | None],
        key_of: Callable[[T], str],
    ) -> list[T]:
        """Collect every page of a snapshot endpoint, or raise -- never a partial result.

        A truncated snapshot would diff as mass departure, so: hitting `max_pages`
        with data still arriving is an endpoint error; an empty page, a page that
        adds nothing new (server ignoring `page`), or reaching the reported `total`
        ends the walk. Pages are spaced `page_delay_s` apart (respectful polling).
        """
        path = self._paths[kind]
        found: dict[str, T] = {}
        total: int | None = None
        for page in range(1, self._max_pages + 1):
            body = {"community_id": source_id, "page": page, "pagesize": self._page_size}
            data = await self._call(kind, body)
            raw_items = next((data[k] for k in list_keys if isinstance(data.get(k), list)), None)
            if raw_items is None:
                raise SpectrumEndpointError(
                    f"spectrum endpoint {path} has no {list_keys[0]!r} list"
                )
            reported = _count_or_none(data.get("total"))
            total = reported if reported is not None else total
            if not raw_items:
                break
            parsed = [
                item
                for raw in raw_items
                if isinstance(raw, Mapping) and (item := parse(raw)) is not None
            ]
            if not parsed:
                raise SpectrumEndpointError(
                    f"spectrum endpoint {path} returned {len(raw_items)} entries, none parseable"
                )
            fresh = 0
            for item in parsed:
                item_key = key_of(item)
                if item_key not in found:
                    fresh += 1
                found[item_key] = item
            if fresh == 0 or (total is not None and len(found) >= total):
                break
            await self._sleep(self._page_delay_s)
        else:
            raise SpectrumEndpointError(
                f"spectrum endpoint {path} still returning data after {self._max_pages} pages "
                "-- refusing a partial snapshot"
            )
        if total is not None and len(found) < total:
            logger.warning(
                "receiver.spectrum_snapshot_short kind=%s source=%s collected=%d reported=%d",
                kind,
                source_id,
                len(found),
                total,
            )
        return list(found.values())

    async def _call(self, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """POST one request and return the RSI envelope's `data` object (error-mapped)."""
        path = self._paths[kind]
        headers = {
            "Cookie": f"Rsi-Token={self._token}",
            "X-Rsi-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = await guarded_request(
                self._client, "POST", f"{self._api_base}{path}", json=body, headers=headers
            )
        except SSRFError as exc:
            raise SpectrumEndpointError(f"spectrum URL rejected by SSRF guard: {exc}") from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise SpectrumTransientError(f"network:{type(exc).__name__}") from exc

        status = response.status_code
        if status in (401, 403):
            raise SpectrumAuthError(f"spectrum rejected the session (HTTP {status})")
        if status == 429:
            raise SpectrumTransientError("rate_limited", _retry_after(response))
        if status >= 500:
            raise SpectrumTransientError(f"http_{status}", _retry_after(response))
        if status >= 400:
            raise SpectrumEndpointError(f"spectrum endpoint {path} returned HTTP {status}")

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SpectrumEndpointError(f"spectrum endpoint {path} returned non-JSON") from exc
        return self._unwrap(payload, path)

    @staticmethod
    def _unwrap(payload: Any, path: str) -> dict[str, Any]:
        """Validate the RSI `{success, code, data}` envelope; return its `data` object."""
        if not isinstance(payload, dict):
            raise SpectrumEndpointError(f"spectrum endpoint {path} envelope is not an object")
        if payload.get("success") != 1:
            code = payload.get("code")
            if isinstance(code, str) and code in _AUTH_CODES:
                raise SpectrumAuthError(f"spectrum rejected the session ({code})")
            raise SpectrumEndpointError(
                f"spectrum endpoint {path} reported failure code={_s(code) or 'unknown'}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SpectrumEndpointError(f"spectrum endpoint {path} has no 'data' object")
        return data


def _retry_after(response: httpx.Response) -> float | None:
    """Parse a numeric `Retry-After` header, capped; `None` if absent/non-numeric."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER_S))
    except ValueError:
        # Typically an HTTP-date form (RFC 9110) we deliberately don't parse; length only.
        logger.debug("spectrum.retry_after_non_numeric: length=%d, using default backoff", len(raw))
        return None


@dataclass(slots=True)
class _PollState:
    """Per-`receive()` mutable state: bounded dedupe set + backoff counters."""

    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    primed: bool = False
    consecutive_errors: int = 0
    flag_on: bool | None = None
    flag_checked_at: float = 0.0

    def remember(self, item_id: str) -> bool:
        """Record `item_id`; return True if it was new. Evicts oldest beyond `_SEEN_CAP`."""
        if item_id in self.seen:
            return False
        self.seen[item_id] = None
        while len(self.seen) > _SEEN_CAP:
            self.seen.popitem(last=False)
        return True


@dataclass(slots=True, frozen=True)
class _LoopConfig:
    """Tunables for one `receive()` run, parsed once from the receiver config."""

    interval_s: float
    max_errors: int
    base_backoff_s: float
    max_backoff_s: float
    recheck_s: float
    emit_backlog: bool
    guard_ratio: float
    guard_min_size: int


@dataclass(slots=True)
class _OrgResult:
    """One org-sync cycle: the changes to yield and the snapshot to persist AFTER yielding."""

    raw_events: list[dict[str, Any]]
    snapshot: dict[str, Any]
    primed: bool
    fetched: int


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker.
class SpectrumPollReceiver(Transport):  # type: ignore[misc]
    """One Spectrum source's poll loop per `receive()` call (inbound only).

    A source is a forum channel, a lobby, or -- org sync -- one community's
    `roster` or `events` (see the module docstring).
    """

    name: ClassVar[str] = "spectrum_poll"
    #: INBOUND only -- outbound posting to Spectrum is explicitly out of scope (issue #101).
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        provider: SpectrumProvider | None = None,
        flag_check: FlagCheck | None = None,
        snapshot_store: SnapshotStore | None = None,
    ) -> None:
        """Build the receiver; nothing is resolved/called until `receive()`.

        `http_client`/`provider` are test-injection points (caller-owned,
        never closed here). `flag_check=None` means "always enabled" -- the
        production wiring (`app.py`) always supplies one. `snapshot_store`
        persists org-sync snapshots (production wiring supplies the Valkey-backed
        one); when omitted an in-process store is used, so snapshots -- and with
        them changes made while the process was down -- do not survive a restart.
        """
        self._injected_http_client = http_client
        self._injected_provider = provider
        self._flag_check = flag_check
        self._snapshot_store: SnapshotStore = snapshot_store or MemorySnapshotStore()
        self._sleep = asyncio.sleep
        self._monotonic = time.monotonic
        self._now = time.time

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Poll one Spectrum source, yielding one raw event dict per NEW item / CHANGE.

        Required: `config["kind"]` (`forum`|`lobby`|`roster`|`events`),
        `config["source_id"]` (channel / lobby / community id).
        Credentials: `config["token_ref"]` (env var NAME) -- unless a
        `provider` was injected. Optional: `api_base`, `paths`,
        `poll_interval_s`, `max_consecutive_errors`, `base_backoff_s`,
        `max_backoff_s`, `flag_recheck_s`, `emit_backlog` (default False --
        the first poll only primes the dedupe set / snapshot so connecting
        never floods downstream platforms with history). Org kinds also read
        `roster_max_departure_ratio`, `roster_guard_min_size`, `page_size`,
        `max_pages` and `page_delay_s`.

        Raises `NonRetryableTransportError` for bad config, missing creds,
        auth rejection or an endpoint RSI changed;
        `RetryableTransportError` once `max_consecutive_errors` transient
        failures accumulate (the supervisor then restarts with backoff).
        """
        kind = config.get("kind")
        source_id = config.get("source_id")
        if kind not in _KINDS:
            raise NonRetryableTransportError(
                "spectrum poll config 'kind' must be forum|lobby|roster|events"
            )
        if not isinstance(source_id, str) or not source_id:
            raise NonRetryableTransportError("spectrum poll config missing required 'source_id'")
        assert isinstance(kind, str)  # noqa: S101 - mypy narrowing after the membership check

        if self._injected_provider is not None:
            async for item in self._poll_loop(self._injected_provider, kind, source_id, config):
                yield item
            return

        token = self._resolve_token(config)
        api_base = str(config.get("api_base") or DEFAULT_API_BASE)
        paths = config.get("paths")
        path_map = paths if isinstance(paths, Mapping) else None
        page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))
        max_pages = int(config.get("max_pages", DEFAULT_MAX_PAGES))
        page_delay_s = float(config.get("page_delay_s", DEFAULT_PAGE_DELAY_S))
        if self._injected_http_client is not None:
            provider = RsiRestProvider(
                self._injected_http_client,
                token,
                api_base=api_base,
                paths=path_map,
                page_size=page_size,
                max_pages=max_pages,
                page_delay_s=page_delay_s,
                sleep=self._sleep,
            )
            async for item in self._poll_loop(provider, kind, source_id, config):
                yield item
            return
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=False) as client:
            provider = RsiRestProvider(
                client,
                token,
                api_base=api_base,
                paths=path_map,
                page_size=page_size,
                max_pages=max_pages,
                page_delay_s=page_delay_s,
                sleep=self._sleep,
            )
            async for item in self._poll_loop(provider, kind, source_id, config):
                yield item

    @staticmethod
    def _resolve_token(config: Mapping[str, Any]) -> str:
        """Resolve `config["token_ref"]` (env var name) to the RSI session token, fail-loud."""
        token_ref = config.get("token_ref")
        if not isinstance(token_ref, str) or not token_ref:
            raise NonRetryableTransportError("spectrum poll config missing required 'token_ref'")
        try:
            return str(resolve_secret(token_ref))
        except SecretResolutionError as exc:
            raise NonRetryableTransportError(
                f"spectrum RSI session token env var {token_ref!r} is not set"
            ) from exc

    async def _flag_enabled(self, state: _PollState, recheck_s: float) -> bool:
        """Cached flag evaluation; a failing flag backend keeps the last-known value (else OFF)."""
        if self._flag_check is None:
            return True
        now = self._monotonic()
        if state.flag_on is not None and now - state.flag_checked_at < recheck_s:
            return state.flag_on
        try:
            enabled = bool(await self._flag_check())
        except Exception as exc:  # noqa: BLE001 - flag backend outage must never kill ingest
            logger.warning(
                "receiver.spectrum_flag_check_failed exc=%s -- keeping last value",
                type(exc).__name__,
            )
            enabled = bool(state.flag_on)
        if enabled != state.flag_on:
            logger.info("receiver.spectrum_flag_changed enabled=%s", enabled)
        state.flag_on = enabled
        state.flag_checked_at = now
        return enabled

    @staticmethod
    def _parse_config(kind: str, config: Mapping[str, Any]) -> _LoopConfig:
        """Read the loop tunables; org kinds get a slower default and a higher interval floor."""
        org = kind in ORG_KINDS
        floor = MIN_ORG_POLL_INTERVAL_S if org else MIN_POLL_INTERVAL_S
        default = DEFAULT_ORG_POLL_INTERVAL_S if org else DEFAULT_POLL_INTERVAL_S
        return _LoopConfig(
            interval_s=max(floor, float(config.get("poll_interval_s", default))),
            max_errors=int(config.get("max_consecutive_errors", DEFAULT_MAX_CONSECUTIVE_ERRORS)),
            base_backoff_s=float(config.get("base_backoff_s", DEFAULT_BASE_BACKOFF_S)),
            max_backoff_s=float(config.get("max_backoff_s", DEFAULT_MAX_BACKOFF_S)),
            recheck_s=float(config.get("flag_recheck_s", DEFAULT_FLAG_RECHECK_S)),
            emit_backlog=bool(config.get("emit_backlog", False)),
            guard_ratio=float(
                config.get("roster_max_departure_ratio", DEFAULT_MAX_DEPARTURE_RATIO)
            ),
            guard_min_size=int(config.get("roster_guard_min_size", DEFAULT_GUARD_MIN_SIZE)),
        )

    async def _transient(
        self,
        exc: SpectrumTransientError,
        state: _PollState,
        cfg: _LoopConfig,
        *,
        kind: str,
        source_id: str,
        started: float,
    ) -> None:
        """Count a transient failure, then back off (or escalate to the supervisor).

        Org kinds never back off to less than one poll interval -- a failing roster
        fetch is several requests, so retrying faster than the normal cadence would
        hammer RSI exactly when it is struggling.
        """
        _record_error(kind, exc.reason, started, self._monotonic())
        state.consecutive_errors += 1
        if state.consecutive_errors >= cfg.max_errors:
            logger.warning(
                "receiver.spectrum_giving_up kind=%s source=%s consecutive=%d reason=%s",
                kind,
                source_id,
                state.consecutive_errors,
                exc.reason,
            )
            raise RetryableTransportError(
                f"spectrum poll failed {state.consecutive_errors}x: {exc.reason}"
            ) from exc
        delay = exc.retry_after_s
        if delay is None:
            delay = min(
                cfg.max_backoff_s, cfg.base_backoff_s * (2 ** (state.consecutive_errors - 1))
            )
        if kind in ORG_KINDS:
            delay = max(delay, cfg.interval_s)
        logger.debug(
            "receiver.spectrum_backoff kind=%s source=%s consecutive=%d delay_s=%.1f",
            kind,
            source_id,
            state.consecutive_errors,
            delay,
        )
        await self._sleep(delay)

    async def _org_cycle(
        self,
        provider: SpectrumProvider,
        kind: str,
        source_id: str,
        cfg: _LoopConfig,
    ) -> _OrgResult:
        """Fetch one org snapshot and diff it against the stored one (nothing persisted yet).

        Store outages and shrink-guard trips surface as `SpectrumTransientError`
        so the loop's single backoff path handles them; an anomalous snapshot is
        deliberately NOT advanced, so the next poll re-evaluates against the last
        known-good state.
        """
        store_key = f"{kind}:{source_id}"
        try:
            previous = await self._snapshot_store.load(store_key)
        except SnapshotStoreError as exc:
            logger.error(
                "receiver.spectrum_snapshot_store_failed kind=%s source=%s op=load exc=%s",
                kind,
                source_id,
                type(exc).__name__,
            )
            raise SpectrumTransientError("snapshot_store") from exc
        now = self._now()
        observed_at = _iso(now) or ""
        try:
            if kind == KIND_ROSTER:
                members = await provider.fetch_roster(source_id)
                rdiff = diff_roster(
                    previous,
                    members,
                    emit_backlog=cfg.emit_backlog,
                    max_departure_ratio=cfg.guard_ratio,
                    guard_min_size=cfg.guard_min_size,
                )
                return _OrgResult(
                    [_roster_raw(c, source_id, observed_at) for c in rdiff.changes],
                    rdiff.snapshot,
                    rdiff.primed,
                    len(members),
                )
            events = await provider.fetch_events(source_id)
            ediff = diff_events(
                previous,
                events,
                now=now,
                emit_backlog=cfg.emit_backlog,
                max_departure_ratio=cfg.guard_ratio,
            )
            return _OrgResult(
                [_event_raw(c, source_id, observed_at) for c in ediff.changes],
                ediff.snapshot,
                ediff.primed,
                len(events),
            )
        except SnapshotAnomalyError as exc:
            logger.error(
                "receiver.spectrum_snapshot_anomaly kind=%s source=%s reason=%s "
                "-- snapshot NOT advanced",
                kind,
                source_id,
                exc,
            )
            raise SpectrumTransientError("snapshot_anomaly") from exc

    async def _poll_loop(
        self,
        provider: SpectrumProvider,
        kind: str,
        source_id: str,
        config: Mapping[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Flag-gated poll/dedupe-or-diff/backoff loop -- see `receive()`'s docstring."""
        cfg = self._parse_config(kind, config)
        org = kind in ORG_KINDS
        state = _PollState()
        attrs = {"kind": kind}
        if org:
            logger.info(
                "receiver.spectrum_org_started kind=%s source=%s store=%s interval_s=%.0f",
                kind,
                source_id,
                type(self._snapshot_store).__name__,
                cfg.interval_s,
            )

        while True:
            if not await self._flag_enabled(state, cfg.recheck_s):
                await self._sleep(cfg.interval_s)
                continue

            started = self._monotonic()
            org_result: _OrgResult | None = None
            items: list[SpectrumItem] = []
            try:
                with _tracer.start_as_current_span("spectrum.poll") as span:
                    span.set_attribute("spectrum.kind", kind)
                    span.set_attribute("spectrum.source_id", source_id)
                    if org:
                        org_result = await self._org_cycle(provider, kind, source_id, cfg)
                        span.set_attribute("spectrum.items", org_result.fetched)
                    else:
                        items = await provider.fetch(kind, source_id)
                        span.set_attribute("spectrum.items", len(items))
            except SpectrumAuthError as exc:
                _record_error(kind, "auth", started, self._monotonic())
                logger.error(
                    "receiver.spectrum_auth_failed kind=%s source=%s -- not retrying",
                    kind,
                    source_id,
                )
                raise NonRetryableTransportError(str(exc)) from exc
            except SpectrumEndpointError as exc:
                _record_error(kind, "endpoint_changed", started, self._monotonic())
                logger.error(
                    "receiver.spectrum_endpoint_unusable kind=%s source=%s reason=%s",
                    kind,
                    source_id,
                    exc,
                )
                raise NonRetryableTransportError(str(exc)) from exc
            except SpectrumTransientError as exc:
                await self._transient(
                    exc, state, cfg, kind=kind, source_id=source_id, started=started
                )
                continue

            state.consecutive_errors = 0
            _poll_duration.record(self._monotonic() - started, {**attrs, "outcome": "ok"})

            if org_result is not None:
                async for raw in self._emit_org(org_result, kind, source_id, cfg):
                    yield raw
                try:
                    await self._snapshot_store.save(f"{kind}:{source_id}", org_result.snapshot)
                except SnapshotStoreError as exc:
                    logger.error(
                        "receiver.spectrum_snapshot_store_failed kind=%s source=%s op=save exc=%s",
                        kind,
                        source_id,
                        type(exc).__name__,
                    )
                    await self._transient(
                        SpectrumTransientError("snapshot_store"),
                        state,
                        cfg,
                        kind=kind,
                        source_id=source_id,
                        started=self._monotonic(),
                    )
                    continue
                await self._sleep(cfg.interval_s)
                continue

            fresh = [i for i in items if state.remember(i.item_id)]
            if not state.primed:
                state.primed = True
                logger.info(
                    "gateway.spectrum_ready kind=%s source=%s primed=%d emit_backlog=%s",
                    kind,
                    source_id,
                    len(fresh),
                    cfg.emit_backlog,
                )
                if not cfg.emit_backlog:
                    fresh = []
            for item in fresh:
                if item.created_epoch is not None:
                    _ingest_lag.record(max(0.0, self._now() - item.created_epoch), attrs)
                _items_counter.add(1, attrs)
                yield _to_raw_event(item)
            logger.debug(
                "receiver.spectrum_poll kind=%s source=%s fetched=%d new=%d",
                kind,
                source_id,
                len(items),
                len(fresh),
            )
            await self._sleep(cfg.interval_s)

    async def _emit_org(
        self, result: _OrgResult, kind: str, source_id: str, cfg: _LoopConfig
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield an org cycle's raw change events (counting each) and log the cycle."""
        if result.primed:
            logger.info(
                "gateway.spectrum_ready kind=%s source=%s primed=%d emit_backlog=%s",
                kind,
                source_id,
                result.fetched,
                cfg.emit_backlog,
            )
        for raw in result.raw_events:
            _items_counter.add(1, {"kind": kind})
            yield raw
        logger.debug(
            "receiver.spectrum_org_poll kind=%s source=%s fetched=%d changes=%d",
            kind,
            source_id,
            result.fetched,
            len(result.raw_events),
        )


def _record_error(kind: str, reason: str, started: float, ended: float) -> None:
    """Record a failed poll's duration + error counter (bounded-cardinality `reason`)."""
    reason_label = reason if len(reason) <= 40 else reason[:40]
    _poll_duration.record(ended - started, {"kind": kind, "outcome": "error"})
    _errors_counter.add(1, {"kind": kind, "reason": reason_label})


def _to_raw_event(item: SpectrumItem) -> dict[str, Any]:
    """Raw event dict that `builtin_handlers/spectrum_ingest.py` `normalize()` consumes."""
    return {
        "platform": "spectrum",
        "kind": item.kind,
        "source_id": item.source_id,
        "text": item.text,
        "message_id": item.item_id,
        "thread_id": item.thread_id,
        "is_reply": item.is_reply,
        "author_id": item.author_id,
        "display_name": item.display_name,
        "created_at": item.created_at,
    }


def _roster_raw(change: RosterChange, source_id: str, observed_at: str) -> dict[str, Any]:
    """Raw event dict for a roster delta; `normalize()` maps it to a `member_*` PlatformEvent."""
    return {
        "platform": "spectrum",
        "kind": KIND_ROSTER,
        "source_id": source_id,
        "change": change.change,
        "member_id": change.member_id,
        "display_name": change.display_name,
        "roles_added": list(change.roles_added),
        "roles_removed": list(change.roles_removed),
        "role_names": list(change.role_names),
        "observed_at": observed_at,
    }


def _event_raw(change: EventChange, source_id: str, observed_at: str) -> dict[str, Any]:
    """Raw event dict for an event delta; `normalize()` maps it to an `org_event_*` event."""
    event = change.event
    description = event.description if event else None
    if description is not None:
        description = description[:MAX_DESCRIPTION_CHARS]
    return {
        "platform": "spectrum",
        "kind": KIND_EVENTS,
        "source_id": source_id,
        "change": change.change,
        "event_id": change.event_id,
        "title": event.title if event else None,
        "description": description,
        "starts_at": _iso(event.starts_epoch) if event else None,
        "ends_at": _iso(event.ends_epoch) if event else None,
        "location": event.location if event else None,
        "organizer_id": event.organizer_id if event else None,
        "status": event.status if event else None,
        "rsvp_count": event.rsvp_count if event else None,
        "rsvp_previous": change.rsvp_previous,
        "observed_at": observed_at,
    }
