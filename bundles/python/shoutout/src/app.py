"""`!so <user>` -> a customizable, per-community shoutout command, kv-only.

Migrated from the `bot_process` monolith's `!so` feature (task: "Build the
shoutout Python WASM app bundle"). Inspiration credit (not a literal port --
see `bundle.yaml`'s `author`/`notice`): the general `!so <user>` shoutout
concept is inspired by superpenguintv (Psychoboy)'s `PenguinTwitchBot`
(https://github.com/Psychoboy/PenguinTwitchBot). No original source code or
text is reused here -- the command grammar, template system, auto-shoutout
list, and sub-module gating below are written fresh for Waddles, so no MIT
notice reproduction is required (`sdk/waddle-sdk/AUTHORING.md`'s attribution
convention: verbatim reuse needs the license text inline, inspiration-only
needs credit only -- see `fish`'s own module docstring for the same shape).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, merged in #618) for every verb/sub-module shape. **One
deliberate grammar extension**: `!so <user>` (bare command + a single
positional username) has no slot in the shared grammar -- `parse_command`'s
bare case takes no argument, and a non-empty `rest` must start with either a
declared verb (`VERBS`) or a declared sub-module name, or it raises
`CommandUsageError`. `_resolve()` below special-cases exactly this: a first
token that is neither a verb nor a sub-module name is treated as the
shoutout target *before* `parse_command` ever sees it; every other shape
(`set`, `enable`/`disable`, and the `auto` sub-module's own verbs) delegates
to `parse_command` unchanged. (`sdk/waddle-sdk/AUTHORING.md` and
`waddle_sdk.sub_modules`'s own docstring both use "`!shoutout`" as the
illustrative sub-module example -- this bundle's actual invoked command is
`!so`, per the task spec; `SubModuleGate`'s `command=` kv-namespace string
is `"shoutout"` to match that illustrative naming, which is independent of
the literal chat verb.)

Two sub-modules, **both default OFF** (`waddle_sdk.sub_modules.SubModuleGate`,
namespaced `"shoutout"`):

- `auto` -- `!so enable auto` / `!so disable auto` (admin/mod only). Once
  enabled, manages a per-community auto-shoutout list: `!so auto add <user>`
  / `!so auto remove <user>` / `!so auto list`. List membership is stored as
  SHA-256 pseudonyms only (never raw usernames -- see `_target_pseudonym`),
  so `!so auto list` can report a count but cannot recover the original
  usernames from storage; this is a deliberate PII-tokenization trade-off,
  not a limitation anyone needs to work around (the admin who added a name
  already knows it). The add/remove/list commands themselves require `auto`
  to already be enabled (`!so enable auto` first) -- same idiom as
  `AUTHORING.md`'s own `ai`-sub-module worked example.
- `ai` -- `!so enable ai` / `!so disable ai` (admin/mod only). **License-
  gated**: `enable ai` additionally requires the tenant's license tier (the
  always-granted WIT `%flags.tier()` import, via
  `waddle_sdk.flask_core.feature_flags.tier_at_least()`) to be at least
  `"professional"` (`critical-rules.md` Feature Flags & License Tiers:
  "basic AI = Professional"). Fails CLOSED -- an unavailable/stale tier
  binding degrades to `"free"` (`tier_at_least`'s own documented behavior),
  never an implicit allow, and is never env-overridable. `disable ai`
  requires no license check (turning a thing off is always allowed), same
  as `lurk`'s own `!lurk disable ai`.

Data scoping: every piece of state here -- the per-community template, the
`auto`/`ai` enabled flags, and the auto-shoutout list -- is keyed by the
envelope's own `community_id` ONLY, via `waddle_sdk.community_kv`
(`AUTHORING.md` Sec2). `dispatch()` raises before touching `kv` at all if
`envelope.community` is falsy, never defaulting to a tenant-wide bucket.

PII note (same caveat as `lurk`/`fish`): the tokenization pipeline (#429) is
not merged yet, so `event.actor` and a command's own `<user>` argument may
currently be raw usernames. Neither is ever stored in `kv` or logged in raw
form -- `_target_pseudonym()` SHA-256-hashes any username before it reaches
`community_kv`, and no log line below includes a raw username or rendered
chat text.

Gated behind the PostHog flag ``waddles.command-shoutout`` -- checked in
`transform()` after the cheap `!so` command-name match and before the real
grammar parse (`eightball`'s documented ordering rationale).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **AI-generated shoutout text.** `ai` is a real, license-checked toggle,
  but `_handle_shoutout()` always renders the community's configured (or
  default) template today, logging at DEBUG that AI generation was
  requested but is pending, when the sub-module is enabled -- the actual
  WaddleAI call needs the AI/http capability wired in (same documented
  pattern as `lurk`'s own Enterprise-tier `ai` toggle, gh-610). Tracked:
  https://github.com/penguintechinc/waddles/issues/626
- **Automatically posting** a shoutout when a listed user is seen (e.g. on
  raid/join) is not built -- `auto` today is pure list CRUD. Triggering on
  a platform event needs that event type added to this bundle's own
  `bundle.yaml` `consumes` filters (today only `chat.message`); until then
  the list is purely an admin-managed roster with no automatic behavior,
  not a stand-in stub for one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import (
    VERBS,
    CommandSpec,
    CommandUsageError,
    ParsedCommand,
    extract_placeholders,
    parse_command,
    substitute_placeholders,
)
from waddle_sdk.flask_core.feature_flags import feature_enabled, tier_at_least
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.sub_modules import SubModuleGate

FLAG_KEY = "waddles.command-shoutout"

#: Chat-invoked command name (see module docstring for why this differs from
#: the SDK docs' illustrative `"shoutout"` naming).
SPEC = CommandSpec(name="so", sub_modules=frozenset({"auto", "ai"}))

#: One gate instance serves both sub-modules -- `SubModuleGate` is keyed by
#: `(command, sub_module)`, so a single instance namespaced `"shoutout"`
#: handles `auto` and `ai` independently with no key collision.
_GATE = SubModuleGate(command="shoutout")

#: License tier required for `!so enable ai` (`critical-rules.md` Feature
#: Flags & License Tiers: "basic AI = Professional").
_AI_TIER_REQUIRED = "professional"

_TEMPLATE_KEY = "shoutout.config.template"
_AUTO_LIST_KEY = "shoutout.auto.list"

#: Rendered when no per-community template has been configured yet.
DEFAULT_TEMPLATE = "Shoutout to $(username) -- go check them out and give them a follow!"

#: The only placeholder this bundle's template system resolves today --
#: `_handle_set_template` rejects any other `$(name)` token outright (same
#: "validated against the known placeholder set, unknown placeholders
#: rejected" rule as `lurk`'s own message-template command).
KNOWN_PLACEHOLDERS = frozenset({"username"})
MAX_TEMPLATE_LEN = 500

#: Bounds for a shoutout target / auto-list entry. Twitch usernames are at
#: most 25 chars; Discord usernames up to 32 -- 32 covers both comfortably.
MAX_TARGET_LEN = 32
_TARGET_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)

#: Caps unbounded growth of one community's auto-shoutout list.
MAX_AUTO_LIST_SIZE = 200

_USAGE = (
    "Usage: !so <user> | !so set <template> | !so enable|disable auto | "
    "!so auto add|remove|list | !so enable|disable ai (admin/mod only except posting a shoutout)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can use !so"
_AUTO_DISABLED_MSG = "the auto sub-module is disabled -- enable with `!so enable auto`"

_KNOWN_COMMANDS = frozenset(
    {
        "shoutout",
        "config_set_template",
        "config_enable_auto",
        "config_disable_auto",
        "auto_add",
        "auto_remove",
        "auto_list",
        "config_enable_ai",
        "config_disable_ai",
        "usage",
    }
)


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!so ` onto this bundle's own command set.

    `rest` is already stripped and may be empty. See module docstring for
    the bare-positional-target grammar extension this implements ahead of
    `parse_command`.
    """
    if not rest:
        return "usage", None

    tok1 = rest.split(" ", 1)[0]
    tok1_lower = tok1.lower()
    if tok1_lower not in VERBS and tok1_lower not in SPEC.sub_modules:
        # Not a verb or sub-module -- a positional shoutout target. Usernames
        # never contain spaces, so a multi-token `rest` here is a usage
        # error, never a best-guess multi-word target.
        if " " in rest:
            return "usage", None
        return "shoutout", rest

    try:
        parsed = parse_command(f"!so {rest}", SPEC)
    except CommandUsageError as exc:
        # Expected-input skip, not a fault -- malformed/unknown `!so` grammar
        # falls through to the usage reply. Never log `rest` (raw user input).
        log.debug("shoutout.command_usage_error", op="so_resolve", error_type=type(exc).__name__)
        return "usage", None
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successfully parsed `ParsedCommand` onto this bundle's own command set."""
    if parsed.sub_module is None:
        if parsed.option == "set":
            return "config_set_template", parsed.args
        return "usage", None

    if parsed.option == "enable":
        return ("config_enable_auto" if parsed.sub_module == "auto" else "config_enable_ai"), None
    if parsed.option == "disable":
        return (
            "config_disable_auto" if parsed.sub_module == "auto" else "config_disable_ai"
        ), None

    if parsed.sub_module == "auto":
        if parsed.option == "add":
            return "auto_add", parsed.args
        if parsed.option == "remove":
            return "auto_remove", parsed.args
        if parsed.option == "list" and parsed.args is None:
            return "auto_list", None

    return "usage", None


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `count`/`lurk`/`fish`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _validate_target(raw: str) -> tuple[str | None, str | None]:
    """Return `(cleaned_target, None)`, or `(None, error_message)`.

    Strips one leading `@` (a common Discord-mention convention) before
    validating. Bounded to `MAX_TARGET_LEN` with a conservative charset so a
    validated target can never itself produce an invalid state key once
    hashed (`_target_pseudonym`).
    """
    cleaned = raw.strip().lstrip("@").strip()
    if not cleaned:
        return None, "a username is required, e.g. `!so add penguin`"
    if len(cleaned) > MAX_TARGET_LEN:
        return None, f"usernames must be {MAX_TARGET_LEN} characters or fewer"
    if any(ch not in _TARGET_ALLOWED_CHARS for ch in cleaned):
        return None, "usernames may only contain letters, digits, '_', '-', and '.'"
    return cleaned, None


def _target_pseudonym(target: str) -> str:
    """Non-reversible per-target key component -- see module docstring's PII note.

    Lowercased before hashing so `!so auto add Penguin` and
    `!so auto remove penguin` resolve to the same list entry.
    """
    return hashlib.sha256(target.lower().encode()).hexdigest()


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!so` and its grammar, via `_resolve`.

    Cheap-skip first (no leading `!so` token -- `None`, zero cost), flag
    check second, real grammar resolution last -- same ordering as
    `eightball`/`fish`'s own documented rationale. A recognized-but-
    malformed `!so ...` still produces a reply (`"usage"`), never a silent
    drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != "!so":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command, arg = _resolve(rest.strip())

    log.info("shoutout.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if present -- see `count`/`lurk`/`fish`'s own
    # identical forwarding comment for why absence must reach `dispatch` as absence, not `False`.
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv backend-error path: log, reply an error to chat, then re-raise -- see `fish`."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("shoutout.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "shoutout is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"shoutout kv {op} failed: {case_name}") from exc


async def _fail_state(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud corrupt-stored-state path -- a registry-shaped value, unlike a lone config value.

    Mirrors `count`'s own choice to raise on a corrupt registry rather than
    silently resetting it to empty (which would be undetectable data loss
    from the admin's point of view).
    """
    log.error("shoutout.state_corrupt", reason=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "shoutout storage is corrupted, please contact support."},
    )
    raise RuntimeError(f"shoutout corrupt state: {reason}")


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")
    # `waddle_sdk` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (`waddle_sdk/community_kv.py`'s own `get()` signature) rather than
    # leaking `Any` (see `count`'s own identical `_kv_get` for the same pattern).
    return cast("bytes | None", result)


async def _kv_set(
    community: str, key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`community_kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _get_template(community: str, *, provider: str, channel_id: str) -> str:
    """Return the community's configured template, or `DEFAULT_TEMPLATE` if unset/corrupt."""
    raw = await _kv_get(community, _TEMPLATE_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return DEFAULT_TEMPLATE
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        log.error("shoutout.template_corrupt", community=community)
        return DEFAULT_TEMPLATE


async def _load_auto_list(community: str, *, provider: str, channel_id: str) -> list[str]:
    """Return the community's auto-shoutout pseudonym list.

    Raises (via `_fail_state`) on corrupt stored JSON -- see that helper's
    own docstring for why this is fail-loud rather than a silent reset.
    """
    raw = await _kv_get(community, _AUTO_LIST_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await _fail_state(
            f"corrupt auto-shoutout list: {exc}", provider=provider, channel_id=channel_id
        )
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        await _fail_state(
            "corrupt auto-shoutout list: expected a JSON array of strings",
            provider=provider,
            channel_id=channel_id,
        )
    return data


async def _save_auto_list(
    community: str, pseudonyms: list[str], *, provider: str, channel_id: str
) -> None:
    """Persist the community's auto-shoutout pseudonym list, sorted and deduplicated."""
    await _kv_set(
        community,
        _AUTO_LIST_KEY,
        json.dumps(sorted(set(pseudonyms))).encode("utf-8"),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


async def _handle_shoutout(
    target_raw: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Render and return the shoutout reply for `target_raw`."""
    if target_raw is None:
        return _USAGE
    target, error = _validate_target(target_raw)
    if error:
        return error

    template = await _get_template(community, provider=provider, channel_id=channel_id)
    # `waddle_sdk` ships no `py.typed` marker (see pyproject.toml's mypy override), so
    # `substitute_placeholders`'s real `str` return is seen as `Any` here -- cast back.
    text = cast(str, substitute_placeholders(template, {"username": target}))

    if await _GATE.is_enabled(community, "ai"):
        # AI-generated text is a documented, pending extension -- see module
        # docstring's own DO-NOT-BUILD section (tracked: issue 626). The
        # toggle and its license gate are real; only the generation call
        # itself is deferred, so the template reply is used either way.
        log.debug("shoutout.ai_requested_but_pending", community=community)

    log.info("shoutout.posted", community=community)
    return text


async def _handle_set_template(
    template_raw: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Validate and persist `!so set <template>`'s own free-text `args` tail."""
    if not template_raw or not template_raw.strip():
        return "Usage: !so set <template> (supports $(username))"
    template = template_raw.strip()
    if len(template) > MAX_TEMPLATE_LEN:
        return f"template must be {MAX_TEMPLATE_LEN} characters or fewer"
    unknown = [p for p in extract_placeholders(template) if p not in KNOWN_PLACEHOLDERS]
    if unknown:
        return (
            f"unknown placeholder(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(KNOWN_PLACEHOLDERS))}"
        )
    await _kv_set(
        community,
        _TEMPLATE_KEY,
        template.encode("utf-8"),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    log.info("shoutout.template_set", community=community)
    return "shoutout template updated"


async def _handle_auto_add(
    target_raw: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Add a target to the community's auto-shoutout list (requires `auto` enabled)."""
    if not await _GATE.is_enabled(community, "auto"):
        return _AUTO_DISABLED_MSG
    if target_raw is None:
        return "Usage: !so auto add <user>"
    target, error = _validate_target(target_raw)
    if error:
        return error
    assert target is not None  # noqa: S101 -- _validate_target guarantees exactly one of (target, error)

    pseudonyms = await _load_auto_list(community, provider=provider, channel_id=channel_id)
    pseudonym = _target_pseudonym(target)
    if pseudonym in pseudonyms:
        return f"{target} is already on the auto-shoutout list"
    if len(pseudonyms) >= MAX_AUTO_LIST_SIZE:
        return f"the auto-shoutout list is full ({MAX_AUTO_LIST_SIZE} max)"
    pseudonyms.append(pseudonym)
    await _save_auto_list(community, pseudonyms, provider=provider, channel_id=channel_id)
    log.info("shoutout.auto_add", community=community)
    return f"added {target} to the auto-shoutout list"


async def _handle_auto_remove(
    target_raw: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Remove a target from the community's auto-shoutout list (requires `auto` enabled)."""
    if not await _GATE.is_enabled(community, "auto"):
        return _AUTO_DISABLED_MSG
    if target_raw is None:
        return "Usage: !so auto remove <user>"
    target, error = _validate_target(target_raw)
    if error:
        return error
    assert target is not None  # noqa: S101 -- _validate_target guarantees exactly one of (target, error)

    pseudonyms = await _load_auto_list(community, provider=provider, channel_id=channel_id)
    pseudonym = _target_pseudonym(target)
    if pseudonym not in pseudonyms:
        return f"{target} is not on the auto-shoutout list"
    pseudonyms.remove(pseudonym)
    await _save_auto_list(community, pseudonyms, provider=provider, channel_id=channel_id)
    log.info("shoutout.auto_remove", community=community)
    return f"removed {target} from the auto-shoutout list"


async def _handle_auto_list(*, community: str, provider: str, channel_id: str) -> str:
    """Report the community's auto-shoutout list size (requires `auto` enabled).

    Usernames are stored only as SHA-256 pseudonyms (module docstring), so
    this reports a count, never the original usernames -- a deliberate
    PII-tokenization trade-off, not a missing feature.
    """
    if not await _GATE.is_enabled(community, "auto"):
        return _AUTO_DISABLED_MSG
    pseudonyms = await _load_auto_list(community, provider=provider, channel_id=channel_id)
    if not pseudonyms:
        return "the auto-shoutout list is empty"
    return f"the auto-shoutout list has {len(pseudonyms)} user(s)"


async def _handle_enable_ai(*, community: str) -> str:
    """Apply `!so enable ai`'s license gate, then enable if it passes (see module docstring)."""
    if not await tier_at_least(_AI_TIER_REQUIRED):
        log.info("shoutout.enable_ai_denied", community=community)
        return f"the ai sub-module requires a {_AI_TIER_REQUIRED} license or higher"
    await _GATE.enable(community, "ai")
    log.info("shoutout.submodule_enabled", sub_module="ai", community=community)
    return "shoutout ai sub-module enabled"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission gate, then all kv/relay work.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed, or stored state was
            corrupt (see `_fail_kv`/`_fail_state` -- a chat error reply and
            an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("shoutout reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized shoutout command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("shoutout.missing_community", command=command)
        raise ValueError("shoutout requires a community context and cannot operate tenant-wide")

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    # Every remaining command -- posting a shoutout included -- is admin/mod only
    # (module docstring: prevents viewer spam of the shoutout relay).
    role_signal = _caller_role_signal(payload)
    if role_signal is not True:
        log.info("shoutout.permission_denied", command=command, role_signal=str(role_signal))
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{command}:denied")

    arg = payload.get("arg")
    arg = arg if isinstance(arg, str) else None

    if command == "shoutout":
        reply_text = await _handle_shoutout(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    elif command == "config_set_template":
        reply_text = await _handle_set_template(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    elif command == "config_enable_auto":
        await _GATE.enable(community, "auto")
        log.info("shoutout.submodule_enabled", sub_module="auto", community=community)
        reply_text = "shoutout auto sub-module enabled"
    elif command == "config_disable_auto":
        await _GATE.disable(community, "auto")
        log.info("shoutout.submodule_disabled", sub_module="auto", community=community)
        reply_text = "shoutout auto sub-module disabled"
    elif command == "auto_add":
        reply_text = await _handle_auto_add(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    elif command == "auto_remove":
        reply_text = await _handle_auto_remove(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    elif command == "auto_list":
        reply_text = await _handle_auto_list(
            community=community, provider=provider, channel_id=channel_id
        )
    elif command == "config_enable_ai":
        reply_text = await _handle_enable_ai(community=community)
    else:  # config_disable_ai
        await _GATE.disable(community, "ai")
        log.info("shoutout.submodule_disabled", sub_module="ai", community=community)
        reply_text = "shoutout ai sub-module disabled"

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("shoutout.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
