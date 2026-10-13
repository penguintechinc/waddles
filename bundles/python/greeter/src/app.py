"""`greeter` -- a keyword/event auto-responder (greetings, name mentions, thanks) + `!greeter`.

Reimplements the free-text half of `core/svc_process/builtin_handlers/bot_process.py`'s
`_handle_keyword` (greeting / bot name-mention / thanks auto-replies) as a standalone bundle
with three differences: every responder is an opt-in **sub-module** (default OFF per
community, `waddle_sdk.sub_modules.SubModuleGate`), replies are rate-limited by a **per-user
cooldown**, and the bundle **never answers itself or another bot**.

Hybrid shape -- two paths through one `transform()`:

- **`!greeter` command** (`enable|disable <sub>` mod-only, `list`, bare/`<sub>` = state): the
  grammar comes from `waddle_sdk.command` (`CommandSpec(name="greeter", sub_modules=...)`).
- **Free text** (any message NOT starting with `!`): classified into at most one category
  (`greet` > `mention` > `thanks`, the same precedence `bot_process` uses), then gated by the
  flag, the sub-module and the cooldown. Anything else returns `None` (no reply).

Consume filter: this bundle must see ordinary chat lines, not only `!greeter`, so its manifests
declare `consumes` with NO `filters.command_prefix` (an absent filter key is "not a
constraint", rust-data-plane design spec §5.3/§6). The stage is therefore woken for every
`chat.message`, which is why `transform()` orders its checks cheapest-first (pure string checks,
then the flag, then regex, and kv only once a category actually matched).

Fail-closed ordering for the free-text path:
1. not a `str` / empty / any other `!command` -> `None`
2. flag `waddles.command-greeter` OFF (default) -> `None`
3. author is the bot itself or another bot -> `None` (see `_is_bot_author`)
4. no category matches (token-boundary regexes) -> `None`
5. first matching category whose sub-module is enabled (default OFF) -> else `None`
6. that (user, category) is inside its cooldown window -> `None`
7. otherwise record the cooldown and reply.

Bot detection: the ingest normalizers (`core/svc_ingest/src/normalize.rs`) drop the bot's own
echoed messages upstream and do not (yet) emit an `is_bot` flag, so absence of a flag means
"human". Any flag that IS present is read strictly: `is_bot`/`author_is_bot`/`bot` must be
exactly `False` to pass (a string `"false"`, `None`, `1` ... is ambiguous -> skip), an
`author_id` equal to `bot_id`/`self_id`/`bot_user_id` is the bot itself, and an actor named
like the bot (`waddles`/`waddlebot`) is skipped as a last-resort loop guard.

PII: `event.actor` may still be a RAW username (tokenization #429 unmerged). It is used only in
the visible chat reply to the same user, never in a kv key (the cooldown key is a SHA-256 of
the stable identity) and never in a log line. This bundle reads free text, so NO log field ever
carries the message text, actor, author id or a mention target -- only community/op/category/
counts/exception-type.

Declares `storage.kv` (sub-module toggles + cooldowns) and `flags.read`. `kv`, `log`, `relay`,
`clock`, `feature_flags` and `sub_modules` are imported eagerly at module top on purpose:
componentize-py only wizens a WIT host-import that is imported at module load (see
`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, kv, log, relay  # noqa: F401 -- eager, see docstring
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.sub_modules import SubModuleGate

#: PostHog flag key, `{product}.{feature-name}` convention. Default OFF.
FLAG_KEY = "waddles.command-greeter"

#: Responder categories == the command's sub-modules, in reply-precedence order.
CATEGORIES: tuple[str, ...] = ("greet", "mention", "thanks")

SPEC = CommandSpec(name="greeter", sub_modules=frozenset(CATEGORIES))
_GATE = SubModuleGate(command="greeter")

#: Per-(user, category) suppression window, seconds. Well under the host's 30-day TTL cap.
COOLDOWN_SECONDS: dict[str, int] = {"greet": 3600, "mention": 60, "thanks": 60}

REPLY_THANKS = "You're welcome! \U0001f427"
REPLY_MENTION = "\U0001f427 You called? I'm here -- try !greeter list to see what I answer to."
_FALLBACK_NAME = "friend"

_USAGE = (
    "Usage: !greeter | !greeter list | !greeter enable <greet|mention|thanks> | "
    "!greeter disable <greet|mention|thanks>"
)
_DENIED = "only moderators/broadcasters can configure !greeter"
_KV_UNAVAILABLE = "greeter is temporarily unavailable, try again shortly."

# `(?<!\w)`/`(?!\w)` = real token boundaries: "ship" never matches "hi", "party" never "ty".
_GREET_RE = re.compile(r"^(?:hi|hello|hey|yo|howdy)(?![\w'-])", re.IGNORECASE)
_THANKS_RE = re.compile(r"(?<!\w)(?:thanks|thank\s+you|thankyou|thx|ty)(?!\w)", re.IGNORECASE)
_MENTION_RE = re.compile(
    r"(?<!\w)(?:@(?:waddle|waddles|waddlebot)|(?<!@)(?:waddles|waddlebot))(?!\w)",
    re.IGNORECASE,
)
_CATEGORY_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greet", _GREET_RE),
    ("mention", _MENTION_RE),
    ("thanks", _THANKS_RE),
)

_BOT_FLAG_KEYS = ("is_bot", "author_is_bot", "bot")
_BOT_ID_KEYS = ("bot_id", "self_id", "bot_user_id")
_BOT_NAMES = frozenset({"waddles", "waddlebot"})


class _KvFailureError(RuntimeError):
    """A `kv` host call failed (generated WIT `Err`, classified structurally); fail loud."""


def _fail(exc: Exception, *, op: str, community: str) -> NoReturn:
    """Log a kv failure (exception TYPE only, never its text) and raise `_KvFailureError`."""
    wit_error = getattr(exc, "value", exc)
    log.error("greeter.kv_failure", op=op, community=community, error=type(wit_error).__name__)
    raise _KvFailureError(f"greeter kv {op} failed: {type(wit_error).__name__}") from exc


def _scope(community: str | None) -> str:
    """Return the community to key state under.

    `None` is the host's own tenant-wide sentinel (alpha's only activation shape) and is scoped
    under `TENANT_WIDE_SENTINEL`; an EMPTY string is a caller bug and fails loud.
    """
    if community == "":
        log.error("greeter.missing_community")
        raise ValueError("greeter requires a community context (got an empty community id)")
    return community if community is not None else TENANT_WIDE_SENTINEL


async def _is_enabled(community: str, category: str) -> bool:
    """`SubModuleGate.is_enabled` (default OFF), reclassifying a kv `Err` into `_KvFailureError`."""
    try:
        return await _GATE.is_enabled(community, category)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="gate_get", community=community)


async def _apply_toggle(community: str, parsed: ParsedCommand) -> bool | None:
    """`SubModuleGate.apply_toggle`, reclassifying a kv `Err` into `_KvFailureError`."""
    try:
        return await _GATE.apply_toggle(community, parsed)
    except CommandUsageError:
        raise
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="gate_set", community=community)


def _caller_is_mod(payload: dict[str, Any]) -> bool:
    """Strict-boolean mod/broadcaster check -- FAILS CLOSED when role info is absent/non-bool."""
    is_mod = payload.get("is_mod")
    is_broadcaster = payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("greeter.role_info_unavailable")
        return False
    return is_mod is True or is_broadcaster is True


def _is_bot_author(event: PlatformEvent) -> bool:
    """True when the author is the bot itself or another bot, or when that is ambiguous."""
    payload = event.payload
    for key in _BOT_FLAG_KEYS:
        if key in payload and payload[key] is not False:
            return True
    author_id = payload.get("author_id")
    if author_id is not None:
        for key in _BOT_ID_KEYS:
            own = payload.get(key)
            if own is not None and str(own) == str(author_id):
                return True
    return bool(event.actor) and str(event.actor).strip().lower().lstrip("@") in _BOT_NAMES


def classify(text: str) -> list[str]:
    """Every category `text` matches, in reply-precedence order (empty when none)."""
    return [name for name, pattern in _CATEGORY_RES if pattern.search(text)]


def _user_hash(event: PlatformEvent) -> str:
    """Non-reversible per-user pseudonym for the cooldown key (never the raw actor/id)."""
    identity = event.payload.get("author_id") or event.actor or "anonymous"
    return hashlib.sha256(f"{event.platform}\0{identity}".encode()).hexdigest()


def _cooldown_key(category: str, user_hash: str) -> str:
    """Per-(category, hashed user) cooldown key; `.`-separated (gh-631), community-scoped by kv."""
    return f"greeter.cd.{category}.{user_hash}"


async def _cooldown_active(community: str, category: str, user_hash: str) -> bool:
    """True while the user's last reply in `category` is younger than its cooldown window.

    Belt and braces: the key carries a host TTL, AND the stored timestamp is re-checked here
    so a host that served an expired entry cannot let a spam burst through. A corrupt value is
    logged and treated as expired (self-heals on the next write).
    """
    try:
        raw = await community_kv.get(community, _cooldown_key(category, user_hash))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="cooldown_get", community=community)
    if raw is None:
        return False
    try:
        stored_ms = int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("greeter.cooldown_corrupt", community=community, category=category)
        return False
    return clock.now_millis() - stored_ms < COOLDOWN_SECONDS[category] * 1000


async def _start_cooldown(community: str, category: str, user_hash: str) -> None:
    """Record now as the user's last reply time, expiring after the category's window."""
    try:
        await community_kv.set(
            community,
            _cooldown_key(category, user_hash),
            str(clock.now_millis()).encode(),
            COOLDOWN_SECONDS[category],
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail`
        _fail(exc, op="cooldown_set", community=community)


def _reply_text(category: str, event: PlatformEvent) -> str:
    """Build the canned reply for `category`."""
    if category == "greet":
        return f"Hey @{event.actor or _FALLBACK_NAME}! \U0001f44b"
    if category == "mention":
        return REPLY_MENTION
    return REPLY_THANKS


def _reply_event(event: PlatformEvent, text: str) -> PlatformEvent:
    """A NEW event carrying only the reply text + the channel to answer in."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": text, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


async def _states(community: str) -> dict[str, bool]:
    """Current on/off state of every responder, in `CATEGORIES` order."""
    return {name: await _is_enabled(community, name) for name in CATEGORIES}


def _render_states(states: dict[str, bool]) -> str:
    """`greet=on, mention=off, thanks=off`."""
    return ", ".join(f"{name}={'on' if on else 'off'}" for name, on in states.items())


async def _handle_command(event: PlatformEvent, text: str, community: str) -> str:
    """Parse + run one `!greeter ...` message and return its chat reply."""
    try:
        parsed = parse_command(text, SPEC)
    except CommandUsageError as exc:
        return str(exc)

    if parsed.option in ("enable", "disable"):
        if not _caller_is_mod(event.payload):
            log.info("greeter.config_denied", op=parsed.option)
            return _DENIED
        try:
            enabled = await _apply_toggle(community, parsed)
        except CommandUsageError as exc:  # defensive: parse_command already guarantees a target
            return str(exc)
        log.info(
            "greeter.toggled",
            op=parsed.option,
            category=str(parsed.sub_module),
            community=community,
        )
        return f"greeter {parsed.sub_module} {'enabled' if enabled else 'disabled'}."

    if parsed.option is None and parsed.sub_module is not None:
        on = await _is_enabled(community, parsed.sub_module)
        return f"greeter {parsed.sub_module} is {'on' if on else 'off'}."

    if parsed.option is None or parsed.option == "list":
        states = await _states(community)
        if parsed.option == "list":
            return f"greeter responders: {_render_states(states)}"
        return f"{_USAGE} | now: {_render_states(states)}"

    return _USAGE


async def _handle_text(event: PlatformEvent, text: str, community: str) -> str | None:
    """Free-text path: flag -> bot guard -> classify -> sub-module -> cooldown -> reply text."""
    if _is_bot_author(event):
        log.debug("greeter.skipped_bot_author")
        return None

    matches = classify(text)
    if not matches:
        return None

    for category in matches:
        if not await _is_enabled(community, category):
            continue
        user_hash = _user_hash(event)
        if await _cooldown_active(community, category, user_hash):
            log.debug("greeter.cooldown_active", category=category, community=community)
            return None
        await _start_cooldown(community, category, user_hash)
        log.info("greeter.matched", category=category, community=community)
        return _reply_text(category, event)
    return None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: answer `!greeter`, or auto-reply to matching chatter.

    Returns `None` for non-text payloads, other `!commands`, the flag being OFF, bot authors,
    non-matching text, a disabled sub-module and an active cooldown. A kv failure on the
    free-text path raises (fail loud, never a silent no-reply); on the `!greeter` command path
    it is logged and answered with an error reply so the moderator sees it.
    """
    raw = event.payload.get("text")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    is_command = text.startswith("!")
    if is_command and text.split(maxsplit=1)[0].lower() != "!greeter":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    community = _scope(get_bundle_context().community)
    try:
        if is_command:
            reply: str | None = await _handle_command(event, text, community)
        else:
            reply = await _handle_text(event, text, community)
    except _KvFailureError:
        if not is_command:
            raise
        reply = _KV_UNAVAILABLE

    if reply is None:
        return None
    return _reply_event(event, reply)


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
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("greeter reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("greeter.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
