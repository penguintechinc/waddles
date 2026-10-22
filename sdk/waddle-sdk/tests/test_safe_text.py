"""Tests for `waddle_sdk.safe_text` -- the mention-injection output sanitizer.

The egress detokenizer (`core/egress_detokenizer/src/lib.rs`) is Rust; these tests carry a
line-for-line Python port of its grammar (`_extract_tokens` / `_render`, below) as the ORACLE for
"would the host resolve this?", written independently of the module under test (it shares no
constants with it). The port was differential-checked against the real Rust `detokenize` /
`extract_tokens` bodies (copied verbatim into a scratch crate) over 200k random inputs, 0
disagreements -- so a green run here means the real detokenizer would not act on the output.
"""

from __future__ import annotations

import random
import re
import string
import uuid

import pytest

from waddle_sdk import safe_text
from waddle_sdk.safe_text import (
    ZERO_WIDTH_SPACE as ZW,
)
from waddle_sdk.safe_text import (
    defang_mentions,
    escape_egress_tokens,
    sanitize_user_text,
    strip_control_chars,
)

# ---------------------------------------------------------------------------------------------
# Oracle: Python port of core/egress_detokenizer/src/lib.rs (detokenize / extract_tokens).
# ---------------------------------------------------------------------------------------------

_ORACLE_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_:.")  # lib.rs:153-155
_ORACLE_PREFIX = "{user:"  # lib.rs:148
_NEUTRAL = "Unknown User"  # lib.rs:39


def _extract_tokens(text: str) -> list[str]:
    """Return the tokens the real detokenizer would resolve in ``text`` (lib.rs:214-247)."""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "{}\\":
            i += 2
            continue
        if c == "{" and text.startswith(_ORACLE_PREFIX, i):
            body_start = i + len(_ORACLE_PREFIX)
            j = body_start
            while j < len(text) and text[j] in _ORACLE_TOKEN_CHARS:
                j += 1
            if j < len(text) and text[j] == "}" and j > body_start:
                token = text[body_start:j]
                if token not in tokens:
                    tokens.append(token)
                i = j + 1
                continue
        i += 1
    return tokens


def _render(text: str, names: dict[str, str]) -> str:
    """Return what the real detokenizer would emit for ``text`` (lib.rs:166-209)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "{}\\":
            out.append(text[i + 1])
            i += 2
            continue
        if c == "{" and text.startswith(_ORACLE_PREFIX, i):
            body_start = i + len(_ORACLE_PREFIX)
            j = body_start
            while j < len(text) and text[j] in _ORACLE_TOKEN_CHARS:
                j += 1
            if j < len(text) and text[j] == "}" and j > body_start:
                out.append(names.get(text[body_start:j], _NEUTRAL))
                i = j + 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def test_oracle_matches_the_documented_grammar() -> None:
    """The oracle port itself: resolves `{user:..}`, honors escapes, rejects near-misses."""
    assert _extract_tokens("a {user:x-1} b {user:x-1} {user:y}") == ["x-1", "y"]
    assert _extract_tokens(r"\{user:x\} \\{user:z}") == ["z"]  # `\\` is an escaped backslash
    assert _extract_tokens("{user:} {user:a b} {USER:a} { user:a} {user:é}") == []
    assert _extract_tokens("{user:{user:a}}") == ["a"]
    assert _render("hi {user:a}", {"a": "Alice"}) == "hi Alice"
    assert _render("hi {user:zz}", {}) == f"hi {_NEUTRAL}"
    assert _render(r"\{user:a\}", {"a": "Alice"}) == "{user:a}"


# ---------------------------------------------------------------------------------------------
# Egress-token forgery
# ---------------------------------------------------------------------------------------------

_VICTIM = str(uuid.UUID("0b8f2c3e-4d5a-4e6f-8a9b-1c2d3e4f5a6b"))
_NAMES = {_VICTIM: "VictimDisplayName", "handle:victim": "VictimDisplayName", "42": "Numeric"}

_FORGERIES = [
    f"{{user:{_VICTIM}}}",
    f"hey {{user:{_VICTIM}}} look",
    "{user:handle:victim}",
    "{user:42}",
    f"{{user:{_VICTIM}}}{{user:{_VICTIM}}}",
    f"{{user:{{user:{_VICTIM}}}}}",  # nested: inner one is well-formed
    f"\\\\{{user:{_VICTIM}}}",  # `\\` = escaped backslash, so the placeholder after it is LIVE
    f"\\\\\\\\{{user:{_VICTIM}}}",
    f"\\{{user:{_VICTIM}}}",  # a user-typed escape: placeholder stays text, but also survives
    f"{{user:{_VICTIM}\\}}",
    f"{{user:{_VICTIM}",  # unterminated
    f"user:{_VICTIM}}}",  # no opener
    f"{{{{user:{_VICTIM}}}}}",
]


def test_the_forgeries_are_live_against_the_oracle_before_sanitizing() -> None:
    """Self-check: the un-sanitized corpus really would be resolved (the test can fail)."""
    live = [f for f in _FORGERIES if _extract_tokens(f)]
    assert len(live) >= 8
    assert any("VictimDisplayName" in _render(f, _NAMES) for f in live)


@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_egress_token_is_never_resolved(forged: str) -> None:
    """The real grammar finds no live token and no victim name leaks into the rendered reply."""
    clean = sanitize_user_text(forged)
    assert _extract_tokens(clean) == []
    assert "VictimDisplayName" not in _render(clean, _NAMES)
    assert "Numeric" not in _render(clean, _NAMES)


def test_forged_token_still_reads_as_the_literal_text_the_user_typed() -> None:
    """After the host's un-escape pass the reader sees exactly what was typed -- nothing lost."""
    typed = f"say {{user:{_VICTIM}}} please"
    assert _render(sanitize_user_text(typed), _NAMES) == typed


def test_forged_token_no_probe_oracle_between_known_and_unknown_tokens() -> None:
    """A known and an unknown token render identically -- no identity-probe signal."""
    known = _render(sanitize_user_text("{user:handle:victim}"), _NAMES)
    unknown = _render(sanitize_user_text("{user:handle:nobody}"), _NAMES)
    assert known == "{user:handle:victim}"
    assert unknown == "{user:handle:nobody}"
    assert _NEUTRAL not in known + unknown


def test_user_text_cannot_swallow_or_forge_next_to_a_bundle_placeholder() -> None:
    """A trailing/leading user backslash can't eat the bundle's own `{user:tok}`, nor add one."""
    for hostile in ("abc\\", "\\", "\\\\", "x\\\\\\", f"{{user:{_VICTIM}}}\\"):
        clean = sanitize_user_text(hostile)
        assert _extract_tokens(f"{clean}{{user:mine}}") == ["mine"]
        assert _extract_tokens(f"{{user:mine}}{clean}") == ["mine"]
        assert _extract_tokens(f"{{user:mine}} said {clean}") == ["mine"]


def test_host_escaped_input_is_not_double_escaped() -> None:
    r"""`event.payload["text"]` arrives already `\{`-escaped: it passes through unchanged."""
    delivered = r"he typed \{user:x\} and a \\ backslash"
    assert sanitize_user_text(delivered) == delivered
    assert _render(sanitize_user_text(delivered), {}) == r"he typed {user:x} and a \ backslash"


def test_escape_form_is_canonical_every_special_char_is_inside_a_pair() -> None:
    r"""Lone `{` `}` `\` all become pairs; pairs are kept; nothing else changes."""
    assert escape_egress_tokens("{a}") == r"\{a\}"
    assert escape_egress_tokens("\\") == "\\\\"
    assert escape_egress_tokens("a\\b") == "a\\\\b"
    assert escape_egress_tokens(r"\{a\}\\") == r"\{a\}\\"
    assert escape_egress_tokens("plain") == "plain"
    assert escape_egress_tokens("") == ""


# ---------------------------------------------------------------------------------------------
# allow_tokens -- keep host-minted mentions live, nothing else
# ---------------------------------------------------------------------------------------------


def test_allow_tokens_keeps_only_the_listed_placeholders_live() -> None:
    """A listed token stays resolvable; an unlisted or forged one is neutralized."""
    text = f"hi {{user:tok-1}} and {{user:{_VICTIM}}} and {{user:tok-1}}"
    clean = sanitize_user_text(text, allow_tokens=["tok-1"])
    assert _extract_tokens(clean) == ["tok-1"]
    assert _render(clean, {"tok-1": "Alice", _VICTIM: "VictimDisplayName"}) == (
        f"hi Alice and {{user:{_VICTIM}}} and Alice"
    )


def test_allow_tokens_is_idempotent_and_does_not_unlock_near_misses() -> None:
    """Idempotent with the same allow-list; `{user:tok-1x}` / `{user:}` are not `tok-1`."""
    text = "{user:tok-1} {user:tok-1x} {user:} {user:tok-1"
    once = sanitize_user_text(text, allow_tokens={"tok-1"})
    assert sanitize_user_text(once, allow_tokens={"tok-1"}) == once
    assert _extract_tokens(once) == ["tok-1"]


def test_allow_tokens_does_not_unlock_a_placeholder_behind_a_user_backslash() -> None:
    r"""`\{user:tok-1}` is an escaped brace in the host's grammar -- it stays inert."""
    clean = sanitize_user_text(r"\{user:tok-1}", allow_tokens={"tok-1"})
    assert _extract_tokens(clean) == []


def test_allow_tokens_mention_after_at_sign_is_still_defanged() -> None:
    """`@{user:tok}` would render `@Name` (a live Twitch ping): the `@` is defanged."""
    assert sanitize_user_text("@{user:t}", allow_tokens={"t"}) == f"@{ZW}{{user:t}}"


def test_non_str_inputs_raise_type_error() -> None:
    """None/bytes/int are a caller bug -- fail loudly, never coerce into a reply."""
    for bad in (None, b"x", 3, ["a"]):
        with pytest.raises(TypeError, match="text must be a str"):
            sanitize_user_text(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            escape_egress_tokens(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            defang_mentions(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            strip_control_chars(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="allow_tokens entry"):
        sanitize_user_text("x", allow_tokens=[1])  # type: ignore[list-item]


# ---------------------------------------------------------------------------------------------
# Mass-mention / mention markup
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@everyone", f"@{ZW}everyone"),
        ("@here", f"@{ZW}here"),
        ("@EVERYONE and @Here", f"@{ZW}EVERYONE and @{ZW}Here"),
        ("hi@everyone", f"hi@{ZW}everyone"),  # Discord matches the substring mid-word too
        ("x.@here!", f"x.@{ZW}here!"),
        ("<@123456789012345678>", f"<@{ZW}123456789012345678>"),
        ("<@!123456789012345678>", f"<@{ZW}!123456789012345678>"),
        ("<@&987654321098765432>", f"<@{ZW}&987654321098765432>"),
        ("<#555555555555555555>", f"<#{ZW}555555555555555555>"),
        ("<!here> <!channel> <!everyone>", f"<!{ZW}here> <!{ZW}channel> <!{ZW}everyone>"),
        ("<@U024BE7LH>", f"<@{ZW}U024BE7LH>"),  # Slack user mention
        ("@bob", f"@{ZW}bob"),  # Twitch-style
        ("ping @mods_team now", f"ping @{ZW}mods_team now"),  # `@role`
        ("@@bob", f"@@{ZW}bob"),
        ("(@bob)", f"(@{ZW}bob)"),
        ("@\nbob", f"@{ZW}\nbob"),  # Twitch strips the newline after the bundle runs
        ("@\r\nbob", f"@{ZW}\nbob"),  # `\r` is stripped as a control character
        ("@\x00everyone", f"@{ZW}everyone"),  # control char removed first, then defanged
        ("@\x1bbob", f"@{ZW}bob"),
        ("<\x00@123>", f"<@{ZW}123>"),
        ("@日本語", f"@{ZW}日本語"),
        ("https://medium.com/@bob", "https://medium.com/@bob"),  # a link, not a mention
        ("see https://x.io/@bob/page, then @bob", f"see https://x.io/@bob/page, then @{ZW}bob"),
        ("https://x.io/@everyone", f"https://x.io/@{ZW}everyone"),  # mass mention: always
        ("https://@here", f"https://@{ZW}here"),
        ("ftp://h/@a?q=@HERE", f"ftp://h/@a?q=@{ZW}HERE"),
        ("https://x.io/@bob<@123>", f"https://x.io/@bob<@{ZW}123>"),  # span ends at `<`
    ],
)
def test_mention_markup_is_defanged(raw: str, expected: str) -> None:
    """Each mention form gains a zero-width space after its sigil -- and only there."""
    assert sanitize_user_text(raw) == expected


_PLATFORM_PARSES_A_MENTION = re.compile(
    r"@(?:everyone|here)|<@[!&]?\w|<#\w|<!\w|(?<!\w)@\w", re.IGNORECASE
)  # what Discord / Slack / Twitch would turn into a ping, as plain regexes


@pytest.mark.parametrize(
    "raw",
    [
        "@everyone", "<@&1234567>", "<@12345>", "<@!12345>", "<#12345>", "<!here>", "@bob",
        "@\x00here", "hi@here", "@\nbob", "<\x00@&9>", "<@@everyone>", "@@@bob", "@HERE @Everyone",
    ],
)  # fmt: skip
def test_no_live_mention_survives(raw: str) -> None:
    """The raw input has a mention a platform would parse; the sanitized text has none."""
    # (precondition: live once the Twitch path has stripped newlines/controls from the raw text)
    assert _PLATFORM_PARSES_A_MENTION.search(strip_control_chars(raw).replace("\n", ""))
    clean = sanitize_user_text(raw)
    assert not _PLATFORM_PARSES_A_MENTION.search(clean), clean
    # ...including after the Twitch send path strips newlines/controls from the final text.
    assert not _PLATFORM_PARSES_A_MENTION.search(clean.replace("\n", "")), clean


def test_url_keeps_working_but_its_braces_are_still_escaped() -> None:
    """A link survives intact (readable after the host un-escape) with no live placeholder."""
    url = "https://example.com/@bob/{user:handle:victim}?x=1"
    clean = sanitize_user_text(url)
    assert _extract_tokens(clean) == []
    assert _render(clean, _NAMES) == url


def test_email_shaped_text_is_left_alone() -> None:
    """`user@host` has a word char before the `@` -- not a mention, not touched."""
    for text in ("write to me@example.com", "a@b", "first.last+tag@sub.example.org"):
        assert sanitize_user_text(text) == text


def test_only_the_sigil_position_changes() -> None:
    """Removing the inserted spaces gives back the control-stripped input exactly."""
    raw = "x <@1> @everyone <#2> <!here> @bob e@mail.io"
    assert sanitize_user_text(raw).replace(ZW, "") == raw


# ---------------------------------------------------------------------------------------------
# Normal text, idempotence, unicode
# ---------------------------------------------------------------------------------------------

_NORMAL = [
    "",
    "hello world",
    "Hello, World! How are you?",
    "50% off -- today only (really)",
    "https://example.com/a?b=c&d=e#frag",
    "I <3 chat; a < b > c; x->y; <b>not html</b>",
    "#hashtag and email me@example.com",
    "**bold** _italic_ ~~strike~~ `code` > quote",
    "line one\nline two\n\tindented",
    "emoji 😀🎉 and CJK 日本語テキスト and Arabic مرحبا and Cyrillic Привет",
    "combining e\u0301 and ZWJ 👨\u200d👩\u200d👧 and fullwidth ＠everyone ＜＠１２３＞",
    "math: 2 * (3 + 4) = 14; set = [1, 2]; ratio 3:4",
    "price $5 & tax; 100% | pipes",
    "!command /slash .dot",  # not this helper's concern (echo handles command leads itself)
    "plain @ sign and trailing @",
    "<> <# <! <@ stray angle brackets",
]


@pytest.mark.parametrize("text", _NORMAL)
def test_normal_text_is_returned_unchanged(text: str) -> None:
    """Text with no braces, backslashes, controls or live mention markup is byte-identical."""
    assert sanitize_user_text(text) == text


def test_lone_special_characters_are_the_only_normal_text_that_changes() -> None:
    """Braces / backslashes are escaped (readable after the host un-escapes), nothing more."""
    for text in ("a {b} c", "C:\\Users\\me", 'json: {"a": 1}', "x \\ y"):
        clean = sanitize_user_text(text)
        assert clean != text
        assert _render(clean, {}) == text  # the reader still sees exactly what was typed


_IDEMPOTENCE_CORPUS = [
    *_NORMAL,
    *_FORGERIES,
    "@everyone <@1> <@&2> <#3> <!here> @bob @@x hi@here",
    "\\{user:x\\} \\\\ \\ {{}} }{ \\",
    "\x00@\x1b\x85here\r\n\t",
    "{user:tok-1} @{user:tok-1} <@{user:tok-1}>",
    "😀{user:é}@日本",
]


@pytest.mark.parametrize("text", _IDEMPOTENCE_CORPUS)
def test_sanitize_is_idempotent(text: str) -> None:
    """Wrapping already-sanitized text again changes nothing (safe to wrap everything)."""
    once = sanitize_user_text(text)
    assert sanitize_user_text(once) == once
    assert sanitize_user_text(once, allow_tokens=["tok-1"]) == sanitize_user_text(
        once, allow_tokens=["tok-1"]
    )
    for step in (escape_egress_tokens, defang_mentions, strip_control_chars):
        assert step(step(text)) == step(text)


def test_unicode_is_safe() -> None:
    """Astral, combining, RTL, and pre-existing zero-width characters survive intact."""
    for text in (
        "😀 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 日本語 e\u0301 مرحبا \u202e rtl",
        "already\u200bdefanged @\u200beveryone stays single",
        "ZWJ 👨\u200d👩\u200d👧 and variation \u2764\ufe0f",
    ):
        assert sanitize_user_text(text) == text
    assert sanitize_user_text("😀@everyone😀") == f"😀@{ZW}everyone😀"
    assert sanitize_user_text("日本{user:é}") == "日本\\{user:é\\}"
    # a non-ASCII digit/letter is NOT a token char in the host grammar, so `{user:١}` is inert
    # either way; it is still escaped (all braces are).
    assert _extract_tokens(sanitize_user_text("{user:١}")) == []


def test_control_characters_other_than_newline_and_tab_are_stripped() -> None:
    r"""C0, DEL and C1 go; `\n` and `\t` stay; the zero-width space is not a control char."""
    assert strip_control_chars("a\x00b\x07c\x1bd\x7fe\x85f\x9fg") == "abcdefg"
    assert strip_control_chars("a\nb\tc") == "a\nb\tc"
    assert strip_control_chars("a\r\nb") == "a\nb"
    assert strip_control_chars(f"a{ZW}b") == f"a{ZW}b"


def test_public_surface() -> None:
    """`__all__` names exist and the constant is a single zero-width space."""
    assert ZW == "\u200b"
    for name in safe_text.__all__:
        assert hasattr(safe_text, name)


# ---------------------------------------------------------------------------------------------
# Randomized: the sanitizer vs the oracle (seeded, so a failure is reproducible)
# ---------------------------------------------------------------------------------------------

_PIECES = [
    "{", "}", "\\", "\\\\", "\\{", "\\}", "{user:", "user:", "{user:a1}", "{user:a1", "a1",
    "handle:bob", "0123", "-", "_", ":", ".", " ", "\n", "\t", "\r", "\x00", "\x1b", "\x85",
    "@", "@everyone", "@here", "<@", "<@!", "<@&", "<#", "<!", ">", "123456789012345678",
    "bob", "é", "日本", "😀", "e\u0301", ZW, "\u202e", "!", "&", "{{", "}}", "USER",
    "{USER:a1}", "{user: a1}", "{user:}", "{user:é}", "https://h/@", "x://@here", "https://h/{user:a1}",
]  # fmt: skip


def _random_texts(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible test inputs, not security
    return ["".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 14))) for _ in range(count)]


def test_randomized_no_live_token_idempotent_and_composable() -> None:
    """5000 random hostile strings: nothing live survives, idempotent, composes safely."""
    texts = _random_texts(5000, seed=20261021)
    live_before = sum(1 for t in texts if _extract_tokens(t))
    assert live_before > 300  # the generator really produces forgeries
    for text in texts:
        clean = sanitize_user_text(text)
        assert _extract_tokens(clean) == [], (text, clean)
        assert sanitize_user_text(clean) == clean, (text, clean)
        assert _extract_tokens(f"{{user:b1}}{clean}") == ["b1"], (text, clean)
        assert _extract_tokens(f"{clean}{{user:b1}}") == ["b1"], (text, clean)
        assert "\x00" not in clean
        assert "@everyone" not in clean.casefold()
        assert "@here" not in clean.casefold()


def test_randomized_allow_tokens_only_the_listed_token_survives() -> None:
    """With `allow_tokens={'a1'}` the only live token that can remain is `a1`."""
    for text in _random_texts(3000, seed=7):
        clean = sanitize_user_text(text, allow_tokens={"a1"})
        assert set(_extract_tokens(clean)) <= {"a1"}, (text, clean)
        assert sanitize_user_text(clean, allow_tokens={"a1"}) == clean, (text, clean)


def test_randomized_escape_is_exact_inverse_of_the_host_unescape_for_backslash_free_text() -> None:
    """Without backslashes in the input, the host's render of the escape gives the input back."""
    for text in _random_texts(3000, seed=99):
        if "\\" in text:
            continue
        assert _render(escape_egress_tokens(text), {}) == text, text
