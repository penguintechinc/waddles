"""Regression test for 0014_wave1a_bundle_seeds (gh-298).

Same harness convention as `test_0012_schema_drift_columns.py`/
`test_0013_music_policy_youtube_labels.py` -- this repo has no pytest-level
fixture that runs Alembic against a real Postgres in CI, so these tests
mock `alembic.op.execute` and assert the exact SQL text 0014's `upgrade()`/
`downgrade()` emit. A real upgrade -> downgrade -> re-upgrade round-trip
against a throwaway Postgres (via this repo's own `migrations/Dockerfile`
image) WAS run during development for this migration -- real output, not
asserted here for the same "no DB fixture in this suite" reason 0012's own
docstring gives.

Additional coverage specific to 0014:
  - every seeded `stages` JSON blob is syntactically valid JSON once the
    `||`-concatenated SQL string literals are joined (catches the exact bug
    this revision was authored with once: a missing `(...)::jsonb` wrap
    around the concatenation meant only the LAST literal got cast, and
    Postgres rejected it as invalid JSON -- caught by a real `docker run`
    round-trip, not by this test alone, but locked in here so a future edit
    can't silently reintroduce it without a real DB);
  - every seeded app_id's `process`/`action` entrypoint resolves to a
    real, already-shipped module file under `core/svc_process/builtin_handlers/` /
    `core/svc_action/builtin_handlers/` -- the "coded but not routable" guard this
    migration exists to close (gh-298);
  - no app_id is seeded twice;
  - `upgrade()`/`downgrade()` are each idempotent when the underlying SQL
    is inspected for `ON CONFLICT`/`IF NOT EXISTS`-equivalent guards.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0014_wave1a_bundle_seeds.py"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROCESS_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_process" / "builtin_handlers"
_ACTION_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_action" / "builtin_handlers"

# app_id -> (process module name or None, action module name or None).
# Mirrors the `bundles.<module>:<function>` entrypoint convention; the
# module name (before the colon) must resolve to `<module>.py` under the
# matching stage's bundles directory.
EXPECTED_ENTRYPOINT_MODULES = {
    "waddles.social.quote.default": ("social_quote_process", "social_quote_action"),
    "waddles.social.alias.default": ("social_alias_process", "social_alias_action"),
    "waddles.social.welcome.default": ("social_welcome_process", "social_welcome_action"),
    "waddles.community.chat.default": ("community_chat_process", None),
    "waddles.community.polls.default": ("community_polls_process", "community_polls_action"),
    "waddles.community.announcements.default": (
        "community_announcements_process",
        "community_announcements_action",
    ),
    "waddles.marketing.engagement.default": (
        "marketing_engagement_process",
        "marketing_engagement_action",
    ),
    "waddles.streaming.stream.default": (None, "streaming_stream_action"),
    "waddles.integrations.waddleai.default": (None, "integrations_waddleai_action"),
}

# The four numbered files (084/091/095/096) that this migration only
# bookkeeps (schema_migrations row) rather than re-porting, because their
# content was already confirmed live on alpha via a read-only psql check
# during authoring (see the migration's own module docstring).
BOOKKEEPING_ONLY_VERSIONS = {
    "084_bot_process_entrypoint",
    "091_community_forums_bundle",
    "095_demo_seed",
    "096_live_activity_events",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_0014_wave1a_bundle_seeds", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


@pytest.fixture
def upgrade_sql(migration) -> str:
    with patch("alembic.op.execute") as mock_execute:
        migration.upgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


@pytest.fixture
def downgrade_sql(migration) -> str:
    with patch("alembic.op.execute") as mock_execute:
        migration.downgrade()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestMigrationMetadata:
    def test_chains_directly_off_0013_music_policy_yt_labels(self, migration) -> None:
        assert migration.revision == "0014_wave1a_bundle_seeds"
        assert migration.down_revision == "0013_music_policy_yt_labels"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """Every other version file's down_revision must not also point at
        0013 -- otherwise alembic has two heads and `alembic upgrade head`
        becomes ambiguous."""
        versions_dir = Path(__file__).resolve().parent.parent / "versions"
        down_revisions = []
        for path in versions_dir.glob("*.py"):
            if path.name == "__init__.py":
                continue
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            down_revisions.append(module.down_revision)

        assert down_revisions.count("0013_music_policy_yt_labels") == 1, (
            "more than one migration chains off 0013_music_policy_yt_labels -- "
            "alembic would report multiple heads"
        )


class TestSeededAppIdsAreCompleteAndUnique:
    def test_every_expected_app_id_is_inserted(self, upgrade_sql) -> None:
        for app_id in EXPECTED_ENTRYPOINT_MODULES:
            assert f"'{app_id}'," in upgrade_sql or f"'{app_id}'" in upgrade_sql, (
                f"upgrade() never inserts app_catalog row for {app_id}"
            )

    def test_no_app_id_is_seeded_twice(self, upgrade_sql) -> None:
        for app_id in EXPECTED_ENTRYPOINT_MODULES:
            # Each app_id appears in exactly one app_catalog INSERT VALUES
            # tuple (as the first column literal) and exactly one
            # app_tenant_availability activation SELECT -- i.e. twice total,
            # never more (a duplicate INSERT block would triple/quadruple
            # this count).
            occurrences = len(re.findall(re.escape(f"'{app_id}'"), upgrade_sql))
            assert occurrences == 2, (
                f"{app_id} appears {occurrences} times in upgrade() SQL, expected "
                "exactly 2 (one app_catalog INSERT, one app_tenant_availability "
                "activation) -- possible duplicate seed block"
            )

    def test_app_catalog_inserts_are_upserts_on_app_id(self, upgrade_sql) -> None:
        # Every seeded app_id's INSERT must self-heal via
        # ON CONFLICT (app_id) DO UPDATE, not DO NOTHING -- a partially
        # drifted catalog row must converge back to this migration's exact
        # stages/config on re-run.
        assert upgrade_sql.count("ON CONFLICT (app_id) DO UPDATE SET") == len(
            EXPECTED_ENTRYPOINT_MODULES
        )

    def test_activation_inserts_are_do_nothing_on_conflict(self, upgrade_sql) -> None:
        # app_tenant_availability rows carry community/tenant-owned state
        # (available/config_defaults) this migration must never clobber.
        assert upgrade_sql.count("ON CONFLICT (tenant_id, app_id) DO NOTHING") == len(
            EXPECTED_ENTRYPOINT_MODULES
        )

    def test_activations_scoped_to_global_tenant(self, upgrade_sql) -> None:
        assert upgrade_sql.count("WHERE t.slug = 'global'") == len(
            EXPECTED_ENTRYPOINT_MODULES
        )


class TestSeededStagesJsonIsValid:
    """Guards the exact bug this migration was authored with once: a bare
    `str1 || str2 || str3::jsonb` casts ONLY the last literal (`::` binds
    tighter than `||`), so Postgres receives a fragment, not the full JSON
    object -- caught for real via a `docker run` round-trip against a
    throwaway Postgres during authoring; this test locks the fix in
    statically so a future edit can't reintroduce it silently."""

    def _stages_blobs(self, upgrade_sql: str) -> list[str]:
        # Every stages payload in this migration is written as
        # `(<literal> (|| <literal>)*)::jsonb` -- extract each parenthesized
        # group, concatenate its string literals, and return the joined
        # JSON text.
        blobs = []
        for match in re.finditer(
            r"\(\s*((?:'(?:[^'\\]|\\.)*'\s*\|\|\s*)*'(?:[^'\\]|\\.)*')\s*\)::jsonb",
            upgrade_sql,
            re.DOTALL,
        ):
            literals = re.findall(r"'((?:[^'\\]|\\.)*)'", match.group(1), re.DOTALL)
            blobs.append("".join(literals))
        return blobs

    def test_every_stages_blob_is_valid_json(self, upgrade_sql) -> None:
        blobs = self._stages_blobs(upgrade_sql)
        assert len(blobs) == len(EXPECTED_ENTRYPOINT_MODULES), (
            f"expected {len(EXPECTED_ENTRYPOINT_MODULES)} parenthesized "
            f"(...)::jsonb stages blobs, found {len(blobs)} -- a catalog "
            "INSERT's stages cast may be missing its wrapping parens again"
        )
        for i, blob in enumerate(blobs):
            try:
                json.loads(blob)
            except json.JSONDecodeError as exc:
                pytest.fail(f"stages blob #{i} is not valid JSON: {exc}\n{blob!r}")

    def test_no_bare_multi_literal_jsonb_cast_without_wrapping_parens(
        self, upgrade_sql
    ) -> None:
        # A `|| '...'::jsonb` immediately preceded by another `||` (i.e. the
        # cast binds to only the last literal of a concatenation chain, with
        # no enclosing parens) is exactly the regression this class guards.
        assert not re.search(r"\|\|\s*'(?:[^'\\]|\\.)*'::jsonb", upgrade_sql), (
            "found a ::jsonb cast applied directly to the last literal of a "
            "|| chain with no wrapping parens -- only that literal would be "
            "cast, not the full concatenated stages JSON"
        )


class TestEveryEntrypointResolvesToAnExistingModuleFile:
    """The core "coded but not routable" guard gh-298 exists to close --
    every entrypoint this migration seeds must resolve to a real module
    file, not just a plausible-looking string."""

    def test_process_bundles_directory_exists(self) -> None:
        assert _PROCESS_BUNDLES_DIR.is_dir(), (
            f"{_PROCESS_BUNDLES_DIR} does not exist -- test is pointed at the "
            "wrong repo layout"
        )

    def test_action_bundles_directory_exists(self) -> None:
        assert _ACTION_BUNDLES_DIR.is_dir(), (
            f"{_ACTION_BUNDLES_DIR} does not exist -- test is pointed at the "
            "wrong repo layout"
        )

    @pytest.mark.parametrize(
        "app_id,modules", sorted(EXPECTED_ENTRYPOINT_MODULES.items())
    )
    def test_process_and_action_modules_exist(self, app_id, modules) -> None:
        process_module, action_module = modules
        assert process_module or action_module, f"{app_id} declares no stage at all"

        if process_module is not None:
            path = _PROCESS_BUNDLES_DIR / f"{process_module}.py"
            assert path.is_file(), (
                f"{app_id}'s process entrypoint references "
                f"bundles.{process_module}:transform but {path} does not exist"
            )

        if action_module is not None:
            path = _ACTION_BUNDLES_DIR / f"{action_module}.py"
            assert path.is_file(), (
                f"{app_id}'s action entrypoint references "
                f"bundles.{action_module}:<fn> but {path} does not exist"
            )

    def test_every_seeded_app_id_has_a_process_or_action_entrypoint_in_sql(
        self, upgrade_sql
    ) -> None:
        for app_id, (process_module, action_module) in EXPECTED_ENTRYPOINT_MODULES.items():
            if process_module is not None:
                assert f"bundles.{process_module}:transform" in upgrade_sql, (
                    f"upgrade() SQL for {app_id} missing process entrypoint "
                    f"bundles.{process_module}:transform"
                )
            if action_module is not None:
                assert f"bundles.{action_module}:" in upgrade_sql, (
                    f"upgrade() SQL for {app_id} missing an action entrypoint "
                    f"referencing bundles.{action_module}"
                )


class TestLegacySchemaMigrationsBookkeeping:
    ALL_VERSIONS = (
        "084_bot_process_entrypoint",
        "085_social_quote_bundle",
        "086_social_alias_bundle",
        "087_social_welcome_bundle",
        "088_community_chat_bundle",
        "089_community_polls_bundle",
        "090_community_announcements_bundle",
        "091_community_forums_bundle",
        "092_marketing_engagement_bundle",
        "093_streaming_stream_bundle",
        "094_integrations_waddleai_bundle",
        "095_demo_seed",
        "096_live_activity_events",
    )

    def test_every_numbered_file_084_through_096_gets_a_bookkeeping_row(
        self, upgrade_sql
    ) -> None:
        assert len(self.ALL_VERSIONS) == 13
        for version in self.ALL_VERSIONS:
            assert f"'{version}'" in upgrade_sql, (
                f"upgrade() never inserts a schema_migrations row for {version}"
            )

    def test_bookkeeping_insert_targets_schema_migrations_with_no_target_conflict(
        self, upgrade_sql
    ) -> None:
        assert "INSERT INTO schema_migrations (version) VALUES" in upgrade_sql
        assert "ON CONFLICT DO NOTHING" in upgrade_sql

    def test_bookkeeping_only_versions_get_no_app_catalog_insert(self, upgrade_sql) -> None:
        # 084/091/095/096's content is already live -- upgrade() must not
        # re-port their DDL/seed content, only bookkeep schema_migrations.
        # 091 is the one bookkeeping-only version that legitimately shares
        # its app_id substring with nothing else seeded, so assert its
        # app_id (waddles.community.forums.default) never appears in an
        # app_catalog INSERT VALUES block.
        assert "'waddles.community.forums.default'" not in upgrade_sql


class TestDowngradeReversesEverythingUpgradeAdds:
    def test_deletes_every_seeded_app_id_from_both_tables(self, downgrade_sql) -> None:
        assert "DELETE FROM app_tenant_availability WHERE app_id IN (" in downgrade_sql
        assert "DELETE FROM app_catalog WHERE app_id IN (" in downgrade_sql
        for app_id in EXPECTED_ENTRYPOINT_MODULES:
            assert f"'{app_id}'" in downgrade_sql

    def test_deletes_app_tenant_availability_before_app_catalog(self, downgrade_sql) -> None:
        # app_tenant_availability.app_id has an FK onto app_catalog.app_id --
        # deleting catalog rows first would violate the constraint.
        ata_index = downgrade_sql.index("DELETE FROM app_tenant_availability")
        catalog_index = downgrade_sql.index("DELETE FROM app_catalog")
        assert ata_index < catalog_index

    def test_removes_every_legacy_bookkeeping_row(self, downgrade_sql) -> None:
        assert "DELETE FROM schema_migrations WHERE version IN (" in downgrade_sql
        for version in TestLegacySchemaMigrationsBookkeeping.ALL_VERSIONS:
            assert f"'{version}'" in downgrade_sql

    def test_downgrade_creates_no_ddl_since_upgrade_created_none(self, downgrade_sql) -> None:
        # This migration never issues CREATE TABLE (096's table already
        # existed live) so downgrade() must never issue a DROP TABLE either.
        assert "DROP TABLE" not in downgrade_sql.upper()


class TestBookkeepingOnlyVersionsAreDocumented:
    def test_four_versions_are_bookkeeping_only(self) -> None:
        assert BOOKKEEPING_ONLY_VERSIONS == {
            "084_bot_process_entrypoint",
            "091_community_forums_bundle",
            "095_demo_seed",
            "096_live_activity_events",
        }

    def test_bookkeeping_only_versions_are_a_subset_of_all_legacy_versions(self) -> None:
        assert BOOKKEEPING_ONLY_VERSIONS <= set(
            TestLegacySchemaMigrationsBookkeeping.ALL_VERSIONS
        )

    def test_ported_versions_plus_bookkeeping_only_covers_everything(self) -> None:
        ported_versions = {
            "085_social_quote_bundle",
            "086_social_alias_bundle",
            "087_social_welcome_bundle",
            "088_community_chat_bundle",
            "089_community_polls_bundle",
            "090_community_announcements_bundle",
            "092_marketing_engagement_bundle",
            "093_streaming_stream_bundle",
            "094_integrations_waddleai_bundle",
        }
        assert len(ported_versions) == len(EXPECTED_ENTRYPOINT_MODULES) == 9
        assert ported_versions | BOOKKEEPING_ONLY_VERSIONS == set(
            TestLegacySchemaMigrationsBookkeeping.ALL_VERSIONS
        )
