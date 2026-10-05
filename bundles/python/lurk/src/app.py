"""`!lurk`/`!unlurk` -> a stateful per-(community, caller) lurk/unlurk, confirmed by relay.

PII note (2026-10-03, re-affirmed 2026-10-05): the tokenization pipeline
(#429) is NOT merged yet, so `event.actor` may currently be a RAW USERNAME,
not an opaque token. This bundle never stores or logs that raw value --
`_state_key()` hashes the actor (never the community, which is not PII) into
a non-reversible pseudonym before it ever reaches `kv`, and no log line
below includes `actor` or any rendered chat text (`client.md` PII
Tokenization: "Reference users by UUID ... never a raw username outside the
API server"; the WIT boundary here is exactly such an outside-the-boundary
context). The raw actor IS used for the `$(username)` substitution in the
chat *reply* itself -- that's the normal, visible chat response to the same
user who triggered it, never stored or logged. Once #429 lands, `actor`
becomes an opaque token; at that point `$(username)` should resolve via the
detokenization path (`svc_action`) and this bundle should emit a token for
the egress to detokenize, rather than rendering the raw value itself here --
flagged as a follow-up, not built now.

Declares the `storage.kv` permission (`bundle.yaml`/`hub-manifest.yaml`) --
without it, `hub_api/services/bundle_approval_service.py::_derive_
capabilities()` never grants `kv` at all (undeclared means denied, per that
module's own docstring).

v1 scope (2026-10-05, feature/lurk-v1 -- supersedes the trivial kv-toggle
this file used to be):

- `!lurk` starts (or resets) a timed lurk for the caller: stores the
  lurk-start timestamp in `kv`, keyed per `(community_id, hashed actor)`,
  with a 24h TTL. Calling it again while already lurking overwrites the
  start timestamp -- the timer resets, exactly like a fresh `!lurk`. Replies
  with the community's lurk-message template, `$(username)` substituted
  with the caller's own display name.
- `!unlurk` reads the caller's lurk-start; if present, replies with the
  elapsed duration (`$(duration)`, human-readable, e.g. "2h 13m") via a
  fixed (non-configurable in v1) message, then deletes the kv entry. If
  absent -- never lurked, already unlurked, OR the 24h TTL silently expired
  it -- replies with a friendly "you weren't lurking" message instead. The
  kv TTL IS the auto-expiry mechanism; no separate sweep/cron exists or is
  needed.
- Community admin config, broadcaster/moderator only: `!lurk set <message>`
  (per-community lurk-message template; validated against the known
  placeholder set `$(username)`/`$(duration)`, unknown placeholders
  rejected), `!lurk enable ai` / `!lurk disable ai` (Enterprise-licensed AI
  response toggle -- see below), `!lurk reset` (restores both to defaults).
  All four require `_caller_role_signal()` to read `True` off the
  normalized event's own `is_mod`/`is_broadcaster` badge fields
  (`core/svc_ingest/src/normalize.rs::normalize_twitch_irc`); if *neither*
  key is present at all (e.g. Discord, whose normalizer emits no badge
  fields today), the config change is rejected and logged -- never silently
  allowed just because the signal happens to be missing.

Data scoping (2026-10-05 correction): every piece of state here -- the
lurk start-time, the per-community message template, and the `ai_enabled`
flag -- is keyed by the envelope's own `community_id` ONLY. There is no
tenant-wide or "community 0" catch-all bucket: `dispatch()` raises before
touching `kv` at all if `envelope.community` is falsy, rather than
defaulting to a shared sentinel the way this bundle's pre-v1 `_kv_key` used
to (`community or "tenant"`).

License gating (2026-10-05 addition): `!lurk enable ai` reads the always-
granted WIT `%flags.tier()` import (`wit/waddle-bundle/stage.wit`) and only
enables the toggle when the tenant is Enterprise-licensed -- fails CLOSED
(treats an unavailable/stale binding as "free", never as an implicit
allow) and is never env-overridable, per `critical-rules.md` Feature Flags
& License Tiers. `!lurk disable ai` and `!lurk reset` require no license
check (turning a thing off is always allowed).

DO NOT BUILD in v1 -- clean extension points, tracked issues, never a
silent stub:

- The AI-generated response itself. `ai_enabled` is a real, license-checked
  toggle, but `_handle_lurk()` always falls back to the template message
  today (and logs at DEBUG that AI was requested but is pending) -- the
  actual WaddleAI call needs the AI/http capability wired in. Tracked:
  https://github.com/penguintechinc/waddles/issues/610
- `stream.offline` cancelling an in-progress lurk. Needs that EventSub
  event added to this bundle's own `bundle.yaml` `consumes` filters (today
  only `chat.message`) -- until then, a lurk through a stream going offline
  simply rides out its 24h TTL like any other. Tracked:
  https://github.com/penguintechinc/waddles/issues/611

Business logic split: `transform` only recognizes the command/subcommand
and extracts the normalized badge signal (no kv access, no side effect) --
`dispatch` performs all state reads/writes and the relay confirmation,
mirroring `pyping`'s own process/action-stage split.

Gated behind the PostHog flag ``waddles.command-lurk`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NoReturn

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-lurk"

#: 24h -- well under the host's 30-day `KV_MAX_TTL_S` cap (spec kv limits).
#: A lurk is a per-session affordance; if a caller never `!unlurk`s, the
#: entry simply expires rather than accumulating forever, and a lurk older
#: than this is treated identically to "never lurked" (see module docstring).
LURK_TTL_SECONDS = 24 * 60 * 60

#: Config keys persist indefinitely (`ttl_seconds=0`) -- durable per-community
#: settings, not session state.
_CONFIG_TTL_SECONDS = 0

DEFAULT_LURK_TEMPLATE = "$(username) is now lurking \U0001f440"
#: Fixed in v1 -- no `!lurk setunlurk` setter (2026-10-05 correction: keep a
#: sensible default rather than building a second template setter).
UNLURK_MESSAGE_TEMPLATE = "Welcome back, $(username)! You lurked for $(duration)."
NOT_LURKING_REPLY = "You weren't lurking!"

_ALLOWED_PLACEHOLDERS = frozenset({"username", "duration"})
_PLACEHOLDER_RE = re.compile(r"\$\(([a-zA-Z_]+)\)")
_MAX_TEMPLATE_LEN = 200

_USAGE = (
    "Usage: !lurk | !unlurk | !lurk set <message> | !lurk enable ai | "
    "!lurk disable ai | !lurk reset (admin/mod only for set/enable/disable/reset)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !lurk"
_ENTERPRISE_REQUIRED_MSG = "AI lurk responses require Enterprise"

_CONFIG_COMMANDS = frozenset(
    {"config_set_message", "config_enable_ai", "config_disable_ai", "config_reset"}
)
_KNOWN_COMMANDS = _CONFIG_COMMANDS | frozenset({"lurk", "unlurk", "usage"})


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!lurk`/`!unlurk` and their subcommands.

    No kv access here -- only command recognition and forwarding the
    normalized badge signal (`is_mod`/`is_broadcaster`, if the platform's
    normalizer emits them) for `dispatch`'s own permission gate. Returns
    `None` for any non-matching payload or while `waddles.command-lurk` is
    disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    lowered = stripped.lower()
    arg: str | None
    if lowered == "!unlurk":
        command, arg = "unlurk", None
    elif lowered == "!lurk":
        command, arg = "lurk", None
    elif lowered.startswith("!lurk "):
        rest = stripped[len("!lurk ") :].strip()
        rest_lower = rest.lower()
        if rest_lower == "reset":
            command, arg = "config_reset", None
        elif rest_lower == "enable ai":
            command, arg = "config_enable_ai", None
        elif rest_lower == "disable ai":
            command, arg = "config_disable_ai", None
        elif rest_lower == "set" or rest_lower.startswith("set "):
            command, arg = "config_set_message", rest[len("set") :].strip()
        else:
            command, arg = "usage", None
    else:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    log.info("lurk.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if the platform's own normalizer
    # emitted one -- absence (e.g. Discord today) must reach `dispatch` as
    # absence, not as an implicit `False`, so these keys are only set when
    # actually present on the inbound event.
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


def _state_key(community: str, actor: str | None) -> str:
    """Per-(community, caller) kv key for the lurk-start timestamp.

    `community` is an opaque community identifier, not PII, so it's kept
    plain and visible in the key for debuggability; `actor` may still be a
    raw username (tokenization pipeline #429 not yet merged), so it alone is
    SHA-256 hashed into a non-reversible pseudonym -- the stored key never
    contains PII, today or after #429.

    Uses `.` as the key separator, never `:` (gh-631): the real `kv` host
    capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`)
    reserves `:` as its own namespace separator and rejects any guest key
    containing one. This bundle's original `lurk:state:{community}:
    {pseudonym}` passed every test (the old hand-rolled fake accepted any
    key) and then failed every real `kv` call in production with
    `kv.error::backend` -- see `waddle_sdk.kv.validate_key`, which now
    rejects this at the SDK boundary before a host call is even attempted.
    """
    pseudonym = hashlib.sha256((actor or "anonymous").encode()).hexdigest()
    return f"lurk.state.{community}.{pseudonym}"


def _message_key(community: str) -> str:
    """Per-community kv key for the customized `!lurk` message template."""
    return f"lurk.config.{community}.message"


def _ai_key(community: str) -> str:
    """Per-community kv key for the `ai_enabled` toggle. Presence = enabled."""
    return f"lurk.config.{community}.ai_enabled"


def _render_template(template: str, *, username: str, duration: str = "") -> str:
    """Substitute `$(username)`/`$(duration)` placeholders. Unused placeholders render empty."""
    return template.replace("$(username)", username).replace("$(duration)", duration)


def _validate_template(template: str) -> str | None:
    """Return an error message if `template` is invalid, else `None`.

    Mirrors `social_alias_process._cmd_set_alias`'s own length-cap
    convention (`_MAX_EXPANSION_LEN = 200`) and rejects any `$(...)`
    placeholder outside the known set, so a caller's typo (`$(usernam)`)
    fails loudly at `!lurk set` time instead of rendering literally in
    every future lurk reply.
    """
    if not template:
        return "message can't be empty"
    if len(template) > _MAX_TEMPLATE_LEN:
        return f"message is too long (max {_MAX_TEMPLATE_LEN} characters)"
    unknown = sorted(
        {m for m in _PLACEHOLDER_RE.findall(template) if m not in _ALLOWED_PLACEHOLDERS}
    )
    if unknown:
        return f"unknown placeholder(s): {', '.join(unknown)} (allowed: $(username), $(duration))"
    return None


def _format_duration(total_seconds: int) -> str:
    """Human-readable elapsed duration, e.g. "2h 13m", "1d 3h", "45s" (two largest units)."""
    total_seconds = max(total_seconds, 0)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    nonzero = [
        (v, s) for v, s in ((days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")) if v > 0
    ]
    if not nonzero:
        return "0s"
    return " ".join(f"{v}{s}" for v, s in nonzero[:2])


def _license_tier() -> str:
    """Read the tenant's license tier via the always-granted WIT `%flags.tier()` import.

    Fails CLOSED to `"free"` -- never `"enterprise"` -- when the binding is
    unavailable (host-side tests) or the generated world has no `flags`
    import (a component wizened against an older world, same defensive
    shape as `waddle_sdk.flask_core.feature_flags.feature_enabled`'s own
    stale-world guard). Unlike that PostHog-flag shim, this answers a
    license-entitlement question, so the fail direction is the opposite:
    closed, never open, and never overridable by any env var/flag per
    `critical-rules.md` Feature Flags & License Tiers.
    """
    try:
        import wit_world
    except ImportError:
        return "free"
    flags_mod = getattr(wit_world.imports, "flags", None)
    if flags_mod is None:
        return "free"
    tier_fn = getattr(flags_mod, "tier", None)
    if tier_fn is None:
        return "free"
    return str(tier_fn())


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    `None` means the platform's normalizer emitted neither `is_mod` nor
    `is_broadcaster` at all (e.g. Discord's `normalize_discord` today --
    `core/svc_ingest/src/normalize.rs`) -- config commands must treat
    `None` as denied, exactly like `False`, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise.

    Classifies the raised exception structurally (`getattr(exc, "value",
    exc)`), same pattern as `waddle_sdk.http._classify_http_error`/
    `waddle_sdk.db`'s own docstrings -- `kv.get/set/delete/increment` raise
    the generated `Err` (`.value` holds `Error_TooLarge`/`Error_Backend`) on
    failure. Never silent: the caller always sees a chat reply AND the
    pipeline still sees a real failure (the re-raise), matching this
    component's own exception-as-failure-signal convention
    (`bundles/python/pyping/src/app.py`'s own `DispatchResult` docstring).
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("lurk.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "lurk is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"lurk kv {op} failed: {case_name}") from exc


async def _kv_get(key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await kv.get(key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")


async def _kv_set(
    key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await kv.set(key, value, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _kv_delete(key: str, *, provider: str, channel_id: str) -> None:
    """`kv.delete`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="delete")


async def _get_lurk_template(community: str, *, provider: str, channel_id: str) -> str:
    """Return the community's custom `!lurk` message template, or the default if unset."""
    raw = await _kv_get(_message_key(community), provider=provider, channel_id=channel_id)
    if raw is None:
        return DEFAULT_LURK_TEMPLATE
    try:
        return raw.decode()
    except UnicodeDecodeError:
        log.error("lurk.template_corrupt", community=community)
        return DEFAULT_LURK_TEMPLATE


async def _get_ai_enabled(community: str, *, provider: str, channel_id: str) -> bool:
    """Return the community's `ai_enabled` toggle. Presence of the key means enabled."""
    raw = await _kv_get(_ai_key(community), provider=provider, channel_id=channel_id)
    return raw is not None


async def _handle_lurk(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Set/reset the caller's lurk-start timestamp and build the templated reply.

    `ai_enabled` is read but not yet acted on -- see module docstring, gh
    #610 -- always falls back to the template message, logging at DEBUG
    that an AI response was requested but is pending.
    """
    key = _state_key(community, actor)
    start_ms = clock.now_millis()
    await _kv_set(
        key,
        str(start_ms).encode(),
        ttl_seconds=LURK_TTL_SECONDS,
        provider=provider,
        channel_id=channel_id,
    )

    if await _get_ai_enabled(community, provider=provider, channel_id=channel_id):
        log.debug("lurk.ai_requested_but_pending", community=community)

    template = await _get_lurk_template(community, provider=provider, channel_id=channel_id)
    return _render_template(template, username=username)


async def _handle_unlurk(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Read+clear the caller's lurk-start and build the elapsed-duration reply.

    Missing state (never lurked, already unlurked, or the 24h kv TTL
    expired it) -> `NOT_LURKING_REPLY`, the single code path for all three
    (see module docstring).
    """
    key = _state_key(community, actor)
    raw = await _kv_get(key, provider=provider, channel_id=channel_id)
    if raw is None:
        return NOT_LURKING_REPLY

    try:
        start_ms = int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("lurk.state_corrupt", community=community)
        await _kv_delete(key, provider=provider, channel_id=channel_id)
        return NOT_LURKING_REPLY

    elapsed_seconds = max((clock.now_millis() - start_ms) // 1000, 0)
    await _kv_delete(key, provider=provider, channel_id=channel_id)
    return _render_template(
        UNLURK_MESSAGE_TEMPLATE, username=username, duration=_format_duration(elapsed_seconds)
    )


async def _handle_config_command(
    command: str, arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Apply one already-authorized config subcommand and return its chat reply."""
    if command == "config_set_message":
        message = (arg or "").strip()
        if not message:
            return _USAGE
        error = _validate_template(message)
        if error is not None:
            return f"invalid lurk message: {error}"
        await _kv_set(
            _message_key(community),
            message.encode(),
            ttl_seconds=_CONFIG_TTL_SECONDS,
            provider=provider,
            channel_id=channel_id,
        )
        return f"lurk message updated: {message}"

    if command == "config_enable_ai":
        tier = _license_tier()
        if tier != "enterprise":
            log.info("lurk.enable_ai_denied", tier=tier)
            return _ENTERPRISE_REQUIRED_MSG
        await _kv_set(
            _ai_key(community),
            b"1",
            ttl_seconds=_CONFIG_TTL_SECONDS,
            provider=provider,
            channel_id=channel_id,
        )
        return "AI lurk responses enabled (Enterprise)."

    if command == "config_disable_ai":
        await _kv_delete(_ai_key(community), provider=provider, channel_id=channel_id)
        return "AI lurk responses disabled."

    # config_reset
    await _kv_delete(_message_key(community), provider=provider, channel_id=channel_id)
    await _kv_delete(_ai_key(community), provider=provider, channel_id=channel_id)
    return "lurk settings reset to defaults."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (see module docstring's Data
            scoping section -- there is no tenant-wide fallback); or an
            unrecognized `command` (defensive -- `transform` only ever
            emits a member of `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("lurk reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized lurk command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("lurk.missing_community", command=command)
        raise ValueError("lurk requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command in _CONFIG_COMMANDS:
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("lurk.config_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")

        arg = payload.get("arg")
        reply_text = await _handle_config_command(
            command,
            arg if isinstance(arg, str) else None,
            community=community,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("lurk.dispatch config applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "lurk":
        reply_text = await _handle_lurk(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # unlurk
        reply_text = await _handle_unlurk(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("lurk.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
