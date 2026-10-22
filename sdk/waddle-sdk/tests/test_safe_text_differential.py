r"""Differential test: `waddle_sdk.safe_text` against the host's egress parsers.

`sanitize_user_text` is the anti-injection primitive for every bundle that echoes user text, so
the property it must hold is stated against the HOST, not against itself:

* **Token parser differential** -- no input may be "safe per the sanitizer" yet "resolved per
  `core/egress_detokenizer`". An independent line-for-line Python port of the detokenizer
  (`oracle_occurrences` / `oracle_render`) is the oracle; it is pinned to the real Rust by a
  digest both sides must reproduce (`test_the_oracle_is_pinned_to_the_real_detokenizer` here,
  `grammar_matches_the_sanitizer_parity_pin` in `core/egress_detokenizer/src/lib.rs`, one shared
  file) so it cannot drift silently.
* **Allow-list exactness** -- with `allow_tokens`, the live placeholders in the output are
  exactly the allow-listed whole placeholders the detokenizer finds in the INPUT, in order:
  nothing is assembled from fragments, stripped-together, nested or split.
* **Post-send mention differential** -- after the host's own steps (detokenizer substitution of
  an adversarial display name, then on the Twitch path the strip of EVERY control character,
  `\t` and `\n` included -- `capabilities.rs::sanitize_irc_component`) no mention is left that
  the author's text helped form. A mention lying wholly inside a substituted display name is
  the sink escaper's job, not the sanitizer's, and is excluded.

The corpora are exhaustive over short sequences of hostile atoms plus seeded random strings;
`_violations` returns what broke, and `test_the_differential_can_fail` proves the checker flags
both a no-op sanitizer and a model of the pre-fix implementation (a gate that cannot fail is not
a gate).
"""

from __future__ import annotations

import html
import itertools
import random
import re
import string
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from waddle_sdk.safe_text import (
    ZERO_WIDTH_SPACE as ZW,
)
from waddle_sdk.safe_text import (
    sanitize_user_text,
)

# ---------------------------------------------------------------------------------------------
# Oracle: Python port of core/egress_detokenizer/src/lib.rs (detokenize / extract_tokens).
# ---------------------------------------------------------------------------------------------

_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_:.")  # lib.rs:153-155
_PREFIX = "{user:"  # lib.rs:148
_NEUTRAL = "Unknown User"  # lib.rs:39


@dataclass(slots=True, frozen=True)
class Occurrence:
    """One placeholder the detokenizer would substitute: ``text[start:end]`` holds ``token``."""

    start: int
    end: int
    token: str


def oracle_occurrences(text: str) -> list[Occurrence]:
    """Every placeholder the real detokenizer substitutes in ``text``, in order (lib.rs:166-209)."""
    found: list[Occurrence] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "{}\\":
            i += 2
            continue
        if c == "{" and text.startswith(_PREFIX, i):
            body = i + len(_PREFIX)
            j = body
            while j < len(text) and text[j] in _TOKEN_CHARS:
                j += 1
            if j < len(text) and text[j] == "}" and j > body:
                found.append(Occurrence(i, j + 1, text[body:j]))
                i = j + 1
                continue
        i += 1
    return found


def oracle_tokens(text: str) -> list[str]:
    """``extract_tokens`` (lib.rs:214-247): the distinct tokens, first-seen order."""
    tokens: list[str] = []
    for occurrence in oracle_occurrences(text):
        if occurrence.token not in tokens:
            tokens.append(occurrence.token)
    return tokens


def oracle_render(text: str, names: dict[str, str]) -> tuple[str, list[bool]]:
    """What ``detokenize`` emits, plus a per-character flag: True where it came from a name."""
    out: list[str] = []
    from_name: list[bool] = []
    occurrences = {o.start: o for o in oracle_occurrences(text)}
    i = 0
    while i < len(text):
        occurrence = occurrences.get(i)
        if occurrence is not None:
            name = names.get(occurrence.token, _NEUTRAL)
            out.extend(name)
            from_name.extend([True] * len(name))
            i = occurrence.end
            continue
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "{}\\":
            out.append(text[i + 1])
            from_name.append(False)
            i += 2
            continue
        out.append(c)
        from_name.append(False)
        i += 1
    return "".join(out), from_name


def _fnv1a64(data: bytes, state: int = 0xCBF29CE484222325) -> int:
    """FNV-1a 64 -- the digest the shared pin uses (no hashing dependency on the Rust side)."""
    for byte in data:
        state = ((state ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return state


_PIN = Path(__file__).resolve().parents[3] / "core/egress_detokenizer/tests/fixtures"
_PIN_FILE = _PIN / "sanitizer_parity.txt"


def _read_pin() -> tuple[list[str], int, list[str], str]:
    """Parse the shared pin file: (atoms, max_len, curated cases, expected digest)."""
    atoms: list[str] = []
    max_len = 0
    curated: list[str] = []
    digest = ""
    for line in _PIN_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key == "atoms":
            atoms = [bytes.fromhex(a).decode() for a in value.split(",")]
        elif key == "max_len":
            max_len = int(value)
        elif key == "case":
            curated.append(bytes.fromhex(value).decode())
        elif key == "fnv1a64":
            digest = value
        else:
            raise AssertionError(f"unknown pin key {key!r}")
    return atoms, max_len, curated, digest


def test_the_oracle_is_pinned_to_the_real_detokenizer() -> None:
    """The oracle reproduces the digest the REAL Rust grammar reproduces (same file, same corpus).

    `cargo test -p egress_detokenizer grammar_matches_the_sanitizer_parity_pin` asserts the Rust
    side; this asserts the Python side, so oracle == Rust on ~9.4k inputs by transitivity and a
    change to either grammar fails one of the two.
    """
    atoms, max_len, curated, want = _read_pin()
    corpus: list[str] = []
    for length in range(max_len + 1):
        corpus.extend("".join(t) for t in itertools.product(atoms, repeat=length))
    corpus.extend(curated)
    assert len(corpus) > 9000  # the corpus really was enumerated
    state = 0xCBF29CE484222325
    for text in corpus:
        tokens = oracle_tokens(text)
        rendered, _ = oracle_render(text, {t: f"<N:{t}>" for t in tokens})
        line = (
            f"{text.encode().hex()}|{','.join(t.encode().hex() for t in tokens)}"
            f"|{rendered.encode().hex()}\n"
        )
        state = _fnv1a64(line.encode(), state)
    assert f"{state:016x}" == want


# ---------------------------------------------------------------------------------------------
# Host steps after the bundle, and what a platform parses as a mention
# ---------------------------------------------------------------------------------------------


def _is_control(ch: str) -> bool:
    """Rust ``char::is_control`` (Unicode Cc) -- what the Twitch send path strips."""
    return ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F


def twitch_final(rendered: str, from_name: list[bool]) -> tuple[str, list[bool]]:
    r"""``sanitize_irc_component`` after the detokenizer: every Cc char (``\t`` ``\n`` too) goes."""
    kept = [k for k, ch in enumerate(rendered) if not _is_control(ch)]
    return "".join(rendered[k] for k in kept), [from_name[k] for k in kept]


#: What Discord / Slack / Twitch turn into a ping: mass mentions anywhere, ``<@id>`` ``<@!id>``
#: ``<@&role>`` ``<#chan>`` ``<!here>``, and a Twitch ``@name`` at a word boundary.
_MENTION = re.compile(r"@(?:everyone|here)|<@[!&]?\w|<#\w|<!\w|(?<!\w)@\w", re.IGNORECASE)

#: Worst-case display names for a live placeholder: the mentioned user controls this text, and
#: a sink may escape none of it. Fragments of every mention form, so the author's text next to
#: the placeholder could complete one.
_ADVERSARIAL_NAMES = (
    "everyone", "veryone", "here", "ere", "ryone", "<", "#123>", "123", "@", "<@", "<#", "<!",
    "!here>", "bob", "1>", "&123>", "e", "ve", "@everyone", "<@!", "y",
)  # fmt: skip


def _leaked_mentions(final: str, from_name: list[bool]) -> list[str]:
    """Mentions in ``final`` that are NOT wholly inside a substituted display name."""
    return [
        m.group(0)
        for m in _MENTION.finditer(final)
        if not all(from_name[k] for k in range(m.start(), m.end()))
    ]


Sanitizer = Callable[[str, frozenset[str]], str]


def _real(text: str, allowed: frozenset[str]) -> str:
    return sanitize_user_text(text, allow_tokens=allowed)


def _violations(sanitize: Sanitizer, texts: Iterable[str], allowed: frozenset[str]) -> list[str]:
    """Check every property on every text; return a description of each one broken."""
    broken: list[str] = []
    for text in texts:
        clean = sanitize(text, allowed)
        want = [o.token for o in oracle_occurrences(text) if o.token in allowed]
        got = [o.token for o in oracle_occurrences(clean)]
        if got != want:
            broken.append(f"live tokens {got} != allow-listed whole placeholders {want}: {text!r}")
        if [o.token for o in oracle_occurrences(html.escape(clean))] != got:
            broken.append(f"html-escaping first changes what resolves (overlay): {text!r}")
        if sanitize(clean, allowed) != clean:
            broken.append(f"not idempotent: {text!r}")
        live = sorted(set(got))  # every distinct live token gets every adversarial name
        assignments = [
            dict(zip(live, combo, strict=True))
            for combo in itertools.product(_ADVERSARIAL_NAMES, repeat=len(live))
        ]
        for assignment in assignments:
            rendered, from_name = oracle_render(clean, assignment)
            for sink, (final, flags) in (
                ("discord", (rendered, from_name)),
                ("twitch", twitch_final(rendered, from_name)),
            ):
                leaked = _leaked_mentions(final, flags)
                if leaked:
                    broken.append(f"{sink} mention {leaked} survives names={assignment}: {text!r}")
                    break
        if len(broken) >= 20:
            break
    return broken


# ---------------------------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------------------------

_EXHAUSTIVE_ATOMS = (
    "\\", "{", "}", "{user:a1}", "{user:b2}", "\n", "\t", "@", "<", "#", "!", "&", "e", "1", ">",
)  # fmt: skip

_RANDOM_ATOMS = (
    "\\", "{", "}", "{user:a1}", "{user:b2}", "{user:a1", "user:a1}", "{user:", "a1", "\n", "\t",
    "\x00", "\x1b", "\x85", "@", "<", "#", "!", "&", ">", "e", "ve", "ryone", "everyone", "here",
    "h", "ere", "1", "23", "é", " ", "\\\\", "\\{", "{{", "}}", ZW, "x", ".", "{user:a1\x00}",
    "{\x00user:a1}", "@\t", "<!\t", "\t#", "\n@", "{user:é}", "{user:١}",
)  # fmt: skip


def _exhaustive(max_len: int) -> Iterable[str]:
    for length in range(max_len + 1):
        for combo in itertools.product(_EXHAUSTIVE_ATOMS, repeat=length):
            yield "".join(combo)


def _random(count: int, seed: int) -> Iterable[str]:
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible test inputs, not security
    for _ in range(count):
        yield "".join(rng.choice(_RANDOM_ATOMS) for _ in range(rng.randint(0, 12)))


_NONE: frozenset[str] = frozenset()
_A1 = frozenset({"a1"})
_A1_B2 = frozenset({"a1", "b2"})


# ---------------------------------------------------------------------------------------------
# The differential itself
# ---------------------------------------------------------------------------------------------


def test_exhaustive_short_inputs_no_allow_list() -> None:
    """Every sequence of up to 4 hostile atoms: nothing the host resolves or pings survives."""
    texts = list(_exhaustive(4))
    assert len(texts) > 30000
    assert any(oracle_occurrences(t) for t in texts)  # the corpus really holds live forgeries
    assert _violations(_real, texts, _NONE) == []


def test_exhaustive_short_inputs_with_allow_list() -> None:
    """Same corpus with `a1` / `b2` allow-listed.

    Exactly their whole placeholders stay live, and no pair of hostile display names completes
    a ping across the author's text.
    """
    assert _violations(_real, _exhaustive(4), _A1_B2) == []


def test_random_hostile_inputs() -> None:
    """60k seeded hostile strings (control splices, nested/split placeholders, tab/NL gaps)."""
    texts = list(_random(30000, seed=20261022))
    assert _violations(_real, texts, _NONE) == []
    assert _violations(_real, texts, _A1) == []
    assert _violations(_real, texts, _A1_B2) == []  # two live tokens keep order and identity


# ---------------------------------------------------------------------------------------------
# The checker can fail: a no-op, and the sanitizer exactly as first shipped (9841e42a)
# ---------------------------------------------------------------------------------------------

_LEGACY_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LEGACY_NL = r"[\r\n]*"
_LEGACY_URL = r"(?i:\b[a-z][a-z0-9+.-]{1,15}://[^\s<>]+)"
_LEGACY_DEFANG = re.compile(
    rf"(?P<url>{_LEGACY_URL})"
    rf"|(?i:@(?={_LEGACY_NL}(?:everyone|here)))"
    rf"|(?<!\w)@(?={_LEGACY_NL}[\w!&{{])"
    r"|(?<=<[#!])(?=\w)"
)
_LEGACY_MASS = re.compile(rf"(?i:@(?={_LEGACY_NL}(?:everyone|here)))")


def _legacy_escape(text: str, allowed: frozenset[str]) -> str:
    """The pre-fix escaper: allow-list matched inline, on text whose controls were stripped."""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            pair = i + 1 < len(text) and text[i + 1] in "{}\\"
            out.append(text[i : i + 2] if pair else "\\\\")
            i += 2 if pair else 1
        elif c == "{":
            live = [o for o in oracle_occurrences(text[i:]) if o.start == 0 and o.token in allowed]
            if live:
                out.append(text[i : i + live[0].end])
                i += live[0].end
            else:
                out.append("\\{")
                i += 1
        elif c == "}":
            out.append("\\}")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _legacy_sanitize(text: str, allowed: frozenset[str]) -> str:
    """`sanitize_user_text` as first shipped: strip, then escape+allow-list, then defang."""

    def defang(match: re.Match[str]) -> str:
        url = match.group("url")
        if url is not None:
            return _LEGACY_MASS.sub(lambda m: m.group(0) + ZW, url)
        return match.group(0) + ZW

    return _LEGACY_DEFANG.sub(defang, _legacy_escape(_LEGACY_CONTROL.sub("", text), allowed))


def test_the_differential_can_fail() -> None:
    """A no-op and the first-shipped sanitizer are both flagged -- with the two known bugs named."""
    assert _violations(lambda text, _allowed: text, _exhaustive(3), _NONE)  # does nothing

    legacy = _violations(_legacy_sanitize, _exhaustive(4), _A1)
    assert legacy, "the checker must flag the first-shipped sanitizer"
    joined = "\n".join(legacy)
    assert "survives" in joined  # tab/newline gap + placeholder adjacency
    pinned = _violations(_legacy_sanitize, ["{user:a\x001}", "{\x00user:a1}"], _A1)
    assert any("live tokens" in v for v in pinned)  # control-char splice reconstructs a token
    gap = _violations(_legacy_sanitize, ["@\tbob", "<!\there>", "<#\n123>"], _NONE)
    assert len(gap) == 3  # the host strips the tab/newline after the detokenizer
