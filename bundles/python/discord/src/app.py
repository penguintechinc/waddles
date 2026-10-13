"""`!discord` -> a per-community Discord invite link.

`!discord` (bare = read, anyone) replies with the community's configured invite
("Join our Discord: <url>"), or a friendly "no link set" message. `!discord set <url>` and
`!discord reset` (broadcaster/moderator only) set and clear it.

State: ONE community-scoped kv key, ``discord.invite`` (the literal guest-side key -- the WIT
`kv` host capability scopes every key server-side by `(tenant, community, app_id)`, so two
communities never see each other's link). Keys use ``.``, never ``:`` (gh-631).

Mod gate FAILS CLOSED: `_is_privileged()` requires a real `bool` ``True`` in
``event.payload["is_mod"]`` or ``["is_broadcaster"]`` -- missing, non-bool (e.g. the string
``"true"``) or false all refuse. Discord messages carry no role info today, so `set`/`reset`
there are refused until the ingest normalizer forwards it -- same documented limitation as
`bundles/python/count`.

URL validation is generic (any well-formed ``http(s)://`` URL with a host, no embedded
credentials, no whitespace) because a community may use a vanity invite domain; a link that
doesn't look like a Discord domain is still stored, with a soft warning in the reply.

Logs are PII-free: platform/op/exception-type only -- never the URL value, the actor, or any
message text.

`kv`, `log`, `relay` and `feature_enabled` are imported at MODULE LOAD on purpose --
componentize-py only wizens WIT host-imports imported eagerly (see
`docs/reference/bundle-host-imports.md`). Gated behind PostHog flag
``waddles.command-discord`` (default OFF; cheap command-match first, flag check second).
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-discord"

COMMAND = "!discord"

#: The single community-scoped kv key holding the invite URL (UTF-8 bytes).
INVITE_KEY = "discord.invite"

#: Longest URL accepted -- well inside the host's per-value limit.
MAX_URL_LEN = 512

_SPEC = CommandSpec(name="discord")
_USAGE = "Usage: !discord | !discord set <url> | !discord reset"
_DISCORD_HOSTS = ("discord.gg", "discord.com", "discordapp.com")
_NOT_PERMITTED = "Only the broadcaster or a moderator can change the Discord link."
_KV_ERROR = "Something went wrong with the Discord link storage - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed. Always caught inside `transform`, never leaked.

    Mirrors `waddle_sdk.db`'s documented pattern ("Err is structurally classified, never
    imported") since `waddle_sdk.kv` is a thin wrapper that does not catch the generated `Err`.
    """


class _InvalidUrl(Exception):
    """Internal-only: the supplied invite URL was rejected; `str(exc)` is the user-facing reason.

    Raised by `_validate_url` and caught in `transform`, which logs it (exception type only,
    never the URL) and replies -- so a rejection is an explicit signal, never a swallowed default.
    """


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure(
            f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- the link persists indefinitely), reclassifying `Err`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED unless a strict `bool` `True` badge is present.

    Anything that is not exactly `True` (missing key, `None`, `"true"`, `1`) is a refusal.
    """
    return (
        event.payload.get("is_mod") is True
        or event.payload.get("is_broadcaster") is True
    )


def _validate_url(raw: str) -> None:
    """Raise `_InvalidUrl` (user-facing message) unless `raw` is an acceptable invite URL."""
    if len(raw) > MAX_URL_LEN:
        raise _InvalidUrl(f"That link is too long (max {MAX_URL_LEN} characters).")
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise _InvalidUrl("The invite link must be a single URL with no spaces.")
    try:
        parts = urlsplit(raw)
        # `.port` validates the port lazily and raises ValueError on a malformed one.
        _ = parts.port
    except ValueError as exc:
        raise _InvalidUrl("That doesn't look like a valid URL.") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise _InvalidUrl("The invite link must start with https:// (or http://).")
    if not parts.hostname:
        raise _InvalidUrl(
            "The invite link must include a host, e.g. https://discord.gg/abc123"
        )
    if "@" in parts.netloc:
        raise _InvalidUrl("The invite link must not contain embedded credentials.")


def _looks_like_discord(raw: str) -> bool:
    """True if the URL's host is a Discord domain (or a subdomain of one)."""
    host = (urlsplit(raw).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _DISCORD_HOSTS)


async def _handle_read() -> str:
    """Reply with the stored invite, or a friendly 'not set' message."""
    raw = await _kv_get(INVITE_KEY)
    if raw is None:
        return "No Discord link has been set yet. A moderator can set one with !discord set <url>"
    try:
        url = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailure(f"corrupt discord invite: {exc}") from exc
    if not url:
        raise _KvFailure("corrupt discord invite: empty value")
    return f"Join our Discord: {url}"


async def _handle_set(args: str | None, event: PlatformEvent) -> str:
    """Validate and store a new invite URL (mod/broadcaster only)."""
    if not _is_privileged(event):
        log.info("discord.permission_denied", op="set", platform=event.platform)
        return _NOT_PERMITTED
    if args is None:
        return "Usage: !discord set <url>"
    _validate_url(args)
    await _kv_set(INVITE_KEY, args.encode("utf-8"))
    log.info("discord.invite_set", platform=event.platform)
    if _looks_like_discord(args):
        return "Discord link updated."
    return (
        "Discord link updated. (Heads up: that doesn't look like a Discord link - "
        "double-check it.)"
    )


async def _handle_reset(args: str | None, event: PlatformEvent) -> str:
    """Clear the stored invite (mod/broadcaster only)."""
    if not _is_privileged(event):
        log.info("discord.permission_denied", op="reset", platform=event.platform)
        return _NOT_PERMITTED
    if args is not None:
        return "Usage: !discord reset"
    await _kv_delete(INVITE_KEY)
    log.info("discord.invite_reset", platform=event.platform)
    return "Discord link cleared."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!discord ...` and build the reply.

    Returns `None` for a non-chat payload, any message whose first token isn't `!discord`,
    or while `waddles.command-discord` is off. A `kv` failure logs loudly
    (`discord.kv_failure`, exception type only) and still replies -- never silent.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    head = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if head != COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(stripped, _SPEC)
        if parsed.option is None:
            reply = await _handle_read()
        elif parsed.option == "set":
            reply = await _handle_set(parsed.args, event)
        elif parsed.option == "reset":
            reply = await _handle_reset(parsed.args, event)
        else:
            reply = _USAGE
    except CommandUsageError as exc:
        reply = f"{exc}. {_USAGE}"
    except _InvalidUrl as exc:
        cause = exc.__cause__ or exc
        log.info(
            "discord.invalid_url",
            error_type=type(cause).__name__,
            platform=event.platform,
        )
        reply = str(exc)
    except _KvFailure as exc:
        cause = exc.__cause__ or exc
        log.error(
            "discord.kv_failure",
            error_type=type(cause).__name__,
            platform=event.platform,
        )
        reply = _KV_ERROR

    log.info("discord.transform matched", platform=event.platform)
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
        ValueError: The envelope's payload is missing `channel_id` or `text` (defensive --
            `transform` always sets both when it returns a non-`None` event).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "discord reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("discord reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("discord.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
