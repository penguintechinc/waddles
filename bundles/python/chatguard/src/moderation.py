"""Cross-platform moderator commands for chatguard: one grammar, fan-out to platform tools.

Commands (all `!<verb> <@user> ...`; the platform word is optional and defaults to `all`):

- `!ban <user> [platform] [reason...]`
- `!unban <user> [platform]`
- `!timeout <user> <duration> [platform] [reason...]` (`45s`, `10m`, `2h`, `1d`, `1w`, `1h30m`)
- `!untimeout <user> [platform]`
- `!kick <user> [platform]` (Discord only; elsewhere it answers "not supported on <platform>")
- `!purge <user> [platform]` (Twitch only: the platform's own purge idiom, a 1 second timeout)
- `!modlog [n]` (recent actions of this community, ids abbreviated)

**Identity.** A username is mutable PII, so it never reaches a platform tool. The target is a plain
username (`bob`/`@bob`) or the host-minted `{user:<token>}` mention of the invoking message;
the host's `identity` import turns it into the community user UUID and every tool call carries
only `{"user": "<uuid>"}`. The host-side tool resolves that UUID to the platform account
linked on ITS platform, so "all" is a fan-out of one UUID to every enabled platform and a
platform where the user has no linked account (or the community has no bound channel) answers
with a per-platform refusal in the single reply.

**Authorization.** None is decided here from chat badges. Every platform tool is declared
`required_scope: community:moderate` and the host checks the host-verified actor before running
it; a refusal (`not_granted` / `forbidden`) is reported, never retried. This module additionally
refuses a self-target. Protection of the broadcaster/owner/other moderators is enforced by the
platform tools' own hierarchy gates (Discord role hierarchy, Twitch Helix); a tool that refuses
a protected target surfaces as `forbidden` here.

**Audit.** Every attempted action emits one PII-free `moderation.action` log line (actor uuid,
target uuid, platform, action, outcome, `has_reason`; never the reason text or any handle) and
appends the same facts plus the (length-capped, sanitized) reason to a 50-slot kv ring read by
`!modlog`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from waddle_sdk import clock, community_kv, event_dedupe, identity, log, tool
from waddle_sdk.safe_text import sanitize_user_text

ALL: Final = "all"
MOD_COMMANDS: Final = frozenset(
    {"!ban", "!unban", "!timeout", "!untimeout", "!kick", "!purge", "!modlog"}
)
#: Platforms whose tools are wired; `kick` is recognised so it answers "not supported" loudly
#: instead of being read as the first word of a ban reason.
PLATFORMS: Final = ("twitch", "discord", "youtube", "kick")
DEFAULT_ALL_PLATFORMS: Final = ("twitch", "discord", "youtube")

TWITCH: Final = "waddles.core.service.twitch"
DISCORD: Final = "waddles.core.service.discord"
YOUTUBE: Final = "waddles.core.service.youtube"

MAX_REASON_CHARS: Final = 200
MAX_LINE_CHARS: Final = 600
HISTORY_SLOTS: Final = 50
MODLOG_MAX: Final = 5
_CONFIG_KEY: Final = "moderation.config"
_SEQ_KEY: Final = "moderation.hist.seq"
_HIST_TTL_S: Final = 90 * 24 * 3600
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UNIT_SECONDS: Final = {"w": 604_800, "d": 86_400, "h": 3_600, "m": 60, "s": 1}
_UNIT_RANK: Final = {"w": 5, "d": 4, "h": 3, "m": 2, "s": 1}
_MAX_DURATION_S: Final = 365 * 86_400
_PAST: Final = {
    "ban": "banned",
    "unban": "unbanned",
    "timeout": "timed out",
    "untimeout": "timeout cleared",
    "kick": "kicked",
    "purge": "purged",
}

USAGE: Final = (
    "Usage: !ban <user> [platform] [reason] | !unban <user> [platform] | "
    "!timeout <user> <duration> [platform] [reason] | !untimeout <user> [platform] | "
    "!kick <user> [platform] | !purge <user> [platform] | !modlog [n]  "
    "(platform: twitch|discord|youtube|kick|all, default all)"
)


class ModUsageError(ValueError):
    """A moderation line broke the grammar; `str(exc)` is a ready-to-send, input-free reply."""


@dataclass(frozen=True, slots=True)
class ModCommand:
    """One parsed moderation command (the target is still the unresolved mention token)."""

    action: str
    token: str
    platform: str
    duration: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class Op:
    """One platform tool call to run: target app, tool name, args, and the platform cap if any."""

    app_id: str
    tool: str
    args: dict[str, Any]
    clamped_from: int | None = None


def parse_duration(raw: str) -> int:
    """Parse `45s`/`10m`/`2h`/`1d`/`1w` or a descending compound (`1h30m`) into seconds.

    Local mirror of the Rust SDK's `parse_duration` (PR #1116, same grammar): each unit at most
    once and strictly descending, no zero, no bare number, bounded to one year.

    Raises:
        ModUsageError: Empty, malformed, zero, repeated/ascending units, or out of range.
    """
    if not raw or len(raw) > 32:
        raise ModUsageError("invalid duration (try 30m, 2h, 1d, 1h30m)")
    total, digits, last = 0, "", 99
    for ch in raw:
        if ch.isascii() and ch.isdigit():
            digits += ch
            continue
        unit = ch.lower()
        rank = _UNIT_RANK.get(unit)
        if rank is None or not digits or rank >= last:
            raise ModUsageError("invalid duration (try 30m, 2h, 1d, 1h30m)")
        total += int(digits) * _UNIT_SECONDS[unit]
        digits, last = "", rank
        if total > _MAX_DURATION_S:
            raise ModUsageError("duration is out of range")
    if digits or last == 99 or total == 0:
        raise ModUsageError("invalid duration (try 30m, 2h, 1d, 1h30m)")
    return total


def format_duration(seconds: int) -> str:
    """Render seconds as the shortest compound (`90061` -> `1d1h1m1s`)."""
    parts = []
    for unit in ("w", "d", "h", "m", "s"):
        n, seconds = divmod(seconds, _UNIT_SECONDS[unit])
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0s"


def _clean_reason(words: list[str]) -> str | None:
    """Sanitize the free-text reason: no control chars, no minted mentions, capped length."""
    text = " ".join(words).strip()
    if not text:
        return None
    if "{user:" in text:
        raise ModUsageError("the reason may not contain a mention")
    text = "".join(c for c in text if c.isprintable())
    if len(text) > MAX_REASON_CHARS:
        raise ModUsageError(f"the reason is too long (max {MAX_REASON_CHARS} characters)")
    return text or None


_HANDLE_RE: Final = re.compile(r"@?[A-Za-z0-9_.\-]{1,64}")


def _target_token(word: str) -> str:
    """Return the first argument as a host-resolvable reference: a mention token or a handle.

    A `{user:<token>}` placeholder is the unambiguous form. A PLAIN handle (`bob` / `@bob`) goes
    to the host's `identity.resolve_mention`, which resolves it on the invoking platform first and
    then through linked identities, answering ambiguous / not-found instead of guessing. The handle
    is resolved host-side only: never sent to a platform tool, logged or echoed.
    """
    if "{" in word or "}" in word or "\\" in word:
        tokens = identity.mention_tokens(word)
        if len(tokens) != 1 or word != "{user:" + tokens[0] + "}":
            raise ModUsageError("the first argument must be a username or an @mention")
        return tokens[0]
    if not _HANDLE_RE.fullmatch(word):
        raise ModUsageError("the first argument must be a username or an @mention")
    return word.removeprefix("@")


def parse(text: str) -> ModCommand:
    """Parse one moderation line (not `!modlog`) into a [`ModCommand`].

    Raises:
        ModUsageError: Any grammar violation; the message never echoes the input.
    """
    if len(text) > MAX_LINE_CHARS:
        raise ModUsageError("that command is too long")
    words = text.split()
    head = words[0].lower()
    action = head.removeprefix("!")
    if action not in _PAST:
        raise ModUsageError(USAGE)
    rest = words[1:]
    if not rest:
        raise ModUsageError(USAGE)
    token = _target_token(rest[0])
    rest = rest[1:]
    duration: int | None = None
    if action == "timeout":
        if not rest:
            raise ModUsageError("!timeout needs a duration, e.g. !timeout @user 10m")
        duration = parse_duration(rest[0])
        rest = rest[1:]
    platform = ALL
    if rest and rest[0].lower() in (*PLATFORMS, ALL):
        platform = rest[0].lower()
        rest = rest[1:]
    reason: str | None = None
    if action in ("ban", "timeout"):
        reason = _clean_reason(rest)
    elif rest:
        raise ModUsageError(f"unexpected extra text after !{action}")
    return ModCommand(action, token, platform, duration, reason)


def plan(cmd: ModCommand, platform: str, reason: str | None) -> Op | str:
    """Map (action, platform) to its tool call, or the "not supported" reason string.

    Per-platform argument shapes follow each service bundle's `exported_tools` schema (see
    `tests/fixtures/tools`). Durations over a platform's cap are clamped and noted on the Op.
    """
    action, user = cmd.action, "__user__"
    if platform == "twitch":
        if action == "ban":
            return Op(TWITCH, "twitch.moderation.ban", _with(user, reason=reason))
        if action in ("unban", "untimeout"):
            # Helix `unban` removes a ban OR an active timeout; there is no separate untimeout.
            return Op(TWITCH, "twitch.moderation.unban", {"user": user})
        if action == "timeout":
            return _timed(TWITCH, "twitch.moderation.timeout", "duration", 1_209_600, cmd, reason)
        if action == "purge":
            return Op(TWITCH, "twitch.moderation.timeout", {"user": user, "duration": 1})
    elif platform == "discord":
        simple = {
            "ban": "discord.moderation.ban",
            "unban": "discord.moderation.unban",
            "untimeout": "discord.moderation.untimeout",
            "kick": "discord.moderation.kick",
        }
        if action in simple:
            return Op(DISCORD, simple[action], {"user": user})
        if action == "timeout":
            return _timed(
                DISCORD, "discord.moderation.timeout", "duration_seconds", 2_419_200, cmd, None
            )
    elif platform == "youtube":
        if action == "ban":
            return Op(YOUTUBE, "youtube.chat.ban", {"user": user})
        if action in ("unban", "untimeout"):
            return Op(YOUTUBE, "youtube.chat.unban", {"user": user})
        if action == "timeout":
            return _timed(YOUTUBE, "youtube.chat.ban", "duration_seconds", 1_209_600, cmd, None)
    return f"!{action} is not supported on {platform}"


def _with(user: str, **optional: str | None) -> dict[str, Any]:
    """Args with `user` plus every non-None optional field."""
    args: dict[str, Any] = {"user": user}
    args.update({k: v for k, v in optional.items() if v is not None})
    return args


def _timed(app_id: str, name: str, field: str, cap: int, cmd: ModCommand, reason: str | None) -> Op:
    """A timeout-shaped Op with the duration clamped to the platform's cap."""
    wanted = cmd.duration or 0
    seconds = min(wanted, cap)
    args = _with("__user__", reason=reason)
    args[field] = seconds
    return Op(app_id, name, args, clamped_from=wanted if wanted > cap else None)


@dataclass(frozen=True, slots=True)
class Config:
    """Per-community settings: the default reason and the platforms `all` fans out to."""

    default_reason: str | None = None
    all_platforms: tuple[str, ...] = DEFAULT_ALL_PLATFORMS


async def load_config(community: str | None) -> Config:
    """Read the community config from kv; a corrupt blob is logged and replaced by defaults."""
    raw = await community_kv.get(community, _CONFIG_KEY)
    if raw is None:
        return Config()
    try:
        data = json.loads(raw.decode())
        reason = data.get("default_reason")
        plats = data.get("all_platforms", list(DEFAULT_ALL_PLATFORMS))
        if reason is not None and not (isinstance(reason, str) and len(reason) <= MAX_REASON_CHARS):
            raise ValueError("default_reason")
        if not (isinstance(plats, list) and plats and all(p in PLATFORMS for p in plats)):
            raise ValueError("all_platforms")
        return Config(reason, tuple(dict.fromkeys(plats)))
    except (ValueError, AttributeError, UnicodeDecodeError):
        log.error("moderation.config_corrupt", community_id=community)
        return Config()


def _idem_key(event_id: str, action: str, platform: str) -> str:
    """Retry-safe write key: stable per (event, action, platform), never contains a name."""
    return hashlib.sha256(f"moderation:{event_id}:{action}:{platform}".encode()).hexdigest()


def _failure_text(exc: tool.ToolError) -> str:
    """Per-platform, user-facing reason for a failed tool call (no handles, no internals)."""
    msg = exc.message.lower()
    if exc.code in ("not_granted", "forbidden"):
        return "refused (no permission, or the target is protected)"
    if exc.code == "invalid_args":
        if "link" in msg:
            return "no linked account"
        if any(w in msg for w in ("bound", "channel", "guild", "connect")):
            return "no bound channel"
        return "rejected"
    return "unavailable, try again"


async def _record(
    community: str | None,
    actor_uuid: str,
    target_uuid: str,
    cmd: ModCommand,
    platform: str,
    outcome: str,
    reason: str | None,
) -> None:
    """Emit the PII-free audit log line and append the same facts to the kv history ring."""
    log.info(
        "moderation.action",
        op=cmd.action,
        platform=platform,
        outcome=outcome,
        actor_uuid=actor_uuid,
        target_uuid=target_uuid,
        has_reason=reason is not None,
        community_id=community,
    )
    seq = await community_kv.increment(community, _SEQ_KEY, 1, 0)
    entry = {
        "seq": seq,
        "at": clock.now_millis(),
        "actor": actor_uuid,
        "target": target_uuid,
        "platform": platform,
        "action": cmd.action,
        "outcome": outcome,
        "reason": reason,
    }
    slot = seq % HISTORY_SLOTS
    await community_kv.set(
        community, f"moderation.hist.{slot}", json.dumps(entry).encode(), _HIST_TTL_S
    )


async def _resolve(cmd: ModCommand) -> tuple[str, str] | str:
    """Resolve (actor uuid, target uuid) host-side, or return the reply for a refusal."""
    try:
        actor_uuid = await identity.resolve_actor()
        target_uuid = await identity.resolve_mention(cmd.token)
    except identity.NotLinkedError:
        return "I can't act on that: an account involved isn't linked to the community"
    except identity.NotFoundError:
        return "user not found"
    except identity.AmbiguousError:
        return "that name matches more than one user -- @mention them to pick one"
    except identity.NotAMemberError:
        return "that user isn't a member of this community"
    except identity.DeniedError:
        return "moderation is not permitted for this community"
    if actor_uuid == target_uuid:
        return "you can't moderate yourself"
    return actor_uuid, target_uuid


def _targets(cmd: ModCommand, cfg: Config) -> list[str]:
    """The platforms to run: the one named, or every enabled platform for `all`."""
    return list(cfg.all_platforms) if cmd.platform == ALL else [cmd.platform]


async def run(
    cmd: ModCommand, payload: dict[str, Any], platform_of_event: str, community: str | None
) -> str:
    """Execute one parsed command: resolve host-side, fan out, audit, and build the one reply."""
    cfg = await load_config(community)
    resolved = await _resolve(cmd)
    if isinstance(resolved, str):
        return resolved
    actor_uuid, target_uuid = resolved
    reason = cmd.reason if cmd.reason is not None else cfg.default_reason
    event_id = event_dedupe.event_id_from_payload(payload, platform=platform_of_event)
    plats = _targets(cmd, cfg)
    done: list[str] = []
    failed: list[str] = []
    notes: list[str] = []
    unsupported: list[str] = []
    for plat in plats:
        planned = plan(cmd, plat, reason)
        if isinstance(planned, str):
            if cmd.platform == ALL:
                unsupported.append(plat)
            else:
                failed.append(f"!{cmd.action} is not supported on {plat}")
            await _record(community, actor_uuid, target_uuid, cmd, plat, "unsupported", reason)
            continue
        if planned.clamped_from is not None and cmd.platform != ALL:
            cap = planned.args.get("duration", planned.args.get("duration_seconds"))
            failed.append(f"{plat}: max timeout is {format_duration(int(cap))}")
            await _record(community, actor_uuid, target_uuid, cmd, plat, "refused_duration", reason)
            continue
        args = {**planned.args, "user": target_uuid}
        try:
            key = _idem_key(event_id, cmd.action, plat)
            await tool.invoke(planned.app_id, planned.tool, args, idempotency_key=key)
        except tool.ToolError as exc:
            text = _failure_text(exc)
            log.warn("moderation.tool_failed", tool=planned.tool, code=exc.code, platform=plat)
            failed.append(f"{plat}: {text}")
            await _record(community, actor_uuid, target_uuid, cmd, plat, exc.code, reason)
            continue
        done.append(plat)
        if planned.clamped_from is not None:
            cap = planned.args.get("duration", planned.args.get("duration_seconds"))
            notes.append(f"{plat} capped at {format_duration(int(cap))}")
        await _record(community, actor_uuid, target_uuid, cmd, plat, "ok", reason)
    return _summary(cmd, done, failed, notes, unsupported)


def _summary(
    cmd: ModCommand, done: list[str], failed: list[str], notes: list[str], unsupported: list[str]
) -> str:
    """One reply: successes first, then per-platform failures, caps, and unsupported platforms."""
    parts: list[str] = []
    verb = _PAST[cmd.action]
    if done:
        timed = cmd.duration is not None and cmd.action == "timeout"
        extra = f" for {format_duration(cmd.duration or 0)}" if timed else ""
        parts.append(f"{verb} on {', '.join(done)}{extra}")
    parts.extend(failed)
    if unsupported:
        parts.append(f"not supported on {', '.join(unsupported)}")
    if not parts:
        return "nothing to do: no platforms are enabled for `all`"
    text = "; ".join(parts)
    return f"{text} ({'; '.join(notes)})" if notes else text


async def modlog(count: int, community: str | None) -> str:
    """Render the most recent `count` history entries (uuids abbreviated, no reasons shown)."""
    head = await community_kv.get(community, _SEQ_KEY)
    if head is None:
        return "no moderation actions recorded yet"
    try:
        newest = int(head.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("moderation.history_corrupt", community_id=community)
        return "the moderation history is unavailable"
    lines: list[str] = []
    for seq in range(newest, max(newest - min(count, HISTORY_SLOTS), 0), -1):
        raw = await community_kv.get(community, f"moderation.hist.{seq % HISTORY_SLOTS}")
        if raw is None:
            continue
        try:
            e = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(e, dict) or e.get("seq") != seq:
            continue
        lines.append(
            f"#{seq} {e.get('action')} {e.get('platform')} {e.get('outcome')} "
            f"by {str(e.get('actor'))[:8]} on {str(e.get('target'))[:8]}"
        )
    return " | ".join(lines) if lines else "no moderation actions recorded yet"


def parse_modlog(text: str) -> int:
    """Parse `!modlog [n]` (default and cap [`MODLOG_MAX`])."""
    words = text.split()
    if len(words) > 2:
        raise ModUsageError("Usage: !modlog [n]")
    if len(words) == 1:
        return MODLOG_MAX
    if not (words[1].isascii() and words[1].isdigit()):
        raise ModUsageError("Usage: !modlog [n]")
    return max(1, min(int(words[1]), MODLOG_MAX))


async def handle(text: str, payload: dict[str, Any], platform: str, community: str | None) -> str:
    """Entry point from `transform`: parse + run one moderation line and return the reply text."""
    head = text.split()[0].lower()
    try:
        if head == "!modlog":
            return await modlog(parse_modlog(text), community)
        return await run(parse(text), payload, platform, community)
    except ModUsageError as exc:
        return sanitize_user_text(str(exc))
    except (event_dedupe.EventIdError, UnicodeError) as exc:
        log.error("moderation.event_id", error=type(exc).__name__, community_id=community)
        return "that command can't be processed right now"
