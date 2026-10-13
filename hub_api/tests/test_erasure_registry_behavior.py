"""Behaviour of the Art. 17 registry executor -- one generated check per rule, on every rule.

`erasure_registry.RULES` is the specification; this suite is generated FROM it. For every DELETE and
SCRUB rule and every match arm it seeds a row that belongs to the subject plus decoys that must
survive (another user's id, the same id on the wrong platform, a prefix-lookalike id), runs the
real executor once against a real sqlite schema built from the registry, then checks each rule. A
rule added to the registry is exercised here with no test edit -- including `tables_examined`,
which must equal the number of executed tables (a pass over zero tables is a failure).

Real pydal + real SQL, no mocking of the query builder; the only doubles are for the network
stores (see `tests/erasure_support.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import erasure_executor as executor
from services import erasure_registry as registry
from services.erasure_executor import ErasureSubject, RegistryResult
from services.erasure_registry import Action, Key, Match, Rule
from tests.erasure_support import create_missing_registry_tables

USER_ID = 7
SUBJECT = ErasureSubject(
    user_id=USER_ID,
    identities=(("discord", "d-7"), ("twitch", "t-7")),
    member_ids=(70, 71),
    user_uuids=("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"),
)
PII = "pii-sentinel"
KEEP = "keep-sentinel"


@dataclass(slots=True)
class Seeded:
    """Rows seeded for one (rule, match arm): ids that must go/change, ids that must not."""

    rule: Rule
    match: Match
    subject: list[int] = field(default_factory=list)
    decoys: list[int] = field(default_factory=list)
    decoy_values: dict[int, dict[str, Any]] = field(default_factory=dict)


def _subject_value(match: Match) -> Any:
    if match.key is Key.HUB_USER:
        return USER_ID
    if match.key is Key.MEMBER:
        return SUBJECT.member_ids[0]
    if match.key is Key.USER_UUID:
        return SUBJECT.user_uuids[0]
    return "d-7"


def _decoy_values(match: Match) -> list[dict[str, Any]]:
    """Column assignments (for the match columns only) that must NOT be treated as the subject."""
    if match.key is Key.HUB_USER:
        return [{match.column: 8}, {match.column: 70}, {match.column: None}]
    if match.key is Key.MEMBER:
        return [{match.column: 72}, {match.column: None}]
    if match.key is Key.USER_UUID:
        return [{match.column: "33333333-3333-3333-3333-333333333333"}, {match.column: None}]
    assert match.platform_column is not None
    return [
        {match.platform_column: "slack", match.column: "d-7"},  # right id, wrong platform
        {match.platform_column: "discord", match.column: "d-70"},  # right platform, other id
        {match.platform_column: "discord", match.column: None},
    ]


def _subject_assignment(match: Match) -> dict[str, Any]:
    if match.key is Key.PLATFORM_ID:
        assert match.platform_column is not None
        return {match.platform_column: "discord", match.column: "d-7"}
    return {match.column: _subject_value(match)}


@pytest.fixture(scope="module")
def world(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Any, list[Seeded], RegistryResult]]:
    """A sqlite schema holding every registry table, seeded for every rule, then erased once."""
    path: Path = tmp_path_factory.mktemp("registry") / "registry.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    create_missing_registry_tables(dal)
    seeded: list[Seeded] = []
    for rule in registry.executable_rules():
        alias = executor._alias(dal, rule.table)
        for match in rule.matches:
            entry = Seeded(rule, match)
            blanks: dict[str, Any] = {}
            for column in (*rule.set_null, *rule.set_placeholder):
                blanks[column] = PII
            subject_values = {**blanks, **_subject_assignment(match)}
            entry.subject.append(int(alias.insert(**subject_values)))
            for assignment in _decoy_values(match):
                values = {**dict.fromkeys(blanks, KEEP), **assignment}
                decoy_id = int(alias.insert(**values))
                entry.decoys.append(decoy_id)
                entry.decoy_values[decoy_id] = values
            seeded.append(entry)
    dal.commit()
    result = executor.run_registry_erasure(dal, SUBJECT)
    dal.commit()
    yield async_dal, seeded, result
    dal.close()


def _rule_id(rule: Rule) -> str:
    cols = "+".join(sorted(m.column for m in rule.matches))
    return f"{rule.action.value}:{rule.table}:{cols}"


def _ids(seeded: list[Seeded]) -> list[Seeded]:
    return seeded


def _exists(dal: Any, table: str, ids: list[int]) -> set[int]:
    alias = executor._alias(dal, table)
    rows = dal(alias.id.belongs(ids)).select(alias.id)
    dal.commit()
    return {int(r.id) for r in rows}


def _row(dal: Any, table: str, row_id: int) -> Any:
    alias = executor._alias(dal, table)
    row = dal(alias.id == row_id).select().first()
    dal.commit()
    return row


class TestEveryRule:
    """Generated from the registry: each DELETE/SCRUB arm does exactly its job."""

    def test_the_run_examined_every_executable_table(
        self, world: tuple[Any, list[Seeded], RegistryResult]
    ) -> None:
        _dal, seeded, result = world
        assert len(seeded) >= 85  # the arms, not just the tables -- a vacuous seed fails here
        assert result.tables_examined == len(registry.executed_tables()) >= 75
        assert result.tables_absent == ()
        assert result.rules_skipped == ()

    def test_every_delete_arm_removes_the_subjects_rows_and_only_those(
        self, world: tuple[Any, list[Seeded], RegistryResult]
    ) -> None:
        async_dal, seeded, _result = world
        dal = async_dal.dal
        checked = 0
        for entry in seeded:
            if entry.rule.action is not Action.DELETE:
                continue
            table = entry.rule.table
            assert (
                _exists(dal, table, entry.subject) == set()
            ), f"{_rule_id(entry.rule)} left the subject's row behind"
            assert _exists(dal, table, entry.decoys) == set(
                entry.decoys
            ), f"{_rule_id(entry.rule)} deleted somebody else's row"
            checked += 1
        assert checked >= 75

    def test_every_scrub_arm_blanks_pii_and_keeps_the_row(
        self, world: tuple[Any, list[Seeded], RegistryResult]
    ) -> None:
        async_dal, seeded, _result = world
        dal = async_dal.dal
        checked = 0
        for entry in seeded:
            rule = entry.rule
            if rule.action is not Action.SCRUB:
                continue
            [subject_id] = entry.subject
            row = _row(dal, rule.table, subject_id)
            assert row is not None, f"{_rule_id(rule)} deleted the row instead of scrubbing it"
            for column in rule.set_null:
                assert row[column] is None, (rule.table, column)
            for column in rule.set_placeholder:
                assert row[column] == registry.ERASED_PLACEHOLDER, (rule.table, column)
            for decoy_id in entry.decoys:
                decoy = _row(dal, rule.table, decoy_id)
                assert decoy is not None
                for column, value in entry.decoy_values[decoy_id].items():
                    assert (
                        decoy[column] == value
                    ), f"{_rule_id(rule)} scrubbed a row that is not the subject's"
            checked += 1
        assert checked >= 10

    def test_reported_counts_match_what_was_seeded(
        self, world: tuple[Any, list[Seeded], RegistryResult]
    ) -> None:
        _dal, seeded, result = world
        expected: dict[str, int] = {}
        for entry in seeded:
            key = (
                entry.rule.table
                if entry.rule.action is Action.DELETE
                else executor.scrubbed_key(entry.rule.table)
            )
            expected[key] = expected.get(key, 0) + len(entry.subject)
        assert result.counts == expected

    def test_no_decoy_anywhere_was_touched(
        self, world: tuple[Any, list[Seeded], RegistryResult]
    ) -> None:
        async_dal, seeded, _result = world
        dal = async_dal.dal
        for entry in seeded:
            assert _exists(dal, entry.rule.table, entry.decoys) == set(entry.decoys)


@pytest.fixture
def small_db(tmp_path: Path) -> Iterator[Any]:
    """A fresh, empty registry schema (per test) for the targeted cases below."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'small.db'}", pool_size=1)
    create_missing_registry_tables(async_dal.dal)
    yield async_dal
    async_dal.dal.close()


class TestSubjectShapes:
    """The executor must behave when the subject lacks some of the keys."""

    def test_no_identities_no_uuids_no_members_still_runs_the_hub_keyed_rules(
        self, small_db: Any
    ) -> None:
        dal = small_db.dal
        alias = executor._alias(dal, "activity_logs")
        alias.insert(user_id=USER_ID)
        alias.insert(user_id=8)
        dal.commit()
        result = executor.run_registry_erasure(dal, ErasureSubject(user_id=USER_ID))
        assert result.counts["activity_logs"] == 1
        assert dal(alias.user_id == 8).count() == 1

    def test_platform_rules_need_a_linked_identity(self, small_db: Any) -> None:
        dal = small_db.dal
        alias = executor._alias(dal, "loyalty_balances")
        alias.insert(platform="discord", platform_user_id="d-7")
        dal.commit()
        result = executor.run_registry_erasure(dal, ErasureSubject(user_id=USER_ID))
        assert "loyalty_balances" not in result.counts  # no identity -> nothing to match safely
        assert dal(alias.id > 0).count() == 1

    def test_economy_rows_follow_the_uuid_tokens_not_the_hub_id(self, small_db: Any) -> None:
        dal = small_db.dal
        ledger = executor._alias(dal, "economy_ledger")
        balances = executor._alias(dal, "economy_balances")
        mine, other = SUBJECT.user_uuids[1], "44444444-4444-4444-4444-444444444444"
        ledger.insert(user_uuid=mine, counterparty_uuid=other)
        ledger.insert(user_uuid=other, counterparty_uuid=mine)  # the other side of a transfer
        balances.insert(user_uuid=mine)
        balances.insert(user_uuid=other)
        dal.commit()
        result = executor.run_registry_erasure(dal, SUBJECT)
        assert result.counts["economy_ledger"] == 1
        assert result.counts["economy_balances"] == 1
        assert result.counts[executor.scrubbed_key("economy_ledger")] == 1
        [survivor] = dal(ledger.id > 0).select()
        assert survivor.user_uuid == other
        assert survivor.counterparty_uuid is None  # the erased user is no longer named


class TestAbsentSchema:
    """A table/column the live database lacks is skipped LOUDLY, never silently."""

    def test_an_absent_table_is_reported_not_raised(self, small_db: Any) -> None:
        dal = small_db.dal
        dal.executesql('DROP TABLE "sso_identities"')
        dal.commit()
        result = executor.run_registry_erasure(dal, SUBJECT)
        assert "sso_identities" in result.tables_absent
        assert result.tables_examined == len(registry.executed_tables()) - 1

    def test_a_missing_column_skips_that_rule_with_a_warning(
        self, small_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal = small_db.dal
        dal.executesql('DROP TABLE "activity_logs"')
        dal.executesql('CREATE TABLE "activity_logs" (id INTEGER PRIMARY KEY, other TEXT)')
        dal.commit()
        with caplog.at_level(logging.WARNING, logger=executor.logger.name):
            result = executor.run_registry_erasure(dal, SUBJECT)
        assert any(s.startswith("activity_logs(") for s in result.rules_skipped)
        assert any(r.message == "dsar.erasure_rule_skipped" for r in caplog.records)

    def test_catalog_lists_tables_and_columns(self, small_db: Any) -> None:
        catalog = executor.table_catalog(small_db.dal)
        assert "hub_user_id" in catalog["sso_identities"]
        assert "user_id" in catalog["activity_logs"]


class TestDetach:
    """`calendar_tickets.transferred_from_ticket_id` is FK NO ACTION: detach before the delete."""

    def test_a_ticket_transferred_from_the_subjects_is_detached_not_blocked(
        self, small_db: Any
    ) -> None:
        dal = small_db.dal
        tickets = executor._alias(dal, "calendar_tickets")
        mine = tickets.insert(hub_user_id=USER_ID)
        theirs = tickets.insert(hub_user_id=8, transferred_from_ticket_id=mine)
        dal.commit()
        result = executor.run_registry_erasure(dal, SUBJECT)
        assert result.counts["calendar_tickets"] == 1
        row = dal(tickets.id == theirs).select().first()
        assert row.transferred_from_ticket_id is None

    def test_detach_is_a_noop_when_nothing_references_the_subjects_tickets(
        self, small_db: Any
    ) -> None:
        dal = small_db.dal
        tickets = executor._alias(dal, "calendar_tickets")
        tickets.insert(hub_user_id=USER_ID)
        dal.commit()
        assert executor.run_registry_erasure(dal, SUBJECT).counts["calendar_tickets"] == 1


class TestAliases:
    """The lazily-defined pydal aliases never collide with another module's table definition."""

    def test_alias_is_defined_once(self, small_db: Any) -> None:
        dal = small_db.dal
        assert executor._alias(dal, "user_labels") is executor._alias(dal, "user_labels")
        assert "erasure__user_labels" in dal.tables

    def test_alias_coexists_with_a_real_binding_of_the_same_table(self, small_db: Any) -> None:
        from pydal import Field

        dal = small_db.dal
        dal.define_table("user_labels", Field("user_id", "integer"), migrate=False)
        executor._alias(dal, "user_labels").insert(user_id=USER_ID)
        dal.commit()
        assert dal(dal.user_labels.user_id == USER_ID).count() == 1
        assert executor.run_registry_erasure(dal, SUBJECT).counts["user_labels"] == 1


class TestObjectUrls:
    """Avatar/banner URLs are read from the subject's rows before those rows are erased."""

    def test_collects_urls_from_every_source_for_the_subject_only(self, small_db: Any) -> None:
        dal = small_db.dal
        profiles = executor._alias(dal, "hub_user_profiles")
        posts = executor._alias(dal, "hub_forum_posts")
        members = executor._alias(dal, "community_members")
        profiles.insert(
            hub_user_id=USER_ID, custom_avatar_url="https://s/a.png", banner_url="https://s/b.png"
        )
        profiles.insert(hub_user_id=8, custom_avatar_url="https://s/other.png")
        posts.insert(author_hub_user_id=USER_ID, author_avatar_url="https://s/post.png")
        member_id = members.insert(avatar_url="https://s/member.png")
        dal.commit()
        subject = ErasureSubject(user_id=USER_ID, member_ids=(int(member_id),))
        urls = executor.collect_object_urls(dal, subject)
        assert urls == (
            "https://s/a.png",
            "https://s/b.png",
            "https://s/member.png",
            "https://s/post.png",
        )

    def test_no_member_ids_means_no_member_avatars(self, small_db: Any) -> None:
        dal = small_db.dal
        members = executor._alias(dal, "community_members")
        members.insert(avatar_url="https://s/member.png")
        dal.commit()
        assert executor.collect_object_urls(dal, ErasureSubject(user_id=USER_ID)) == ()
