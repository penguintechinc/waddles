r"""`!cc` -> per-community custom text commands with `$(variable)` substitution.

Moderators teach the bot new text commands without a deploy: `!cc add welcome Hi $(user)!` makes
`!welcome` reply with that text, `!cc edit` replaces it, `!cc del` removes it, `!cc list` shows what
exists (`!customcommand` is an alias of `!cc`). Idea-inspired by the custom-command features of
popular chat bots (concepts only); the code here is original.

Grammar: `!cc add|edit|del|list` runs through `waddle_sdk.command.parse_command`. The shared verb
vocabulary has no `edit` or `del`, so those two words are folded onto the shared `set` / `delete`
verbs BEFORE parsing (`remove`, `delete` and `set` work too) -- one grammar, two friendly spellings.

Variables (validated when a command is stored, so a typo is rejected up front, never rendered
literally): `$(user)` the invoker (a live mention), `$(target)` / `$(touser)` the first user
mentioned in the invocation (else its first word, else the invoker), `$(count)` how many times the
command has run (a per-command counter in kv, incremented on every run), `$(random <min> <max>)`
a whole number in [min, max] (stdlib `random`, as `eightball`/`dice`; |n| <= 1e9). At most
`MAX_VARIABLES` per response. The substituted values are never re-scanned, so an invoker cannot
smuggle a variable (`!welcome $(count)`) into the output.

**Output safety -- the stored response and every substituted value are untrusted text.** The reply
is assembled and then passed through `waddle_sdk.safe_text.sanitize_user_text` exactly once, LAST:

1. every plain piece (each literal run of the stored response, a plain-text actor handle, a plain
   target word) is first run through `escape_egress_tokens` ON ITS OWN, so each piece holds only
   balanced `\{ \} \\` pairs -- a stray backslash at the end of one piece can never swallow the
   brace of the live mention placeholder glued after it, and a `{user:<tok>}` the mod stored can
   never fire;
2. the only unescaped placeholders are the ones this bundle inserts itself: the host-minted actor
   token (anchored `^\{user:<tok>\}$` match on `event.actor`) for `$(user)`, and the first mention
   token `identity.mention_tokens` finds in the RAW delivered invocation text for `$(target)`;
   exactly those tokens become the `allow_tokens` of the final pass (nothing derived from stored
   text, ever);
3. the final `sanitize_user_text(assembled, allow_tokens=...)` then neutralises `@everyone` /
   `@here` / `<@id>` / `<#id>` / `<!here>` / `@name`, strips control characters, and defangs any
   mention a substituted display name could complete across a placeholder boundary;
4. the sanitized reply is run through a leading-command guard (a reply whose first visible
   character is `/`, `.` or `!` would run as the bot's own command on Twitch/IRC or re-trigger
   other bots, so it is given a visible prefix). The same three leads are also refused when a
   response is stored; the guard covers a lead produced by a substituted value (`$(target)` as the
   first word with `/ban` typed as the argument).

Storing is stricter than rendering: a response may not hold a live mention token (the host swaps a
typed `@name` for a short-lived `{user:<tok>}`, which is meaningless later -- use `$(user)` /
`$(target)`), control characters, more than `MAX_RESPONSE_LEN` characters or an unsupported
variable. Names are lowercase `[a-z0-9_-]` (<= 32, first character alphanumeric) and may not
shadow an existing bundle command (`RESERVED_NAMES`; a repo test fails when a bundle command is
missing from it). At most `MAX_COMMANDS` per community.

State (kv only; the host scopes every key by tenant/community/app_id, so communities never see
each other's commands): `cc.names` (JSON list, drives `list` and the cap), `cc.cmd.<name>` (the
response), `cc.uses.<name>` (atomic run counter), `cc.lock` (a 5 s claim taken with
`kv.increment`, the only compare-and-swap, so two moderators editing at once cannot lose a names
update). Keys use `.`, never `:` (gh-631). Running a command costs 2 kv ops (get + increment); an
unknown `!token` costs 1 get (command_prefix is not an enforced routing filter, so every `!`
message reaches `transform`); text with no leading `!` costs 0.

Permission model: `add`/`edit`/`del` are broadcaster/moderator only and FAIL CLOSED -- strictly
`is_mod is True or is_broadcaster is True` (a string `"false"` badge is not a moderator; neither
badge present, as on Discord today, is a denial). `list` and running a command are open to anyone.

PII-free: no actor identity is read into state or logs. `event.actor` is used transiently to
render `$(user)` and is never stored; kv keys hold only mod-chosen command names; logs carry only
the operation and counts (`op`, `count`) -- never a command name, response, argument or actor.
Failures are never swallowed: a kv failure logs `customcommand.kv_failure` (exception class name
only) and still replies with an explicit error.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from waddle_sdk import identity, kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import escape_egress_tokens, sanitize_user_text

SPEC = CommandSpec(name="cc")

#: Both spellings of the management command; `!customcommand` is folded onto `!cc` for parsing.
MANAGEMENT_COMMANDS = frozenset({"!cc", "!customcommand"})

NAMES_KEY = "cc.names"
CMD_KEY_PREFIX = "cc.cmd."
USES_KEY_PREFIX = "cc.uses."
LOCK_KEY = "cc.lock"
#: A mutation claims `LOCK_KEY` for this long at most (it is released as soon as the work ends).
LOCK_TTL_SECONDS = 5

MAX_NAME_LEN = 32
MAX_RESPONSE_LEN = 300
MAX_COMMANDS = 100
MAX_VARIABLES = 5
MAX_RANDOM_ABS = 1_000_000_000
#: Cap on a plain (non-mention) handle/word substituted for `$(user)` / `$(target)`.
MAX_PLAIN_VALUE_LEN = 32
#: `!cc list` stops adding names once the reply would pass this (chat messages are short).
MAX_LIST_REPLY_LEN = 380

_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_NAME_FIRST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

#: Verbs this bundle accepts that the shared `parse_command` vocabulary spells differently.
_VERB_ALIASES = {"edit": "set", "del": "delete"}

#: Every command an existing bundle answers to (bundle directory names plus their
#: `command_prefix` filters, including bundles still on unmerged feature branches) and this bundle's own
#: words. A custom command named like one of them would reply on top of the real command.
RESERVED_NAMES: frozenset[str] = frozenset(
    """
    8ball about advice affirmation aiso alias announce ask bingo
    birthday bookmark boop bot catfact cc channels chat chat-history
    choose clap coinflip command commands compliment count countdown counter
    cringe csping customcommand dadjoke dailycounter dance death define defuse
    dice discord dogfact donate duel echo eightball eli5 emojify
    enter event example fact faq first fish flip forget
    fortune gamble giveaway greeter guess hangman heist hello help
    hey hi highfive horoscope hug hype insult inv inventory
    iq jackpot joke keywordgiveaway kiss label lastseen lfg love
    loyalty lurk memories merch mock mood motivate music numberfact
    pat pickup ping points poke poll predict prediction pronouns
    pyping queue quote quotes raffle rank rate remember remind
    rephrase reverse riddle rip roast roll roulette rps rsvp
    rules say scramble secret sentiment ship shoutout slap slots
    so social socials songrequest sr steal summarize superpenguin-roll ticket
    time timer tldr top translate trivia truthordare unlurk uptime
    uwu vibecheck vote vso waddle wave weather wheel winner
    wordcount wouldyourather wyr yomama
    """.split()
)

#: Command leads that the platform would execute (`/`, `.` on Twitch/IRC) or that re-trigger other
#: bots (`!`) -- mirrors `waddle_sdk.safe_text`'s completion-lead guard.
_COMMAND_LEADS = ("/", ".", "!")
_SAFE_LEAD = "\U0001f4ac "

_FALLBACK_ACTOR = "someone"

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]{1,128})\}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_VARIABLE_RE = re.compile(r"\$\(([^()]*)\)")
_INT_RE = re.compile(r"^-?[0-9]{1,10}$")

_USAGE = "Usage: !cc add <name> <response> | !cc edit <name> <response> | !cc del <name> | !cc list"
_VARIABLES_HELP = "Variables: $(user) $(target) $(count) $(random <min> <max>)"
_VARIABLES_REJECTED = f"That response uses an unsupported variable. {_VARIABLES_HELP}"
_PERMISSION_DENIED = "Only the broadcaster or a moderator can change custom commands."
_BUSY = "Another change is in progress - please try again."
_UNAVAILABLE = "Custom command storage is unavailable right now - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


class _TemplateError(ValueError):
    """A stored/submitted response uses an unsupported or malformed `$(...)` variable."""


@dataclass(slots=True, frozen=True)
class _Variable:
    """One parsed `$(...)` variable: `kind` is user | target | count | random."""

    kind: str
    low: int = 0
    high: int = 0


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result: bytes | None = await kv.get(key)
    except Exception as exc:
        raise _KvFailure("kv.get failed") from exc
    return result


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- commands persist), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure("kv.set failed") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvFailure("kv.delete failed") from exc


async def _kv_increment(key: str, ttl_seconds: int = 0) -> int:
    """`kv.increment(key, +1)`, reclassifying `Err` into `_KvFailure`."""
    try:
        result: int = await kv.increment(key, 1, ttl_seconds=ttl_seconds)
    except Exception as exc:
        raise _KvFailure("kv.increment failed") from exc
    return result


def _cmd_key(name: str) -> str:
    """Return the kv key one command's response lives under (`name` is already validated)."""
    return f"{CMD_KEY_PREFIX}{name}"


def _uses_key(name: str) -> str:
    """Return the kv key one command's run counter lives under."""
    return f"{USES_KEY_PREFIX}{name}"


async def _load_names() -> list[str]:
    """Return the community's command names, `[]` if none exist yet.

    Raises:
        _KvFailure: The kv call failed or the stored list is corrupt -- never silently reset to
            `[]` (that would drop the whole index on the next write).
    """
    raw = await _kv_get(NAMES_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure("corrupt command index") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt command index: expected a JSON array of strings")
    return cast("list[str]", data)


async def _save_names(names: list[str]) -> None:
    """Persist the command-name index as a sorted, de-duplicated JSON array."""
    await _kv_set(NAMES_KEY, json.dumps(sorted(set(names))).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- strict `is True`, fails CLOSED when no badge is present."""
    return event.payload.get("is_mod") is True or event.payload.get("is_broadcaster") is True


def _normalize_name(raw: str) -> str:
    """Strip an optional leading `!` and lowercase -- `!Welcome` and `welcome` both yield `welcome`."""
    return raw.strip().lstrip("!").strip().lower()


def _split_name_and_response(args: str | None) -> tuple[str, str]:
    """Split `<name> <response...>` on the first run of whitespace; the name is normalized."""
    parts = (args or "").strip().split(None, 1)
    name = _normalize_name(parts[0]) if parts else ""
    return name, parts[1].strip() if len(parts) > 1 else ""


def _name_error(name: str) -> str | None:
    """Return an error message, or `None` if `name` is a legal custom-command name."""
    if not name:
        return "A command name is required, e.g. `!cc add welcome Hi $(user)!`"
    if len(name) > MAX_NAME_LEN:
        return f"Command names must be {MAX_NAME_LEN} characters or fewer."
    if name[0] not in _NAME_FIRST_CHARS or any(ch not in _NAME_ALLOWED_CHARS for ch in name):
        return (
            "Command names may only contain lowercase letters, digits, '_' and '-', "
            "and must start with a letter or digit."
        )
    if name in RESERVED_NAMES:
        return "That name belongs to an existing command and can't be used."
    return None


def _leads_with_command(text: str) -> bool:
    """Return True if the first VISIBLE character of `text` is a command lead (`/`, `.`, `!`).

    "Visible" mirrors what the host's send path leaves behind: control (`Cc`) and format (`Cf`:
    zero-width, bidi, Unicode tag characters) code points are dropped and leading whitespace is
    skipped, so a `/ban` hidden behind a zero-width space still counts as leading with `/`.
    """
    visible = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    return visible.lstrip().startswith(_COMMAND_LEADS)


def _guard_leading_command(text: str) -> str:
    """Return `text`, given a visible prefix if it would lead with a command (idempotent)."""
    return _SAFE_LEAD + text if _leads_with_command(text) else text


def _parse_variable(inner: str) -> _Variable:
    """Parse the inside of one `$(...)`; raise `_TemplateError` for anything unsupported."""
    words = inner.split()
    kind = words[0].lower() if words else ""
    if len(words) == 1 and kind in ("user", "count"):
        return _Variable(kind)
    if len(words) == 1 and kind in ("target", "touser"):
        return _Variable("target")
    if kind == "random" and len(words) == 3 and all(_INT_RE.match(w) for w in words[1:]):
        low, high = int(words[1]), int(words[2])
        if abs(low) <= MAX_RANDOM_ABS and abs(high) <= MAX_RANDOM_ABS and low <= high:
            return _Variable("random", low, high)
    raise _TemplateError(_VARIABLES_REJECTED)


def _parse_template(template: str) -> list[str | _Variable]:
    """Split a response into literal runs and variables.

    Raises:
        _TemplateError: A `$(...)` is not a supported variable, or there are more than
            `MAX_VARIABLES` of them.
    """
    pieces: list[str | _Variable] = []
    pos = 0
    variables = 0
    for match in _VARIABLE_RE.finditer(template):
        if match.start() > pos:
            pieces.append(template[pos : match.start()])
        variables += 1
        if variables > MAX_VARIABLES:
            raise _TemplateError(f"A response can use at most {MAX_VARIABLES} variables.")
        pieces.append(_parse_variable(match.group(1)))
        pos = match.end()
    if pos < len(template):
        pieces.append(template[pos:])
    return pieces


def _response_error(response: str) -> str | None:
    """Return why `response` can't be stored, or `None` if it is acceptable."""
    if not response:
        return "A response is required, e.g. `!cc add welcome Hi $(user)!`"
    if len(response) > MAX_RESPONSE_LEN:
        return f"Responses must be {MAX_RESPONSE_LEN} characters or fewer."
    if any(unicodedata.category(ch) == "Cc" for ch in response):
        return "Responses may not contain control characters."
    if identity.mention_tokens(response):
        return "Responses can't contain user mentions - use $(user) or $(target) instead."
    if _leads_with_command(response):
        return "Responses can't start with /, . or ! (the bot would run it as a command)."
    try:
        _parse_template(response)
    except _TemplateError as exc:
        log.debug("customcommand.template_rejected")
        return str(exc)
    return None


def _escape_plain(text: str) -> str:
    """Return `text` with every egress brace/backslash inside a balanced backslash pair."""
    escaped: str = escape_egress_tokens(text)
    return escaped


def _actor_text(actor: object, live: list[str]) -> str:
    """Render `$(user)`: the host-minted `{user:<tok>}` stays live, anything else is plain text."""
    if not isinstance(actor, str) or not actor.strip():
        return _FALLBACK_ACTOR
    label = actor.strip()
    match = _USER_TOKEN_RE.match(label)
    if match is not None:
        live.append(match.group(1))
        return label
    return _escape_plain(label[:MAX_PLAIN_VALUE_LEN])


def _target_text(actor: object, args_text: str, live: list[str]) -> str:
    """Render `$(target)`: first mention in the RAW invocation, else first word, else invoker."""
    tokens: list[str] = identity.mention_tokens(args_text)
    for token in tokens:
        if _TOKEN_RE.match(token):
            live.append(token)
            return "{user:" + token + "}"
    words = args_text.split()
    if words:
        return _escape_plain(words[0][:MAX_PLAIN_VALUE_LEN])
    return _actor_text(actor, live)


def _render(
    pieces: list[str | _Variable], *, actor: object, args_text: str, uses: int
) -> tuple[str, list[str]]:
    """Assemble the reply from escaped pieces. Returns `(text, live_tokens)` -- see module docs."""
    live: list[str] = []
    parts: list[str] = []
    for piece in pieces:
        if isinstance(piece, str):
            parts.append(_escape_plain(piece))
        elif piece.kind == "user":
            parts.append(_actor_text(actor, live))
        elif piece.kind == "target":
            parts.append(_target_text(actor, args_text, live))
        elif piece.kind == "count":
            parts.append(str(uses))
        else:
            parts.append(str(random.randint(piece.low, piece.high)))  # noqa: S311 - not security
    return "".join(parts), live


def _finalize(text: str, live: list[str]) -> str:
    """Apply the ONE output gate: sanitize the assembled reply, then guard a command lead."""
    sanitized: str = sanitize_user_text(text, allow_tokens=live)
    return _guard_leading_command(sanitized)


async def _run_locked(work: Callable[[], Awaitable[str]]) -> str:
    """Run `work()` while holding the mutation claim.

    `kv.increment` is the only compare-and-swap: it returns 1 to exactly one caller, so a second
    moderator editing concurrently is told to retry instead of racing the names-index write.
    """
    claimed = await _kv_increment(LOCK_KEY, LOCK_TTL_SECONDS)
    if claimed != 1:
        log.info("customcommand.busy")
        return _BUSY
    try:
        return await work()
    finally:
        await _kv_delete(LOCK_KEY)


async def _handle_add(args: str | None) -> str:
    """`!cc add <name> <response>`: validate, then create the command (refuses to overwrite)."""
    name, response = _split_name_and_response(args)
    error = _name_error(name) or _response_error(response)
    if error is not None:
        return error

    async def work() -> str:
        names = await _load_names()
        if name in names or await _kv_get(_cmd_key(name)) is not None:
            return f"!{name} already exists - use !cc edit {name} <response> to change it."
        if len(names) >= MAX_COMMANDS:
            return f"This community already has the maximum of {MAX_COMMANDS} custom commands."
        await _kv_set(_cmd_key(name), response.encode("utf-8"))
        names.append(name)
        await _save_names(names)
        log.info("customcommand.added", op="add", count=len(names))
        return f"Added !{name}. Try it with !{name}"

    return await _run_locked(work)


async def _handle_edit(args: str | None) -> str:
    """`!cc edit <name> <response>`: replace an existing command's response (keeps its counter)."""
    name, response = _split_name_and_response(args)
    error = _name_error(name) or _response_error(response)
    if error is not None:
        return error

    async def work() -> str:
        names = await _load_names()
        if name not in names and await _kv_get(_cmd_key(name)) is None:
            return f"!{name} doesn't exist - use !cc add {name} <response> to create it."
        await _kv_set(_cmd_key(name), response.encode("utf-8"))
        if name not in names:  # self-heal an index entry lost to a partial failure
            names.append(name)
            await _save_names(names)
        log.info("customcommand.edited", op="edit", count=len(names))
        return f"Updated !{name}."

    return await _run_locked(work)


async def _handle_delete(args: str | None) -> str:
    """`!cc del <name>`: remove the command, its counter and its index entry (idempotent)."""
    name, _ = _split_name_and_response(args)
    error = _name_error(name)
    if error is not None:
        return error

    async def work() -> str:
        names = await _load_names()
        exists = name in names or await _kv_get(_cmd_key(name)) is not None
        if not exists:
            return f"!{name} doesn't exist."
        await _kv_delete(_cmd_key(name))
        await _kv_delete(_uses_key(name))
        if name in names:
            names.remove(name)
            await _save_names(names)
        log.info("customcommand.removed", op="del", count=len(names))
        return f"Removed !{name}."

    return await _run_locked(work)


async def _handle_list() -> str:
    """`!cc list`: the command names (open to everyone), shortened to fit one chat message."""
    names = sorted(await _load_names())
    if not names:
        return "No custom commands have been added yet."
    shown: list[str] = []
    length = len(f"Custom commands ({len(names)}): ")
    for name in names:
        length += len(name) + 3  # "!" + name + ", "
        if length > MAX_LIST_REPLY_LEN:
            break
        shown.append(f"!{name}")
    reply = f"Custom commands ({len(names)}): " + ", ".join(shown)
    if len(shown) < len(names):
        reply += f" ... and {len(names) - len(shown)} more"
    return reply


def _canonical_management_text(rest: str) -> str:
    """Rebuild `!cc <verb> <tail>` with this bundle's verb spellings folded onto the shared ones."""
    parts = rest.strip().split(None, 1)
    if not parts:
        return "!cc"
    verb = _VERB_ALIASES.get(parts[0].lower(), parts[0])
    return f"!cc {verb} {parts[1]}" if len(parts) > 1 else f"!cc {verb}"


async def _handle_management(rest: str, event: PlatformEvent) -> str:
    """Handle `!cc ...`; always produces a reply (never silence for a recognised command)."""
    first = rest.strip().split(None, 1)[0].lower() if rest.strip() else ""
    if first in ("", "help"):
        return f"{_USAGE}. {_VARIABLES_HELP}"
    try:
        parsed: ParsedCommand = parse_command(_canonical_management_text(rest), SPEC)
    except CommandUsageError:
        log.debug("customcommand.usage_error")
        return _USAGE
    option = parsed.option
    if option == "list":
        return await _handle_list()
    if option in ("add", "set", "delete", "remove"):
        if not _is_privileged(event):
            log.info("customcommand.permission_denied", op=option)
            return _PERMISSION_DENIED
        if option == "add":
            return await _handle_add(parsed.args)
        if option == "set":
            return await _handle_edit(parsed.args)
        return await _handle_delete(parsed.args)
    return _USAGE


async def _handle_invocation(
    name: str, args_text: str, event: PlatformEvent
) -> tuple[str, list[str]] | None:
    """Run `!<name> ...` if `name` is a stored command; `None` means "not ours" (1 kv.get)."""
    if _name_error(name) is not None:
        return None
    raw = await _kv_get(_cmd_key(name))
    if raw is None:
        return None
    try:
        pieces = _parse_template(raw.decode("utf-8"))
    except (UnicodeDecodeError, _TemplateError) as exc:
        raise _KvFailure("corrupt stored command") from exc
    uses = await _kv_increment(_uses_key(name))
    return _render(pieces, actor=event.actor, args_text=args_text, uses=uses)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!cc ...` management and every stored `!<name>`.

    Returns `None` for non-chat payloads, text with no leading `!` (zero kv cost) and `!tokens`
    that are not stored commands. On a kv failure logs `customcommand.kv_failure` (exception
    class only) and replies with an explicit error -- never a silent default.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None

    parts = stripped.split(None, 1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    reply: tuple[str, list[str]] | None
    try:
        if head in MANAGEMENT_COMMANDS:
            reply = (await _handle_management(rest, event), [])
        else:
            reply = await _handle_invocation(head[1:], rest, event)
    except _KvFailure as exc:
        cause = exc.__cause__
        log.error(
            "customcommand.kv_failure",
            error_type=type(cause).__name__ if cause else type(exc).__name__,
        )
        reply = (_UNAVAILABLE, [])

    if reply is None:
        return None

    log.info(
        "customcommand.transform matched",
        op="management" if head in MANAGEMENT_COMMANDS else "invoke",
    )
    reply_text, live = reply
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={
            "channel_id": event.payload.get("channel_id"),
            "text": _finalize(reply_text, live),
        },
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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already finalized.

    Deliberately does NOT sanitize again: the text is final, and a second pass without the
    `allow_tokens` `transform` used would escape the live mention placeholders.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("customcommand reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("customcommand reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("customcommand.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
