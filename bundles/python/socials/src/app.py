"""`!socials` -> the community's configured social links, KV-backed (v2 port).

Commands: `!socials` / `!socials list` (anyone) lists the links; `!socials set <platform> <url>`
and `!socials remove <platform>` (mod/broadcaster only, fail CLOSED when role info is absent).
Structure mirrors `bundles/python/bookmark`: `transform` does all `kv` work, `dispatch` is a pure
relay of the reply text `transform` built.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Key
`socials.links` (`.`-separated, never `:`, gh-631) holds one JSON object `platform -> url`,
capped at `MAX_LINKS` entries so the blob stays far below the host's 64 KiB value limit.
Platform names are lowercase `[a-z0-9_-]`; URLs are http/https only (no credentials).

Logs are PII-free: op, counts, exception-type names -- never a platform name or URL. A corrupt
stored blob raises `_KvFailure` (logged at ERROR, answered with an error reply), never a
silent default. Gated behind the PostHog flag ``waddles.command-socials``.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urlsplit

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-socials"
COMMAND = "!socials"

LINKS_KEY = "socials.links"
MAX_LINKS = 20
MAX_URL_LEN = 300
MAX_PLATFORM_LEN = 20
_REPLY_BUDGET = 400
_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_USAGE = "Usage: !socials | !socials set <platform> <url> | !socials remove <platform>"
_UNAVAILABLE_MSG = "Something went wrong accessing socials storage - please try again."


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
        log.debug("socials.role_info_unavailable", platform=event.platform)
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
        log.debug("socials.invalid_url", error=type(exc).__name__)
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


async def _load() -> dict[str, str]:
    """Return the platform -> url map (`{}` if unset); corrupt data raises `_KvFailure`."""
    raw = await _kv_get(LINKS_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return {str(k): str(v) for k, v in dict(data).items()}
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _KvFailure("corrupt_links", type(exc).__name__) from exc


async def _save(links: dict[str, str]) -> None:
    """Persist the full link map."""
    await _kv_set(LINKS_KEY, json.dumps(links, sort_keys=True).encode("utf-8"))


def _render(links: dict[str, str]) -> str:
    """Format the links within the chat reply budget."""
    parts: list[str] = []
    used = 0
    names = sorted(links)
    for shown, name in enumerate(names):
        piece = f"{name}: {links[name]}"
        if used + len(piece) + 3 > _REPLY_BUDGET and shown > 0:
            parts.append(f"... and {len(names) - shown} more")
            break
        parts.append(piece)
        used += len(piece) + 3
    return " | ".join(parts)


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!socials`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()

    if verb in ("", "list"):
        links = await _load()
        log.info("socials.list", count=len(links))
        return "No social links configured yet." if not links else _render(links)

    if verb == "set":
        if not _is_privileged(event):
            log.info("socials.permission_denied", op="set")
            return "Only the broadcaster or a moderator can change social links."
        platform, _, url = arg.partition(" ")
        platform = platform.lower()
        url = url.strip()
        if not platform or not url:
            return "Usage: !socials set <platform> <url>"
        if len(platform) > MAX_PLATFORM_LEN or not _PLATFORM_RE.match(platform):
            return f"Platform must be 1-{MAX_PLATFORM_LEN} chars of a-z, 0-9, _ or -."
        error = validate_url(url)
        if error:
            return f"Can't set: {error}."
        links = await _load()
        if platform not in links and len(links) >= MAX_LINKS:
            return f"The social list is full ({MAX_LINKS}); remove one first."
        links[platform] = url
        await _save(links)
        log.info("socials.set", count=len(links))
        return f"Saved the {platform} link."

    if verb == "remove":
        if not _is_privileged(event):
            log.info("socials.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can change social links."
        platform = arg.lower()
        if not platform:
            return "Usage: !socials remove <platform>"
        links = await _load()
        if platform not in links:
            return f"No {platform} link is set."
        del links[platform]
        await _save(links)
        log.info("socials.removed", count=len(links))
        return f"Removed the {platform} link."

    return f"Unknown !socials subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!socials` and build the reply.

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
        log.error("socials.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("socials.transform matched")
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
        raise ValueError("socials reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("socials reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("socials.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
