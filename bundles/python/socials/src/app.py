"""`!socials` -> per-community social-media links, kv-only.

`!socials` / `!socials list` (public) reply with the community's configured links
("Follow us: twitter → <url>, youtube → <url>"); `!socials set <platform> <url>` and
`!socials remove <platform>` (moderator/broadcaster only) upsert/delete one link.

State is community-scoped `kv` (the host scopes every key by tenant/community/app_id, so keys
here are plain literals): a registry key `socials.registry` (JSON array of platform names) plus one
`socials.link.<platform>` per platform holding its URL. Keys use `.`, NEVER `:` (gh-631 -- the
host reserves `:`). Platform names are charset-validated (lowercase alnum/`_`/`-`) so they can
never form an invalid kv key.

Mod gate FAILS CLOSED: `_is_privileged()` needs `event.payload["is_mod"]`/`["is_broadcaster"]`
as a strict `bool` `True`; missing, non-bool or false is refused with a permission reply.

URLs are checked generically (http/https, a host, no userinfo, no control chars, bounded
length) -- deliberately no platform allowlist. A bad URL gets an explicit reply, never a silent drop.

Fail loud: any `kv` Err (reclassified to `_KvFailure`), a corrupt registry/link or a registry entry
with no link logs `socials.kv_failure` at ERROR and replies "something went wrong".

PII-free logs: only the op, the platform-name and counts/exception type are logged -- never URL
values, the actor, or raw message text.

Business logic lives in `transform()` (like `count`); `dispatch()` is a pure relay.

Gated behind the PostHog flag ``waddles.command-socials`` (default OFF); cheap command-match
first, flag check second. `kv`/`log`/`relay`/`feature_flags` are imported at MODULE LOAD on
purpose -- componentize-py only wizens eagerly imported WIT host-imports (see
`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.command import (
    CommandSpec,
    CommandUsageError,
    ParsedCommand,
    parse_command,
)
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention.
FLAG_KEY = "waddles.command-socials"

COMMAND = "!socials"
SPEC = CommandSpec(name="socials")

REGISTRY_KEY = "socials.registry"
LINK_KEY_PREFIX = "socials.link."

#: Platform names: lowercase alnum/`_`/`-`, inside the host's guest-key charset.
MAX_PLATFORM_LEN = 32
_PLATFORM_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")

#: Bounds keep one read (1 + N kv gets, well under the host's 64-op cap) and one chat reply small.
MAX_URL_LEN = 300
MAX_LINKS = 20

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_USAGE = "Usage: !socials | !socials list | !socials set <platform> <url> | !socials remove <platform>"


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed or stored state is corrupt. Caught in `transform`."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:
        raise _KvFailure(
            f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- links persist), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure(
            f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvFailure(
            f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


def _link_key(platform: str) -> str:
    """The `kv` key one platform's URL lives under."""
    return f"{LINK_KEY_PREFIX}{platform}"


async def _load_registry() -> list[str]:
    """Return the community's configured platform names (`[]` if none yet).

    Raises:
        _KvFailure: kv failed, or the stored registry isn't a JSON array of strings (fail loud,
            never silently reset).
    """
    raw = await _kv_get(REGISTRY_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure(f"corrupt socials registry: {type(exc).__name__}") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt socials registry: expected a JSON array of strings")
    return cast("list[str]", data)


async def _save_registry(platforms: list[str]) -> None:
    """Persist the platform list as a sorted, deduplicated JSON array."""
    await _kv_set(REGISTRY_KEY, json.dumps(sorted(set(platforms))).encode("utf-8"))


async def _load_link(platform: str) -> str:
    """Return one platform's stored URL.

    Raises:
        _KvFailure: kv failed, the link is missing despite being registered, or isn't UTF-8.
    """
    raw = await _kv_get(_link_key(platform))
    if raw is None:
        raise _KvFailure(f"registered platform {platform!r} has no stored link")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailure(f"corrupt link for platform {platform!r}") from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- FAILS CLOSED: only a strict `bool` `True` badge passes."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("socials.role_info_unavailable", platform=event.platform)
    return is_mod is True or is_broadcaster is True


def _normalize_platform(raw: str) -> str:
    """Lowercase/trim a platform argument (`Twitter` -> `twitter`)."""
    return raw.strip().lower()


def _validate_platform(platform: str) -> str | None:
    """Return an error message, or `None` if `platform` is a safe, key-valid name."""
    if len(platform) > MAX_PLATFORM_LEN:
        return f"platform names must be {MAX_PLATFORM_LEN} characters or fewer"
    if any(ch not in _PLATFORM_ALLOWED_CHARS for ch in platform):
        return "platform names may only contain lowercase letters, digits, '_' and '-'"
    return None


def _validate_url(url: str) -> str | None:
    """Return an error message, or `None` if `url` is a well-formed http(s) URL with a host."""
    if len(url) > MAX_URL_LEN:
        return f"links must be {MAX_URL_LEN} characters or fewer"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return "that link contains control characters"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for an out-of-range / non-numeric port
    except ValueError:
        return "that doesn't look like a valid URL"
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "links must start with https:// (or http://)"
    if not host:
        return "that link has no host, e.g. https://twitter.com/me"
    if parts.username is not None or parts.password is not None:
        return "links must not contain a username or password"
    return None


async def _handle_read() -> str:
    """Reply with every configured link, sorted by platform, or a friendly empty message."""
    platforms = sorted(await _load_registry())
    if not platforms:
        return "No socials have been set yet."
    links = [f"{name} → {await _load_link(name)}" for name in platforms]
    return "Follow us: " + ", ".join(links)


async def _handle_set(args: str | None, event: PlatformEvent) -> str:
    """Handle `!socials set <platform> <url>` (mod-only, validated, upsert)."""
    if not _is_privileged(event):
        log.info("socials.permission_denied", op="set")
        return "Only the broadcaster or a moderator can set social links."
    tokens = (args or "").split()
    if len(tokens) != 2:
        return "Usage: !socials set <platform> <url>"
    platform = _normalize_platform(tokens[0])
    url = tokens[1]
    error = _validate_platform(platform) or _validate_url(url)
    if error:
        log.info("socials.invalid_input", op="set")
        return error
    platforms = await _load_registry()
    created = platform not in platforms
    if created and len(platforms) >= MAX_LINKS:
        return f"You can set at most {MAX_LINKS} social links; remove one first."
    # Link first, registry second: a failed registry write leaves an invisible orphan value,
    # never a registry entry without a link.
    await _kv_set(_link_key(platform), url.encode("utf-8"))
    if created:
        await _save_registry([*platforms, platform])
    log.info("socials.link_set", op="set", social=platform, created=created)
    return f"Set {platform} → {url}" if created else f"Updated {platform} → {url}"


async def _handle_remove(args: str | None, event: PlatformEvent) -> str:
    """Handle `!socials remove <platform>` (mod-only)."""
    if not _is_privileged(event):
        log.info("socials.permission_denied", op="remove")
        return "Only the broadcaster or a moderator can remove social links."
    tokens = (args or "").split()
    if len(tokens) != 1:
        return "Usage: !socials remove <platform>"
    platform = _normalize_platform(tokens[0])
    platforms = await _load_registry()
    if platform not in platforms:
        return f"No social link set for '{platform}'."
    # Registry first: a failed delete then leaves a harmless orphan value, never a registry
    # entry pointing at a missing link.
    await _save_registry([p for p in platforms if p != platform])
    await _kv_delete(_link_key(platform))
    log.info(
        "socials.link_removed", op="remove", social=platform, count=len(platforms) - 1
    )
    return f"Removed {platform}."


async def _route(parsed: ParsedCommand, event: PlatformEvent) -> str:
    """Map a parsed command onto this bundle's `read`/`list`/`set`/`remove` behavior."""
    option = parsed.option
    if option is None or option == "list":
        if parsed.args is not None:
            return _USAGE
        return await _handle_read()
    if option == "set":
        return await _handle_set(parsed.args, event)
    if option == "remove":
        return await _handle_remove(parsed.args, event)
    return _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!socials` and build the reply.

    Returns `None` for a non-chat payload, a message that isn't `!socials`, or while the flag is
    off. A recognized-but-malformed command always gets a usage/error reply, never silence; a `kv`
    failure logs `socials.kv_failure` loudly and still replies.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(stripped, SPEC)
        reply = await _route(parsed, event)
    except CommandUsageError as exc:
        reply = f"{exc}. {_USAGE}"
    except _KvFailure as exc:
        log.error("socials.kv_failure", cause_type=type(exc.__cause__).__name__)
        reply = "Something went wrong with the socials storage - please try again."

    log.info("socials.transform matched", platform=event.platform)
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
        raise ValueError(
            "socials reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("socials reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("socials.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
