"""Chat Guard: configurable spam / content filters that delete the offending message and warn.

Original Waddles implementation (rules, text and code written fresh for this bundle).

Filters (each default OFF per community, enabled by a moderator): `blacklist` (whole-word /
phrase match), `links` (URLs, bare domains, IPv4 literals -- a `permit` lifts it for one user),
`caps` (excessive CAPITAL percentage) and `symbols` (excessive punctuation/symbol/emoji
percentage). Broadcasters and moderators (`is_mod is True` / `is_broadcaster is True`, strict --
a string or integer badge is NOT a moderator) are exempt from every filter.

Commands (moderators/broadcasters only; the grammar is `waddle_sdk.command`'s):

- `!chatguard enable|disable <blacklist|links|caps|symbols>`
- `!chatguard set caps|capsmin|symbols|symbolsmin|permit|warn <n>`
- `!chatguard add|remove blacklist <word or phrase>`, `!chatguard list`, `!chatguard reset`
- `!permit <user> [seconds]` (also `!chatguard permit ...`): the user may post links for that long.
  `permit` is not a `parse_command` shape (its first argument is a mention), so it has its own
  small parser; everything else goes through `parse_command`.

**Moderation capability (verified against the host, not assumed).** The only moderation op the
host implements is `chat.delete` on **Discord**: `relay.push("discord", {"op": "chat.delete", ...})`
under the `chat.delete:discord` permission (`core/svc_action/src/capabilities.rs`
`relay_op_supported` / `handle_discord_queued_op`; the host confirms the delete with the platform
before `relay.push` returns and derives the channel from the triggering event itself). Twitch
`chat.delete` is authorized-then-refused (`unsupported_op`; `core/svc_ingest/src/outbound_ops.rs`
still has the Helix client as a STUB), so on Twitch chatguard can only WARN -- it never requests
a capability that cannot run. **There is no timeout capability anywhere**: the relay ops are
exactly `chat.send` / `chat.delete` / `dm.send`, the gate catalog's `moderation.<platform>` family
has no WIT import and no host handler (`wit/waddle-bundle/stage.wit` lists moderation as future
`stage-next` work), so a timeout is a host follow-up and is deliberately NOT invented here.

**Where the code runs.** `identity` is served by the PROCESS stage only and `relay` by the ACTION
stage only, so `transform` decides (config, permit, rule evaluation; it also resolves the
offender to a pseudonym) and emits a `violation` event carrying only the rule name, the platform
message id and the pseudonym -- never the message text -- and `dispatch` enforces (delete, then a
short warning limited by a per-user cooldown `kv.increment`). A failed delete is logged and the
warning still goes out (worded without claiming a removal), then `dispatch` raises so the failure
is visible instead of swallowed; a redelivery retries the delete but the warn cooldown stops a
duplicate warning. No per-event marker is minted: a spammer would fill the 10,000-key kv quota
with them, so the delete is relied on to be idempotent and only the per-user warning keys exist.

**Privacy.** Logs carry only `op` / `rule` / `action` / `platform` / `community_id` / error-class
fields -- NEVER the offending message, the actor, a handle or a message id. kv keys use
`sha256(community:uuid)` pseudonyms (`chatguard.permit.<p>`, `chatguard.warn.<p>`) plus the one
`chatguard.config` blob, all `.`-separated (never `:`); permit and warn keys are TTL'd.
Activation-gated only -- no feature flag. A corrupt config blob is logged at ERROR and treated as
"no rules enabled" (a poisoned value must not turn every chat line into a transform failure); a
moderator's next config command overwrites it.

**Output safety.** Replies only carry integers, static text and the host-minted `{user:<token>}`
placeholder (validated, then kept live through `waddle_sdk.safe_text.sanitize_user_text`'s
allow-list); blacklist words are never echoed; grammar errors are never echoed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from waddle_sdk import clock, community_kv, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

#: Rule names in evaluation order (first match wins).
RULES = ("blacklist", "links", "caps", "symbols")
SPEC = CommandSpec(name="chatguard", sub_modules=frozenset(RULES))

#: Platforms whose host sender implements `chat.delete`. Add "twitch" (and its manifest
#: permission) only once the host's Helix client lands -- see the module docstring.
DELETE_PLATFORMS = frozenset({"discord"})

DEFAULT_CAPS_PERCENT = 70
DEFAULT_CAPS_MIN = 10
DEFAULT_SYMBOLS_PERCENT = 60
DEFAULT_SYMBOLS_MIN = 10
DEFAULT_PERMIT_SECONDS = 60
PERMIT_MAX_SECONDS = 600
DEFAULT_WARN_SECONDS = 30
WARN_MAX_SECONDS = 3600
MAX_WORDS = 50
MIN_WORD_LEN = 2
MAX_WORD_LEN = 40
#: Longest message the filters analyse IN FULL. No platform delivers more (Twitch 500, Discord 2000,
#: 4000 with Nitro, 4096 for an embed), so a longer line is not analysed at all and is treated as
#: breaking the first enabled rule -- the filters never look at a PREFIX and wave the tail through
#: (padding a message past a cut-off would hide a link / word / caps behind it).
MAX_ANALYSIS_CHARS = 4096

_CONFIG_KEY = "chatguard.config"
_CONFIG_VERSION = 1
_ANON_PSEUDONYM = "anon"
_DROPPED_CATEGORIES = frozenset({"Mn", "Cf", "Cc"})
#: Characters a platform renders as BLANK but that are letters/symbols to Unicode (so the category
#: test above misses them): Hangul choseong/jungseong/halfwidth fillers and the Braille blank.
#: Left in, they dilute the caps / symbol percentage or split a blacklisted word.
_INVISIBLE_FILLERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800)))
#: Longest run a URL is exempted from the caps / symbol analysis for. A URL is `\S+`, so an
#: unbounded exemption would let `https://` + 1900 characters of caps/punctuation vanish from the
#: statistics; past this length the rest of the "URL" is counted like any other text.
_URL_EXEMPT_MAX = 200

#: `set <key> <n>` keys -> (GuardConfig field, minimum, maximum).
_SETTINGS: dict[str, tuple[str, int, int]] = {
    "caps": ("caps_percent", 1, 100),
    "capsmin": ("caps_min", 1, 200),
    "symbols": ("symbols_percent", 1, 100),
    "symbolsmin": ("symbols_min", 1, 200),
    "permit": ("permit_seconds", 1, PERMIT_MAX_SECONDS),
    "warn": ("warn_seconds", 0, WARN_MAX_SECONDS),
}

_USAGE = (
    "Usage: !chatguard enable|disable <blacklist|links|caps|symbols> | set "
    "<caps|capsmin|symbols|symbolsmin|permit|warn> <n> | add|remove blacklist <phrase> | list | "
    "reset | !permit <user> [seconds]"
)
_PERMIT_USAGE = "Usage: !permit <@user> [seconds] -- let that user post links for a while"
_DENIED_MSG = "only moderators/broadcasters can configure chatguard"
_UNAVAILABLE_MSG = "chatguard is temporarily unavailable"

#: User-facing replies for the identity errors a permit target can legitimately raise.
_PERMIT_TARGET_ERRORS: dict[type[Exception], str] = {
    identity.NotFoundError: "I couldn't find that user in your message -- @mention them",
    identity.NotLinkedError: "that user hasn't linked their account, so a permit can't be tracked",
    identity.AmbiguousError: "that name matches more than one person -- mention them directly",
    identity.NotAMemberError: "that user isn't a member of this community",
}

_REASONS = {
    "blacklist": "that language isn't allowed here",
    "links": "links aren't allowed here without a permit",
    "caps": "please go easy on the caps",
    "symbols": "please go easy on the symbols and emotes",
}

#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an actor
#: that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")
_FALLBACK_ACTOR = "someone"
_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")
_PSEUDONYM_RE = re.compile(r"^[0-9a-f]{64}$")

#: Mirrors `core/egress_detokenizer/src/lib.rs`: a placeholder is `{user:` + 1..=256 token chars
#: (ASCII alphanumerics and `-_:.`) + `}`, and a backslash escapes exactly `{`, `}` and itself.
_PLACEHOLDER_OPEN = "{user:"
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.")
_ESCAPABLE = frozenset("{}\\")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_URL_EXEMPT_RE = re.compile(rf"(?i)\b(?:https?://|www\.)\S{{1,{_URL_EXEMPT_MAX}}}")
_CUSTOM_EMOTE_RE = re.compile(r"<a?:\w{2,32}:\d{15,25}>")
_TLDS = (
    "com net org io gg tv me co uk de fr ru ly be xyz info app dev gl cc us ca au nl pl es jp br "
    "tk ml ga cf gq top site online store shop club live link fun win vip cloud pw ws sx ai biz "
    "cn eu gift click icu"
).split()
#: A label is any-script letters/digits (`[^\W_]`), so an internationalised name such as
#: `evil` with a Cyrillic `i` -- which every platform linkifies -- is still a domain.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.-])(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+(?:"
    + "|".join(sorted(_TLDS, key=len, reverse=True))
    + r")(?![\w-])",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w])")
_DOT_OBFUSCATION_RE = re.compile(r"\s*[\[({]\s*(?:\.|dot)\s*[\])}]\s*", re.IGNORECASE)
_SPOKEN_DOT_RE = re.compile(
    r"(?<=\w)\s+dot\s+(?=(?:" + "|".join(sorted(_TLDS, key=len, reverse=True)) + r")\b)",
    re.IGNORECASE,
)
_DOT_LOOKALIKES = str.maketrans({"。": ".", "｡": "."})
#: Discord renders `evil\.com` as `evil.com` (a backslash escapes any ASCII punctuation), so the
#: link view drops a backslash that precedes punctuation.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\(?=[!-/:-@\[-`{-~])")


@dataclass(slots=True, frozen=True)
class GuardConfig:
    """One community's filter configuration (everything defaults to off / the stock thresholds)."""

    enabled: frozenset[str] = frozenset()
    caps_percent: int = DEFAULT_CAPS_PERCENT
    caps_min: int = DEFAULT_CAPS_MIN
    symbols_percent: int = DEFAULT_SYMBOLS_PERCENT
    symbols_min: int = DEFAULT_SYMBOLS_MIN
    permit_seconds: int = DEFAULT_PERMIT_SECONDS
    warn_seconds: int = DEFAULT_WARN_SECONDS
    words: tuple[str, ...] = ()

    def to_bytes(self) -> bytes:
        """Serialize to the compact JSON stored in kv."""
        return json.dumps(
            {
                "v": _CONFIG_VERSION,
                "on": sorted(self.enabled),
                "cp": self.caps_percent,
                "cm": self.caps_min,
                "sp": self.symbols_percent,
                "sm": self.symbols_min,
                "pm": self.permit_seconds,
                "ws": self.warn_seconds,
                "w": list(self.words),
            },
            separators=(",", ":"),
        ).encode()


def _config_from_bytes(raw: bytes) -> GuardConfig | None:
    """Parse a stored config; anything corrupt or out of range is `None` (never partially used)."""
    try:
        data = json.loads(raw.decode())
        enabled = frozenset(str(r) for r in data["on"])
        words = tuple(str(w) for w in data["w"])
        cfg = GuardConfig(
            enabled=enabled,
            caps_percent=int(data["cp"]),
            caps_min=int(data["cm"]),
            symbols_percent=int(data["sp"]),
            symbols_min=int(data["sm"]),
            permit_seconds=int(data["pm"]),
            warn_seconds=int(data["ws"]),
            words=words,
        )
        valid = (
            data["v"] == _CONFIG_VERSION
            and enabled <= frozenset(RULES)
            and 1 <= cfg.caps_percent <= 100
            and 1 <= cfg.caps_min <= 200
            and 1 <= cfg.symbols_percent <= 100
            and 1 <= cfg.symbols_min <= 200
            and 1 <= cfg.permit_seconds <= PERMIT_MAX_SECONDS
            and 0 <= cfg.warn_seconds <= WARN_MAX_SECONDS
            and len(words) <= MAX_WORDS
            and all(MIN_WORD_LEN <= len(w) <= MAX_WORD_LEN for w in words)
        )
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        log.debug("chatguard.config_parse_failed", error=type(exc).__name__)
        return None
    return cfg if valid else None


def _actor_label(actor: object) -> str:
    """Return a reply-safe label for `event.actor`.

    The host-minted `{user:<token>}` stays live so egress renders the display name. Anything else
    (a raw handle when tokenization is off, a missing actor) is untrusted text and goes through
    `sanitize_user_text` in full, so a platform display name can never forge a mention.
    """
    if not isinstance(actor, str) or not actor.strip():
        return _FALLBACK_ACTOR
    match = _USER_TOKEN_RE.fullmatch(actor)  # the WHOLE, unpadded actor: the pipeline adds none
    return sanitize_user_text(actor.strip(), allow_tokens=(match.group(1),) if match else ())


def _mention(token: str) -> str:
    """The live `{user:<token>}` placeholder for a host-minted token, else a neutral label."""
    if not _TOKEN_RE.fullmatch(token):
        return "that user"
    return sanitize_user_text("{user:" + token + "}", allow_tokens=(token,))


def _pseudonym(community: str | None, user_uuid: str) -> str:
    """Non-reversible per-(community, user) key component; never the raw uuid/handle."""
    scope = community if community is not None else community_kv.TENANT_WIDE_SENTINEL
    return hashlib.sha256(f"{scope}:{user_uuid}".encode()).hexdigest()


def _permit_key(pseudonym: str) -> str:
    """Per-(community, user) link-permit key (value: expiry epoch-ms; TTL = the permit)."""
    return f"chatguard.permit.{pseudonym}"


def _warn_key(pseudonym: str) -> str:
    """Per-(community, user) warning-cooldown key (an atomic counter with the cooldown as TTL)."""
    return f"chatguard.warn.{pseudonym}"


def _is_exempt(payload: dict[str, Any]) -> bool:
    """True only for a real `True` badge (strict `is True`; absent/stringy badges are NOT mods)."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _int_or_none(raw: bytes | None) -> int | None:
    """Decode a kv integer, or None if absent/corrupt (corrupt is logged, never raised)."""
    if raw is None:
        return None
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("chatguard.kv_value_corrupt", op="int")
        return None


def _is_invisible(ch: str) -> bool:
    """True for a character the platform renders as nothing or as a blank filler."""
    return ch in _INVISIBLE_FILLERS or unicodedata.category(ch) in _DROPPED_CATEGORIES


def _fold(text: str) -> str:
    """Canonical comparison form: NFKD, invisible chars dropped, casefolded, one space."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(
        " " if ch.isspace() else ch for ch in decomposed if ch.isspace() or not _is_invisible(ch)
    )
    return " ".join(kept.casefold().split())


def _placeholder_end(text: str, start: int) -> int | None:
    """Index just past the live `{user:<token>}` at `start`, or `None` if there is none there."""
    first = start + len(_PLACEHOLDER_OPEN)
    cursor = first
    limit = min(len(text), first + identity.MAX_MENTION_TOKEN_LEN)
    while cursor < limit and text[cursor] in _TOKEN_CHARS:
        cursor += 1
    if cursor == first or cursor >= len(text) or text[cursor] != "}":
        return None
    return cursor + 1


@lru_cache(maxsize=8)
def _plain_view(text: str) -> str:
    r"""The message as the platform renders it, with the host's own mention placeholders blanked.

    One left-to-right pass that reads the text exactly as the egress detokenizer does: a
    backslash escapes `{`, `}` and itself (the inbound pipeline escapes EVERY brace a user
    typed), and only an UNescaped `{user:<token>}` is a placeholder -- those are the host-minted
    mentions and become a space (their display name is not the author's text). Order matters: a
    typed `{user:evil.com}` arrives as `\{user:evil.com\}`, so un-escaping it first and
    stripping placeholders second would hide the author's own words from every filter.
    """
    out: list[str] = []
    cursor, size = 0, len(text)
    while cursor < size:
        char = text[cursor]
        if char == "\\" and cursor + 1 < size and text[cursor + 1] in _ESCAPABLE:
            out.append(text[cursor + 1])
            cursor += 2
            continue
        if char == "{" and text.startswith(_PLACEHOLDER_OPEN, cursor):
            end = _placeholder_end(text, cursor)
            if end is not None:
                out.append(" ")
                cursor = end
                continue
        out.append(char)
        cursor += 1
    return "".join(out)


def _strip_markup(text: str) -> str:
    """Rendered text minus mentions, and minus URLs up to `_URL_EXEMPT_MAX` chars each."""
    return _URL_EXEMPT_RE.sub(" ", _plain_view(text))


def _valid_ipv4(candidate: str) -> bool:
    """True for a dotted quad whose octets are all <= 255."""
    return all(int(part) <= 255 for part in candidate.split("."))


def has_link(text: str) -> bool:
    """Whether `text` contains a URL, a bare domain on a common TLD, or an IPv4 literal.

    Works on a de-obfuscated, folded view: zero-width/format characters are dropped, ideographic
    full stops and `[.]` / `(dot)` / ` dot com` spellings read as a dot, so the obvious evasions
    do not hide a link. It is a spam heuristic, not a URL parser.
    """
    folded = _MARKDOWN_ESCAPE_RE.sub("", _fold(_plain_view(text))).translate(_DOT_LOOKALIKES)
    folded = _DOT_OBFUSCATION_RE.sub(".", folded)
    folded = _SPOKEN_DOT_RE.sub(".", folded)
    if _URL_RE.search(folded) or _BARE_DOMAIN_RE.search(folded):
        return True
    return any(_valid_ipv4(m.group(1)) for m in _IPV4_RE.finditer(folded))


def _letters_view(text: str) -> str:
    """Text with placeholders, URLs and custom-emote tokens removed (caps / symbol analysis)."""
    return _CUSTOM_EMOTE_RE.sub(" ", _strip_markup(text))


def caps_stats(text: str) -> tuple[int, int]:
    """`(uppercase letters, all letters)` in `text`, ignoring mentions, URLs and custom emotes."""
    letters = [ch for ch in _letters_view(text) if ch.isalpha() and not _is_invisible(ch)]
    return sum(1 for ch in letters if ch.isupper()), len(letters)


def symbol_stats(text: str) -> tuple[int, int]:
    """`(symbol units, visible units)`: punctuation/symbols/emoji; a custom emote counts one."""
    stripped = _strip_markup(text)
    stripped, emotes = _CUSTOM_EMOTE_RE.subn(" ", stripped)
    visible = [ch for ch in stripped if not ch.isspace() and not _is_invisible(ch)]
    symbols = sum(1 for ch in visible if unicodedata.category(ch)[0] in "PS")
    return symbols + emotes, len(visible) + emotes


def caps_violation(text: str, cfg: GuardConfig) -> bool:
    """Too many capital letters: enough letters AND the uppercase share reaches the threshold."""
    upper, letters = caps_stats(text)
    return letters >= cfg.caps_min and upper * 100 >= cfg.caps_percent * letters


def symbols_violation(text: str, cfg: GuardConfig) -> bool:
    """Too many symbols/emoji: enough visible characters AND the symbol share reaches the limit."""
    symbols, total = symbol_stats(text)
    return total >= cfg.symbols_min and symbols * 100 >= cfg.symbols_percent * total


@lru_cache(maxsize=16)
def _blacklist_re(words: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the whole-word/phrase alternation for `words` (already folded, literal)."""
    alternatives = "|".join(r"\s+".join(re.escape(part) for part in word.split()) for word in words)
    return re.compile(rf"(?<![^\W_])(?:{alternatives})(?![^\W_])")


def blacklist_hit(text: str, words: tuple[str, ...]) -> bool:
    """Whether any blacklisted word/phrase occurs in `text` (folded, whole-word, multi-word ok)."""
    if not words:
        return False
    return _blacklist_re(words).search(_fold(_plain_view(text))) is not None


def normalize_word(raw: str) -> str | None:
    """The stored form of a blacklist entry, or `None` if it is unusable."""
    word = _fold(raw)
    if not MIN_WORD_LEN <= len(word) <= MAX_WORD_LEN:
        return None
    if "{" in word or "}" in word or not any(ch.isalnum() for ch in word):
        return None
    return word


async def _load_config(community: str | None) -> GuardConfig:
    """Read the community's config (1 kv op); absent or corrupt means no rules are enabled."""
    raw = await community_kv.get(community, _CONFIG_KEY)
    if raw is None:
        return GuardConfig()
    cfg = _config_from_bytes(raw)
    if cfg is None:
        log.error("chatguard.config_corrupt", op="load", community_id=community)
        return GuardConfig()
    return cfg


async def _save_config(community: str | None, cfg: GuardConfig) -> None:
    """Persist `cfg` with no expiry."""
    await community_kv.set(community, _CONFIG_KEY, cfg.to_bytes(), 0)


@dataclass(slots=True)
class _Offender:
    """Lazily resolves the message author to a community uuid / pseudonym (at most once)."""

    community: str | None
    _resolved: bool = False
    _user_uuid: str | None = None

    async def user_uuid(self) -> str | None:
        """The author's community uuid, or None when unlinked/unresolvable (fail-closed)."""
        if not self._resolved:
            self._resolved = True
            try:
                self._user_uuid = await identity.resolve_actor()
            except (identity.NotLinkedError, identity.NotAMemberError) as exc:
                log.debug("chatguard.actor_unlinked", error=type(exc).__name__)
            except identity.IdentityError as exc:
                log.error("chatguard.identity_error", op="actor", error=type(exc).__name__)
        return self._user_uuid

    async def pseudonym(self) -> str:
        """The author's key pseudonym, or the shared anonymous bucket when unresolvable."""
        user_uuid = await self.user_uuid()
        return _ANON_PSEUDONYM if user_uuid is None else _pseudonym(self.community, user_uuid)


async def _link_permitted(community: str | None, offender: _Offender) -> bool:
    """Whether the author holds an unexpired `permit` (kv TTL AND the stored expiry must agree)."""
    user_uuid = await offender.user_uuid()
    if user_uuid is None:
        return False
    raw = await community_kv.get(community, _permit_key(_pseudonym(community, user_uuid)))
    expires = _int_or_none(raw)
    return expires is not None and clock.now_millis() < expires


async def _first_violation(
    text: str, cfg: GuardConfig, community: str | None, offender: _Offender
) -> str | None:
    """The first enabled rule the message breaks, in `RULES` order, else None."""
    too_long = len(text) > MAX_ANALYSIS_CHARS
    for rule in RULES:
        if rule not in cfg.enabled:
            continue
        if rule == "blacklist":
            hit = bool(cfg.words) and (too_long or blacklist_hit(text, cfg.words))
        elif rule == "links":
            hit = (too_long or has_link(text)) and not await _link_permitted(community, offender)
        elif rule == "caps":
            hit = too_long or caps_violation(text, cfg)
        else:
            hit = too_long or symbols_violation(text, cfg)
        if hit:
            return rule
    return None


def _event_like(event: PlatformEvent, payload: dict[str, Any]) -> PlatformEvent:
    """A NEW event for the action stage: same platform/actor/stamp, bundle-built payload only."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


async def _guard_line(
    event: PlatformEvent, text: str, community: str | None
) -> PlatformEvent | None:
    """Evaluate one ordinary chat line; a `violation` event for dispatch, or None to let it pass."""
    if _is_exempt(event.payload):
        return None
    cfg = await _load_config(community)
    if not cfg.enabled:
        return None
    offender = _Offender(community)
    rule = await _first_violation(text, cfg, community, offender)
    if rule is None:
        return None
    channel_id = event.payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "chatguard enforcement requires a channel_id from the inbound chat.message"
        )
    message_id = event.payload.get("message_id")
    log.debug("chatguard.violation_detected", rule=rule, community_id=community)
    return _event_like(
        event,
        {
            "command": "violation",
            "rule": rule,
            "channel_id": channel_id,
            "message_id": message_id if isinstance(message_id, str) else None,
            "ps": await offender.pseudonym(),
            "warn_s": cfg.warn_seconds,
        },
    )


def _parse(text: str, spec: CommandSpec) -> ParsedCommand:
    """`parse_command`, but a grammar error is the static usage line, never an echo of the input."""
    try:
        return parse_command(text, spec)
    except CommandUsageError as exc:
        raise CommandUsageError(_USAGE) from exc


def _list_reply(cfg: GuardConfig, platform: str) -> str:
    """The configuration summary a moderator sees (blacklist words are counted, never shown)."""
    on = ", ".join(r for r in RULES if r in cfg.enabled) or "none"
    mode = "delete+warn" if platform in DELETE_PLATFORMS else "warn-only (no delete here)"
    return (
        f"chatguard rules on: {on} | caps >= {cfg.caps_percent}% (min {cfg.caps_min} letters) | "
        f"symbols >= {cfg.symbols_percent}% (min {cfg.symbols_min}) | permit {cfg.permit_seconds}s "
        f"| warn cooldown {cfg.warn_seconds}s | blacklist {len(cfg.words)} words | mode: {mode}"
    )


async def _cmd_set(tokens: list[str], community: str | None) -> str:
    """`set <key> <n>`: validate against the key's range and persist."""
    if len(tokens) != 2 or tokens[0].lower() not in _SETTINGS:
        return _USAGE
    field, low, high = _SETTINGS[tokens[0].lower()]
    raw = tokens[1]
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        return f"'{sanitize_user_text(raw[:32])}' isn't a whole number"
    value = int(raw)
    if not low <= value <= high:
        return f"{tokens[0].lower()} must be between {low} and {high}"
    cfg = await _load_config(community)
    changes: dict[str, Any] = {field: value}
    await _save_config(community, replace(cfg, **changes))
    return f"chatguard {tokens[0].lower()} set to {value}"


async def _cmd_blacklist(option: str, phrase: str | None, community: str | None) -> str:
    """`add|remove blacklist <phrase>`: the phrase is normalized and never echoed back."""
    word = normalize_word(phrase or "")
    if word is None:
        return f"blacklist entries must be {MIN_WORD_LEN}-{MAX_WORD_LEN} characters with a letter"
    cfg = await _load_config(community)
    words = list(cfg.words)
    if option == "add":
        if word in words:
            return f"already on the blacklist ({len(words)} words)"
        if len(words) >= MAX_WORDS:
            return f"the blacklist is full ({MAX_WORDS} words)"
        words.append(word)
        verb = "added"
    else:
        if word not in words:
            return f"not on the blacklist ({len(words)} words)"
        words.remove(word)
        verb = "removed"
    await _save_config(community, replace(cfg, words=tuple(words)))
    return f"{verb} -- blacklist now has {len(words)} words"


async def _cmd_permit(rest: str, community: str | None) -> str:
    """`!permit <user> [seconds]`: grant one mentioned user a time-boxed link permit."""
    tokens = identity.mention_tokens(rest)
    if len(tokens) != 1:
        return _PERMIT_USAGE
    token = tokens[0]
    leftover = rest.replace("{user:" + token + "}", " ", 1).split()
    cfg = await _load_config(community)
    seconds = cfg.permit_seconds
    if leftover:
        raw = leftover[0]
        if len(leftover) != 1 or not raw.isascii() or not raw.isdigit() or len(raw) > 6:
            return _PERMIT_USAGE
        seconds = int(raw)
        if not 1 <= seconds <= PERMIT_MAX_SECONDS:
            return f"permit seconds must be between 1 and {PERMIT_MAX_SECONDS}"
    try:
        target = await identity.resolve_mention(token)
    except identity.IdentityError as exc:
        message = _PERMIT_TARGET_ERRORS.get(type(exc))
        if message is None:
            log.error("chatguard.identity_error", op="mention", error=type(exc).__name__)
            return _UNAVAILABLE_MSG
        log.info("chatguard.permit_target_unresolved", error=type(exc).__name__)
        return message
    expires = clock.now_millis() + seconds * 1000
    await community_kv.set(
        community, _permit_key(_pseudonym(community, target)), str(expires).encode(), seconds
    )
    log.info("chatguard.permit_granted", op="permit", community_id=community)
    return f"{_mention(token)} may post links for {seconds}s."


async def _run_command(
    head: str, stripped: str, event: PlatformEvent, community: str | None
) -> str:
    """Run one `!chatguard` / `!permit` command and return the chat reply."""
    rest = stripped.partition(" ")[2].strip()
    first = rest.partition(" ")[0].lower()
    if head == "!chatguard" and not rest:
        return _USAGE
    if not _is_exempt(event.payload):
        log.info("chatguard.config_denied", op=head[1:], community_id=community)
        return _DENIED_MSG
    if head == "!permit":
        return await _cmd_permit(rest, community)
    if first == "permit":
        return await _cmd_permit(rest.partition(" ")[2], community)
    parsed = _parse(stripped, SPEC)
    option = parsed.option
    if option in ("enable", "disable") and parsed.sub_module is not None:
        cfg = await _load_config(community)
        if option == "enable":
            rules = cfg.enabled | {parsed.sub_module}
        else:
            rules = cfg.enabled - {parsed.sub_module}
        await _save_config(community, replace(cfg, enabled=frozenset(rules)))
        return f"chatguard {parsed.sub_module} {option}d"
    if option == "set":
        head_tokens = [parsed.sub_module] if parsed.sub_module else []
        return await _cmd_set(head_tokens + (parsed.args or "").split(), community)
    if option in ("add", "remove", "delete"):
        if parsed.sub_module == "blacklist":
            phrase: str | None = parsed.args
        elif parsed.sub_module is None:
            keyword, _, tail = (parsed.args or "").partition(" ")
            if keyword.lower() != "blacklist":
                return _USAGE
            phrase = tail
        else:
            return _USAGE
        return await _cmd_blacklist("add" if option == "add" else "remove", phrase, community)
    if option == "list" and parsed.sub_module is None and parsed.args is None:
        return _list_reply(await _load_config(community), event.platform)
    if option == "reset" and parsed.sub_module is None and parsed.args is None:
        await _save_config(community, GuardConfig())
        return "chatguard reset to defaults (all rules off, blacklist cleared)"
    return _USAGE


async def _screen(event: PlatformEvent, text: str, community: str | None) -> PlatformEvent | None:
    """`_guard_line`, with a kv/backend failure logged and re-raised (never a silent pass)."""
    try:
        return await _guard_line(event, text, community)
    except ValueError:
        raise
    except Exception as exc:
        log.error("chatguard.kv_error", op="guard", error=type(getattr(exc, "value", exc)).__name__)
        raise RuntimeError("chatguard state backend failed") from exc


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: handle commands, and screen ordinary chat lines.

    Returns a reply event for `!chatguard`/`!permit`, a `violation` event (rule + message id +
    pseudonym, never the text) for a line that breaks an enabled rule, and `None` for everything
    else (including every message from a moderator/broadcaster and any unconfigured community).
    A non-moderator's `!chatguard ...` / `!permit ...` line is still a chat line the author
    posted, so it is screened BEFORE the command is looked at: otherwise prefixing any message
    with `!permit` would turn it into a "permission denied" reply and skip every filter.

    Raises:
        ValueError: A reply/violation had no `channel_id` to answer in.
        RuntimeError: The kv backend failed while screening a line (an ERROR log is emitted
            first -- never a silent pass).
    """
    raw = event.payload.get("text")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    community = get_bundle_context().community
    head = stripped.partition(" ")[0].lower()
    if head in ("!chatguard", "!permit"):
        channel_id = event.payload.get("channel_id")
        if not channel_id:
            raise ValueError("chatguard reply requires a channel_id from the inbound chat.message")
        flagged = await _screen(event, stripped, community)
        if flagged is not None:
            return flagged
        try:
            reply = await _run_command(head, stripped, event, community)
        except CommandUsageError as exc:
            reply = str(exc)
        except Exception as exc:
            log.error(
                "chatguard.kv_error", op=head[1:], error=type(getattr(exc, "value", exc)).__name__
            )
            reply = _UNAVAILABLE_MSG
        return _event_like(event, {"command": "reply", "text": reply, "channel_id": channel_id})
    return await _screen(event, stripped, community)


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `defuse`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _warn_text(username: str, rule: str, deleted: bool) -> str:
    """The short warning: it claims a removal only when the delete was confirmed."""
    reason = _REASONS[rule]
    if deleted:
        return f"{username}, your message was removed: {reason}."
    return f"{username}, heads up: {reason}."


async def _delete_message(
    provider: str, payload: dict[str, Any], channel_id: str, community: str | None, rule: str
) -> bool | None:
    """Delete the message where the host can: True/False = confirmed/failed, None = n/a."""
    if provider not in DELETE_PLATFORMS:
        log.info("chatguard.enforce_unsupported", op="delete", platform=provider, rule=rule)
        return None
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or not _SNOWFLAKE_RE.fullmatch(message_id):
        log.error("chatguard.no_message_id", rule=rule, community_id=community)
        return False
    try:
        await relay.push(
            provider,
            {
                "v": 1,
                "platform": provider,
                "op": "chat.delete",
                "channel": channel_id,
                "message_id": message_id,
            },
        )
    except Exception as exc:
        log.error(
            "chatguard.delete_failed",
            rule=rule,
            community_id=community,
            exc=type(getattr(exc, "value", exc)).__name__,
        )
        return False
    return True


async def _warn_allowed(community: str | None, payload: dict[str, Any]) -> bool:
    """Per-user warning cooldown: one atomic `increment`, exactly one caller per window sees 1."""
    warn_s = payload.get("warn_s")
    seconds = warn_s if isinstance(warn_s, int) and 0 <= warn_s <= WARN_MAX_SECONDS else 0
    ps = payload.get("ps")
    pseudonym = ps if isinstance(ps, str) and _PSEUDONYM_RE.fullmatch(ps) else _ANON_PSEUDONYM
    if seconds == 0:
        return True
    return await community_kv.increment(community, _warn_key(pseudonym), 1, seconds) == 1


async def _enforce(
    envelope: StageEnvelope, payload: dict[str, Any], channel_id: str
) -> DispatchResult:
    """Delete the offending message (where possible), warn once per cooldown, then fail loud."""
    rule = payload.get("rule")
    if not isinstance(rule, str) or rule not in RULES:
        raise ValueError(f"unrecognized chatguard rule: {rule!r}")
    provider = envelope.event.platform
    community = envelope.community
    deleted = await _delete_message(provider, payload, channel_id, community, rule)
    warned = await _warn_allowed(community, payload)
    if warned:
        text = _warn_text(_actor_label(envelope.event.actor), rule, deleted is True)
        await relay.push(provider, {"channel": channel_id, "text": text})
    action = "warn_only" if deleted is None else ("delete" if deleted else "delete_failed")
    log.info(
        "chatguard.enforced",
        rule=rule,
        action=action,
        warned=warned,
        platform=provider,
        community_id=community,
    )
    if deleted is False:
        raise RuntimeError("chatguard could not delete the offending message")
    return DispatchResult(transport=provider, detail=action)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay a command reply, or enforce a violation.

    Raises:
        ValueError: No `channel_id`, no reply text, an unrecognized command or rule.
        RuntimeError: The offending message could not be deleted (after the warning was attempted).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("chatguard requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    provider = envelope.event.platform
    if command == "reply":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("chatguard reply requires non-empty text")
        await relay.push(provider, {"channel": channel_id, "text": text})
        log.info("chatguard.dispatch relayed", platform=provider)
        return DispatchResult(transport=provider, detail="relayed")
    if command == "violation":
        return await _enforce(envelope, payload, str(channel_id))
    raise ValueError(f"unrecognized chatguard command: {command!r}")
