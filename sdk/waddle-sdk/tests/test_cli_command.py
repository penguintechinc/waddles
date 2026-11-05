"""Shared CLI grammar tests pin flag typing, quoting, help and fail-loud schemas."""

import pytest

from waddle_sdk.command import CliArgument, CliCommandSpec, CommandUsageError, parse_cli_command


def spec() -> CliCommandSpec:
    """A representative command uses required short flags and optional positionals."""
    return CliCommandSpec(
        "poll",
        arguments=(
            CliArgument("title", ("-t", "--title"), help="poll title"),
            CliArgument("ttl", ("-T",), kind="integer", required=False, default=60),
            CliArgument("choice", required=False),
        ),
    )


def test_flags_quoted_values_and_typed_defaults() -> None:
    """Quoted spaces survive and integer options/defaults retain integer types."""
    parsed = parse_cli_command('!poll -t "favorite game" -T 30 "first choice"', spec())
    assert parsed.values == {"title": "favorite game", "ttl": 30, "choice": "first choice"}
    assert parse_cli_command("!poll -t game", spec()).values["ttl"] == 60


@pytest.mark.parametrize(
    "text", ["!poll", "!poll -t title -x bad", "!poll -t title -T nan", '!poll -t "unterminated']
)
def test_invalid_cli_refused(text: str) -> None:
    """Unknown flags, missing required values and bad quotes are typed usage errors."""
    with pytest.raises(CommandUsageError):
        parse_cli_command(text, spec())


def test_generated_help_does_not_require_arguments() -> None:
    """Help text comes from the declared schema without invoking a command handler."""
    parsed = parse_cli_command("!poll --help", spec())
    assert parsed.help is not None and "poll title" in parsed.help and "-t TITLE" in parsed.help
    assert parsed.values == {}


def test_subcommands_and_default_subcommand() -> None:
    """Explicit subcommands and the declared default share the same typed parsing."""
    command = CliCommandSpec(
        "points",
        subcommands=(
            CliCommandSpec("view", (CliArgument("user", required=False),)),
            CliCommandSpec("set", (CliArgument("amount", kind="integer"), CliArgument("user"))),
        ),
        default_subcommand="view",
    )
    assert parse_cli_command("!points", command).subcommand == "view"
    assert parse_cli_command("!points alice", command).values == {"user": "alice"}
    assert parse_cli_command("!points set 12 bob", command).values == {"amount": 12, "user": "bob"}
