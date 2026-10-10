"""`!discord` -> the community's Discord invite link, KV-backed (v2 port).

Commands: `!discord` (anyone) shows the invite; `!discord set <url>` and `!discord remove`
(mod/broadcaster only, fail CLOSED when role info is absent). Structure mirrors
`bundles/python/bookmark`: `transform` does all `kv` work, `dispatch` is a pure relay.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Key
`discord.invite` (`.`-separated, never `:`, gh-631) holds the plain URL string. Only https
URLs on `discord.gg`, `discord.com` or `discordapp.com` are accepted, so a compromised or
careless mod cannot turn the command into an arbitrary-link poster.

Logs are PII-free: op and exception-type names only -- never the URL. A non-UTF-8 stored value
raises `_KvFailure` (ERROR log + error reply), never a silent default. Gated behind the
PostHog flag ``waddles.command-discord``.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-discord"
COMMAND = "!discord"

INVITE_KEY = "discord.invite"
MAX_URL_LEN = 200
INVITE_HOSTS = ("discord.gg", "discord.com", "discordapp.com")

_USAGE = "Usage: !discord | !discord set <url> | !discord remove"
_UNAVAILABLE_MSG = "Something went wrong accessing discord storage - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call or stored-data check failed; caught in `transform`."""

    def __init__(self, op: str, error: str) -> None:
        """Record the failing op and a PII-free error classification (exception type name)."""
        super().__init__(f"{op}: {error}")
        self.op = op
        self.error = error


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


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying a host `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvFailure("kv_delete", type(getattr(exc, "value", exc)).__name__) from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("discord.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def validate_url(raw: str, *, hosts: tuple[str, ...] = ()) -> str | None:
    """Return an error message, or `None` when `raw` is an acceptable http(s) URL.

    When `hosts` is non-empty the URL's host must equal, or be a subdomain of, one of them.
    """
    if not raw:
        return "a URL is required"
    if len(raw) > MAX_URL_LEN:
        return f"URLs must be {MAX_URL_LEN} characters or fewer"
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return "provide a single URL with no spaces"
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        log.debug("discord.invalid_url", error=type(exc).__name__)
        return "that doesn't look like a valid URL"
    if parts.scheme not in ("http", "https"):
        return "only http:// and https:// URLs are allowed"
    host = (parts.hostname or "").lower()
    if "." not in host or host.startswith(".") or host.endswith("."):
        return "that URL needs a valid host name"
    if parts.username is not None or parts.password is not None:
        return "URLs with embedded credentials aren't allowed"
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        return "that URL isn't on an allowed host (" + ", ".join(hosts) + ")"
    return None


async def _load() -> str | None:
    """Return the stored invite URL, or `None` if unset; undecodable data raises `_KvFailure`."""
    raw = await _kv_get(INVITE_KEY)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailure("corrupt_invite", type(exc).__name__) from exc


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!discord`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()

    if verb == "":
        invite = await _load()
        log.info("discord.show", configured=invite is not None)
        if invite is None:
            return "No Discord invite is configured yet."
        return f"Join our Discord: {invite}"

    if verb == "set":
        if not _is_privileged(event):
            log.info("discord.permission_denied", op="set")
            return "Only the broadcaster or a moderator can change the Discord invite."
        if not arg:
            return "Usage: !discord set <url>"
        error = validate_url(arg, hosts=INVITE_HOSTS)
        if error:
            return f"Can't set: {error}."
        await _kv_set(INVITE_KEY, arg.encode("utf-8"))
        log.info("discord.set")
        return "Saved the Discord invite."

    if verb == "remove":
        if not _is_privileged(event):
            log.info("discord.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can change the Discord invite."
        await _kv_delete(INVITE_KEY)
        log.info("discord.removed")
        return "Removed the Discord invite."

    return f"Unknown !discord subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!discord` and build the reply.

    Cheap-skip (exact first-token match) comes before the flag check; a `kv` failure is logged
    loudly (PII-free) and answered with an error reply, never swallowed into a default.
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
        log.error("discord.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("discord.transform matched")
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
        raise ValueError("discord reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("discord reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("discord.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
