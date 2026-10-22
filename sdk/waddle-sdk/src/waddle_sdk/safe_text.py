r"""Output sanitizer for UNTRUSTED text a bundle echoes back into a chat reply.

A bundle that puts user-derived text into a reply (``!echo``, ``!say``, ``!translate``, a quote
read back out of kv, an LLM completion ...) hands that text to the host's egress pass, which
*acts on* two kinds of markup it finds in it:

1. **Egress tokens** -- ``core/egress_detokenizer`` (``src/lib.rs``, ``detokenize`` /
   ``extract_tokens``) substitutes every well-formed, unescaped ``{user:<token>}`` placeholder
   (``<token>`` = 1+ of ``[A-Za-z0-9_:.-]``) with a resolved display name. Untrusted text that
   carries one can forge a mention of any user and turns the reply into an identity-probe oracle
   (``{user:handle:<someone>}`` resolves, ``Unknown User`` does not).
2. **Platform mass-mention markup** -- ``@everyone`` / ``@here``, Discord ``<@id>`` /
   ``<@&role>`` / ``<#channel>``, Slack ``<!here>`` / ``<!channel>``, Twitch-style ``@name``
   -- which the platform turns into real pings. The host's Discord send path attaches no
   ``allowed_mentions`` allow-list, so nothing downstream stops them.

:func:`sanitize_user_text` neutralizes both, keeps ordinary text byte-identical and readable, is
idempotent, and is safe to wrap around *all* user-derived reply text. Call it LAST -- after every
transform (translate, reverse, case-mangling, LLM post-processing), immediately before the text
goes into the reply -- because a later transform can re-create markup the sanitizer removed::

    reply = f"{actor_label} says: {sanitize_user_text(user_text)}"

Only USER-SUPPLIED text goes through it. A ``{user:<token>}`` placeholder the bundle itself
emits for a legitimate mention is trusted bundle output -- build it separately and never pass it
through the sanitizer.

**How the token form is neutralized.** The host already defines the escape convention
(``core/svc_process/src/pii_tokenize.rs::escape_braces`` on the way in, the detokenizer's
``\{`` / ``\}`` / ``\\`` handling on the way out): a backslash-escaped brace is *never* a
placeholder, and the detokenizer removes the backslash again at the final send, so the reader
sees exactly the text the user typed. :func:`escape_egress_tokens` canonicalizes untrusted
text into that form -- every ``{``, ``}`` and ``\`` ends up inside a backslash pair -- without
re-escaping pairs the host already escaped on the way in, which keeps it idempotent and means
``event.payload["text"]`` (already host-escaped) is not double-escaped.

**How ``allow_tokens`` stays exact.** A placeholder is kept live only when it is, *in the text
exactly as given* and parsed with the detokenizer's own left-to-right escape handling, a whole
``{user:<token>}`` whose token is in the allow-list. The allowed spans are cut out BEFORE
anything else touches the text; everything between them is control-stripped and escaped on its
own, so no attacker character -- adjacent, nested, split by a control character, or glued on by
the stripping pass -- can complete, extend or forge a placeholder. A live placeholder resolves
to a display name the *mentioned user* controls, so the mention rules treat it as an opaque
wildcard: a sigil or half-written mention next to it (``<!{user:t}>``, ``me@e{user:t}``) is
defanged, and a word/``#``/``>`` right after it is separated by a zero-width space, because the
resolved name could otherwise complete a ping across the boundary.

**How mention markup is neutralized.** A zero-width space (U+200B) is inserted right after the
``@`` / ``<@`` / ``<#`` / ``<!`` that would make the platform parse a mention -- the same
technique ``discord.py``'s ``escape_mentions`` and ``discord.js``'s ``cleanContent`` use. The
text still reads identically; the platform no longer recognizes a mention. Inside a URL-shaped
span only ``@everyone`` / ``@here`` are defanged, so ``https://medium.com/@name`` keeps working.
That exemption covers a bare ``@name`` and nothing else: token forgery is escaped in URL spans
exactly as everywhere (the egress detokenizer has no URL awareness, so ``https://x/{user:<tok>}``
would resolve), and a span never extends across a live placeholder -- its display name can hold
whitespace, ``<`` and ``>``, so the rules around a placeholder apply even next to a URL.
Control characters (other than newline and tab) are stripped first, and newline and tab are
*seen through*: the host's send paths (``capabilities.rs::sanitize_irc_component``, its Python
twin in ``waddle_transports``) strip EVERY control character -- ``\n`` and ``\t`` included --
AFTER the detokenizer, so ``@<TAB>name`` / ``<!<NL>here>`` / ``@ev<TAB>eryone`` collapse into a
live mention on Twitch. Mention detection therefore runs on the text as written AND on the text as
the host will leave it (``\t\r\n`` removed), and the space goes in at the sigil in the written
text, which breaks both views.

**Completion leads.** A model completion is attacker-influenced and is relayed raw, so one that
STARTS with ``/`` or ``.`` (Twitch/IRC run it as the bot's own command) or ``!`` (re-triggers other
bots) would be executed by the platform, not read. :func:`guard_leading_command` is the one shared
guard every ``ai.generate`` bundle puts last in its reply path; it is separate from
:func:`sanitize_user_text` because it must look at the lead of the FINAL reply (an emoji or prefix
the bundle builds around the completion decides what leads), not at the completion alone.

Deliberately dependency-free (no ``wit_world`` / kv imports): pure text in, text out.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable

__all__ = [
    "COMMAND_LEADS",
    "SAFE_LEAD",
    "ZERO_WIDTH_SPACE",
    "defang_mentions",
    "escape_egress_tokens",
    "guard_leading_command",
    "leads_with_command",
    "sanitize_user_text",
    "strip_control_chars",
]

#: Inserted after a mention sigil so the platform no longer parses it as a mention. Zero-width,
#: not a control character (Unicode category Cf), so every send path in ``core/svc_action``
#: (``sanitize_irc_component`` strips only ``char::is_control`` / category Cc) preserves it.
ZERO_WIDTH_SPACE = "\u200b"

#: Twitch/IRC treat a leading ``/`` or ``.`` as a chat command and ``!`` re-triggers other bots.
COMMAND_LEADS = ("/", ".", "!")

#: Visible prefix put in front of a reply that would otherwise START with a :data:`COMMAND_LEADS`
#: character, so no platform can parse it as a command. Not a control character (the host strips
#: only those), so it survives every send path.
SAFE_LEAD = "\U0001f4ac "

#: Mirrors ``core/egress_detokenizer/src/lib.rs::TOKEN_PREFIX``.
_PLACEHOLDER_OPEN = "{user:"

#: Mirrors ``core/egress_detokenizer/src/lib.rs::is_token_char``: ASCII alphanumerics plus
#: ``-``, ``_``, ``:``, ``.`` -- NOT ``str.isalnum()`` (which accepts non-ASCII digits/letters).
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.")

#: The three characters the detokenizer treats as escapable after a backslash
#: (``detokenize``'s ``matches!(chars[i + 1], '{' | '}' | '\\')``).
_ESCAPABLE = frozenset("{}\\")

_SPECIAL_RE = re.compile(r"[{}\\]")

#: The only characters that can start something the detokenizer acts on: an escape pair or a
#: placeholder. ``}`` never starts anything, so the allow-list scan skips it.
_OPENER_RE = re.compile(r"[{\\]")

#: Cc controls except ``\n`` and ``\t``: C0 (U+0000-U+001F), DEL, C1 (U+0080-U+009F).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

#: Characters the host strips from the FINAL text after the detokenizer (every Cc control on
#: the Twitch send path, ``\n`` and ``\t`` included). :func:`strip_control_chars` already removes
#: the rest, so after it only these can sit between two characters the platform will see joined.
_HOST_STRIPPED = "\t\r\n"
_HOST_STRIPPED_RE = re.compile(r"[\t\r\n]")

#: Internal stand-in for ONE live (allow-listed) placeholder while mentions are defanged. NUL is
#: safe as a marker because :func:`strip_control_chars` removes every NUL from untrusted text
#: before a marker is ever placed (and :func:`defang_mentions` drops NULs from its input), so
#: attacker text can neither forge nor swallow a marker. The marker stands for an opaque display
#: name the mentioned user controls: it is treated as the start of a name (``\w``-like) for
#: what precedes it and as a possible name tail for what follows it.
_LIVE = "\x00"

#: A URL-shaped span. Inside one, only the mass mentions are defanged: ``https://medium.com/@name``
#: and ``https://host/@user/page`` are links, and a zero-width space would break them. The span
#: STOPS at a live-placeholder marker: the display name it stands for is user-controlled and can
#: hold whitespace, ``<`` and ``>`` -- exactly what ends a URL -- so nothing on the far side of a
#: marker is known to still be inside the link (``ab://{P}123>`` + a name ``<@`` is ``<@123>``).
_URL_RE = re.compile(r"(?i:\b[a-z][a-z0-9+.-]{1,15}://[^\s<>\x00]+)")

#: ``@everyone`` / ``@here`` in any case and anywhere (Discord matches the substring, so
#: ``hi@everyone`` is live too) -- plus ``@`` followed by name characters and then a live
#: placeholder (``hi@eve{user:t}`` plus a display name ``ryone`` is ``hi@everyone``). Applies
#: everywhere, URL spans included.
_MASS_MENTION_RE = re.compile(r"@(?=(?i:everyone|here)|[\w\x00]*\x00)")

#: Mention rules that do NOT apply strictly inside a URL span. Each match's end is where the
#: zero-width space goes. Every lookahead fails on a second pass because the inserted space is
#: neither a word character nor a marker -- which is what makes the whole pass idempotent.
_SIGIL_RULES: tuple[re.Pattern[str], ...] = (
    # A mention at a word boundary: Twitch ``@name``, Discord/Slack ``<@id>`` ``<@!id>``
    # ``<@&role>`` (the ``@`` follows ``<``, a non-word char), or ``@`` + a placeholder.
    # ``user@host`` (email-shaped, ``@`` preceded by a word char) is left alone -- but an
    # ``@`` right after a live placeholder is NOT: the display name may end in a non-word
    # character, which leaves a bare ``@name`` behind it.
    re.compile(r"(?<!\w)@(?=[\w!&{\x00])"),
    # Discord channel mention ``<#id>`` and Slack broadcast ``<!here>`` / ``<!channel>`` /
    # ``<!subteam^id>``: the space goes after the sigil, also when a live placeholder follows
    # it (``<!{user:t}>`` + a display name ``here`` is ``<!here>``) or when ``<`` alone is
    # followed by one (the display name could open with ``#`` / ``!``).
    re.compile(r"(?<=<[#!])(?=[\w\x00])|(?<=<)(?=\x00)"),
    # What follows a live placeholder can finish a mention the display name started
    # (``<#`` + ``123>``, ``@`` + ``bob``): break the join before a word character, ``#``, ``>``,
    # another placeholder, or ``!`` / ``&`` + word character or placeholder (``<@`` + ``&`` +
    # ``123>`` across two names). Plain punctuation and spaces after a name (the normal case)
    # are left byte-identical.
    re.compile(r"(?<=\x00)(?=[\w#>\x00]|[!&][\w\x00])"),
)


def _require_str(value: object, what: str) -> None:
    """Raise ``TypeError`` for a non-``str`` -- never coerce, never let ``None`` slip through."""
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, got {type(value).__name__}")


def strip_control_chars(text: str) -> str:
    r"""Remove C0/C1 control characters from ``text``, keeping ``\n`` and ``\t``.

    The host strips every control character on the Twitch path AFTER the bundle runs, so
    mention defanging must be decided on the text as it will look once they are gone.
    """
    _require_str(text, "text")
    return _CONTROL_RE.sub("", text)


def _coerce_allowed(allow_tokens: Iterable[str]) -> frozenset[str]:
    """Validate ``allow_tokens`` and return the tokens that can ever match a placeholder.

    A bare ``str`` / ``bytes`` is rejected: iterating it yields single characters, which would
    silently allow-list ``{user:a}``-style one-character tokens instead of the intended token.
    Entries that are not valid tokens (empty, or holding a character outside
    ``[A-Za-z0-9_:.-]``) can never equal the token of a well-formed placeholder, so they are
    dropped -- fail-closed, nothing about them can become live.
    """
    if isinstance(allow_tokens, str | bytes | bytearray):
        raise TypeError(
            "allow_tokens must be an iterable of token strings, not a bare "
            f"{type(allow_tokens).__name__} (wrap a single token in a list)"
        )
    valid: set[str] = set()
    for token in allow_tokens:
        _require_str(token, "allow_tokens entry")
        if token and all(ch in _TOKEN_CHARS for ch in token):
            valid.add(token)
    return frozenset(valid)


def _placeholder_end(text: str, start: int, allowed: frozenset[str] | None) -> int:
    """Return the index just past a live ``{user:<token>}`` at ``start``, else ``-1``.

    Matches exactly what ``egress_detokenizer::detokenize`` would substitute: the literal prefix,
    a non-empty run of token characters, then ``}``. ``allowed`` restricts it to those exact
    whole tokens (``None`` accepts every well-formed one).
    """
    if not text.startswith(_PLACEHOLDER_OPEN, start):
        return -1
    body_start = start + len(_PLACEHOLDER_OPEN)
    j = body_start
    n = len(text)
    while j < n and text[j] in _TOKEN_CHARS:
        j += 1
    if j == body_start or j >= n or text[j] != "}":
        return -1
    if allowed is not None and text[body_start:j] not in allowed:
        return -1
    return j + 1


def _split_live(text: str, allowed: frozenset[str] | None) -> list[tuple[str, bool]]:
    r"""Cut ``text`` into ``(piece, is_live)`` runs: whole live placeholders vs everything else.

    Scans left to right with the detokenizer's own escape handling (a backslash followed by
    ``{``, ``}`` or ``\`` consumes both), so a placeholder behind an escape pair is never live.
    The plain pieces are returned raw -- unstripped, unescaped -- and joined they give ``text``
    back. An empty ``allowed`` (nothing can be live) returns one plain piece.
    """
    if allowed is not None and not allowed:
        return [(text, False)]
    pieces: list[tuple[str, bool]] = []
    plain_start = 0
    i = 0
    n = len(text)
    while True:
        match = _OPENER_RE.search(text, i)
        if match is None:
            break
        j = match.start()
        if text[j] == "\\":
            # A well-formed escape pair is consumed whole; a lone backslash consumes itself.
            i = j + 2 if j + 1 < n and text[j + 1] in _ESCAPABLE else j + 1
            continue
        end = _placeholder_end(text, j, allowed)
        if end == -1:
            i = j + 1
            continue
        if j > plain_start:
            pieces.append((text[plain_start:j], False))
        pieces.append((text[j:end], True))
        plain_start = i = end
    if plain_start < n:
        pieces.append((text[plain_start:], False))
    return pieces


def _escape_plain(text: str) -> str:
    r"""Canonicalize ``text`` so every ``{``, ``}`` and ``\`` sits inside a backslash pair."""
    out: list[str] = []
    i = 0
    n = len(text)
    while True:
        match = _SPECIAL_RE.search(text, i)
        if match is None:
            out.append(text[i:])
            break
        j = match.start()
        out.append(text[i:j])
        ch = text[j]
        if ch == "\\":
            if j + 1 < n and text[j + 1] in _ESCAPABLE:
                out.append(text[j : j + 2])  # already a well-formed escape pair: keep verbatim
                i = j + 2
            else:
                out.append("\\\\")  # a lone backslash: pair it so it can't swallow what follows
                i = j + 1
        elif ch == "{":
            out.append("\\{")
            i = j + 1
        else:  # "}"
            out.append("\\}")
            i = j + 1
    return "".join(out)


def escape_egress_tokens(text: str, *, allow_tokens: Iterable[str] = ()) -> str:
    r"""Make ``text`` inert to the egress detokenizer (``{user:<token>}`` can no longer fire).

    Canonicalizes ``text`` so every ``{``, ``}`` and ``\`` sits inside a backslash pair
    (``\{``, ``\}``, ``\\``) -- the escape form the detokenizer never treats as a placeholder
    and un-escapes at the final send, so the reader still sees the original characters. Pairs
    already in that form (e.g. ``event.payload["text"]``, which the host escaped on the way in)
    are kept as-is, so the function is idempotent and never double-escapes.

    Args:
        text: Untrusted text destined for a chat reply.
        allow_tokens: Tokens whose ``{user:<token>}`` placeholder is kept LIVE (so it still
            resolves to a display name) -- ONLY for tokens the bundle has positively identified
            as host-minted mentions of the triggering message, e.g.
            ``identity.mention_tokens(event.payload["text"])`` applied to the text exactly as
            the host delivered it. Never feed it tokens derived from LLM/HTTP/kv text. Matching
            is exact and whole-token only: the text must already hold a complete, unescaped
            ``{user:<token>}`` for it to stay live; nothing is ever assembled from fragments.

    Returns:
        The escaped text.

    Raises:
        TypeError: ``text`` is not a ``str``, ``allow_tokens`` is a bare ``str``/``bytes`` (its
            characters would become one-character tokens), or holds a non-``str``.
    """
    _require_str(text, "text")
    allowed = _coerce_allowed(allow_tokens)
    return "".join(
        piece if is_live else _escape_plain(piece) for piece, is_live in _split_live(text, allowed)
    )


def _host_stripped_view(text: str) -> tuple[str, list[int]]:
    r"""Return ``text`` without ``\t\r\n`` and, per remaining char, its index in ``text``."""
    kept = [k for k, ch in enumerate(text) if ch not in _HOST_STRIPPED]
    return "".join(text[k] for k in kept), kept


def _insertion_points(view: str) -> set[int]:
    """Return the indexes in ``view`` before which a zero-width space must go."""
    spans = [(m.start(), m.end()) for m in _URL_RE.finditer(view)]
    starts = [start for start, _ in spans]
    points = {m.end() for m in _MASS_MENTION_RE.finditer(view)}
    for rule in _SIGIL_RULES:
        for match in rule.finditer(view):
            point = match.end()
            k = bisect_right(starts, point) - 1
            if k >= 0 and spans[k][0] < point < spans[k][1]:
                continue  # strictly inside a URL span: only the mass mentions apply there
            points.add(point)
    return points


def _defang_masked(text: str) -> str:
    r"""Defang mention markup in ``text``, where each ``_LIVE`` char is one live placeholder.

    Detection runs on the text as written (Discord keeps ``\n`` / ``\t``) AND on the text with
    ``\t\r\n`` removed (the Twitch path strips them after the detokenizer), each insertion
    landing right after the sigil in the written text -- which separates the sigil from the
    name in both views.
    """
    if _LIVE not in text and "@" not in text and "<" not in text:
        return text  # fast path: no mention sigil can be present
    points = _insertion_points(text)
    if _HOST_STRIPPED_RE.search(text):
        view, kept = _host_stripped_view(text)
        # A view point is always preceded by a sigil or marker, so ``p >= 1``.
        points.update(kept[p - 1] + 1 for p in _insertion_points(view))
    if not points:
        return text
    out: list[str] = []
    prev = 0
    for point in sorted(points):
        out.append(text[prev:point])
        out.append(ZERO_WIDTH_SPACE)
        prev = point
    out.append(text[prev:])
    return "".join(out)


def _restore_live(masked: str, live: list[str]) -> str:
    """Put the live placeholders back, in order, where ``_defang_masked`` kept their markers."""
    if not live:
        return masked
    pieces = masked.split(_LIVE)
    if len(pieces) != len(live) + 1:
        raise RuntimeError("live-placeholder markers were lost or forged")  # unreachable
    out = [pieces[0]]
    for placeholder, piece in zip(live, pieces[1:], strict=True):
        out.append(placeholder)
        out.append(piece)
    return "".join(out)


def defang_mentions(text: str) -> str:
    """Insert a zero-width space so the platform no longer parses ``text``'s mentions.

    Covers ``@everyone`` / ``@here`` (any case, even mid-word), Twitch-style ``@name`` and
    ``@role`` at a word boundary, Discord ``<@id>`` / ``<@!id>`` / ``<@&role>`` / ``<#channel>``
    and Slack ``<@U..>`` / ``<!here>`` / ``<!channel>``, including when the sigil and the name
    are separated by a newline or tab the host strips later. Email-shaped ``user@host`` is left
    untouched. A well-formed ``{user:<token>}`` in ``text`` is treated as a live placeholder
    resolving to an arbitrary display name. Idempotent. Expects control characters already
    stripped (:func:`strip_control_chars`) -- :func:`sanitize_user_text` does both in order; any
    NUL is dropped here because it is this module's internal placeholder marker.
    """
    _require_str(text, "text")
    if "@" not in text and "<" not in text and "{" not in text:
        return text  # fast path: no mention sigil and no placeholder can be present
    text = text.replace(_LIVE, "")
    masked: list[str] = []
    live: list[str] = []
    for piece, is_live in _split_live(text, None):
        if is_live:
            masked.append(_LIVE)
            live.append(piece)
        else:
            masked.append(piece)
    return _restore_live(_defang_masked("".join(masked)), live)


def sanitize_user_text(text: str, *, allow_tokens: Iterable[str] = ()) -> str:
    """Neutralize untrusted user-derived ``text`` for use inside a chat reply.

    Cuts out the allow-listed whole placeholders, strips control characters and escapes
    egress-token syntax in everything else (:func:`escape_egress_tokens`), then defangs
    mass-mention / mention markup (:func:`defang_mentions`). Ordinary text is returned
    unchanged; the result is idempotent (``f(f(x)) == f(x)``) and unicode-safe. Call it last,
    on user-supplied text only -- never on a ``{user:<token>}`` placeholder the bundle emits
    itself. See the module docstring for the full contract and the ``allow_tokens`` caveat.

    Raises:
        TypeError: ``text`` is not a ``str`` (a ``None`` payload field must be handled by the
            caller, never silently coerced into the reply), or ``allow_tokens`` is a bare
            ``str``/``bytes`` or holds a non-``str``.
    """
    _require_str(text, "text")
    allowed = _coerce_allowed(allow_tokens)
    masked: list[str] = []
    live: list[str] = []
    for piece, is_live in _split_live(text, allowed):
        if is_live:
            masked.append(_LIVE)
            live.append(piece)
        else:
            masked.append(_escape_plain(strip_control_chars(piece)))
    return _restore_live(_defang_masked("".join(masked)), live)


def leads_with_command(text: str) -> bool:
    """Return True if the first VISIBLE character of ``text`` is a chat-command lead.

    "Visible" mirrors what the host's send path leaves behind: control (``Cc``) and format
    (``Cf``: zero-width, bidi, Unicode tag characters) code points are dropped and leading
    whitespace skipped before the lead is tested, so a ``/ban`` hidden behind a zero-width space
    or a NUL still counts as leading with ``/`` exactly as the platform will see it.
    """
    _require_str(text, "text")
    visible = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    return visible.lstrip().startswith(COMMAND_LEADS)


def guard_leading_command(text: str) -> str:
    """Return ``text`` unchanged, or prefixed with :data:`SAFE_LEAD` if it would lead a command.

    Call it LAST on the complete reply of any bundle that relays model output (every
    ``ai.generate`` bundle), after :func:`sanitize_user_text` and after any emoji/label the bundle
    puts in front of the completion -- the guard has to see what actually leads the final text.
    It is idempotent (a guarded reply leads with :data:`SAFE_LEAD`, which is not a command lead)
    and leaves every reply that already leads safely byte-identical.

    Raises:
        TypeError: ``text`` is not a ``str``.
    """
    return SAFE_LEAD + text if leads_with_command(text) else text
