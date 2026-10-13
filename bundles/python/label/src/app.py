"""`!label` -> per-community user labels keyed by user UUID, KV-backed (v2 port).

Commands: `!label add <user> <label>` and `!label remove <user> <label>` (mod/broadcaster
only), `!label list [<user>]` (anyone; defaults to the caller). Structure mirrors
`bundles/python/count`: `transform` does all `kv` work, `dispatch` is a pure relay.

**Users are referenced by UUID, never by username (PII rule).** `_resolve_target()` accepts, in
order: a literal UUID (what a UUID-aware client/hub sends); a Discord mention `<@id>`/`<@!id>`
(UUIDv5 of the platform id); or, for platforms that only expose a handle (Twitch), `@name` /
`name` -- hashed straight into a UUIDv5 and DISCARDED. Only the resulting UUID is ever stored
as a key, logged, or echoed (replies show just the UUID's first 8 hex chars, never the typed
target). `_actor_uuid()` uses the identical derivation for the caller, so `!label list` and a
mention of oneself agree on Discord. Until the identity-tokenization pipeline (#429) supplies
real `hub_users` UUIDs, a handle-derived UUID is a non-reversible pseudonym, not a hub id; a
Discord `@name` typed as plain text derives differently from the same user's id-based mention.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Keys are
`.`-separated (never `:`, gh-631): `label.user.<uuid>` -> JSON array of labels. Bounds:
`MAX_LABELS_PER_USER` labels each, `MAX_LABEL_LEN` chars, charset `[a-z0-9 _-]`.

Logs are PII-free: op, counts, exception-type names -- never a target, handle or label text.
Gated behind the PostHog flag ``waddles.command-label``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-label"
COMMAND = "!label"

USER_PREFIX = "label.user."

MAX_LABELS_PER_USER = 10
MAX_LABEL_LEN = 32
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]*$")
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_ACTOR_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity"
)

_USAGE = "Usage: !label add <user> <label> | !label remove <user> <label> | !label list [<user>]"
_UNAVAILABLE_MSG = "Something went wrong accessing label storage - please try again."
_BAD_TARGET_MSG = (
    "I couldn't identify that user - mention them (e.g. @name) or give their UUID."
)


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
        raise _KvFailure(
            "kv_delete", type(getattr(exc, "value", exc)).__name__
        ) from exc


async def _load_labels(user_uuid: str) -> list[str]:
    """Return a user's labels (`[]` if none); corrupt stored data raises `_KvFailure`."""
    raw = await _kv_get(f"{USER_PREFIX}{user_uuid}")
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise TypeError("expected a JSON array of strings")
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _KvFailure("corrupt_labels", type(exc).__name__) from exc
    return cast("list[str]", data)


async def _save_labels(user_uuid: str, labels: list[str]) -> None:
    """Persist a user's labels, deleting the key when none remain."""
    key = f"{USER_PREFIX}{user_uuid}"
    if labels:
        await _kv_set(key, json.dumps(labels).encode("utf-8"))
    else:
        await _kv_delete(key)


def _actor_uuid(event: PlatformEvent) -> str:
    """Stable pseudonymous UUID for the event's actor (same derivation as `_resolve_target`)."""
    author_id = event.payload.get("author_id")
    if event.platform == "discord" and isinstance(author_id, str) and author_id:
        basis = f"discord:{author_id}"
    else:
        basis = f"{event.platform}:name:{(event.actor or 'anonymous').strip().lower()}"
    return str(uuid.uuid5(_ACTOR_NAMESPACE, basis))


def _resolve_target(raw: str, platform: str) -> str | None:
    """Resolve a typed target to a UUID string, or `None` if it isn't a recognisable user.

    The raw text is hashed and dropped here -- callers only ever see the UUID.
    """
    token = raw.strip()
    try:
        return str(uuid.UUID(token))
    except ValueError:
        log.debug("label.target_not_uuid", platform=platform, error="ValueError")
    mention = _DISCORD_MENTION_RE.match(token)
    if mention:
        return str(uuid.uuid5(_ACTOR_NAMESPACE, f"discord:{mention.group(1)}"))
    handle = token.removeprefix("@")
    if _HANDLE_RE.match(handle):
        return str(uuid.uuid5(_ACTOR_NAMESPACE, f"{platform}:name:{handle.lower()}"))
    return None


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("label.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


def validate_label(raw: str) -> tuple[str | None, str | None]:
    """Normalise a label; returns `(label, None)` or `(None, error_message)`."""
    label = " ".join(raw.strip().lower().split())
    if not label:
        return None, "a label is required"
    if len(label) > MAX_LABEL_LEN:
        return None, f"labels must be {MAX_LABEL_LEN} characters or fewer"
    if not _LABEL_RE.match(label):
        return None, "labels may only contain letters, digits, spaces, '_' and '-'"
    return label, None


def _short(user_uuid: str) -> str:
    """First 8 hex chars of a UUID -- the only user reference ever shown in a reply."""
    return user_uuid[:8]


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!label`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if not verb:
        return _USAGE

    if verb == "list":
        if arg:
            target = _resolve_target(arg.split()[0], event.platform)
            if target is None:
                return _BAD_TARGET_MSG
        else:
            target = _actor_uuid(event)
        labels = await _load_labels(target)
        log.info("label.list", count=len(labels))
        if not labels:
            return f"User {_short(target)} has no labels."
        return f"User {_short(target)} labels: " + ", ".join(labels)

    if verb in ("add", "remove"):
        if not _is_privileged(event):
            log.info("label.permission_denied", op=verb)
            return "Only the broadcaster or a moderator can change labels."
        target_raw, _, label_raw = arg.partition(" ")
        if not target_raw:
            return f"Usage: !label {verb} <user> <label>"
        target = _resolve_target(target_raw, event.platform)
        if target is None:
            return _BAD_TARGET_MSG
        label, error = validate_label(label_raw)
        if label is None:
            return f"Can't {verb} label: {error}."
        labels = await _load_labels(target)
        if verb == "add":
            if label in labels:
                return f"User {_short(target)} already has that label."
            if len(labels) >= MAX_LABELS_PER_USER:
                return f"A user can have at most {MAX_LABELS_PER_USER} labels."
            labels.append(label)
            await _save_labels(target, labels)
            log.info("label.added", count=len(labels))
            return f"Added label to user {_short(target)}."
        if label not in labels:
            return f"User {_short(target)} doesn't have that label."
        labels.remove(label)
        await _save_labels(target, labels)
        log.info("label.removed", count=len(labels))
        return f"Removed label from user {_short(target)}."

    return f"Unknown !label subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!label` and build the reply.

    Exact first-token match (so `!labels` never matches) comes before the flag check; a `kv`
    failure is logged loudly (PII-free) and answered with an error reply, never swallowed.
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
        log.error("label.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("label.transform matched")
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
            "label reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("label reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("label.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
