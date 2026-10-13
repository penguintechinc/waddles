"""`!pronouns` -> per-user pronouns: set your own, read your own, read someone else's, clear.

Commands (all replies are explicit -- a recognized-but-malformed command is never dropped):

- ``!pronouns`` -- read the CALLER's own pronouns (or a "you haven't set pronouns" hint).
- ``!pronouns set <text>`` -- the caller sets their OWN pronouns. **Self-only, deliberately NOT
  mod-gated**: it is the user's own profile, so this bundle never reads `is_mod` /
  `is_broadcaster` and there is no way to set anyone else's pronouns.
- ``!pronouns @user`` / ``!pronouns <@id>`` -- read another user's pronouns (public read).
- ``!pronouns clear`` (also ``remove``/``delete``/``reset``, the shared verb vocabulary) -- the
  caller removes their own.

Validation of ``<text>`` (`_validate_pronouns`): non-empty after trimming, at most
`MAX_PRONOUNS_LEN` (32) characters, printable and single-line, and free of ``@``/``<``/``>``
(a stored value is echoed into other users' chat, so a mention such as ``@everyone`` must never
be storable). A rejected value gets an explicit reply stating the reason.

**Identity is hashed, never raw (PII rule).** One `kv` key per user:
``pronouns.<sha256(basis)>`` (`_pseudonym()`, same scheme as `duel`/`social`; `.`, never `:` --
gh-631). The basis is platform-scoped and is the SAME derivation for storage and lookup, so a
read resolves to exactly the key a set wrote:

* Discord: ``discord:<author_id>`` -- the caller's `payload.author_id`, or the id inside a
  ``<@id>``/``<@!id>`` mention typed as the target;
* every other platform: ``<platform>:name:<lowercased handle>`` -- the event `actor` for the
  caller, the typed ``@name``/bare name for a target (same fallback `iq` uses).

The raw handle/id is hashed and dropped; it is never stored, logged, or used in a key. **Known
limitation (documented, not hidden):** on Discord a typed ``@name`` cannot be mapped to an id
without the identity-tokenization pipeline (#429), so it gets an explicit "use a real mention"
reply -- never a hashed guess; a handle that is also a verb (``list``, ``add``, ...) cannot be
looked up directly. The host scopes `kv` per `(tenant, community, app_id)` so pronouns are
per-community; a user sets them once in each community.

Ordering inside `transform()`: cheap command match, then the PostHog flag
``waddles.command-pronouns`` (default OFF), then grammar classification, then `kv`. A `kv`
failure (or a corrupt stored value) is reclassified into a private `_KvError`, logged at ERROR
and answered with an explicit failure notice -- never a silent no-op. Logs are PII-free: only
op names, command kinds, reason codes and exception types -- never the pronouns text, the
actor, or a typed target.

`kv`, `log`, `relay` and `feature_enabled` are imported at MODULE LOAD on purpose:
componentize-py only wizens a WIT host-import that is imported eagerly, so a capability
imported only inside a function is absent from `wit_world.imports` at runtime
(`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.command import VERBS, CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (default OFF).
FLAG_KEY = "waddles.command-pronouns"

#: `kv` key prefix; the full key is `pronouns.<sha256 hex>` (colon-free, 73 bytes).
KEY_PREFIX = "pronouns."

#: Upper bound (characters) for a stored pronouns string.
MAX_PRONOUNS_LEN = 32

_SPEC = CommandSpec(name="pronouns")
_COMMAND = "!pronouns"
_USAGE = "Usage: !pronouns | !pronouns set <text> | !pronouns clear | !pronouns @user"

#: Shared-vocabulary verbs that all mean "clear my pronouns" (`clear` itself is not a SDK verb).
_CLEAR_VERBS = frozenset({"remove", "delete", "reset"})

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_PLATFORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: A plausible handle after an optional leading `@` (shape check only, not a roster lookup).
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
#: Characters that would let a stored value ping/format other users when echoed back.
_FORBIDDEN_CHARS = frozenset("@<>")

_NO_SELF_ID = "I couldn't identify you on this platform, so I can't manage your pronouns."
_BAD_TARGET = "I couldn't identify that user - mention them (e.g. @name)."
_DISCORD_NEEDS_MENTION = (
    "On Discord I can only look users up by a real @mention - names can't be resolved yet."
)
_KV_FAILURE_REPLY = "Something went wrong with pronoun storage - please try again."

#: Command kinds `_classify` can produce.
_READ_SELF = "read_self"
_READ_OTHER = "read_other"
_SET = "set"
_CLEAR = "clear"
_USAGE_KIND = "usage"


class _KvError(Exception):
    """Internal-only: a `kv` host call failed or a stored value is corrupt.

    Always caught inside `transform`, never leaked. Mirrors `waddle_sdk.db`'s documented
    pattern ("Err is structurally classified, never imported"): `waddle_sdk.kv` does not
    classify the generated WIT `Err` itself.
    """

    def __init__(self, op: str) -> None:
        """Record which operation failed (`get`/`set`/`delete`/`decode`) -- never a key or value."""
        super().__init__(f"pronouns kv {op} failed")
        self.op = op


class _UserError(Exception):
    """Internal-only: a user-correctable problem answered with an explicit reply.

    `str(exc)` is the static, ready-to-send reply (it never embeds user-supplied text);
    `code` is the PII-free reason logged at DEBUG.
    """

    def __init__(self, code: str, reply: str) -> None:
        """Record the logged reason code and the reply text sent to chat."""
        super().__init__(reply)
        self.code = code


@dataclass(slots=True, frozen=True)
class _Command:
    """One classified `!pronouns ...` invocation (`arg` is raw, unvalidated text)."""

    kind: str
    arg: str | None = None


@dataclass(slots=True, frozen=True)
class _Target:
    """A resolved read target: the hash `basis` plus a chat-safe display name (if any)."""

    basis: str
    display: str | None


def _pseudonym(basis: str) -> str:
    """Non-reversible per-user key component -- see `duel`'s own `_pseudonym()` for why.

    `basis` is a platform-scoped identity string (module docstring); hashing it before it
    ever reaches `kv` keeps the raw handle/id out of storage entirely.
    """
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _key(basis: str) -> str:
    """The `kv` key one user's pronouns live under: `pronouns.<sha256>`."""
    return f"{KEY_PREFIX}{_pseudonym(basis)}"


def _caller_basis(event: PlatformEvent) -> str:
    """Hash basis for the event's own author.

    Raises:
        _UserError: The event carries neither a usable Discord author id nor an actor name.
    """
    author_id = event.payload.get("author_id")
    if (
        event.platform == "discord"
        and isinstance(author_id, str)
        and _PLATFORM_ID_RE.match(author_id)
    ):
        return f"discord:{author_id}"
    actor = event.actor
    if isinstance(actor, str) and actor.strip():
        return f"{event.platform}:name:{actor.strip().lower()}"
    raise _UserError("no_self_identity", _NO_SELF_ID)


def _resolve_target(raw: str, platform: str) -> _Target:
    """Resolve a typed target to the SAME basis scheme storage uses.

    Discord mention (``<@id>``/``<@!id>``, Discord only) -> ``discord:<id>``; a handle
    (optional ``@`` stripped) on a non-Discord platform -> ``<platform>:name:<lowercase>``.

    Raises:
        _UserError: A Discord typed name (unresolvable without #429) or an unrecognizable
            token -- an explicit reply, never a guessed key.
    """
    token = raw.strip()
    mention = _DISCORD_MENTION_RE.match(token)
    if mention is not None:
        if platform != "discord":
            raise _UserError("mention_off_discord", _BAD_TARGET)
        return _Target(basis=f"discord:{mention.group(1)}", display=None)
    handle = token[1:] if token.startswith("@") else token
    if not _HANDLE_RE.match(handle):
        raise _UserError("bad_target_shape", _BAD_TARGET)
    if platform == "discord":
        raise _UserError("discord_name_unresolvable", _DISCORD_NEEDS_MENTION)
    return _Target(basis=f"{platform}:name:{handle.lower()}", display=handle)


def _validate_pronouns(raw: str) -> str:
    """Return the trimmed pronouns text, or raise `_UserError` stating why it was rejected.

    The reply never echoes the rejected text (it may be arbitrarily long or hostile).
    """
    text = raw.strip()
    if not text:
        raise _UserError("empty", "pronouns can't be empty - try !pronouns set they/them")
    if len(text) > MAX_PRONOUNS_LEN:
        raise _UserError("too_long", f"pronouns must be at most {MAX_PRONOUNS_LEN} characters long")
    if not text.isprintable():
        raise _UserError("not_printable", "pronouns must be a single line of printable characters")
    if any(ch in _FORBIDDEN_CHARS for ch in text):
        raise _UserError("forbidden_chars", "pronouns can't contain @, < or >")
    return text


def _classify(text: str) -> _Command:
    """Classify a full `!pronouns ...` message into this bundle's own command set.

    Structure only -- identity resolution and validation happen in `_handle`. `clear` is not
    an SDK grammar verb so it is matched first; any other non-verb single token is a lookup
    target (same split `duel` uses), anything else is a usage reply.
    """
    stripped = text.strip()
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return _Command(_READ_SELF)

    tok1, _, tail = rest.partition(" ")
    tail = tail.strip()
    if tok1.lower() == "clear":
        return _Command(_CLEAR) if not tail else _Command(_USAGE_KIND)

    try:
        parsed = parse_command(stripped, _SPEC)
    except CommandUsageError:
        # Not a grammar verb: a single bare token is a read target; two or more tokens
        # ("!pronouns foo bar") fit neither grammar. Static event only -- the token is never logged.
        log.debug("pronouns.classify_non_verb")
        if tail or tok1.lower() in VERBS:
            # A reserved verb the grammar rejected (e.g. a bare `enable`) is a usage error,
            # not a lookup of a user who happens to share its name (module docstring).
            return _Command(_USAGE_KIND)
        return _Command(_READ_OTHER, tok1)

    if parsed.option == "set":
        return _Command(_SET, parsed.args) if parsed.args else _Command(_USAGE_KIND)
    if parsed.option in _CLEAR_VERBS and parsed.args is None:
        return _Command(_CLEAR)
    # list/add/sub/enable/disable (or a clear alias with trailing args): nothing declared.
    return _Command(_USAGE_KIND)


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvError("get") from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- pronouns persist until cleared), reclassifying `Err`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvError("set") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvError`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _KvError("delete") from exc


async def _read_pronouns(basis: str) -> str | None:
    """Return one user's stored pronouns, or `None` if unset.

    Raises:
        _KvError: `kv.get` failed, or the stored bytes are not valid UTF-8 (corrupt, fail loud).
    """
    raw = await _kv_get(_key(basis))
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvError("decode") from exc


async def _handle(event: PlatformEvent, command: _Command) -> str:
    """Execute one classified command and return the reply text.

    Raises:
        _UserError: A user-correctable problem (bad text/target, no identity).
        _KvError: A `kv` round trip failed or a stored value is corrupt.
    """
    if command.kind == _USAGE_KIND:
        return _USAGE

    if command.kind == _READ_OTHER:
        target = _resolve_target(command.arg or "", event.platform)
        found = await _read_pronouns(target.basis)
        who = f"{target.display}'s" if target.display else "that user's"
        if found is None:
            return f"{target.display or 'that user'} hasn't set pronouns."
        return f"{who} pronouns: {found}"

    basis = _caller_basis(event)

    if command.kind == _READ_SELF:
        found = await _read_pronouns(basis)
        if found is None:
            return "You haven't set pronouns - use !pronouns set <text>."
        return f"Your pronouns: {found}"

    if command.kind == _SET:
        text = _validate_pronouns(command.arg or "")
        await _kv_set(_key(basis), text.encode("utf-8"))
        return f"Your pronouns are now: {text}"

    # clear
    if await _kv_get(_key(basis)) is None:
        return "You haven't set pronouns, so there's nothing to clear."
    await _kv_delete(_key(basis))
    return "Your pronouns have been cleared."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!pronouns`, run it, build the reply.

    Returns `None` for a non-chat payload, any message that is not `!pronouns`, and while
    `waddles.command-pronouns` is off. A user error replies with its static message; a `kv`
    failure logs at ERROR and replies with an explicit failure notice.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != _COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command = _classify(text)
    try:
        reply = await _handle(event, command)
    except _UserError as exc:
        log.debug("pronouns.user_error", command=command.kind, reason=exc.code)
        reply = str(exc)
    except _KvError as exc:
        log.error(
            "pronouns.kv_failure",
            command=command.kind,
            op=exc.op,
            error_type=type(exc.__cause__).__name__,
        )
        reply = _KV_FAILURE_REPLY

    log.info("pronouns.transform matched", command=command.kind)
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
        raise ValueError("pronouns reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("pronouns reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("pronouns.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
