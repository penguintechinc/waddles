"""Tests for flask_core.reserved_commands and flask_core.reputation_tiers."""

import pytest

from flask_core.reputation_tiers import REPUTATION_TIERS, reputation_tier
from flask_core.reserved_commands import (
    RESERVED_COMMANDS,
    get_conflicting_platforms,
    is_reserved_command,
)


class TestReservedCommands:
    def test_platform_specific_hit_and_miss(self):
        assert is_reserved_command("/ban", "twitch") is True
        assert is_reserved_command("/custom", "twitch") is False
        assert is_reserved_command("/kick", "twitch") is False

    def test_platform_name_is_case_insensitive(self):
        assert is_reserved_command("/kick", "DISCORD") is True

    def test_any_platform_scan(self):
        assert is_reserved_command("/shrug") is True  # slack only
        assert is_reserved_command("/definitely-not-reserved") is False

    def test_unknown_platform_raises(self):
        with pytest.raises(ValueError, match="Unknown platform"):
            is_reserved_command("/ban", "myspace")

    def test_conflicting_platforms(self):
        assert get_conflicting_platforms("/ban") == ["twitch", "discord", "youtube", "kick"]
        assert get_conflicting_platforms("/me") == ["twitch", "slack"]
        assert get_conflicting_platforms("/custom") == []

    def test_every_listed_command_reports_its_platform(self):
        for platform, commands in RESERVED_COMMANDS.items():
            for cmd in commands:
                assert platform in get_conflicting_platforms(cmd)


class TestReputationTier:
    @pytest.mark.parametrize(
        "score,label",
        [
            (300, "Newcomer"),
            (464, "Newcomer"),
            (465, "Regular"),
            (574, "Regular"),
            (575, "Trusted"),
            (657, "Trusted"),
            (658, "Respected"),
            (739, "Respected"),
            (740, "Champion"),
            (794, "Champion"),
            (795, "Legend"),
            (850, "Legend"),
        ],
    )
    def test_boundaries(self, score, label):
        assert reputation_tier(score) == label

    def test_out_of_range_never_raises(self):
        assert reputation_tier(-5) == "Newcomer"
        assert reputation_tier(10_000) == "Legend"

    def test_table_is_ascending(self):
        bounds = [b for b, _ in REPUTATION_TIERS]
        assert bounds == sorted(bounds)
