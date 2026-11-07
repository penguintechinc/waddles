"""Golden-vector parity test: the same fixture drives the Rust port (`waddle-sdk-rs`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from waddle_sdk.command import (
    CliArgument,
    CliCommandSpec,
    CommandSpec,
    CommandUsageError,
    MissingPlaceholderError,
    extract_placeholders,
    parse_cli_command,
    parse_command,
    substitute_placeholders,
)

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "testdata" / "command-vectors.json").read_text("utf-8")
)


def _cli_spec(d: dict) -> CliCommandSpec:
    return CliCommandSpec(
        name=d["name"],
        arguments=tuple(
            CliArgument(a["name"], tuple(a["flags"]), a["required"], a["kind"], a["default"])
            for a in d.get("arguments", [])
        ),
        subcommands=tuple(_cli_spec(s) for s in d.get("subcommands", [])),
        default_subcommand=d.get("default_subcommand"),
    )


def test_fixture_is_non_empty() -> None:
    assert len(_VECTORS["parse_command"]) >= 40
    assert len(_VECTORS["placeholders"]) >= 5
    assert len(_VECTORS["cli"]) >= 40


@pytest.mark.parametrize("case", _VECTORS["parse_command"], ids=lambda c: repr(c["text"])[:60])
def test_parse_command_vectors(case: dict) -> None:
    spec = CommandSpec(name=case["spec"]["name"], sub_modules=frozenset(case["spec"]["sub_modules"]))
    if "error" in case:
        with pytest.raises(CommandUsageError) as exc:
            parse_command(case["text"], spec)
        assert str(exc.value) == case["error"]
        return
    r = parse_command(case["text"], spec)
    assert {
        "command": r.command,
        "sub_module": r.sub_module,
        "option": r.option,
        "args": r.args,
    } == case["expect"]


@pytest.mark.parametrize("case", _VECTORS["placeholders"], ids=lambda c: c["template"])
def test_placeholder_vectors(case: dict) -> None:
    assert extract_placeholders(case["template"]) == case["extract"]
    if "missing" in case:
        with pytest.raises(MissingPlaceholderError) as exc:
            substitute_placeholders(case["template"], case["values"])
        assert exc.value.args[0] == case["missing"]
    else:
        assert substitute_placeholders(case["template"], case["values"]) == case["substitute"]


@pytest.mark.parametrize("case", _VECTORS["cli"], ids=lambda c: repr(c["text"])[:60])
def test_cli_vectors(case: dict) -> None:
    spec = _cli_spec(case["spec"])
    if "error" in case:
        with pytest.raises(CommandUsageError):
            parse_cli_command(case["text"], spec)
        return
    r = parse_cli_command(case["text"], spec)
    exp = case["expect"]
    assert (r.command, r.subcommand) == (exp["command"], exp["subcommand"])
    if exp["help"]:
        assert r.help
    else:
        assert r.help is None
        assert r.values == exp["values"]
