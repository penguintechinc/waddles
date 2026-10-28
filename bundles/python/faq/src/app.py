"""`!faq <key>` -> a stored Q&A answer, KV-backed (v2 port).

Commands: `!faq <key>` and `!faq list` (anyone); `!faq set <key> <answer>` and
`!faq remove <key>` (mod/broadcaster only, fail CLOSED when role info is absent). Structure
mirrors `bundles/python/bookmark`/`quote`: `transform` does all `kv` work, `dispatch` relays.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Key
`faq.entries` (`.`-separated, never `:`, gh-631) holds one JSON object `key -> answer`, capped
at `MAX_ENTRIES` entries of at most `MAX_ANSWER_LEN` chars (well under the host's 64 KiB value
limit). Keys are lowercase `[a-z0-9_-]` and may not collide with the verbs `set`/`remove`/
`list`. Answers may not start with `!` or `/` (so the bot can't be made to trigger another
command), and may not contain control characters.

Logs are PII-free: op, counts, exception-type names -- never a key or answer. A corrupt stored
blob raises `_KvFailure` (ERROR log + error reply), never a silent default. Gated behind the
PostHog flag ``waddles.command-faq``.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-faq"
COMMAND = "!faq"

ENTRIES_KEY = "faq.entries"
MAX_ENTRIES = 100
MAX_KEY_LEN = 32
MAX_ANSWER_LEN = 300
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RESERVED = frozenset({"set", "remove", "list"})
_REPLY_BUDGET = 400

_USAGE = "Usage: !faq <key> | !faq list | !faq set <key> <answer> | !faq remove <key>"
_UNAVAILABLE_MSG = "Something went wrong accessing faq storage - please try again."


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
        log.debug("faq.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


async def _load() -> dict[str, str]:
    """Return the key -> answer map (`{}` if unset); corrupt data raises `_KvFailure`."""
    raw = await _kv_get(ENTRIES_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return {str(k): str(v) for k, v in dict(data).items()}
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _KvFailure("corrupt_entries", type(exc).__name__) from exc


async def _save(entries: dict[str, str]) -> None:
    """Persist the full FAQ map."""
    await _kv_set(ENTRIES_KEY, json.dumps(entries, sort_keys=True).encode("utf-8"))


def _validate_key(key: str) -> str | None:
    """Return an error message, or `None` when `key` is an acceptable FAQ key."""
    if not key:
        return "a key is required"
    if key in _RESERVED:
        return "that key is reserved"
    if len(key) > MAX_KEY_LEN or not _KEY_RE.match(key):
        return f"keys must be 1-{MAX_KEY_LEN} chars of a-z, 0-9, _ or -"
    return None


def validate_answer(answer: str) -> str | None:
    """Return an error message, or `None` when `answer` is an acceptable stored answer."""
    if not answer:
        return "an answer is required"
    if len(answer) > MAX_ANSWER_LEN:
        return f"answers must be {MAX_ANSWER_LEN} characters or fewer"
    if any(ord(ch) < 32 for ch in answer):
        return "answers can't contain control characters"
    if answer[0] in "!/":
        return "answers can't start with ! or /"
    return None


def _render_keys(entries: dict[str, str]) -> str:
    """List the keys within the chat reply budget."""
    parts: list[str] = []
    used = 0
    names = sorted(entries)
    for shown, name in enumerate(names):
        if used + len(name) + 2 > _REPLY_BUDGET and shown > 0:
            parts.append(f"... and {len(names) - shown} more")
            break
        parts.append(name)
        used += len(name) + 2
    return f"FAQ topics ({len(names)}): " + ", ".join(parts)


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!faq`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if not verb:
        return _USAGE

    if verb == "list":
        entries = await _load()
        log.info("faq.list", count=len(entries))
        return "No FAQ entries yet." if not entries else _render_keys(entries)

    if verb == "set":
        if not _is_privileged(event):
            log.info("faq.permission_denied", op="set")
            return "Only the broadcaster or a moderator can change the FAQ."
        key, _, answer = arg.partition(" ")
        key = key.lower()
        answer = answer.strip()
        key_error = _validate_key(key)
        if key_error:
            return f"Can't set: {key_error}. Usage: !faq set <key> <answer>"
        answer_error = validate_answer(answer)
        if answer_error:
            return f"Can't set: {answer_error}."
        entries = await _load()
        if key not in entries and len(entries) >= MAX_ENTRIES:
            return f"The FAQ is full ({MAX_ENTRIES}); remove an entry first."
        entries[key] = answer
        await _save(entries)
        log.info("faq.set", count=len(entries))
        return f"Saved FAQ entry '{key}'."

    if verb == "remove":
        if not _is_privileged(event):
            log.info("faq.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can change the FAQ."
        key = arg.lower()
        if not key:
            return "Usage: !faq remove <key>"
        entries = await _load()
        if key not in entries:
            return f"No FAQ entry '{key}'."
        del entries[key]
        await _save(entries)
        log.info("faq.removed", count=len(entries))
        return f"Removed FAQ entry '{key}'."

    entries = await _load()
    found = entries.get(verb)
    log.info("faq.lookup", found=found is not None)
    if found is None:
        return "No such FAQ entry. Try !faq list."
    return found


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!faq` and build the reply.

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
        log.error("faq.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("faq.transform matched")
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
        raise ValueError("faq reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("faq reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("faq.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
