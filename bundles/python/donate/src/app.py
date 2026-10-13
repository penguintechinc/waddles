"""`!donate` -> show / set / reset this community's donation (tip) link.

A static, per-community link: `!donate` shows it (or says it is not set), `!donate set <url>`
stores it and `!donate reset` clears it -- both mutations MOD-ONLY. State is the single `kv`
key `donate.url` (UTF-8 URL). It is community-scoped without any manual hashing: the key is a
plain literal and the `kv` host capability scopes every key server-side by
`(tenant, community, app_id)` (`core/bundle_host_kv/src/scope.rs::KvScope`). The key uses `.`,
never `:` (the host's reserved namespace separator, gh-631). No actor identity is stored.

Ordering inside `transform()`: cheap command match first, then the PostHog flag
``waddles.command-donate`` (default OFF), then command-grammar parsing, then the mod gate
(`set`/`reset` only), then URL validation, then `kv`.

Fail loud, never silent:

* The mod gate FAILS CLOSED -- it needs a real ``bool`` ``True`` in ``is_mod`` /
  ``is_broadcaster``; missing, ``False`` or any non-bool badge is refused with an explicit reply.
* A URL is accepted only if it is ``http(s)://`` with a well-formed dotted host, no embedded
  ``user:pass@`` credentials, no whitespace/control characters and at most 512 characters.
  Every rejection is an explicit reply naming the reason (never the URL value in logs).
* A `kv` failure (or an un-parseable stored value) is reclassified into a private `_KvError`,
  logged at ERROR and answered with an explicit reply.

Logs are PII-free: only op names, reason codes and exception types -- never the URL, the
message text or the actor.

`kv`, `log`, `relay` and `feature_enabled` are imported at MODULE LOAD on purpose:
componentize-py only wizens a WIT host-import that is imported eagerly, so a capability
imported only inside a function is absent from `wit_world.imports` at runtime
(`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (default OFF).
FLAG_KEY = "waddles.command-donate"

#: The single `kv` key this bundle owns (community-scoped by the host; `.`, never `:`).
URL_KEY = "donate.url"

#: Longest URL accepted (characters), checked before any parsing.
MAX_URL_LEN = 512

_SPEC = CommandSpec(name="donate")
_COMMAND = "!donate"
_USAGE = "Usage: !donate | !donate set <url> | !donate reset"
_SET_USAGE = "Usage: !donate set <url>"
_RESET_USAGE = "Usage: !donate reset"

_SHOW_REPLY = "Support the stream: {url}"
_UNSET_REPLY = "No donation link is set yet. A mod can add one with !donate set <url>"
_SET_REPLY = "Donation link set: {url}"
_RESET_REPLY = "Donation link cleared."
_DENIED_REPLY = "Only the broadcaster or a moderator can change the donation link."
_KV_FAILURE_REPLY = "Something went wrong with donation link storage - please try again."

_SCHEMES = frozenset({"http", "https"})
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_HOST_LEN = 253
_HOST_PARTS_MIN = 2


class UrlRejectedError(ValueError):
    """A submitted URL failed validation; `str(exc)` is the explicit user-facing reply."""


class _KvError(Exception):
    """Internal-only: a `kv` host-call failed or the stored value is corrupt.

    Always caught inside `transform`, never leaked. Mirrors `waddle_sdk.db`'s documented
    pattern ("Err is structurally classified, never imported") since `waddle_sdk.kv` does
    not classify the generated WIT `Err` itself.
    """


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # classified like waddle_sdk.db/http
        raise _KvError(f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- the link persists), reclassifying `Err` into `_KvError`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvError(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvError`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvError(f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


def _is_privileged(payload: dict[str, Any]) -> bool:
    """True only when a real ``bool`` ``True`` badge marks the sender mod/broadcaster.

    Strict identity (`is True`), never truthiness: a string ``"false"`` is truthy, so a
    truthiness gate would admit a non-moderator. Absent / ``False`` / any non-bool value is
    denied -- the gate FAILS CLOSED (Discord's normalizer carries no badges today).
    """
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _check_chars(raw: str) -> None:
    """Reject empty input and any whitespace, control or non-printable character."""
    if not raw:
        raise UrlRejectedError("That link is empty.")
    if any(c.isspace() or not c.isprintable() or c == "\\" for c in raw):
        raise UrlRejectedError("That link contains spaces or control characters.")


def _split(raw: str) -> SplitResult:
    """`urlsplit` + eager port parse; a malformed URL is logged (PII-free) and rejected."""
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError on a non-numeric / out-of-range port
    except ValueError as exc:
        log.debug("donate.url_malformed", error_type=type(exc).__name__)
        raise UrlRejectedError("That link is not a well-formed URL.") from exc
    return parts


def _check_host(host: str | None) -> None:
    """Require a lowercase-ASCII dotted domain name (no IP literals, no single label)."""
    if not host or len(host) > _MAX_HOST_LEN:
        raise UrlRejectedError("That link needs a valid host name, e.g. https://example.com/you")
    labels = host.split(".")
    if len(labels) < _HOST_PARTS_MIN or not all(_LABEL_RE.match(label) for label in labels):
        raise UrlRejectedError("That link needs a valid host name, e.g. https://example.com/you")
    if labels[-1].isdigit():
        raise UrlRejectedError("That link needs a domain name, not an IP address.")


def validate_url(raw: str) -> str:
    """Return `raw` unchanged if it is an acceptable link, else raise `UrlRejectedError`.

    Rules: <= 512 chars; no whitespace/control chars; scheme `http`/`https`; no embedded
    `user:pass@` credentials; a well-formed dotted domain host; a valid port if present.
    """
    if len(raw) > MAX_URL_LEN:
        raise UrlRejectedError(f"That link is too long (max {MAX_URL_LEN} characters).")
    _check_chars(raw)
    parts = _split(raw)
    if parts.scheme not in _SCHEMES:
        raise UrlRejectedError("That link must start with https:// or http://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UrlRejectedError("That link must not contain a username or password.")
    _check_host(parts.hostname)
    return raw


async def _show_reply() -> str:
    """Return the stored link reply (or the unset notice).

    Raises:
        _KvError: A `kv` round trip failed, or the stored value is not a valid link.
    """
    raw = await _kv_get(URL_KEY)
    if raw is None:
        return _UNSET_REPLY
    try:
        url = validate_url(raw.decode("utf-8"))
    except (UnicodeDecodeError, UrlRejectedError) as exc:
        raise _KvError(f"corrupt {URL_KEY}: {type(exc).__name__}") from exc
    return _SHOW_REPLY.format(url=url)


async def _mutate_reply(option: str, args: str | None) -> str:
    """Handle `set`/`reset` after the mod gate; returns the reply text.

    Raises:
        CommandUsageError: `set` without a URL, or `reset` with trailing text.
        UrlRejectedError: The URL failed validation.
        _KvError: A `kv` round trip failed.
    """
    if option == "set":
        if args is None:
            raise CommandUsageError(_SET_USAGE)
        url = validate_url(args)
        await _kv_set(URL_KEY, url.encode("utf-8"))
        log.info("donate.url_set")
        return _SET_REPLY.format(url=url)
    if args is not None:
        raise CommandUsageError(_RESET_USAGE)
    await _kv_delete(URL_KEY)
    log.info("donate.url_reset")
    return _RESET_REPLY


async def _build_reply(event: PlatformEvent, text: str) -> str:
    """Parse `text` and produce the reply; every failure mode maps to an explicit reply."""
    try:
        parsed = parse_command(text, _SPEC)
        if parsed.option is None:
            return await _show_reply()
        if parsed.option not in ("set", "reset"):
            raise CommandUsageError(_USAGE)
        if not _is_privileged(event.payload):
            log.info("donate.denied", op=parsed.option)
            return _DENIED_REPLY
        return await _mutate_reply(parsed.option, parsed.args)
    except CommandUsageError as exc:
        log.debug("donate.usage_error")
        return str(exc)
    except UrlRejectedError as exc:
        log.info("donate.url_rejected")
        return str(exc)
    except _KvError as exc:
        log.error("donate.kv_failure", error_type=type(exc.__cause__).__name__)
        return _KV_FAILURE_REPLY


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!donate` and build the reply.

    Returns `None` for a non-chat payload, any message that is not `!donate`, and while
    `waddles.command-donate` is off. Every other outcome -- link, unset notice, usage,
    denial, URL rejection, `kv` failure -- is an explicit reply.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != _COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    reply = await _build_reply(event, text)
    log.info("donate.transform matched")
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
        ValueError: The envelope's payload is missing `channel_id` or `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("donate reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("donate reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("donate.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
