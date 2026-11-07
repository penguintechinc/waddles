# ruff: noqa: F811  -- `host` is a re-exported pytest fixture used as a test parameter
"""Host-native tests for chatguard's cross-platform moderation commands.

The tool host is a VALIDATING fake: every call is checked against the real platform tools'
`input_schema` (fixtures/moderation_tools.json, copied from the service bundles' manifests; the
YouTube entries are ASSUMED until S3a merges), so a drifting argument shape fails here instead of
at the host. The platform tools only become invocable once the host tool path (S0c) lands; until
then these tests are the contract.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from typing import Any

import pytest
from test_app import (  # noqa: F401 -- `host` is a pytest fixture re-exported for this module
    ACTOR,
    COMMUNITY,
    TARGET,
    _event,
    _Host,
    _run,
    _transform,
    host,
    reply,
    say,
    send,
)
from waddle_sdk import community_kv
from waddle_sdk.testing import _TOOL_CASES, _FakeWitError, _variant

import moderation

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "moderation_tools.json").read_text())
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TTOK = "{user:tok-target}"


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Tiny JSON-schema subset check (object/props/required/additionalProperties/min/max/uuid)."""
    props = schema["properties"]
    for key in args:
        if key not in props and schema.get("additionalProperties") is False:
            return f"unknown argument {key}"
    for key in schema.get("required", []):
        if key not in args:
            return f"missing {key}"
    for key, val in args.items():
        spec = props.get(key, {})
        kind = spec.get("type")
        if kind == "string" and not isinstance(val, str):
            return f"{key} must be a string"
        if kind == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return f"{key} must be an integer"
        if spec.get("format") == "uuid" and not UUID_RE.fullmatch(str(val)):
            return f"{key} must be a uuid"
        if "minimum" in spec and val < spec["minimum"]:
            return f"{key} below minimum"
        if "maximum" in spec and val > spec["maximum"]:
            return f"{key} above maximum"
    return None


class ToolHost:
    """Validating fake of `wit_world.imports.tool`; scriptable per-tool failures."""

    def __init__(self) -> None:
        """Start with no recorded calls and no scripted failures."""
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.fail: dict[str, tuple[str, str]] = {}

    def invoke(self, target: str, name: str, args: bytes, key: str | None) -> bytes:
        parsed = json.loads(bytes(args))
        self.calls.append((target, name, parsed, key))
        fixture = FIXTURES.get(name)
        if fixture is None or fixture["app_id"] != target:
            raise _err("target_unavailable", "")
        if key is None:
            raise _err("invalid_args", "write requires an idempotency key")
        if problem := _validate(fixture["input_schema"], parsed):
            raise _err("invalid_args", problem)
        if name in self.fail:
            raise _err(*self.fail[name])
        return b'{"ok":true}'

    def names(self) -> list[str]:
        return [c[1] for c in self.calls]


def _err(code: str, message: str) -> _FakeWitError:
    name, has_payload = _TOOL_CASES[code]
    case = _variant(name, has_payload)
    return _FakeWitError(case(message) if has_payload else case())


@pytest.fixture
def tools(host: _Host) -> ToolHost:  # noqa: F811
    import wit_world

    th = ToolHost()
    wit_world.imports.tool = types.SimpleNamespace(invoke=th.invoke)
    host.ident.add_mention("tok-target", TARGET)
    return th


def mod_reply(host: _Host, text: str, **kw: Any) -> str:  # noqa: F811
    return reply(host, text, is_mod=True, **kw)


# --- grammar (golden cases) -------------------------------------------------------------------

GOLDEN = [
    (f"!ban {TTOK}", ("ban", "all", None, None)),
    (f"!ban {TTOK} twitch", ("ban", "twitch", None, None)),
    (f"!BAN {TTOK} Discord spamming links", ("ban", "discord", "spamming links", None)),
    (f"!ban {TTOK} being rude", ("ban", "all", "being rude", None)),
    (f"!unban {TTOK} youtube", ("unban", "youtube", None, None)),
    (f"!timeout {TTOK} 10m", ("timeout", "all", None, 600)),
    (f"!timeout {TTOK} 1h30m twitch cool off", ("timeout", "twitch", "cool off", 5400)),
    (f"!untimeout {TTOK} all", ("untimeout", "all", None, None)),
    (f"!kick {TTOK} discord", ("kick", "discord", None, None)),
    (f"!purge {TTOK}", ("purge", "all", None, None)),
]


@pytest.mark.parametrize(("text", "expected"), GOLDEN)
def test_grammar_golden(text: str, expected: tuple[Any, ...], host: _Host) -> None:  # noqa: F811
    cmd = moderation.parse(text)
    assert (cmd.action, cmd.platform, cmd.reason, cmd.duration) == expected
    assert cmd.token == "tok-target"  # noqa: S105 -- a mention token, not a credential


BAD = [
    "!ban",
    "!ban someone",
    "!ban @someone twitch",
    f"!ban {TTOK} {TTOK}x",
    f"!timeout {TTOK}",
    f"!timeout {TTOK} soon",
    f"!timeout {TTOK} 0s",
    f"!timeout {TTOK} 30s1m",
    f"!timeout {TTOK} 600",
    f"!unban {TTOK} twitch extra words",
    f"!kick {TTOK} discord now",
    f"!ban {TTOK} twitch {TTOK}",
    "!ban " + TTOK + " " + "x" * 700,
]


@pytest.mark.parametrize("text", BAD)
def test_grammar_rejects(text: str, host: _Host) -> None:  # noqa: F811
    with pytest.raises(moderation.ModUsageError) as exc:
        moderation.parse(text)
    assert "tok-target" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("45s", 45), ("10m", 600), ("2h", 7200), ("1d", 86400), ("1w", 604800), ("1h30m", 5400)],
)
def test_parse_duration_matches_rust_sdk_grammar(raw: str, seconds: int) -> None:
    assert moderation.parse_duration(raw) == seconds


# --- fan-out ----------------------------------------------------------------------------------


def test_ban_all_fans_out_with_uuid_only_args(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    out = mod_reply(host, f"!ban {TTOK} spamming")
    assert out == "banned on twitch, discord, youtube"
    assert tools.names() == [
        "twitch.moderation.ban",
        "discord.moderation.ban",
        "youtube.chat.ban",
    ]
    for _, _, args, key in tools.calls:
        assert args["user"] == TARGET and key
    assert tools.calls[0][2]["reason"] == "spamming"
    assert "reason" not in tools.calls[1][2]  # discord schema has no reason field
    assert len({c[3] for c in tools.calls}) == 3  # distinct idempotency key per platform


def test_partial_failure_reports_each_platform(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    tools.fail["youtube.chat.ban"] = ("invalid_args", "no linked account for user")
    tools.fail["discord.moderation.ban"] = ("target_unavailable", "")
    out = mod_reply(host, f"!ban {TTOK}")
    assert out == "banned on twitch; discord: unavailable, try again; youtube: no linked account"


def test_unbound_channel_message(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    tools.fail["twitch.moderation.ban"] = ("invalid_args", "community has no bound channel")
    assert "twitch: no bound channel" in mod_reply(host, f"!ban {TTOK} twitch")


def test_explicit_platform_calls_only_that_platform(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    assert mod_reply(host, f"!unban {TTOK} discord") == "unbanned on discord"
    assert tools.names() == ["discord.moderation.unban"]


def test_untimeout_and_purge_mappings(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    mod_reply(host, f"!untimeout {TTOK}")
    assert tools.names() == [
        "twitch.moderation.unban",
        "discord.moderation.untimeout",
        "youtube.chat.unban",
    ]
    tools.calls.clear()
    assert mod_reply(host, f"!purge {TTOK} twitch") == "purged on twitch"
    assert tools.calls[0][1:3] == ("twitch.moderation.timeout", {"user": TARGET, "duration": 1})


def test_kick_is_discord_only(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    assert mod_reply(host, f"!kick {TTOK}") == "kicked on discord; not supported on twitch, youtube"
    assert tools.names() == ["discord.moderation.kick"]
    assert "!kick is not supported on twitch" in mod_reply(host, f"!kick {TTOK} twitch")


def test_kick_platform_has_no_tools_and_fails_loud(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    assert "not supported on kick" in mod_reply(host, f"!ban {TTOK} kick")
    assert tools.calls == []


def test_all_platform_set_comes_from_config(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    key = community_kv._scoped_key(COMMUNITY, "moderation.config")
    host.kv.store[key] = json.dumps(
        {"default_reason": "house rules", "all_platforms": ["discord"]}
    ).encode()
    assert mod_reply(host, f"!timeout {TTOK} 5m") == "timed out on discord for 5m"
    assert tools.names() == ["discord.moderation.timeout"]
    tools.calls.clear()
    host.kv.store[key] = b"{not json"
    assert mod_reply(host, f"!ban {TTOK}") == "banned on twitch, discord, youtube"
    assert "moderation.config_corrupt" in host.logged()


def test_default_reason_applies_to_twitch(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    key = community_kv._scoped_key(COMMUNITY, "moderation.config")
    host.kv.store[key] = json.dumps({"default_reason": "house rules"}).encode()
    mod_reply(host, f"!ban {TTOK} twitch")
    assert tools.calls[0][2]["reason"] == "house rules"


# --- durations / caps -------------------------------------------------------------------------


def test_timeout_all_clamps_per_platform(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    out = mod_reply(host, f"!timeout {TTOK} 4w")
    assert (
        out == "timed out on twitch, discord, youtube for 4w "
        "(twitch capped at 2w; youtube capped at 2w)"
    )
    by = {c[1]: c[2] for c in tools.calls}
    assert by["twitch.moderation.timeout"]["duration"] == 1_209_600
    assert by["discord.moderation.timeout"]["duration_seconds"] == 2_419_200
    assert by["youtube.chat.ban"]["duration_seconds"] == 1_209_600


def test_timeout_explicit_platform_over_cap_is_refused(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    assert mod_reply(host, f"!timeout {TTOK} 3w twitch") == "twitch: max timeout is 2w"
    assert tools.calls == []


# --- authorization / protection ---------------------------------------------------------------


def test_non_moderator_is_refused_by_the_host_not_by_badges(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    for name in ("twitch.moderation.ban", "discord.moderation.ban", "youtube.chat.ban"):
        tools.fail[name] = ("forbidden", "")
    out = reply(host, f"!ban {TTOK}")  # no is_mod badge: still reaches the host gate
    assert out.count("refused (no permission, or the target is protected)") == 3
    assert "banned on" not in out
    assert len(tools.calls) == 3  # the badge was never trusted to deny OR allow


def test_not_granted_is_reported_not_retried(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    tools.fail["twitch.moderation.ban"] = ("not_granted", "")
    out = mod_reply(host, f"!ban {TTOK} twitch")
    assert out == "twitch: refused (no permission, or the target is protected)"
    assert len(tools.calls) == 1


def test_mod_on_mod_refusal_surfaces_from_the_platform_gate(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    tools.fail["discord.moderation.timeout"] = ("forbidden", "")
    out = mod_reply(host, f"!timeout {TTOK} 10m discord")
    assert out == "discord: refused (no permission, or the target is protected)"


def test_self_target_refused_before_any_tool_call(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    host.ident.add_mention("tok-author", ACTOR)
    assert mod_reply(host, "!ban {user:tok-author}") == "you can't moderate yourself"
    assert tools.calls == []


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("unknown", "couldn't find that user"),
        ("unlinked", "isn't linked"),
        ("ambiguous", "more than one person"),
    ],
)
def test_target_resolution_failures(
    setup: str,
    expected: str,
    host: _Host,
    tools: ToolHost,  # noqa: F811
) -> None:
    if setup == "unlinked":
        host.ident.add_unlinked_mention("tok-target")
    elif setup == "ambiguous":
        host.ident.add_ambiguous_mention("tok-target")
    else:
        host.ident._mentions.clear()
    assert expected in mod_reply(host, f"!ban {TTOK}")
    assert tools.calls == []


# --- audit / history / privacy ----------------------------------------------------------------


def test_audit_log_is_pii_free_and_history_records_outcomes(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    tools.fail["youtube.chat.ban"] = ("invalid_args", "no linked account")
    mod_reply(host, f"!ban {TTOK} secret reason text")
    logged = host.logged()
    assert "secret reason text" not in logged and "tok-target" not in logged
    assert (
        ACTOR in logged
        and TARGET in logged
        and '"has_reason": true' in logged.replace("true", "true")
    )
    log_out = mod_reply(host, "!modlog 5")
    assert log_out.count("#") == 3
    assert "ban youtube invalid_args" in log_out and "ban twitch ok" in log_out
    assert TARGET not in log_out  # abbreviated only


def test_modlog_empty_and_bad_args(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    assert mod_reply(host, "!modlog") == "no moderation actions recorded yet"
    assert mod_reply(host, "!modlog nope") == "Usage: !modlog [n]"


def test_history_ring_is_bounded(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    for _ in range(moderation.HISTORY_SLOTS + 5):
        mod_reply(host, f"!unban {TTOK} discord")
    slots = [k for k in host.kv.store if "moderation.hist." in k and not k.endswith(".seq")]
    assert len(slots) == moderation.HISTORY_SLOTS


def test_missing_message_id_fails_loud_without_acting(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    out = _transform(host, _event(f"!ban {TTOK}", drop=("message_id",), is_mod=True))
    assert out is not None and "can't be processed" in out.payload["text"]
    assert tools.calls == []


def test_reply_relays_through_dispatch(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    out = _transform(host, _event(f"!ban {TTOK} discord", is_mod=True))
    assert out is not None
    send(host, out)
    assert host.relay_calls[-1][1]["text"] == "banned on discord"


def test_moderation_lines_are_screened_first(host: _Host, tools: ToolHost) -> None:  # noqa: F811
    from test_app import configure

    configure(host, enabled={"links"})
    out = say(host, f"!ban {TTOK} see https://evil.example")  # non-mod line with a link
    assert out is not None and out.payload["command"] == "violation"
    assert tools.calls == []


def test_unknown_tool_error_codes_are_all_mapped() -> None:
    for code in _TOOL_CASES:
        text = moderation._failure_text(moderation.tool.ToolError(code, ""))
        assert text
    assert _run is not None
