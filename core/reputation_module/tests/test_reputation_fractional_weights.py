"""Regression coverage: gh-310 -- reputation accrual invisible on INTEGER score columns.

`community_members.reputation` and `reputation_global.score` are `INTEGER`
columns (`config/postgres/migrations/080_add_reputation_tables.sql`).
`WeightManager`'s defaults were ``chat_message = 0.01`` /
``command_usage = -0.1`` (`services/weight_manager.py`), and
`ReputationService._clamp_score()` re-rounded the already-STORED integer on
every single `adjust()` call with no column to carry a fractional
remainder across separate events -- `round(600 + 0.01) == 600` forever, so
100 consecutive `chat_message` adjustments left both scores at 600 while
only `reputation_events`/`total_events` moved (verified against a live
migrated Postgres, see `tests/test_reputation_tables.py`'s companion test).

Fixed by (a) making the default weights whole, positive integers (``1.0``
each -- svc-process's own per-user cooldowns, not the weight, bound
farming) and (b) `_clamp_score()` rounding `stored + delta` exactly once
via round-half-away-from-zero rather than Python's banker's-rounding
`round()`.

Documented, intentional consequence (no schema change / no remainder
column): a per-event weight whose magnitude is < 0.5 still truncates to a
zero delta on that call, every call, with no memory across events -- only
a *premium* community's custom `community_reputation_config` override can
configure a weight small enough to hit this, and this suite proves that
rule too (`TestFractionalOverrideTruncationRule`).

Uses an in-memory fake DAL (never a live Postgres) so this suite runs, and
proves the fix, in every environment -- ``tests/test_reputation_tables.py``
skips without ``TEST_DATABASE_URL``/``DATABASE_URL``.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from config import Config  # noqa: E402
from services.reputation_service import ReputationService, _round_half_away_from_zero  # noqa: E402
from services.weight_manager import WeightManager  # noqa: E402
from tests.conftest import NullLogger  # noqa: E402


class _InMemoryReputationDal:
    """Sync pydal-style stand-in that actually persists state across calls.

    Unlike `tests/test_reputation_service_audit.py`'s `_FakeReputationDal`
    (every SELECT empty, every write a no-op -- fine for a single-call
    signature check), this one keeps `community_members` / `reputation_global`
    state between repeated `adjust()` calls in the same test -- gh-310's bug
    only shows up across REPEATED events, never within a single call.
    """

    def __init__(
        self,
        community_config: tuple[Any, ...] | None = None,
    ) -> None:
        self._community_config = community_config
        self._members: dict[tuple[int, str, str], dict[str, Any]] = {}
        self._global: dict[int, dict[str, Any]] = {}
        self.reputation_events: list[dict[str, Any]] = []

    def executesql(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        params = list(params or [])
        stripped = sql.strip()

        if "FROM community_reputation_config" in sql:
            return [self._community_config] if self._community_config else []

        if "SELECT cm.id, cm.reputation, cm.user_id" in sql:
            community_id, platform, platform_user_id = params
            member = self._members.get((community_id, platform, platform_user_id))
            if member is None:
                return []
            return [(member["id"], member["reputation"], member["user_id"])]

        if stripped.startswith("INSERT INTO community_members"):
            community_id, user_id, platform, platform_user_id, reputation = params
            key = (community_id, platform, platform_user_id)
            self._members[key] = {
                "id": len(self._members) + 1,
                "reputation": reputation,
                "user_id": user_id,
            }
            return []

        if stripped.startswith("UPDATE community_members"):
            reputation, community_id, platform, platform_user_id = params
            self._members[(community_id, platform, platform_user_id)]["reputation"] = reputation
            return []

        if stripped.startswith("INSERT INTO reputation_events"):
            (community_id, hub_user_id, platform, platform_user_id,
             event_type, score_change, score_before, score_after,
             reason, metadata) = params
            self.reputation_events.append({
                "community_id": community_id,
                "hub_user_id": hub_user_id,
                "event_type": event_type,
                "score_change": score_change,
                "score_before": score_before,
                "score_after": score_after,
            })
            return []

        if "SELECT score FROM reputation_global" in sql:
            row = self._global.get(params[0])
            return [(row["score"],)] if row else []

        if stripped.startswith("UPDATE reputation_global"):
            score, hub_user_id = params
            entry = self._global[hub_user_id]
            entry["score"] = score
            entry["total_events"] += 1
            return []

        if stripped.startswith("INSERT INTO reputation_global"):
            hub_user_id, score = params[0], params[1]
            self._global[hub_user_id] = {"score": score, "total_events": 1}
            return []

        raise AssertionError(f"unexpected SQL in fake DAL: {sql!r}")

    def commit(self) -> None:
        return None

    def member_reputation(self, community_id: int, platform: str, platform_user_id: str) -> int:
        return int(self._members[(community_id, platform, platform_user_id)]["reputation"])

    def global_score(self, hub_user_id: int) -> int:
        return int(self._global[hub_user_id]["score"])


async def _seed_member(
    dal: _InMemoryReputationDal,
    community_id: int,
    user_id: int,
    platform: str,
    platform_user_id: str,
    reputation: int = 600,
) -> None:
    dal.executesql(
        """INSERT INTO community_members
           (community_id, user_id, platform, platform_user_id, reputation, role)
           VALUES (%s, %s, %s, %s, %s, 'member')""",
        [community_id, str(user_id), platform, platform_user_id, reputation],
    )


class TestChatMessageAccrualRegression:
    """# regression: gh-310."""

    async def test_repeated_chat_message_events_accrue_on_both_scopes(self) -> None:
        """100 default-weight chat_message events must move both scores by 100.

        Pre-fix (weight ``0.01``, `_clamp_score` re-rounding the stored
        integer every call): both scores stayed pinned at 600 after all 100
        events -- this is the exact scenario reported in gh-310.
        """
        dal = _InMemoryReputationDal()
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 42, 7
        platform, platform_user_id = "twitch", "u-gh310"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        for _ in range(100):
            result = await service.adjust(
                community_id=community_id,
                user_id=hub_user_id,
                event_type="chat_message",
                platform=platform,
                platform_user_id=platform_user_id,
            )
            assert result.success, result.error

        assert dal.member_reputation(community_id, platform, platform_user_id) == 700
        assert dal.global_score(hub_user_id) == Config.REPUTATION_DEFAULT + 100

    async def test_single_chat_message_moves_both_scopes_by_one(self) -> None:
        dal = _InMemoryReputationDal()
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 1, 2
        platform, platform_user_id = "discord", "u-1"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        result = await service.adjust(
            community_id=community_id,
            user_id=hub_user_id,
            event_type="chat_message",
            platform=platform,
            platform_user_id=platform_user_id,
        )

        assert result.success, result.error
        assert result.score_change == 1.0
        assert result.score_before == 600
        assert result.score_after == 601
        assert dal.member_reputation(community_id, platform, platform_user_id) == 601
        assert dal.global_score(hub_user_id) == Config.REPUTATION_DEFAULT + 1


class TestFractionalOverrideTruncationRule:
    """Premium `community_reputation_config` overrides can set sub-1.0 weights.

    `_clamp_score`'s documented rule: magnitude >= 0.5 moves the score by
    +/-1 on EVERY event (round-half-away-from-zero, no ties-to-even);
    magnitude < 0.5 truncates to a zero delta on every event, with no
    carried remainder across calls (no column exists to carry one without a
    migration).
    """

    @staticmethod
    def _premium_config_row(chat_message_weight: float) -> tuple[Any, ...]:
        # Matches WeightManager.get_weights()'s premium SELECT column order:
        # is_premium, chat_message, command_usage, giveaway_entry, follow,
        # subscription, subscription_tier2, subscription_tier3,
        # gift_subscription, donation_per_dollar, cheer_per_100bits, raid,
        # boost, warn, timeout, kick, ban, auto_ban_enabled,
        # auto_ban_threshold, starting_score, min_score, max_score.
        return (
            True, chat_message_weight, 1.0, -1.0, 1.0, 5.0, 10.0, 20.0, 3.0,
            1.0, 1.0, 2.0, 5.0, -25.0, -50.0, -75.0, -200.0, False, 450,
            600, 300, 850,
        )

    async def test_half_magnitude_override_moves_score_every_event(self) -> None:
        dal = _InMemoryReputationDal(community_config=self._premium_config_row(0.5))
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 9, 10
        platform, platform_user_id = "twitch", "u-half"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        for expected in (601, 602, 603):
            result = await service.adjust(
                community_id=community_id,
                user_id=hub_user_id,
                event_type="chat_message",
                platform=platform,
                platform_user_id=platform_user_id,
            )
            assert result.success, result.error
            assert result.score_change == 0.5
            assert result.score_after == expected

        assert dal.member_reputation(community_id, platform, platform_user_id) == 603
        assert dal.global_score(hub_user_id) == Config.REPUTATION_DEFAULT + 3

    async def test_sub_half_magnitude_override_never_moves_score(self) -> None:
        dal = _InMemoryReputationDal(community_config=self._premium_config_row(0.3))
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 11, 12
        platform, platform_user_id = "twitch", "u-sub-half"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        for _ in range(20):
            result = await service.adjust(
                community_id=community_id,
                user_id=hub_user_id,
                event_type="chat_message",
                platform=platform,
                platform_user_id=platform_user_id,
            )
            assert result.success, result.error
            assert result.score_change == pytest.approx(0.3)
            # Documented truncation: no carried remainder, every event.
            assert result.score_after == 600

        assert dal.member_reputation(community_id, platform, platform_user_id) == 600
        assert dal.global_score(hub_user_id) == Config.REPUTATION_DEFAULT


class TestModerationWeightAndClampBounds:
    async def test_negative_moderation_weight_decrements_both_scopes(self) -> None:
        dal = _InMemoryReputationDal()
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 20, 21
        platform, platform_user_id = "discord", "u-warn"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        result = await service.adjust(
            community_id=community_id,
            user_id=hub_user_id,
            event_type="warn",
            platform=platform,
            platform_user_id=platform_user_id,
            reason="test warn",
        )

        assert result.success, result.error
        assert result.score_change == -25.0
        assert result.score_before == 600
        assert result.score_after == 575
        assert dal.member_reputation(community_id, platform, platform_user_id) == 575
        assert dal.global_score(hub_user_id) == Config.REPUTATION_DEFAULT - 25

    async def test_repeated_bans_clamp_to_min_score(self) -> None:
        dal = _InMemoryReputationDal()
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 30, 31
        platform, platform_user_id = "discord", "u-ban"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        for _ in range(5):
            result = await service.adjust(
                community_id=community_id,
                user_id=hub_user_id,
                event_type="ban",
                platform=platform,
                platform_user_id=platform_user_id,
            )
            assert result.success, result.error

        assert result.score_after == Config.REPUTATION_MIN
        member_score = dal.member_reputation(community_id, platform, platform_user_id)
        assert member_score == Config.REPUTATION_MIN
        assert dal.global_score(hub_user_id) == Config.REPUTATION_MIN

    async def test_repeated_subscriptions_clamp_to_max_score(self) -> None:
        dal = _InMemoryReputationDal()
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        community_id, hub_user_id = 40, 41
        platform, platform_user_id = "discord", "u-sub"
        await _seed_member(dal, community_id, hub_user_id, platform, platform_user_id)

        for _ in range(60):
            result = await service.adjust(
                community_id=community_id,
                user_id=hub_user_id,
                event_type="subscription",
                platform=platform,
                platform_user_id=platform_user_id,
            )
            assert result.success, result.error

        assert result.score_after == Config.REPUTATION_MAX
        member_score = dal.member_reputation(community_id, platform, platform_user_id)
        assert member_score == Config.REPUTATION_MAX
        assert dal.global_score(hub_user_id) == Config.REPUTATION_MAX


class TestRoundHalfAwayFromZero:
    """Direct coverage of the rounding helper -- proves it is not Python's banker's rounding."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (600.5, 601),   # round() would give 600 (ties-to-even)
            (599.5, 600),
            (600.4, 600),
            (600.6, 601),
            (-600.5, -601),
            (-600.4, -600),
            (0.0, 0),
        ],
    )
    def test_rounds_ties_away_from_zero(self, value: float, expected: int) -> None:
        assert _round_half_away_from_zero(value) == expected
