"""Tests for `waddle_sdk.command` -- the standard `!<command>` grammar parser."""

from __future__ import annotations

import pytest

from waddle_sdk.command import (
    CommandSpec,
    CommandUsageError,
    MissingPlaceholderError,
    ParsedCommand,
    extract_placeholders,
    parse_command,
    substitute_placeholders,
)

_COUNT = CommandSpec(name="count")
_LURK = CommandSpec(name="lurk", sub_modules=frozenset({"ai"}))
_SHOUTOUT = CommandSpec(name="shoutout", sub_modules=frozenset({"auto", "ai"}))


class TestBareCommand:
    def test_bare_command_has_no_option_or_args(self) -> None:
        result = parse_command("!count", _COUNT)
        assert result == ParsedCommand(command="count", sub_module=None, option=None, args=None)

    def test_bare_command_is_case_insensitive(self) -> None:
        result = parse_command("!COUNT", _COUNT)
        assert result.command == "count"

    def test_wrong_command_name_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unrecognized command"):
            parse_command("!lurk", _COUNT)

    def test_empty_text_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="empty"):
            parse_command("   ", _COUNT)


class TestSetVerb:
    def test_set_with_args(self) -> None:
        result = parse_command("!count set 5", _COUNT)
        assert result == ParsedCommand(command="count", sub_module=None, option="set", args="5")

    def test_set_without_args(self) -> None:
        result = parse_command("!count set", _COUNT)
        assert result == ParsedCommand(command="count", sub_module=None, option="set", args=None)

    def test_set_preserves_argument_case(self) -> None:
        result = parse_command("!sr set youtube-labels Gaming,Music", CommandSpec(name="sr"))
        assert result.args == "youtube-labels Gaming,Music"


class TestAddSubVerbs:
    def test_add_with_args(self) -> None:
        result = parse_command("!count add 3", _COUNT)
        assert result.option == "add"
        assert result.args == "3"

    def test_add_without_args(self) -> None:
        result = parse_command("!count add", _COUNT)
        assert result.option == "add"
        assert result.args is None

    def test_sub_with_args(self) -> None:
        result = parse_command("!count sub 2", _COUNT)
        assert result.option == "sub"
        assert result.args == "2"


class TestListResetRemoveDelete:
    @pytest.mark.parametrize("verb", ["list", "reset", "remove", "delete"])
    def test_bare_verb(self, verb: str) -> None:
        result = parse_command(f"!count {verb}", _COUNT)
        assert result.option == verb
        assert result.args is None

    def test_remove_with_args(self) -> None:
        result = parse_command("!count remove 42", _COUNT)
        assert result.args == "42"


class TestEnableDisableToggle:
    def test_enable_submodule_option_first(self) -> None:
        result = parse_command("!lurk enable ai", _LURK)
        assert result == ParsedCommand(command="lurk", sub_module="ai", option="enable", args=None)

    def test_disable_submodule_option_first(self) -> None:
        result = parse_command("!lurk disable ai", _LURK)
        assert result == ParsedCommand(
            command="lurk", sub_module="ai", option="disable", args=None
        )

    def test_enable_submodule_option_second(self) -> None:
        result = parse_command("!lurk ai enable", _LURK)
        assert result == ParsedCommand(command="lurk", sub_module="ai", option="enable", args=None)

    def test_enable_is_case_insensitive_for_submodule(self) -> None:
        result = parse_command("!lurk enable AI", _LURK)
        assert result.sub_module == "ai"

    def test_enable_missing_submodule_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="requires a sub-module name"):
            parse_command("!lurk enable", _LURK)

    def test_enable_unknown_submodule_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unknown sub-module"):
            parse_command("!lurk enable bogus", _LURK)

    def test_enable_extra_argument_after_submodule_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="exactly one sub-module name"):
            parse_command("!lurk enable ai extra", _LURK)

    def test_enable_submodule_prefix_plus_trailing_value_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="takes no further arguments"):
            parse_command("!lurk ai enable extra", _LURK)


class TestSubModuleRouting:
    def test_submodule_alone_has_no_option(self) -> None:
        result = parse_command("!shoutout auto", _SHOUTOUT)
        assert result == ParsedCommand(
            command="shoutout", sub_module="auto", option=None, args=None
        )

    def test_submodule_then_set_with_input(self) -> None:
        result = parse_command("!shoutout auto set 30", _SHOUTOUT)
        assert result == ParsedCommand(
            command="shoutout", sub_module="auto", option="set", args="30"
        )

    def test_submodule_then_unknown_option_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unknown option"):
            parse_command("!shoutout auto frobnicate", _SHOUTOUT)

    def test_undeclared_submodule_token_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unknown option or sub-module"):
            parse_command("!shoutout bogus", _SHOUTOUT)

    def test_submodule_on_command_with_no_submodules_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unknown option or sub-module"):
            parse_command("!count auto", _COUNT)


class TestUnknownOption:
    def test_unknown_bare_option_raises(self) -> None:
        with pytest.raises(CommandUsageError, match="unknown option or sub-module"):
            parse_command("!count frobnicate", _COUNT)


class TestPlaceholderExtraction:
    def test_extracts_in_first_seen_order_deduplicated(self) -> None:
        template = "hi $(username), welcome to $(channel)! bye $(username)"
        assert extract_placeholders(template) == ["username", "channel"]

    def test_no_placeholders_returns_empty_list(self) -> None:
        assert extract_placeholders("no placeholders here") == []

    def test_substitute_replaces_every_occurrence(self) -> None:
        result = substitute_placeholders(
            "hi $(username), welcome to $(channel)!",
            {"username": "penguin", "channel": "general"},
        )
        assert result == "hi penguin, welcome to general!"

    def test_substitute_missing_value_raises(self) -> None:
        with pytest.raises(MissingPlaceholderError):
            substitute_placeholders("hi $(username)", {})
