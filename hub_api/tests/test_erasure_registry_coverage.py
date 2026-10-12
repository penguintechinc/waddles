"""Art. 17 erasure coverage gate: no user-keyed table may exist that erasure does not account for.

The audit finding (GRC HIGH, GDPR Art. 17) was that erasure hand-listed ~16 of ~121 user-keyed
tables. The fix is `services/erasure_registry.py`; THIS file is what keeps it fixed. It reads the
whole migration chain as text (`tests/erasure_schema_scan.py` -- no database, no Docker, so it runs
on every CI job), finds every table with a column that names a person (`user_id`, `hub_user_id`,
`*_user_id`, `user_uuid`, `platform_user_id`, `*_by`, ...) or carries raw PII about one
(`*_username`, `*_email`, `*_display_name`, `*_avatar_url`, `ip_address`), and FAILS unless the
registry classifies it. Adding a user-keyed table without deciding what erasure does to it is a
red build, not a silent GDPR hole.

Denominators are asserted (a gate that examined nothing is not a pass), and
`TestTheGateCanFail` feeds the checker a synthetic unregistered table to prove it actually
reports one -- a coverage check that cannot fail is not a check.

The scan was cross-checked column-for-column against a Postgres built from the same files (the
only differences were phantom superset columns from statements a failed replay skipped); the
numbers in `docs/PRIVACY.md` come from that run.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping, Set
from pathlib import Path

import pytest

from services import data_privacy_service as privacy
from services import erasure_executor, erasure_external, erasure_registry
from services.erasure_registry import Action
from tests.erasure_schema_scan import SchemaScan, scan_schema

#: The REVIEW set -- known gaps -- pinned exactly. Adding to it must be a deliberate, reviewed edit
#: of this test AND of `docs/PRIVACY.md`; it can never grow because a table was forgotten.
EXPECTED_REVIEW: dict[str, frozenset[str]] = {
    "platform_integrations": frozenset({"user_id"}),
    "activity_stats_daily": frozenset({"platform_user_id", "platform_username"}),
    "hub_admins": frozenset({"email", "username"}),
    "live_activity_events": frozenset({"actor"}),
    "loyalty_giveaway_keys": frozenset({"claimed_by_user_id"}),
    "communities": frozenset({"created_by", "deleted_by", "owner_id", "owner_name"}),
    "command_aliases": frozenset({"deleted_by"}),
    "clip_bookmarks": frozenset({"bookmarked_by"}),
    "clip_highlight_reels": frozenset({"created_by"}),
    "module_scopes": frozenset({"granted_by_user_id", "revoked_by_user_id"}),
    "revoked_tokens": frozenset({"revoked_by_user_id"}),
    "server_link_requests": frozenset(
        {"initiator_platform_user_id", "initiator_platform_username"}
    ),
    "shoutout_creators": frozenset({"platform_username"}),
    "video_shoutout_history": frozenset({"triggered_by_user_id", "triggered_by_username"}),
    "vendor_submissions": frozenset({"vendor_email", "support_email"}),
}


@pytest.fixture(scope="module")
def scan() -> SchemaScan:
    """The final schema reconstructed from the legacy SQL + Alembic files (scanned once)."""
    return scan_schema()


def flagged_columns(tables: Mapping[str, Set[str]]) -> dict[str, set[str]]:
    """`table -> user-keyed/PII columns` for every table that has any."""
    found: dict[str, set[str]] = {}
    for table, columns in tables.items():
        hits = {
            c
            for c in columns
            if erasure_registry.is_key_column(c) or erasure_registry.is_descriptor_column(c)
        }
        if hits:
            found[table] = hits
    return found


def uncovered(tables: Mapping[str, Set[str]]) -> list[str]:
    """`table.column` for every flagged column the registry does not account for."""
    missing: list[str] = []
    for table, columns in sorted(flagged_columns(tables).items()):
        covered = erasure_registry.covered_columns(table)
        if covered is None:
            continue
        for column in sorted(columns):
            if column in covered:
                continue
            if erasure_registry.is_descriptor_column(column) and erasure_registry.has_delete_rule(
                table
            ):
                continue  # describes the very row a DELETE rule removes
            missing.append(f"{table}.{column}")
    return missing


class TestScanDenominators:
    """A scan that examined nothing would 'pass' vacuously -- pin that it saw the real schema."""

    def test_scan_read_the_whole_migration_chain(self, scan: SchemaScan) -> None:
        assert scan.files_scanned >= 150
        assert len(scan.tables) >= 250

    def test_scan_found_a_realistic_number_of_user_keyed_tables(self, scan: SchemaScan) -> None:
        flagged = flagged_columns({t: set(c) for t, c in scan.tables.items()})
        # The audit counted 121; the scan sees a few more (it also flags actor columns).
        assert len(flagged) >= 120, sorted(flagged)

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("hub_users", "email"),
            ("activity_logs", "user_id"),
            ("sso_identities", "subject"),
            ("economy_ledger", "user_uuid"),
            ("community_members", "platform_user_id"),
            ("audit_events", "actor_uuid"),
            ("calendar_tickets", "hub_user_id"),
        ],
    )
    def test_scan_sees_known_columns(self, scan: SchemaScan, table: str, column: str) -> None:
        assert column in scan.columns(table)


class TestCoverage:
    """The gate itself."""

    def test_every_user_keyed_column_is_classified(self, scan: SchemaScan) -> None:
        missing = uncovered({t: set(c) for t, c in scan.tables.items()})
        assert not missing, (
            "user-keyed columns with no erasure rule -- add a rule to "
            "services/erasure_registry.py (DELETE the subject's rows, SCRUB their PII, REF a "
            "pseudonymous actor column, RETAIN with a legal reason, or REVIEW with why not): "
            f"{missing}"
        )

    def test_the_sso_subject_is_erased(self, scan: SchemaScan) -> None:
        """`sso_identities.subject` is the IdP's stable id for the person -- PII by itself."""
        rules = [r for r in erasure_registry.RULES if r.table == "sso_identities"]
        assert [r.action for r in rules] == [Action.DELETE]
        assert rules[0].matches[0].column == "hub_user_id"

    def test_economy_ledger_and_balances_are_erased(self) -> None:
        by_table = {
            r.table: r
            for r in erasure_registry.RULES
            if r.action is Action.DELETE and r.table.startswith("economy_")
        }
        assert set(by_table) == {"economy_balances", "economy_ledger"}
        for rule in by_table.values():
            assert [m.column for m in rule.matches] == ["user_uuid"]

    def test_every_registry_table_exists_in_the_schema(self, scan: SchemaScan) -> None:
        stale = sorted(erasure_registry.registry_tables() - set(scan.tables))
        assert not stale, f"registry names tables the migrations never create: {stale}"

    def test_every_rule_column_exists_on_its_table(self, scan: SchemaScan) -> None:
        bad: list[str] = []
        for rule in erasure_registry.RULES:
            columns = scan.columns(rule.table)
            bad += [
                f"{rule.table}.{c}"
                for c in sorted(erasure_registry.columns_in_rule(rule) - columns)
            ]
        assert not bad, f"rules name columns the migrations never create (typo/stale): {bad}"

    def test_object_sources_exist_in_the_schema(self, scan: SchemaScan) -> None:
        for source in erasure_registry.OBJECT_SOURCES:
            columns = scan.columns(source.table) | {"id"}
            assert {source.url_column, source.match.column} <= columns, source

    def test_no_table_is_given_conflicting_whole_table_treatments(self) -> None:
        for table in erasure_registry.registry_tables():
            actions = {r.action for r in erasure_registry.rules_for(table)}
            whole = actions & {Action.CORE, Action.RETAIN}
            assert not whole or actions == whole, f"{table}: {sorted(actions)}"


class TestReviewSetIsPinned:
    """Known gaps are listed, explained, and cannot grow unnoticed."""

    def test_review_rules_are_exactly_the_documented_set(self) -> None:
        actual: dict[str, set[str]] = {}
        for rule in erasure_registry.RULES:
            if rule.action is Action.REVIEW:
                actual.setdefault(rule.table, set()).update(rule.columns)
        assert {t: frozenset(c) for t, c in actual.items()} == EXPECTED_REVIEW

    def test_every_review_rule_explains_itself(self) -> None:
        for rule in erasure_registry.RULES:
            if rule.action in (Action.REVIEW, Action.RETAIN, Action.EXEMPT):
                assert len(rule.reason) >= 15, f"{rule.table}: say WHY it is not erased"


class TestAssumedHubIdColumns:
    """Integer user columns with no FK are matched as `hub_users.id`: each is a listed decision."""

    def test_every_assumed_column_is_a_hub_match_on_an_executable_rule(self) -> None:
        matched = {
            (rule.table, m.column)
            for rule in erasure_registry.executable_rules()
            for m in rule.matches
            if m.key is erasure_registry.Key.HUB_USER
        }
        assert erasure_registry.ASSUMED_HUB_ID_COLUMNS <= matched

    def test_every_hub_match_without_a_foreign_key_is_listed_as_assumed(
        self, scan: SchemaScan
    ) -> None:
        fk_columns = scan.hub_user_fks
        unlisted = sorted(
            f"{rule.table}.{m.column}"
            for rule in erasure_registry.executable_rules()
            for m in rule.matches
            if m.key is erasure_registry.Key.HUB_USER
            and (rule.table, m.column) not in fk_columns
            and (rule.table, m.column) not in erasure_registry.ASSUMED_HUB_ID_COLUMNS
        )
        assert (
            not unlisted
        ), f"hub-id matches with no FK to hub_users and no review entry: {unlisted}"


class TestRegistryIntegrity:
    """Invariants that keep the registry honest about retention, core steps and gating."""

    def test_executable_rules_never_touch_a_retained_table(self) -> None:
        retained = set(privacy.ERASURE_RETAINED_TABLES) | {
            r.table for r in erasure_registry.RULES if r.action is Action.RETAIN
        }
        assert retained.isdisjoint(erasure_registry.executed_tables())

    def test_retained_set_is_a_superset_of_the_documented_art_5_2_tables(self) -> None:
        retained = {r.table for r in erasure_registry.RULES if r.action is Action.RETAIN}
        assert set(privacy.ERASURE_RETAINED_TABLES) <= retained

    def test_each_core_table_is_really_touched_by_the_core_steps(self) -> None:
        source = (
            inspect.getsource(privacy._sync_erase)
            + inspect.getsource(privacy._delete_memberships)
            + inspect.getsource(privacy._delete_reputation_and_identity)
            + inspect.getsource(privacy._collect_erasure_scope)
        )
        core = [r.table for r in erasure_registry.RULES if r.action is Action.CORE]
        assert len(core) >= 15
        for table in core:
            assert (
                f"dal.{table}" in source or f".{table}\n" in source
            ), f"{table} is classified CORE but _sync_erase() never touches it"

    def test_hub_users_is_anonymized_not_deleted(self) -> None:
        source = inspect.getsource(privacy._sync_erase)
        assert "dal.hub_users.id == user_id).update(" in source
        assert "dal.hub_users.id == user_id).delete(" not in source

    def test_a_scrub_never_blanks_the_column_it_matches_unless_that_is_the_pii(self) -> None:
        # The id of a SCRUB's match column is a retained pseudonymous reference; the only
        # legitimate exceptions blank a RAW platform/uuid identifier of the subject.
        allowed = {
            ("loyalty_transactions", "actor_platform_user_id"),
            ("economy_ledger", "counterparty_uuid"),
        }
        for rule in erasure_registry.RULES:
            if rule.action is not Action.SCRUB:
                continue
            for match in rule.matches:
                if match.column in rule.set_null or match.column in rule.set_placeholder:
                    assert (rule.table, match.column) in allowed, rule

    def test_every_delete_and_scrub_rule_has_a_usable_match(self) -> None:
        for rule in erasure_registry.executable_rules():
            assert rule.matches, rule.table
            if rule.action is Action.SCRUB:
                assert rule.set_null or rule.set_placeholder, rule.table
            for match in rule.matches:
                assert (match.platform_column is not None) == (
                    match.key is erasure_registry.Key.PLATFORM_ID
                ), (rule.table, match)

    def test_erasure_is_never_tier_or_flag_gated(self) -> None:
        """Statutory right: in every tier incl. Free. No license/flag/tier machinery imported."""
        forbidden = ("licens", "posthog", "feature_flag", "flags", "tier", "entitle")
        for module in (erasure_registry, erasure_executor, erasure_external):
            tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
            imported = [
                (node.module or "") + "".join(a.name for a in node.names)
                if isinstance(node, ast.ImportFrom)
                else "".join(a.name for a in node.names)
                for node in ast.walk(tree)
                if isinstance(node, ast.Import | ast.ImportFrom)
            ]
            for name in imported:
                assert not any(f in name.lower() for f in forbidden), (module.__name__, name)
        source = inspect.getsource(privacy._sync_erase) + inspect.getsource(
            privacy.anonymize_user_data
        )
        assert not any(f in source.lower() for f in ("get_tier", "feature_enabled", "license_"))


class TestTheGateCanFail:
    """Prove the coverage check reports a regression instead of vacuously passing."""

    def test_an_unregistered_user_keyed_table_is_reported(self) -> None:
        synthetic = {"new_feature_prefs": {"id", "hub_user_id", "payload"}}
        assert uncovered(synthetic) == ["new_feature_prefs.hub_user_id"]

    def test_an_unregistered_actor_and_pii_column_on_a_known_table_is_reported(self) -> None:
        synthetic = {
            "announcements": {"id", "created_by", "updated_by", "created_by_name", "approved_by"}
        }
        assert uncovered(synthetic) == ["announcements.approved_by"]

    def test_a_new_pii_column_on_a_deleted_table_is_not_a_false_alarm(self) -> None:
        synthetic = {"activity_logs": {"id", "user_id", "reviewed_by", "contact_email"}}
        assert uncovered(synthetic) == ["activity_logs.reviewed_by"]

    def test_unflagged_tables_are_ignored(self) -> None:
        assert uncovered({"app_catalog": {"id", "name", "version"}}) == []

    def test_the_real_schema_minus_one_rule_fails(
        self, scan: SchemaScan, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation proof on the REAL schema: drop the sso_identities rule -> the gate goes red."""
        trimmed = tuple(r for r in erasure_registry.RULES if r.table != "sso_identities")
        monkeypatch.setattr(erasure_registry, "RULES", trimmed)
        missing = uncovered({t: set(c) for t, c in scan.tables.items()})
        assert {"sso_identities.hub_user_id", "sso_identities.subject"} <= set(missing)
