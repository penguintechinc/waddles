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
    COMMAND_LEADS,
    SAFE_LEAD,
    defang_mentions,
    escape_egress_tokens,
    guard_leading_command,
    leads_with_command,
    sanitize_user_text,
    strip_control_chars,
)
from waddle_sdk.safe_text import (
    ZERO_WIDTH_SPACE as ZW,
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


# ---------------------------------------------------------------------------------------------
# Review findings: the host strips `\t`/`\n` AFTER the detokenizer; allow-list exactness
# (the broader proof is tests/test_safe_text_differential.py)
# ---------------------------------------------------------------------------------------------


def _twitch_final(sanitized: str) -> str:
    """The Twitch send path: detokenize (nothing live here), then strip EVERY control char."""
    rendered = _render(sanitized, {})
    return "".join(c for c in rendered if not (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F))


@pytest.mark.parametrize(
    "raw",
    [
        "@\tbob", "@\t\tbob", "@\n\tbob", "@\t\neveryone", "@\there", "@ev\teryone",
        "<@\t123456789012345678>", "<@\t!123>", "<@!\t123>", "<@&\n987>", "<\t@123>",
        "<#\t123>", "<#\n123>", "<\n#123>", "<\t#\t123>", "<!\there>", "<!\nchannel>",
        "<\t!here>", "<\n!\nhere>", "hi@\teveryone", "x \t@\tbob",
    ],
)  # fmt: skip
def test_tab_and_newline_gaps_cannot_hide_a_mention_from_the_twitch_strip(raw: str) -> None:
    r"""`sanitize_irc_component` strips `\t` and `\n` after the detokenizer, closing the gap."""
    precondition = _twitch_final(raw)  # what the host would send for the UNsanitized text
    assert _PLATFORM_PARSES_A_MENTION.search(precondition), precondition
    clean = sanitize_user_text(raw)
    assert not _PLATFORM_PARSES_A_MENTION.search(_twitch_final(clean)), clean  # Twitch
    assert not _PLATFORM_PARSES_A_MENTION.search(_render(clean, {})), clean  # Discord keeps them


def test_tab_and_newline_gaps_defang_at_the_sigil_and_stay_readable() -> None:
    """The space lands right after the sigil; removing it gives back the input exactly."""
    assert sanitize_user_text("@\tbob") == f"@{ZW}\tbob"
    assert sanitize_user_text("<!\there>") == f"<!{ZW}\there>"
    assert sanitize_user_text("<\t!here>") == f"<\t!{ZW}here>"
    assert sanitize_user_text("<#\n123>") == f"<#{ZW}\n123>"
    assert sanitize_user_text("tab\tseparated\tcolumns\nand lines") == (
        "tab\tseparated\tcolumns\nand lines"
    )
    assert sanitize_user_text("a @\n b") == "a @\n b"  # `@` then whitespace is no mention


def test_defang_mentions_alone_sees_through_tab_and_newline() -> None:
    """The public `defang_mentions` (no stripping step) closes the same gap."""
    assert defang_mentions("@\tbob") == f"@{ZW}\tbob"
    assert defang_mentions("<!\r\nhere>") == f"<!{ZW}\r\nhere>"
    assert defang_mentions("email me \t@\tbob") == f"email me \t@{ZW}\tbob"
    assert defang_mentions("user\t@\thost") == "user\t@\thost"  # strips to `user@host`: email


# -- allow-list: exact whole-token matching only -------------------------------------------------

_T = "tok-1"
_LIVE_T = f"{{user:{_T}}}"


def _live_tokens(clean: str) -> list[str]:
    return [t for t in re.findall(r"(?<!\\)\{user:([A-Za-z0-9_:.-]+)\}", clean)]


@pytest.mark.parametrize(
    ("hostile", "expected_live"),
    [
        # placeholder fragments never combine: the stripped control char, split tokens, partials
        ("{user:tok\x00-1}", 0),
        ("{user:tok-1\x00}", 0),
        ("{\x00user:tok-1}", 0),
        ("{user:\x1btok-1}", 0),
        ("{us\x00er:tok-1}", 0),
        ("{user:tok\n-1}", 0),
        ("{user:tok\t-1}", 0),
        ("{user:tok-1\x00}{user:tok-1}", 1),
        # partial / extended tokens are different tokens
        ("{user:tok-}", 0),
        ("{user:tok-12}", 0),
        ("{user:ttok-1}", 0),
        ("{user:tok-1.}", 0),
        ("{user:tok-1:}", 0),
        ("{user:TOK-1}", 0),
        ("{user:tok-1 }", 0),
        ("{user:tok-1", 0),
        ("user:tok-1}", 0),
        # nested / adjacent braces
        ("{user:{user:tok-1}}", 1),
        ("{{user:tok-1}}", 1),
        ("{user:tok-1}}", 1),
        ("{{{user:tok-1}}}", 1),
        ("{user:{user:{user:tok-1}}}", 1),
        ("{user:tok-1}{user:tok-1}", 2),
        ("{user:tok-1}{user:evil}{user:tok-1}", 2),
        ("{user:tok-1{user:tok-1}", 1),
        ("{user:evil{user:tok-1}evil}", 1),
        # escape confusion: an escaped brace is never live; an escaped backslash does not escape
        (r"\{user:tok-1}", 0),
        (r"\{user:tok-1\}", 0),
        (r"\\{user:tok-1}", 1),
        (r"\\\{user:tok-1}", 0),
        (r"\\\\{user:tok-1}", 1),
        (r"{user:tok-1\}", 0),
        (r"{user:tok-1}\}", 1),
        (r"{user:\tok-1}", 0),
        (r"\{user:tok-1}{user:tok-1}", 1),
        ("\\\x00{user:tok-1}", 1),  # a control char breaks the pair: live in the text as given
        ("\\\x00\\{user:tok-1}", 0),
    ],
)
def test_allow_list_keeps_exactly_the_whole_placeholders_the_detokenizer_finds(
    hostile: str, expected_live: int
) -> None:
    """Live output = allow-listed whole placeholders live in the INPUT, nothing assembled."""
    precondition = [o for o in _live_occurrences(hostile) if o == _T]
    assert len(precondition) == expected_live, hostile  # the table matches the detokenizer
    clean = sanitize_user_text(hostile, allow_tokens=[_T])
    assert _live_occurrences(clean) == [_T] * expected_live, (hostile, clean)
    assert sanitize_user_text(clean, allow_tokens=[_T]) == clean
    assert set(_extract_tokens(clean)) <= {_T}


def _live_occurrences(text: str) -> list[str]:
    """Every placeholder token the oracle (detokenizer port) resolves in ``text``, in order."""
    found: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "{}\\":
            i += 2
        elif text.startswith("{user:", i):
            j = i + 6
            while j < len(text) and text[j] in _ORACLE_TOKEN_CHARS:
                j += 1
            if j < len(text) and text[j] == "}" and j > i + 6:
                found.append(text[i + 6 : j])
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return found


def test_allow_list_control_char_splice_cannot_forge_a_live_token() -> None:
    """Controls were once stripped BEFORE the allow-list scan, re-forming a split token.

    `{user:tok<NUL>-1}` became a live `{user:tok-1}` out of text that never contained one.
    """
    for forged in ("{user:tok\x00-1}", "{user:tok-\x1b1}", "{\x00user:\x00tok-1\x00}"):
        clean = sanitize_user_text(forged, allow_tokens=[_T])
        assert _extract_tokens(clean) == [], clean
        assert _render(clean, {_T: "Alice"}) == forged.replace("\x00", "").replace("\x1b", "")


def test_allow_list_text_around_a_live_placeholder_never_joins_it() -> None:
    """Attacker characters glued to an allowed placeholder stay separate text."""
    text = f"{{user:evil}}{_LIVE_T}{{user:evil}} {{user:{_VICTIM}}}{_LIVE_T}"
    clean = sanitize_user_text(text, allow_tokens=[_T])
    assert _extract_tokens(clean) == [_T]
    assert _render(clean, {_T: "Alice", _VICTIM: "VictimDisplayName"}) == (
        f"{{user:evil}}Alice{{user:evil}} {{user:{_VICTIM}}}Alice"
    )


def test_allow_tokens_bare_string_is_rejected_not_split_into_characters() -> None:
    """`allow_tokens="tok-1"` would allow-list the one-character tokens t, o, k, -, 1."""
    for bad in ("tok-1", b"tok-1", bytearray(b"tok-1")):
        with pytest.raises(TypeError, match="not a bare"):
            sanitize_user_text("{user:t}{user:1}", allow_tokens=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="not a bare"):
            escape_egress_tokens("{user:t}", allow_tokens=bad)  # type: ignore[arg-type]


def test_allow_tokens_that_can_never_be_a_token_are_inert() -> None:
    """Empty / non-token entries never match anything (and never raise): fail-closed."""
    text = "{user:} {user:a b} {user:é} {user:tok-1}"
    for inert in ([], [""], ["a b"], ["é"], ["{user:tok-1}"], ["tok-1}"], ["{"], ["\\"]):
        clean = sanitize_user_text(text, allow_tokens=inert)
        assert _extract_tokens(clean) == [], (inert, clean)
    assert _extract_tokens(sanitize_user_text(text, allow_tokens=["tok-1", "é", ""])) == ["tok-1"]


def test_allow_list_with_several_tokens_keeps_each_placeholder_where_it_was() -> None:
    """Two different live placeholders keep their own identity and order (not swapped)."""
    text = "{user:a-1} and {user:b-2}, not {user:c-3}, then {user:a-1} again"
    clean = sanitize_user_text(text, allow_tokens=["a-1", "b-2"])
    assert clean == "{user:a-1} and {user:b-2}, not \\{user:c-3\\}, then {user:a-1} again"
    assert _render(clean, {"a-1": "Ann", "b-2": "Bo", "c-3": "Cy"}) == (
        "Ann and Bo, not {user:c-3}, then Ann again"
    )


def test_allow_tokens_accepts_any_iterable_of_strings() -> None:
    """A generator, tuple, set, dict (its keys) and frozenset all work, consumed once."""
    text = f"hi {_LIVE_T}"
    for allow in ((t for t in [_T]), (_T,), {_T}, {_T: 1}, frozenset([_T])):
        assert _extract_tokens(sanitize_user_text(text, allow_tokens=allow)) == [_T]


# -- a live placeholder resolves to a name the mentioned user controls --------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"<!{_LIVE_T}>", f"<!{ZW}{_LIVE_T}{ZW}>"),  # `<!` + name `here` = Slack broadcast
        (f"<#{_LIVE_T}>", f"<#{ZW}{_LIVE_T}{ZW}>"),  # `<#` + digits = channel link
        (f"<{_LIVE_T}>", f"<{ZW}{_LIVE_T}{ZW}>"),  # `<` + name `#123>`
        (f"<@{_LIVE_T}>", f"<@{ZW}{_LIVE_T}{ZW}>"),
        (f"<@!{_LIVE_T}>", f"<@{ZW}!{_LIVE_T}{ZW}>"),
        (f"hi@{_LIVE_T}", f"hi@{ZW}{_LIVE_T}"),  # email-shaped `@` + name `everyone`
        (f"hi@eve{_LIVE_T}", f"hi@{ZW}eve{_LIVE_T}"),  # `@eve` + name `ryone`
        (f"hi@e{_LIVE_T}ne", f"hi@{ZW}e{_LIVE_T}{ZW}ne"),
        (f"@\t{_LIVE_T}", f"@{ZW}\t{_LIVE_T}"),
        (f"{_LIVE_T}@bob", f"{_LIVE_T}@{ZW}bob"),  # name ending in `<`/`@` + `@bob`
        (f"{_LIVE_T}bob", f"{_LIVE_T}{ZW}bob"),  # name ending in `@` + `bob`
        (f"{_LIVE_T}#123>", f"{_LIVE_T}{ZW}#123>"),  # name ending in `<` + `#123>`
        (f"{_LIVE_T}>", f"{_LIVE_T}{ZW}>"),
        (f"{_LIVE_T}!here>", f"{_LIVE_T}{ZW}!here>"),
        (f"{_LIVE_T}{_LIVE_T}", f"{_LIVE_T}{ZW}{_LIVE_T}"),
        # the host strips `\n`/`\t` from the final text, joining what is on either side
        (f"{_LIVE_T}\n{_LIVE_T}", f"{_LIVE_T}{ZW}\n{_LIVE_T}"),
        (f"{_LIVE_T}\tbob", f"{_LIVE_T}{ZW}\tbob"),
        (f"@{_LIVE_T}", f"@{ZW}{_LIVE_T}"),
    ],
)
def test_text_adjacent_to_a_live_placeholder_cannot_complete_a_mention(
    text: str, expected: str
) -> None:
    """The sanitizer cannot see the display name, so the boundary around it is broken."""
    assert sanitize_user_text(text, allow_tokens=[_T]) == expected


@pytest.mark.parametrize("name", ["everyone", "veryone", "here", "<", "#123>", "123", "@", "<@"])
@pytest.mark.parametrize(
    "shape", ["<!{P}>", "<#{P}>", "<{P}", "hi@{P}", "hi@e{P}", "{P}bob", "@{P}"]
)
def test_no_mention_forms_across_a_resolved_display_name(shape: str, name: str) -> None:
    """Resolve the allowed placeholder to a hostile name: no ping the author's text helped form."""
    clean = sanitize_user_text(shape.replace("{P}", _LIVE_T), allow_tokens=[_T])
    rendered = _render(clean, {_T: name})
    for final in (rendered, _twitch_final_text(rendered)):
        for match in _PLATFORM_PARSES_A_MENTION.finditer(final):
            assert match.group(0) in name, (shape, name, final)


def _twitch_final_text(rendered: str) -> str:
    return "".join(c for c in rendered if not (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F))


@pytest.mark.parametrize(
    "text",
    [
        f"hello {_LIVE_T}",
        f"hello {_LIVE_T}, welcome!",
        f"{_LIVE_T}: hi",
        f"{_LIVE_T}'s turn",
        f"({_LIVE_T}) said {_LIVE_T}.",
        f"{_LIVE_T}\n- {_LIVE_T} \t {_LIVE_T}",
        f"thanks {_LIVE_T}!",
        f"@ {_LIVE_T}",
    ],
)
def test_ordinary_text_around_an_allowed_mention_is_byte_identical(text: str) -> None:
    """Normal replies (space, punctuation, newline next to a mention) are not touched."""
    assert sanitize_user_text(text, allow_tokens=[_T]) == text


def test_defang_mentions_treats_a_raw_placeholder_as_an_opaque_name() -> None:
    """The public helper knows `{user:..}`, ignores escaped ones, and drops NUL (its marker)."""
    assert defang_mentions(f"<!{_LIVE_T}>") == f"<!{ZW}{_LIVE_T}{ZW}>"
    assert defang_mentions(r"<!\{user:tok-1\}>") == r"<!\{user:tok-1\}>"
    assert defang_mentions(" \x00@\x00bob") == f" @{ZW}bob"
    assert defang_mentions(defang_mentions(f"hi@e{_LIVE_T} <#{_LIVE_T}>")) == defang_mentions(
        f"hi@e{_LIVE_T} <#{_LIVE_T}>"
    )


def test_urls_still_only_defang_mass_mentions_but_placeholders_in_them_are_guarded() -> None:
    """A link keeps working; an `@` next to a live placeholder inside one is defanged."""
    assert sanitize_user_text("https://medium.com/@bob/x") == "https://medium.com/@bob/x"
    assert sanitize_user_text(f"https://x.io/@{_LIVE_T}", allow_tokens=[_T]) == (
        f"https://x.io/@{ZW}{_LIVE_T}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://x/{user:victim-uuid}",
        "https://x/{user:handle:victim}?q={user:42}&z={user:a1}",
        "ab://{user:a1}",
        "ftp://h/@bob/{user:a1}@everyone",
    ],
)
@pytest.mark.parametrize("allow", [(), (_T,)])
def test_token_forgery_is_neutralized_inside_url_spans_too(
    url: str, allow: tuple[str, ...]
) -> None:
    """The URL carve-out covers a bare `@name` ONLY: the detokenizer has no URL awareness.

    `https://x/{user:<tok>}` resolves exactly like a bare placeholder would, so every brace in a
    URL span is escaped like anywhere else -- with or without an (unrelated) allow-list -- and the
    reader still sees the URL as typed once the egress un-escape has run.
    """
    clean = sanitize_user_text(url, allow_tokens=allow)
    assert _extract_tokens(clean) == []  # nothing resolves
    assert _render(clean, _NAMES).replace(ZW, "") == url  # the reader still sees the URL as typed
    assert "{user:" not in clean.replace("\\{", "")  # every brace sits inside an escape pair


def test_a_url_span_never_extends_across_a_live_placeholder() -> None:
    """A display name can hold `<`, `>` and spaces, so the link cannot be assumed to continue.

    Regression (third review): ``ab://{P}123>`` with the display name ``<@`` renders
    ``ab://<@123>`` -- a live user mention -- because everything after a placeholder inside a
    URL span used to be exempt. The rules around a placeholder now apply even next to a URL.
    """
    for tail, want in (
        ("123>", f"{ZW}123>"),  # name `<@` / `<#` / `@` completes a ping or a channel mention
        ("here>", f"{ZW}here>"),  # name `<!` / `@`: Slack broadcast / @here
        ("#123>", f"{ZW}#123>"),  # name `<`: Discord channel mention
        ("&123>", f"{ZW}&123>"),  # name `<@`: role mention
        ("@bob", f"@{ZW}bob"),  # name `<` or ending in whitespace: `<@bob` / a word-start @bob
    ):
        got = sanitize_user_text(f"ab://{_LIVE_T}{tail}", allow_tokens=[_T])
        assert got == f"ab://{_LIVE_T}{want}", tail
    assert sanitize_user_text(f"ab://a/!{_LIVE_T}!here>", allow_tokens=[_T]) == (
        f"ab://a/!{_LIVE_T}{ZW}!here>"
    )
    # ...and the public helper agrees (it treats a raw placeholder as live).
    assert defang_mentions(f"ab://{_LIVE_T}123>") == f"ab://{_LIVE_T}{ZW}123>"


def test_the_url_exemption_still_holds_where_no_placeholder_is_involved() -> None:
    """Links, and a placeholder SEPARATED from one by whitespace, are left byte-identical."""
    for text in (
        "https://medium.com/@bob/x",
        "ab://@bob",
        f"https://medium.com/@bob/x {_LIVE_T}",
        f"{_LIVE_T} https://medium.com/@bob",
        f"see {_LIVE_T}: https://x.io/@bob/page",
    ):
        assert sanitize_user_text(text, allow_tokens=[_T]) == text


def test_a_lost_or_forged_live_marker_fails_loudly_never_silently() -> None:
    """The internal marker count must match the live placeholders it stands for (unreachable)."""
    assert safe_text._restore_live("a\x00b", ["{user:x}"]) == "a{user:x}b"
    for masked in ("ab", "\x00\x00"):
        with pytest.raises(RuntimeError, match="lost or forged"):
            safe_text._restore_live(masked, ["{user:x}"])


# -- guard_leading_command: the shared completion-lead guard (was copy-pasted into 6 AI bundles) --

_ZWSP = chr(0x200B)
_BOM = chr(0xFEFF)
_RLO = chr(0x202E)
_TAG_A = chr(0xE0041)
_NUL = chr(0)


@pytest.mark.parametrize("lead", COMMAND_LEADS)
@pytest.mark.parametrize(
    "wrap",
    [
        "{}ban everyone",
        "  {}ban everyone",
        "\t\n{}ban everyone",
        _ZWSP + "{}ban everyone",
        _BOM + " {}ban everyone",
        _RLO + "{}ban everyone",
        _TAG_A + "{}ban everyone",
        _NUL + "{}ban everyone",
        _ZWSP + _NUL + " " + _BOM + "{}ban everyone",
    ],
)
def test_a_command_lead_is_seen_through_invisible_and_control_characters(
    lead: str, wrap: str
) -> None:
    """The host strips Cc/Cf before sending, so the guard must judge the text as sent."""
    text = wrap.format(lead)
    assert leads_with_command(text) is True
    guarded = guard_leading_command(text)
    assert guarded == SAFE_LEAD + text
    assert not guarded.startswith(COMMAND_LEADS)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "hello", "a/b", "what? no!", "end.", "\u00bfque?", "@everyone", "\U0001f604 /ban"],
)
def test_text_that_does_not_lead_with_a_command_is_returned_byte_identical(text: str) -> None:
    assert leads_with_command(text) is False
    assert guard_leading_command(text) == text


@pytest.mark.parametrize("text", ["/ban x", ".mod y", "!points", _ZWSP + "!x", "hello"])
def test_guard_is_idempotent(text: str) -> None:
    once = guard_leading_command(text)
    assert guard_leading_command(once) == once


def test_safe_lead_is_visible_and_not_itself_a_command_lead() -> None:
    assert SAFE_LEAD.strip() and not leads_with_command(SAFE_LEAD)
    assert COMMAND_LEADS == ("/", ".", "!")


@pytest.mark.parametrize("bad", [None, 3, b"/x"])
def test_guard_rejects_non_text_instead_of_coercing_it(bad: object) -> None:
    with pytest.raises(TypeError):
        guard_leading_command(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        leads_with_command(bad)  # type: ignore[arg-type]


def test_there_is_exactly_one_safe_text_implementation() -> None:
    """Two merged lineages once risked an older sanitizer winning: only ONE may exist.

    Walks the Python source trees for any other definition of the sanitizer entry points (a
    vendored or forked copy would silently lack the later hardening: control-char differential,
    allow-list escape, URL-span fix) and for any bundle-local copy of the lead check.
    """
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    skip = {"node_modules", ".venv", ".git", ".worktrees", "__pycache__", ".mypy_cache", "target"}
    names = ("sanitize_user_text", "escape_egress_tokens", "defang_mentions")
    needles = tuple("def " + n for n in names)
    found: list[str] = []
    scanned = 0
    for top in ("sdk", "bundles", "libs", "core", "hub_api", "services", "scripts"):
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                scanned += 1
                path = Path(dirpath) / name
                text = path.read_text(encoding="utf-8", errors="replace")
                if any(needle in text for needle in needles):
                    found.append(str(path.relative_to(root)))
    assert scanned > 500, f"only {scanned} python files scanned -- the repo-root path is wrong"
    assert found == ["sdk/waddle-sdk/src/waddle_sdk/safe_text.py"], found
