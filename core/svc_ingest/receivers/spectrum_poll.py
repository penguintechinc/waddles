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

One poller per source (`kind` = `forum` channel or `lobby`), lease-guarded
by `app.py` (`provider="spectrum", community="<kind>-<id>"`), matching
`receivers/youtube_live_poll.py`'s per-channel precedent.

Logs are PII-free by construction: only source ids, counts, status codes
and exception types -- never message text, handles, or the token.

DEFERRED (documented in the PR, not stubbed): websocket real-time lobby
streaming, roster/role-change sync, event/RSVP sync, DM capture (opt-in),
RSI-handle <-> profile linking and the admin connection-management API.
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

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Spectrum item declares
#: (`bundles/spectrum_ingest.py`'s `stages.ingest.consumes`).
CONSUMES_TAG = "spectrum.message"

#: Flag key (ENV baseline `FLAG_WADDLES_SPECTRUM_INTEGRATION`, default OFF).
FLAG_KEY = "waddles.spectrum-integration"

KIND_FORUM = "forum"
KIND_LOBBY = "lobby"
_KINDS = frozenset({KIND_FORUM, KIND_LOBBY})

DEFAULT_API_BASE = "https://robertsspaceindustries.com/api/spectrum"
_HTTP_TIMEOUT_S = 15.0
#: Floor under any configured poll interval -- respectful polling, never a tight loop.
MIN_POLL_INTERVAL_S = 5.0
DEFAULT_POLL_INTERVAL_S = 15.0
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


class RsiRestProvider:
    """Reverse-engineered RSI Spectrum REST provider (community-documented, may change).

    Paths are config-overridable because RSI can change them without notice;
    the defaults are the community-documented ones. Never logs the token.
    """

    #: kind -> (path, request-body builder, response list key)
    DEFAULT_PATHS: ClassVar[dict[str, str]] = {
        KIND_FORUM: "/forum/channel/threads",
        KIND_LOBBY: "/lobby/messages",
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        paths: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the shared client + session token; resolves nothing at construction."""
        self._client = client
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._paths = {**self.DEFAULT_PATHS, **(paths or {})}

    async def fetch(self, kind: str, source_id: str) -> list[SpectrumItem]:
        """POST the source's list endpoint and map the response; see class docstring."""
        path = self._paths[kind]
        body: dict[str, Any] = (
            {"channel_id": source_id, "page": 1, "sort": "newest"}
            if kind == KIND_FORUM
            else {"lobby_id": source_id, "limit": 50}
        )
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
        return self._parse(payload, kind, source_id, path)

    @staticmethod
    def _parse(payload: Any, kind: str, source_id: str, path: str) -> list[SpectrumItem]:
        """Validate the RSI `{success, code, data}` envelope and map its item list."""
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


def _retry_after(response: httpx.Response) -> float | None:
    """Parse a numeric `Retry-After` header, capped; `None` if absent/non-numeric."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER_S))
    except ValueError:
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


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker.
class SpectrumPollReceiver(Transport):  # type: ignore[misc]
    """One Spectrum source's (forum channel or lobby) poll loop per `receive()` call (inbound)."""

    name: ClassVar[str] = "spectrum_poll"
    #: INBOUND only -- outbound posting to Spectrum is explicitly out of scope (issue #101).
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        provider: SpectrumProvider | None = None,
        flag_check: FlagCheck | None = None,
    ) -> None:
        """Build the receiver; nothing is resolved/called until `receive()`.

        `http_client`/`provider` are test-injection points (caller-owned,
        never closed here). `flag_check=None` means "always enabled" -- the
        production wiring (`app.py`) always supplies one.
        """
        self._injected_http_client = http_client
        self._injected_provider = provider
        self._flag_check = flag_check
        self._sleep = asyncio.sleep
        self._monotonic = time.monotonic
        self._now = time.time

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Poll one Spectrum source, yielding one raw event dict per NEW item.

        Required: `config["kind"]` (`forum`|`lobby`), `config["source_id"]`.
        Credentials: `config["token_ref"]` (env var NAME) -- unless a
        `provider` was injected. Optional: `api_base`, `paths`,
        `poll_interval_s`, `max_consecutive_errors`, `base_backoff_s`,
        `max_backoff_s`, `flag_recheck_s`, `emit_backlog` (default False --
        the first poll only primes the dedupe set so connecting never
        floods downstream platforms with history).

        Raises `NonRetryableTransportError` for bad config, missing creds,
        auth rejection or an endpoint RSI changed;
        `RetryableTransportError` once `max_consecutive_errors` transient
        failures accumulate (the supervisor then restarts with backoff).
        """
        kind = config.get("kind")
        source_id = config.get("source_id")
        if kind not in _KINDS:
            raise NonRetryableTransportError("spectrum poll config 'kind' must be forum|lobby")
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
        if self._injected_http_client is not None:
            provider = RsiRestProvider(
                self._injected_http_client, token, api_base=api_base, paths=path_map
            )
            async for item in self._poll_loop(provider, kind, source_id, config):
                yield item
            return
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=False) as client:
            provider = RsiRestProvider(client, token, api_base=api_base, paths=path_map)
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

    async def _poll_loop(
        self,
        provider: SpectrumProvider,
        kind: str,
        source_id: str,
        config: Mapping[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Flag-gated poll/dedupe/backoff loop -- see `receive()`'s docstring."""
        interval_s = max(
            MIN_POLL_INTERVAL_S, float(config.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S))
        )
        max_errors = int(config.get("max_consecutive_errors", DEFAULT_MAX_CONSECUTIVE_ERRORS))
        base_backoff = float(config.get("base_backoff_s", DEFAULT_BASE_BACKOFF_S))
        max_backoff = float(config.get("max_backoff_s", DEFAULT_MAX_BACKOFF_S))
        recheck_s = float(config.get("flag_recheck_s", DEFAULT_FLAG_RECHECK_S))
        emit_backlog = bool(config.get("emit_backlog", False))
        state = _PollState()
        attrs = {"kind": kind}

        while True:
            if not await self._flag_enabled(state, recheck_s):
                await self._sleep(interval_s)
                continue

            started = self._monotonic()
            outcome = "ok"
            try:
                with _tracer.start_as_current_span("spectrum.poll") as span:
                    span.set_attribute("spectrum.kind", kind)
                    span.set_attribute("spectrum.source_id", source_id)
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
                outcome = "transient"
                _record_error(kind, exc.reason, started, self._monotonic())
                state.consecutive_errors += 1
                if state.consecutive_errors >= max_errors:
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
                    delay = min(max_backoff, base_backoff * (2 ** (state.consecutive_errors - 1)))
                logger.debug(
                    "receiver.spectrum_backoff kind=%s source=%s consecutive=%d delay_s=%.1f",
                    kind,
                    source_id,
                    state.consecutive_errors,
                    delay,
                )
                await self._sleep(delay)
                continue

            state.consecutive_errors = 0
            _poll_duration.record(self._monotonic() - started, {**attrs, "outcome": outcome})

            fresh = [i for i in items if state.remember(i.item_id)]
            if not state.primed:
                state.primed = True
                logger.info(
                    "gateway.spectrum_ready kind=%s source=%s primed=%d emit_backlog=%s",
                    kind,
                    source_id,
                    len(fresh),
                    emit_backlog,
                )
                if not emit_backlog:
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
            await self._sleep(interval_s)


def _record_error(kind: str, reason: str, started: float, ended: float) -> None:
    """Record a failed poll's duration + error counter (bounded-cardinality `reason`)."""
    reason_label = reason if len(reason) <= 40 else reason[:40]
    _poll_duration.record(ended - started, {"kind": kind, "outcome": "error"})
    _errors_counter.add(1, {"kind": kind, "reason": reason_label})


def _to_raw_event(item: SpectrumItem) -> dict[str, Any]:
    """Raw event dict `bundles/spectrum_ingest.py::normalize()` consumes (receiver contract)."""
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
