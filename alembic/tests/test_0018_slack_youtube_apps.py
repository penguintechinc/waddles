"""Regression test for 0018_slack_youtube_apps (gh-318).

Same harness convention as `test_0017_loyalty_shoutout_apps.py`/
`test_0014_wave1a_bundle_seeds.py` -- this repo has no pytest-level
fixture that runs Alembic against a real Postgres in CI, so these tests
mock `alembic.op.execute` and assert the exact SQL text 0018's
`upgrade()`/`downgrade()` emit: (a) both `app_catalog` rows are
ingest+action upserts with the exact `stages` JSON the task spec calls
for; (b) each concatenated `stages`/`config_defaults` JSON blob is
syntactically valid once its `||`-joined literals are combined (the exact
bug `0014_wave1a_bundle_seeds`' own test suite was authored to catch: a
bare `... || '...'::jsonb` casts only the LAST literal unless the whole
concatenation is wrapped in `(...)::jsonb`); (c) both ingest and action
entrypoints resolve to real, already-shipped module files under
`core/svc_ingest/builtin_handlers/` / `core/svc_action/builtin_handlers/` (the "coded but
not routable" guard `0014`'s own test establishes); (d) both
`app_tenant_availability` rows are upserted with the app-specific
`config_defaults` secret refs set directly; (e) both upgrade()/downgrade()
are idempotent (`ON CONFLICT ... DO UPDATE`) and downgrade() removes
exactly what upgrade() added, in FK-safe order.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0018_slack_youtube_apps.py"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INGEST_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_ingest" / "builtin_handlers"
_ACTION_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_action" / "builtin_handlers"

SLACK_APP_ID = "waddles.bot.slack.default"
YOUTUBE_APP_ID = "waddles.bot.youtube.default"
TENANT_SLUG = "global"

SLACK_CONFIG_DEFAULTS = {"bot_token_ref": "SLACK_BOT_TOKEN", "app_token_ref": "SLACK_APP_TOKEN"}
YOUTUBE_CONFIG_DEFAULTS = {
    "api_key_ref": "YOUTUBE_API_KEY",
    "client_id_ref": "YOUTUBE_CLIENT_ID",
    "client_secret_ref": "YOUTUBE_CLIENT_SECRET",
    "refresh_token_ref": "YOUTUBE_REFRESH_TOKEN",
}

# app_id -> (expected ingest module name, expected action module name,
# expected stages dict, expected config_defaults dict).
EXPECTED = {
    SLACK_APP_ID: (
        "slack_ingest",
        "slack_send_action",
        {
            "ingest": {
                "entrypoint": "bundles.slack_ingest:normalize",
                "consumes": ["slack.message"],
                "config": {},
                "spec": {},
            },
            "action": {
                "entrypoint": "bundles.slack_send_action:send_message",
                "spec": {"required_config": ["channel_id", "bot_token_ref"]},
                "config": {"api_base": "https://slack.com/api"},
            },
        },
        SLACK_CONFIG_DEFAULTS,
    ),
    YOUTUBE_APP_ID: (
        "youtube_live_ingest",
        "youtube_send_action",
        {
            "ingest": {
                "entrypoint": "bundles.youtube_live_ingest:normalize",
                "consumes": ["youtube.message"],
                "config": {},
                "spec": {"required_config": ["channel_id"]},
                "communication_model": "rest_pull",
            },
            "action": {
                "entrypoint": "bundles.youtube_send_action:send_message",
                "spec": {"required_config": ["refresh_token_ref"]},
                "config": {"api_base": "https://www.googleapis.com/youtube/v3"},
            },
        },
        YOUTUBE_CONFIG_DEFAULTS,
    ),
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_0018_slack_youtube_apps", _MIGRATION_PATH
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
    def test_chains_directly_off_0017_loyalty_shoutout_apps(self, migration) -> None:
        assert migration.revision == "0018_slack_youtube_apps"
        assert migration.down_revision == "0017_loyalty_shoutout_apps"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """Every other version file's down_revision must not also point at
        0017 -- otherwise alembic has two heads and `alembic upgrade head`
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

        assert down_revisions.count("0017_loyalty_shoutout_apps") == 1, (
            "more than one migration chains off 0017_loyalty_shoutout_apps -- "
            "alembic would report multiple heads"
        )

    def test_constants_match_both_apps_and_tenant_slug(self, migration) -> None:
        assert migration.SLACK_APP_ID == SLACK_APP_ID
        assert migration.YOUTUBE_APP_ID == YOUTUBE_APP_ID
        assert migration.TENANT_SLUG == TENANT_SLUG


class TestUpgradeSeedsAppCatalog:
    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_inserts_app_catalog_row_for_app_id(self, upgrade_sql, app_id) -> None:
        assert f"'{app_id}'" in upgrade_sql
        assert upgrade_sql.count("INSERT INTO app_catalog") == 2

    def test_both_app_catalog_inserts_are_upserts_on_app_id(self, upgrade_sql) -> None:
        assert upgrade_sql.count("ON CONFLICT (app_id) DO UPDATE SET") == 2
        assert upgrade_sql.count("stages = EXCLUDED.stages") == 2

    def test_slack_entrypoints_target_slack_bundles(self, upgrade_sql) -> None:
        assert "bundles.slack_ingest:normalize" in upgrade_sql
        assert "bundles.slack_send_action:send_message" in upgrade_sql

    def test_youtube_entrypoints_target_youtube_bundles(self, upgrade_sql) -> None:
        assert "bundles.youtube_live_ingest:normalize" in upgrade_sql
        assert "bundles.youtube_send_action:send_message" in upgrade_sql

    def test_no_process_stage_declared_for_either_app(self, upgrade_sql) -> None:
        """Both apps are ingest+action only in the catalog -- !<command>
        dispatch for both runs in-process inside bot_process.py's own
        router (see module docstring)."""
        assert '"process"' not in upgrade_sql

    def test_youtube_ingest_declares_rest_pull_communication_model(self, upgrade_sql) -> None:
        assert '"communication_model": "rest_pull"' in upgrade_sql

    def test_slack_ingest_declares_no_communication_model_key(self, upgrade_sql) -> None:
        """Matches the live waddles.bot.discord.default row's own shape --
        discord's ingest stage carries no communication_model key either."""
        slack_ingest_start = upgrade_sql.index('"ingest": {"entrypoint": "bundles.slack_ingest')
        slack_ingest_end = upgrade_sql.index("}}, ", slack_ingest_start) + len("}}, ")
        assert "communication_model" not in upgrade_sql[slack_ingest_start:slack_ingest_end]

    def test_module_and_feature_follow_each_app_ids_namespace(self, upgrade_sql) -> None:
        assert "'bot'" in upgrade_sql
        assert "'waddles.bot.slack'" in upgrade_sql
        assert "'waddles.bot.youtube'" in upgrade_sql


class TestSeededStagesJsonIsValid:
    """Guards the exact bug 0014_wave1a_bundle_seeds' own test suite was
    authored to catch: a bare `str1 || str2::jsonb` casts ONLY the last
    literal (`::` binds tighter than `||`) unless the whole concatenation
    is wrapped in `(...)::jsonb`."""

    def _wrapped_jsonb_blobs(self, sql: str) -> list[str]:
        matches = re.findall(
            r"\(\s*((?:'(?:[^'\\]|\\.)*'\s*\|\|\s*)*'(?:[^'\\]|\\.)*')\s*\)::jsonb",
            sql,
            re.DOTALL,
        )
        blobs = []
        for group in matches:
            literals = re.findall(r"'((?:[^'\\]|\\.)*)'", group, re.DOTALL)
            blobs.append("".join(literals))
        return blobs

    def test_exactly_two_wrapped_stages_blobs(self, upgrade_sql) -> None:
        # Only the two `stages` blobs use multi-literal `||` concatenation
        # wrapped in `(...)::jsonb` -- both apps' config_defaults blobs are
        # single literals cast directly, no wrapping needed.
        blobs = self._wrapped_jsonb_blobs(upgrade_sql)
        assert len(blobs) == 2, f"expected exactly two wrapped (...)::jsonb blobs -- found {len(blobs)}"

    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_stages_blob_is_valid_json_and_matches_expected_shape(
        self, upgrade_sql, app_id
    ) -> None:
        _, _, expected_stages, _ = EXPECTED[app_id]
        blobs = self._wrapped_jsonb_blobs(upgrade_sql)
        parsed_blobs = []
        for blob in blobs:
            try:
                parsed_blobs.append(json.loads(blob))
            except json.JSONDecodeError as exc:
                pytest.fail(f"stages blob is not valid JSON: {exc}\n{blob!r}")
        assert expected_stages in parsed_blobs, (
            f"expected stages shape for {app_id} not found among parsed blobs: "
            f"{parsed_blobs!r}"
        )

    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_config_defaults_literal_is_valid_json_and_matches_expected_shape(
        self, upgrade_sql, app_id
    ) -> None:
        _, _, _, expected_config_defaults = EXPECTED[app_id]
        # config_defaults literals are single-quoted strings cast directly
        # (`'{...}'::jsonb`), not wrapped in parens -- extract them directly.
        match = re.search(
            r"SELECT t\.id, '" + re.escape(app_id) + r"', TRUE,\s*'((?:[^'\\]|\\.)*)'::jsonb",
            upgrade_sql,
            re.DOTALL,
        )
        assert match is not None, f"no config_defaults literal found for {app_id}"
        parsed = json.loads(match.group(1))
        assert parsed == expected_config_defaults

    def test_no_bare_multi_literal_jsonb_cast_without_wrapping_parens(
        self, upgrade_sql
    ) -> None:
        assert not re.search(r"\|\|\s*'(?:[^'\\]|\\.)*'::jsonb", upgrade_sql), (
            "found a ::jsonb cast applied directly to the last literal of a "
            "|| chain with no wrapping parens -- only that literal would be "
            "cast, not the full concatenated JSON"
        )


class TestEntrypointsResolveToRealModules:
    """The 'coded but not routable' guard 0014_wave1a_bundle_seeds' own
    test suite establishes: every seeded entrypoint must resolve to a
    real, already-shipped module file, not a stale/typo'd path."""

    @pytest.mark.parametrize(
        "app_id,module_name",
        [(app_id, ingest_mod) for app_id, (ingest_mod, _, _, _) in EXPECTED.items()],
    )
    def test_ingest_module_file_exists(self, app_id, module_name) -> None:
        module_path = _INGEST_BUNDLES_DIR / f"{module_name}.py"
        assert module_path.is_file(), (
            f"{app_id}'s ingest entrypoint references bundles.{module_name}, "
            f"but no {module_path} file exists"
        )

    @pytest.mark.parametrize(
        "app_id,module_name",
        [(app_id, action_mod) for app_id, (_, action_mod, _, _) in EXPECTED.items()],
    )
    def test_action_module_file_exists(self, app_id, module_name) -> None:
        module_path = _ACTION_BUNDLES_DIR / f"{module_name}.py"
        assert module_path.is_file(), (
            f"{app_id}'s action entrypoint references bundles.{module_name}, "
            f"but no {module_path} file exists"
        )


class TestUpgradeActivatesTenantWithConfigDefaults:
    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_inserts_app_tenant_availability_row_for_global_tenant(
        self, upgrade_sql, app_id
    ) -> None:
        assert upgrade_sql.count("INSERT INTO app_tenant_availability") == 2
        assert f"'{app_id}'" in upgrade_sql
        assert f"WHERE t.slug = '{TENANT_SLUG}'" in upgrade_sql

    def test_slack_activation_sets_bot_and_app_token_refs(self, upgrade_sql) -> None:
        assert '"bot_token_ref": "SLACK_BOT_TOKEN"' in upgrade_sql
        assert '"app_token_ref": "SLACK_APP_TOKEN"' in upgrade_sql

    def test_youtube_activation_sets_all_four_oauth_refs(self, upgrade_sql) -> None:
        assert '"api_key_ref": "YOUTUBE_API_KEY"' in upgrade_sql
        assert '"client_id_ref": "YOUTUBE_CLIENT_ID"' in upgrade_sql
        assert '"client_secret_ref": "YOUTUBE_CLIENT_SECRET"' in upgrade_sql
        assert '"refresh_token_ref": "YOUTUBE_REFRESH_TOKEN"' in upgrade_sql

    def test_both_activation_inserts_are_upserts_merging_config_defaults(
        self, upgrade_sql
    ) -> None:
        assert upgrade_sql.count("ON CONFLICT (tenant_id, app_id) DO UPDATE SET") == 2
        assert upgrade_sql.count("COALESCE(app_tenant_availability.config_defaults") == 2


class TestUpgradeIsIdempotentOnRerun:
    def test_rerunning_upgrade_emits_identical_sql(self, migration) -> None:
        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()
        first_run = [call.args[0] for call in mock_execute.call_args_list]

        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()
        second_run = [call.args[0] for call in mock_execute.call_args_list]

        assert first_run == second_run


class TestDowngradeRemovesEverythingUpgradeAdds:
    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_deletes_app_tenant_availability_row(self, downgrade_sql, app_id) -> None:
        assert "DELETE FROM app_tenant_availability" in downgrade_sql
        assert f"'{app_id}'" in downgrade_sql

    @pytest.mark.parametrize("app_id", [SLACK_APP_ID, YOUTUBE_APP_ID])
    def test_deletes_app_catalog_row(self, downgrade_sql, app_id) -> None:
        assert f"DELETE FROM app_catalog WHERE app_id = '{app_id}'" in downgrade_sql

    def test_deletes_app_tenant_availability_before_app_catalog_for_each_app(
        self, downgrade_sql
    ) -> None:
        # app_tenant_availability.app_id has an FK onto app_catalog.app_id --
        # deleting catalog rows first would violate the constraint.
        for app_id in (SLACK_APP_ID, YOUTUBE_APP_ID):
            assert downgrade_sql.index(
                f"DELETE FROM app_tenant_availability\n        WHERE app_id = '{app_id}'"
            ) < downgrade_sql.index(f"DELETE FROM app_catalog WHERE app_id = '{app_id}'")

    def test_downgrade_removes_exactly_the_two_seeded_app_ids(self, downgrade_sql) -> None:
        for app_id in (SLACK_APP_ID, YOUTUBE_APP_ID):
            assert f"'{app_id}'" in downgrade_sql
        assert downgrade_sql.count("DELETE FROM app_catalog") == 2
        assert downgrade_sql.count("DELETE FROM app_tenant_availability") == 2
