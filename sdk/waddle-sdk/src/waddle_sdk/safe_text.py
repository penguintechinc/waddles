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

**How mention markup is neutralized.** A zero-width space (U+200B) is inserted right after the
``@`` / ``<@`` / ``<#`` / ``<!`` that would make the platform parse a mention -- the same
technique ``discord.py``'s ``escape_mentions`` and ``discord.js``'s ``cleanContent`` use. The
text still reads identically; the platform no longer recognizes a mention. Inside a URL-shaped
span only ``@everyone`` / ``@here`` are defanged, so ``https://medium.com/@name`` keeps working.
Control characters (other than newline and tab) are stripped first because the Twitch send path
strips them AFTER the bundle runs, which would otherwise let ``@<NUL>name`` collapse back into a
live ``@name``.

Deliberately dependency-free (no ``wit_world`` / kv imports): pure text in, text out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "ZERO_WIDTH_SPACE",
    "defang_mentions",
    "escape_egress_tokens",
    "sanitize_user_text",
    "strip_control_chars",
]

#: Inserted after a mention sigil so the platform no longer parses it as a mention. Zero-width,
#: not a control character (Unicode category Cf), so every send path in ``core/svc_action``
#: (``sanitize_irc_component`` strips only ``char::is_control`` / category Cc) preserves it.
ZERO_WIDTH_SPACE = "\u200b"

#: Mirrors ``core/egress_detokenizer/src/lib.rs::TOKEN_PREFIX``.
_PLACEHOLDER_OPEN = "{user:"

#: Mirrors ``core/egress_detokenizer/src/lib.rs::is_token_char``: ASCII alphanumerics plus
#: ``-``, ``_``, ``:``, ``.`` -- NOT ``str.isalnum()`` (which accepts non-ASCII digits/letters).
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.")

#: The three characters the detokenizer treats as escapable after a backslash
#: (``detokenize``'s ``matches!(chars[i + 1], '{' | '}' | '\\')``).
_ESCAPABLE = frozenset("{}\\")

_SPECIAL_RE = re.compile(r"[{}\\]")

#: Cc controls except ``\n`` and ``\t``: C0 (U+0000-U+001F), DEL, C1 (U+0080-U+009F).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Newlines are tolerated between an ``@`` and the name it introduces: the Twitch send path strips
# them after the bundle runs, which would otherwise rejoin ``@\nname`` into a live ``@name``.
_NL = r"[\r\n]*"

#: ``@everyone`` / ``@here`` in any case and anywhere (Discord matches the substring, so
#: ``hi@everyone`` is live too), possibly split from the name by newlines Twitch strips later.
_MASS_MENTION = rf"(?i:@(?={_NL}(?:everyone|here)))"

#: A URL-shaped span. Inside one, only the mass mentions are defanged: ``https://medium.com/@name``
#: and ``https://host/@user/page`` are links, and a zero-width space would break them.
_URL = r"(?i:\b[a-z][a-z0-9+.-]{1,15}://[^\s<>]+)"

#: One alternation. Every branch's lookahead/lookbehind fails on a second pass because the
#: inserted space is not a word character -- which is what makes the whole pass idempotent.
_DEFANG_RE = re.compile(
    # 0. A URL span: handled whole (see ``_defang_match``) so rule 2 cannot corrupt a link.
    rf"(?P<url>{_URL})"
    # 1. Mass mentions anywhere.
    rf"|{_MASS_MENTION}"
    # 2. A mention at a word boundary: Twitch ``@name``, Discord/Slack ``<@id>`` ``<@!id>``
    #    ``<@&role>`` (the ``@`` follows ``<``, a non-word char), or ``@`` + a placeholder.
    #    ``user@host`` (email-shaped, ``@`` preceded by a word char) is left alone.
    rf"|(?<!\w)@(?={_NL}[\w!&{{])"
    # 3. Discord channel mention ``<#id>`` and Slack broadcast ``<!here>`` / ``<!channel>`` /
    #    ``<!subteam^id>``: the space goes after the sigil.
    r"|(?<=<[#!])(?=\w)"
)
_MASS_MENTION_RE = re.compile(_MASS_MENTION)


def _defang_match(match: re.Match[str]) -> str:
    """Return the replacement for one ``_DEFANG_RE`` match: the match plus a zero-width space."""
    url = match.group("url")
    if url is not None:
        return _MASS_MENTION_RE.sub(lambda m: m.group(0) + ZERO_WIDTH_SPACE, url)
    return match.group(0) + ZERO_WIDTH_SPACE


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


def _placeholder_end(text: str, start: int, allowed: frozenset[str]) -> int:
    """Return the index just past an allow-listed ``{user:<token>}`` at ``start``, else ``-1``.

    Matches exactly what ``egress_detokenizer::detokenize`` would substitute: the literal prefix,
    a non-empty run of token characters, then ``}``.
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
    return j + 1 if text[body_start:j] in allowed else -1


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
            the host delivered it. Never feed it tokens derived from LLM/HTTP/kv text.

    Returns:
        The escaped text.

    Raises:
        TypeError: ``text`` is not a ``str``, or ``allow_tokens`` holds a non-``str``.
    """
    _require_str(text, "text")
    allowed = frozenset(allow_tokens)
    for token in allowed:
        _require_str(token, "allow_tokens entry")

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
            end = _placeholder_end(text, j, allowed) if allowed else -1
            if end == -1:
                out.append("\\{")
                i = j + 1
            else:
                out.append(text[j:end])  # allow-listed host-minted mention: stays live
                i = end
        else:  # "}"
            out.append("\\}")
            i = j + 1
    return "".join(out)


def defang_mentions(text: str) -> str:
    """Insert a zero-width space so the platform no longer parses ``text``'s mentions.

    Covers ``@everyone`` / ``@here`` (any case, even mid-word), Twitch-style ``@name`` and
    ``@role`` at a word boundary, Discord ``<@id>`` / ``<@!id>`` / ``<@&role>`` / ``<#channel>``
    and Slack ``<@U..>`` / ``<!here>`` / ``<!channel>``. Email-shaped ``user@host`` is left
    untouched. Idempotent. Expects control characters already stripped
    (:func:`strip_control_chars`) -- :func:`sanitize_user_text` does both in order.
    """
    _require_str(text, "text")
    if "@" not in text and "<" not in text:
        return text  # fast path: no mention sigil can be present
    return _DEFANG_RE.sub(_defang_match, text)


def sanitize_user_text(text: str, *, allow_tokens: Iterable[str] = ()) -> str:
    """Neutralize untrusted user-derived ``text`` for use inside a chat reply.

    Strips control characters, escapes egress-token syntax (:func:`escape_egress_tokens`) and
    defangs mass-mention / mention markup (:func:`defang_mentions`). Ordinary text is returned
    unchanged; the result is idempotent (``f(f(x)) == f(x)``) and unicode-safe. Call it last,
    on user-supplied text only -- never on a ``{user:<token>}`` placeholder the bundle emits
    itself. See the module docstring for the full contract and the ``allow_tokens`` caveat.

    Raises:
        TypeError: ``text`` is not a ``str`` (a ``None`` payload field must be handled by the
            caller, never silently coerced into the reply).
    """
    escaped = escape_egress_tokens(strip_control_chars(text), allow_tokens=allow_tokens)
    return defang_mentions(escaped)
