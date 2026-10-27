r"""Regression coverage for reputation_events / reputation_tenant.

Both tables are read/written by ``services/reputation_service.py`` (and
``services/policy_enforcer.py``) but had no ``CREATE TABLE`` migration --
every one of those call sites failed at runtime with
``relation "reputation_events" does not exist`` /
``relation "reputation_tenant" does not exist``. Migrations
080_add_reputation_tables.sql / 097_reputation_tenant_scope.sql add both
tables (097 re-scoping the original, cross-tenant ``reputation_global`` to
``reputation_tenant``, keyed by ``(tenant_id, hub_user_id)`` --
security.md Tenant Isolation); this suite exercises the real read/write
SQL against a live Postgres running the actual migration files (not a
hand-defined pydal/SQLite schema), so a regression on either table's
columns is caught here instead of in production.

``ReputationService.get_reputation()``/``.adjust()``/``.set_reputation()``/
``.get_leaderboard()``/``.initialize_member()`` previously also joined
against ``community_members.hub_user_id`` -- a column that does not exist on
that table (it has ``user_id VARCHAR``, see
config/postgres/migrations/000_create_base_schema.sql /
037_fix_community_schema.sql). That was a separate, pre-existing bug
(gh-299) outside this migration's original scope -- fixed in
``services/reputation_service.py`` to match on ``user_id``/platform
identity instead, and now covered by
``test_adjust_writes_community_and_tenant_reputation`` below. The rest of
this suite covers the code paths whose *only* runtime dependency was the
missing reputation_events/reputation_tenant tables --
``_update_tenant_reputation()``/``get_tenant_reputation()``,
``get_history()``, and ``get_tenant_leaderboard()``.

Run locally against a fresh migrated Postgres, e.g.:

    docker run -d -p 55432:5432 -e POSTGRES_USER=waddlebot \\
        -e POSTGRES_PASSWORD=password -e POSTGRES_DB=waddlebot \\
        postgres:17-bookworm
    for f in config/postgres/migrations/*.sql; do
        psql postgresql://waddlebot:password@localhost:55432/waddlebot -f "$f"
    done
    TEST_DATABASE_URL=postgresql://waddlebot:password@localhost:55432/waddlebot \\
        pytest core/reputation_module/tests/test_reputation_tables.py -v
"""

from __future__ import annotations

import os
import sys

import pytest
from pydal import DAL

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import Config  # noqa: E402
from services.reputation_service import ReputationService  # noqa: E402
from services.weight_manager import WeightManager  # noqa: E402
from tests.conftest import NullLogger  # noqa: E402


async def test_tenant_reputation_round_trip(
    dal: DAL, seeded_ids: tuple[int, int], seeded_tenant_id: int
) -> None:
    """_update_tenant_reputation()'s upsert + get_tenant_reputation()'s SELECT.

    Both hit reputation_tenant exclusively -- no community_members join.
    Pre-097 (pre-080 for the original reputation_global) this raised
    ``relation "reputation_tenant" does not exist``.
    """
    community_id, hub_user_id = seeded_ids
    weight_manager = WeightManager(dal, NullLogger())
    service = ReputationService(dal, weight_manager, NullLogger())

    # No row yet -- default reputation returned, not an error.
    default_rep = await service.get_tenant_reputation(seeded_tenant_id, hub_user_id)
    assert default_rep is not None
    assert default_rep.score == Config.REPUTATION_DEFAULT
    assert default_rep.total_events == 0

    # First event: upsert inserts a new row.
    await service._update_tenant_reputation(community_id, hub_user_id, score_change=5.0)
    dal.commit()

    after_first = await service.get_tenant_reputation(seeded_tenant_id, hub_user_id)
    assert after_first is not None
    assert after_first.score == Config.REPUTATION_DEFAULT + 5
    assert after_first.total_events == 1
    # The INSERT's VALUES clause only sets tenant_id/hub_user_id/score/
    # total_events -- last_event_at is only assigned on the UPDATE branch,
    # so it is still NULL after the very first event (matches the code's
    # own INSERT statement, not a schema gap).
    assert after_first.last_event_at is None

    # Second event: upsert hits the UPDATE branch, which does set
    # last_event_at.
    await service._update_tenant_reputation(community_id, hub_user_id, score_change=-2.0)
    dal.commit()

    after_second = await service.get_tenant_reputation(seeded_tenant_id, hub_user_id)
    assert after_second is not None
    assert after_second.score == Config.REPUTATION_DEFAULT + 5 - 2
    assert after_second.total_events == 2
    assert after_second.last_event_at is not None

    # Score is clamped to the FICO-style [300, 850] bounds by
    # ReputationService._clamp_score() before the write.
    await service._update_tenant_reputation(community_id, hub_user_id, score_change=-10_000.0)
    dal.commit()
    clamped = await service.get_tenant_reputation(seeded_tenant_id, hub_user_id)
    assert clamped is not None
    assert clamped.score == 300


async def test_reputation_events_insert_and_history(dal: DAL, seeded_ids: tuple[int, int]) -> None:
    """Insert matching adjust()/set_reputation()'s exact reputation_events columns.

    Then read it back via get_history() -- both hit reputation_events
    exclusively. Pre-080 the INSERT raised
    ``relation "reputation_events" does not exist``.
    """
    community_id, hub_user_id = seeded_ids
    weight_manager = WeightManager(dal, NullLogger())
    service = ReputationService(dal, weight_manager, NullLogger())

    assert await service.get_history(community_id, hub_user_id) == []

    dal.executesql(
        """INSERT INTO reputation_events
           (community_id, hub_user_id, platform, platform_user_id,
            event_type, score_change, score_before, score_after,
            reason, metadata)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        [
            community_id,
            hub_user_id,
            "discord",
            "repmig-platform-user",
            "follow",
            1.0,
            600,
            601,
            "regression test",
            '{"source": "test"}',
        ],
    )
    dal.commit()

    history = await service.get_history(community_id, hub_user_id)
    assert len(history) == 1
    event = history[0]
    assert event.event_type == "follow"
    assert event.score_change == pytest.approx(1.0)
    assert event.score_before == 600
    assert event.score_after == 601
    assert event.reason == "regression test"
    assert event.metadata == {"source": "test"}

    # get_reputation()'s two correlated subqueries against reputation_events
    # (COUNT(*)/MAX(created_at)) use the same (community_id, hub_user_id)
    # index this migration adds -- verify the raw subquery shape directly
    # since get_reputation() itself also joins community_members.hub_user_id
    # (the separate, out-of-scope bug documented in this file's docstring).
    counts = dal.executesql(
        """SELECT COUNT(*), MAX(created_at) FROM reputation_events re
           WHERE re.community_id = %s AND re.hub_user_id = %s""",
        [community_id, hub_user_id],
    )
    assert counts[0][0] == 1
    assert counts[0][1] is not None


async def test_tenant_leaderboard(
    dal: DAL, seeded_ids: tuple[int, int], seeded_tenant_id: int
) -> None:
    """get_tenant_leaderboard() joins reputation_tenant to hub_users, scoped to tenant_id.

    No community_members involved, so it is fully exercised by this
    migration. Pre-097 this raised
    ``relation "reputation_tenant" does not exist``.
    """
    community_id, hub_user_id = seeded_ids
    weight_manager = WeightManager(dal, NullLogger())
    service = ReputationService(dal, weight_manager, NullLogger())

    await service._update_tenant_reputation(community_id, hub_user_id, score_change=50.0)
    dal.commit()

    leaderboard = await service.get_tenant_leaderboard(seeded_tenant_id, limit=10)
    assert any(row["user_id"] == hub_user_id for row in leaderboard)
    entry = next(row for row in leaderboard if row["user_id"] == hub_user_id)
    assert entry["score"] == Config.REPUTATION_DEFAULT + 50
    assert entry["total_events"] == 1
    assert entry["rank"] >= 1


async def test_adjust_writes_community_and_tenant_reputation(
    dal: DAL, seeded_ids: tuple[int, int], seeded_tenant_id: int
) -> None:
    # regression: gh-299
    """adjust() must persist a real delta to BOTH reputation tiers.

    Pre-fix, every accrual write in ``adjust()`` (and
    ``set_reputation()``/``get_reputation()``/``get_leaderboard()``/
    ``initialize_member()``) filtered ``community_members`` on a
    ``hub_user_id`` column that does not exist on that table --
    ``community_members`` only has ``user_id VARCHAR`` (storing
    ``str(hub_user_id)``) -- so every one of those statements raised
    ``column cm.hub_user_id does not exist`` and reputation never changed.
    This exercises the real ``adjust()`` write path end to end against the
    actual migrated schema: the seeded ``community_members`` row (matched
    by ``user_id``, not the phantom column) and ``reputation_tenant``
    (matched by its real ``(tenant_id, hub_user_id)`` composite key) both
    receive a persisted, non-zero delta.
    """
    community_id, hub_user_id = seeded_ids
    weight_manager = WeightManager(dal, NullLogger())
    service = ReputationService(dal, weight_manager, NullLogger())

    platform = "discord"
    platform_user_id = f"repmig-adjust-{hub_user_id}"

    # Seed the community_members row adjust() will update -- linked to the
    # hub user via the table's real `user_id` column.
    dal.executesql(
        """INSERT INTO community_members
           (community_id, user_id, platform, platform_user_id, reputation, role)
           VALUES (%s, %s, %s, %s, %s, 'member')""",
        [community_id, str(hub_user_id), platform, platform_user_id, 600],
    )
    dal.commit()

    result = await service.adjust(
        community_id=community_id,
        user_id=hub_user_id,
        event_type="follow",
        platform=platform,
        platform_user_id=platform_user_id,
    )

    assert result.success, result.error
    assert result.error is None
    assert result.score_change != 0.0
    assert result.score_before == 600
    assert result.score_after == result.score_before + result.score_change

    # community_members.reputation actually moved -- looked up by the real
    # `user_id` column, never the nonexistent `hub_user_id`.
    community_row = dal.executesql(
        "SELECT reputation FROM community_members WHERE community_id = %s AND user_id = %s",
        [community_id, str(hub_user_id)],
    )
    assert community_row[0][0] == result.score_after

    # reputation_tenant.score also moved -- looked up by its real
    # (tenant_id, hub_user_id) composite key.
    tenant_row = dal.executesql(
        "SELECT score FROM reputation_tenant WHERE tenant_id = %s AND hub_user_id = %s",
        [seeded_tenant_id, hub_user_id],
    )
    assert tenant_row[0][0] == Config.REPUTATION_DEFAULT + result.score_change


async def test_adjust_chat_message_accrues_community_and_tenant_reputation(
    dal: DAL, seeded_ids: tuple[int, int], seeded_tenant_id: int
) -> None:
    # regression: gh-310
    """`adjust(event_type="chat_message")` must process end to end and audit-log the real weight.

    `services/activity_accrual.py` (svc-process) is the first production
    caller of `adjust()` with a positive-activity `event_type` -- before
    gh-310, the sole caller was `svc_process.services.moderation_gate` with
    a fixed `event_type="warn"` (a penalty), so reputation never accrued
    from ordinary activity at all. This exercises `adjust()` against the
    real migrated schema for `chat_message` specifically: the write
    succeeds, a `reputation_tenant` row is created for the (now
    hub-linked) user with `total_events` incremented, and exactly one
    `reputation_events` audit row is written preserving the full-precision
    `score_change` (that column is `DECIMAL(10,4)`, migration
    080_add_reputation_tables.sql).

    FIXED (gh-310): `WeightManager`'s default `chat_message`/`command_usage`
    weights were previously ``0.01``/``-0.1`` (`CommunityWeights`,
    `services/weight_manager.py`) against `INTEGER`-typed
    `community_members.reputation`/`reputation_global.score` columns --
    `_clamp_score()` re-rounded the already-STORED integer on every call
    with no column to carry the sub-1.0 remainder across events, so
    ``round(600 + 0.01) == 600`` on every single call, forever (empirically
    verified against a live migrated Postgres: 100 consecutive
    `chat_message` adjustments left both scores unchanged at 600). Fixed by
    making both defaults whole, positive integers (``1.0`` each -- activity
    farming is bounded by svc-process's own per-user cooldowns, not by a
    fractional weight) so a single event always moves the score by exactly
    the configured amount; `_clamp_score()`'s documented, still-real
    behavior for a *premium* community that configures its own sub-1.0
    override is exercised separately in
    `tests/test_reputation_fractional_weights.py`.
    """
    community_id, hub_user_id = seeded_ids
    weight_manager = WeightManager(dal, NullLogger())
    service = ReputationService(dal, weight_manager, NullLogger())

    platform = "twitch"
    platform_user_id = f"repmig-chat-{hub_user_id}"
    expected_weight = weight_manager._default_weights.chat_message
    assert expected_weight != 0.0  # sanity: this event type must actually carry weight

    # Seed the community_members row adjust() will update -- linked to the
    # hub user via the table's real `user_id` column, same convention
    # `test_adjust_writes_community_and_global_reputation` uses above.
    dal.executesql(
        """INSERT INTO community_members
           (community_id, user_id, platform, platform_user_id, reputation, role)
           VALUES (%s, %s, %s, %s, %s, 'member')""",
        [community_id, str(hub_user_id), platform, platform_user_id, 600],
    )
    dal.commit()

    assert await service.get_history(community_id, hub_user_id) == []

    result = await service.adjust(
        community_id=community_id,
        user_id=hub_user_id,
        event_type="chat_message",
        platform=platform,
        platform_user_id=platform_user_id,
        metadata={"event_id": "evt-repmig-310", "source": "activity_accrual"},
    )

    assert result.success, result.error
    assert result.error is None
    assert result.score_change == pytest.approx(expected_weight)
    assert result.score_before == 600
    # gh-310 fix: a whole-number weight always moves the score by exactly
    # that amount on a single event.
    assert result.score_after == 600 + int(expected_weight)

    community_row = dal.executesql(
        "SELECT reputation FROM community_members WHERE community_id = %s AND user_id = %s",
        [community_id, str(hub_user_id)],
    )
    assert community_row[0][0] == 600 + int(expected_weight)

    # reputation_tenant row is created for this now-hub-linked user, with
    # the same delta applied via ReputationService._clamp_score (see
    # _update_tenant_reputation's docstring) and total_events incremented.
    tenant_row = dal.executesql(
        "SELECT score, total_events FROM reputation_tenant "
        "WHERE tenant_id = %s AND hub_user_id = %s",
        [seeded_tenant_id, hub_user_id],
    )
    assert tenant_row[0][0] == Config.REPUTATION_DEFAULT + int(expected_weight)
    assert tenant_row[0][1] == 1

    # Exactly one reputation_events audit row was written for this accrual,
    # preserving the full-precision configured weight (DECIMAL column).
    history = await service.get_history(community_id, hub_user_id)
    assert len(history) == 1
    event = history[0]
    assert event.event_type == "chat_message"
    assert event.score_change == pytest.approx(expected_weight)
    assert event.score_before == 600
    assert event.score_after == 600 + int(expected_weight)
    assert event.metadata == {"event_id": "evt-repmig-310", "source": "activity_accrual"}


async def test_tenant_reputation_never_leaks_across_tenants(
    dal: DAL, seeded_ids: tuple[int, int], seeded_tenant_id: int
) -> None:
    """A second, independent tenant's reputation must never surface under tenant A.

    Hard isolation boundary (security.md Tenant Isolation): seeds a SECOND
    tenant/community/hub_user set (never through `seeded_ids`, which is
    tenant A's own fixture) and drives the exact same `hub_user_id` value
    as tenant A's seeded user so a key collision bug (bare `hub_user_id`
    lookup forgetting the `tenant_id` filter -- precisely the
    `reputation_global` bug this migration fixes) would otherwise merge the
    two tenants' scores.
    """
    community_a, hub_user_id = seeded_ids
    tenant_a = seeded_tenant_id

    suffix = hub_user_id  # reuse seeded_ids' own uniqueness suffix
    tenant_b = suffix + 1
    community_b = suffix + 1

    dal.executesql(
        "INSERT INTO tenants (id, slug, display_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        [tenant_b, f"repmig-tenant-b-{suffix}", "Reputation Migration Test Tenant B"],
    )
    dal.executesql(
        "INSERT INTO communities "
        "(id, name, display_name, primary_platform, platform, tenant_id) "
        "VALUES (%s, %s, %s, 'discord', 'discord', %s) ON CONFLICT DO NOTHING",
        [
            community_b,
            f"repmig-community-b-{suffix}",
            "Reputation Migration Test Community B",
            tenant_b,
        ],
    )
    dal.commit()

    try:
        weight_manager = WeightManager(dal, NullLogger())
        service = ReputationService(dal, weight_manager, NullLogger())

        platform, platform_user_id_a = "discord", f"repmig-isolation-a-{suffix}"
        platform_user_id_b = f"repmig-isolation-b-{suffix}"

        # Same hub_user_id, same platform, DIFFERENT tenants (via different
        # communities) -- the exact shape a tenant-forgetting query would
        # conflate.
        dal.executesql(
            """INSERT INTO community_members
               (community_id, user_id, platform, platform_user_id, reputation, role)
               VALUES (%s, %s, %s, %s, 600, 'member')""",
            [community_a, str(hub_user_id), platform, platform_user_id_a],
        )
        dal.executesql(
            """INSERT INTO community_members
               (community_id, user_id, platform, platform_user_id, reputation, role)
               VALUES (%s, %s, %s, %s, 600, 'member')""",
            [community_b, str(hub_user_id), platform, platform_user_id_b],
        )
        dal.commit()

        result_a = await service.adjust(
            community_id=community_a, user_id=hub_user_id, event_type="donation",
            platform=platform, platform_user_id=platform_user_id_a, amount_multiplier=50.0,
        )
        assert result_a.success, result_a.error

        result_b = await service.adjust(
            community_id=community_b, user_id=hub_user_id, event_type="warn",
            platform=platform, platform_user_id=platform_user_id_b,
        )
        assert result_b.success, result_b.error

        tenant_a_info = await service.get_tenant_reputation(tenant_a, hub_user_id)
        tenant_b_info = await service.get_tenant_reputation(tenant_b, hub_user_id)

        # Tenant A only saw its own +50 donation; tenant B only saw its own
        # -25 warn. Neither aggregate includes the other tenant's delta.
        assert tenant_a_info.score == Config.REPUTATION_DEFAULT + 50
        assert tenant_b_info.score == Config.REPUTATION_DEFAULT - 25

        # Leaderboards are equally isolated -- tenant A's leaderboard never
        # contains a row seeded only under tenant B's id, and vice versa.
        leaderboard_a = await service.get_tenant_leaderboard(tenant_a, limit=50)
        leaderboard_b = await service.get_tenant_leaderboard(tenant_b, limit=50)
        assert all(row["score"] != Config.REPUTATION_DEFAULT - 25 for row in leaderboard_a)
        assert all(row["score"] != Config.REPUTATION_DEFAULT + 50 for row in leaderboard_b)
    finally:
        dal.executesql("DELETE FROM reputation_events WHERE community_id = %s", [community_b])
        dal.executesql(
            "DELETE FROM reputation_tenant WHERE tenant_id = %s AND hub_user_id = %s",
            [tenant_b, hub_user_id],
        )
        dal.executesql("DELETE FROM community_members WHERE community_id = %s", [community_a])
        dal.executesql("DELETE FROM community_members WHERE community_id = %s", [community_b])
        dal.executesql("DELETE FROM communities WHERE id = %s", [community_b])
        dal.executesql("DELETE FROM tenants WHERE id = %s", [tenant_b])
        dal.commit()
