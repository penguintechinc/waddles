"""Regression test for 0019_kick_app (gh-318).

Same harness convention as `test_0018_slack_youtube_apps.py`/
`test_0017_loyalty_shoutout_apps.py`/`test_0014_wave1a_bundle_seeds.py` --
this repo has no pytest-level fixture that runs Alembic against a real
Postgres in CI, so these tests mock `alembic.op.execute` and assert the
exact SQL text 0019's `upgrade()`/`downgrade()` emit: (a) the `app_catalog`
row is an ingest+action upsert with the exact `stages` JSON the task spec
calls for; (b) the concatenated `stages` JSON blob is syntactically valid
once its `||`-joined literals are combined (the exact bug `0014_wave1a_bundle_seeds`'
own test suite was authored to catch: a bare `... || '...'::jsonb` casts
only the LAST literal unless the whole concatenation is wrapped in
`(...)::jsonb`); (c) both ingest and action entrypoints resolve to real,
already-shipped module files under `core/svc_ingest/builtin_handlers/` /
`core/svc_action/builtin_handlers/` (the "coded but not routable" guard `0014`'s
own test establishes); (d) the `app_tenant_availability` row is upserted
with the app-specific `config_defaults` secret refs set directly; (e) both
upgrade()/downgrade() are idempotent (`ON CONFLICT ... DO UPDATE`) and
downgrade() removes exactly what upgrade() added, in FK-safe order.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0019_kick_app.py"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INGEST_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_ingest" / "builtin_handlers"
_ACTION_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_action" / "builtin_handlers"

KICK_APP_ID = "waddles.bot.kick.default"
TENANT_SLUG = "global"

KICK_CONFIG_DEFAULTS = {
    "access_token_ref": "KICK_ACCESS_TOKEN",
    "client_id_ref": "KICK_CLIENT_ID",
    "client_secret_ref": "KICK_CLIENT_SECRET",
    "webhook_secret_ref": "KICK_WEBHOOK_SECRET",
}

# app_id -> (expected ingest module name, expected action module name,
# expected stages dict, expected config_defaults dict).
EXPECTED = {
    KICK_APP_ID: (
        "kick_ingest",
        "kick_send_action",
        {
            "ingest": {
                "entrypoint": "bundles.kick_ingest:normalize",
                "consumes": ["kick.message"],
                "config": {},
                "spec": {"required_config": ["channel_slug"]},
            },
            "action": {
                "entrypoint": "bundles.kick_send_action:send_message",
                "spec": {"required_config": ["access_token_ref"]},
                "config": {"api_base": "https://kick.com/api/v2"},
            },
        },
        KICK_CONFIG_DEFAULTS,
    ),
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0019_kick_app", _MIGRATION_PATH)
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
    def test_chains_directly_off_0018_slack_youtube_apps(self, migration) -> None:
        assert migration.revision == "0019_kick_app"
        assert migration.down_revision == "0018_slack_youtube_apps"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """Every other version file's down_revision must not also point at
        0018_slack_youtube_apps -- otherwise alembic has two heads and
        `alembic upgrade head` becomes ambiguous."""
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

        assert down_revisions.count("0018_slack_youtube_apps") == 1, (
            "more than one migration chains off 0018_slack_youtube_apps -- "
            "alembic would report multiple heads"
        )

    def test_constants_match_app_and_tenant_slug(self, migration) -> None:
        assert migration.KICK_APP_ID == KICK_APP_ID
        assert migration.TENANT_SLUG == TENANT_SLUG


class TestUpgradeSeedsAppCatalog:
    def test_inserts_app_catalog_row_for_kick(self, upgrade_sql) -> None:
        assert f"'{KICK_APP_ID}'" in upgrade_sql
        assert upgrade_sql.count("INSERT INTO app_catalog") == 1

    def test_app_catalog_insert_is_upsert_on_app_id(self, upgrade_sql) -> None:
        assert upgrade_sql.count("ON CONFLICT (app_id) DO UPDATE SET") == 1
        assert upgrade_sql.count("stages = EXCLUDED.stages") == 1

    def test_kick_entrypoints_target_kick_bundles(self, upgrade_sql) -> None:
        assert "bundles.kick_ingest:normalize" in upgrade_sql
        assert "bundles.kick_send_action:send_message" in upgrade_sql

    def test_no_process_stage_declared_for_kick(self, upgrade_sql) -> None:
        """Kick is ingest+action only in the catalog -- !<command>
        dispatch for Kick runs in-process inside bot_process.py's own
        router (see module docstring)."""
        assert '"process"' not in upgrade_sql

    def test_kick_ingest_declares_no_communication_model_key(self, upgrade_sql) -> None:
        """Matches the live waddles.bot.discord.default row's own shape --
        discord's ingest stage carries no communication_model key either."""
        kick_ingest_start = upgrade_sql.index(
            '"ingest": {"entrypoint": "bundles.kick_ingest'
        )
        kick_ingest_end = upgrade_sql.index("}}, ", kick_ingest_start) + len("}}, ")
        assert "communication_model" not in upgrade_sql[kick_ingest_start:kick_ingest_end]

    def test_module_and_feature_follow_kick_app_id_namespace(self, upgrade_sql) -> None:
        assert "'bot'" in upgrade_sql
        assert "'waddles.bot.kick'" in upgrade_sql


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

    def test_exactly_one_wrapped_stages_blob(self, upgrade_sql) -> None:
        # Only the `stages` blob uses multi-literal `||` concatenation
        # wrapped in `(...)::jsonb` -- the config_defaults blob is
        # a single literal cast directly, no wrapping needed.
        blobs = self._wrapped_jsonb_blobs(upgrade_sql)
        assert len(blobs) == 1, f"expected exactly one wrapped (...)::jsonb blob -- found {len(blobs)}"

    def test_stages_blob_is_valid_json_and_matches_expected_shape(self, upgrade_sql) -> None:
        _, _, expected_stages, _ = EXPECTED[KICK_APP_ID]
        blobs = self._wrapped_jsonb_blobs(upgrade_sql)
        parsed_blobs = []
        for blob in blobs:
            try:
                parsed_blobs.append(json.loads(blob))
            except json.JSONDecodeError as exc:
                pytest.fail(f"stages blob is not valid JSON: {exc}\n{blob!r}")
        assert expected_stages in parsed_blobs, (
            f"expected stages shape for {KICK_APP_ID} not found among parsed blobs: "
            f"{parsed_blobs!r}"
        )

    def test_config_defaults_literal_is_valid_json_and_matches_expected_shape(
        self, upgrade_sql
    ) -> None:
        _, _, _, expected_config_defaults = EXPECTED[KICK_APP_ID]
        # config_defaults literal is a single-quoted string cast directly
        # (`'{...}'::jsonb`), not wrapped in parens -- extract it directly.
        match = re.search(
            r"SELECT t\.id, '" + re.escape(KICK_APP_ID) + r"', TRUE,\s*'((?:[^'\\]|\\.)*)'::jsonb",
            upgrade_sql,
            re.DOTALL,
        )
        assert match is not None, f"no config_defaults literal found for {KICK_APP_ID}"
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

    def test_ingest_module_file_exists(self) -> None:
        ingest_module_name, _, _, _ = EXPECTED[KICK_APP_ID]
        module_path = _INGEST_BUNDLES_DIR / f"{ingest_module_name}.py"
        assert module_path.is_file(), (
            f"{KICK_APP_ID}'s ingest entrypoint references bundles.{ingest_module_name}, "
            f"but no {module_path} file exists"
        )

    def test_action_module_file_exists(self) -> None:
        _, action_module_name, _, _ = EXPECTED[KICK_APP_ID]
        module_path = _ACTION_BUNDLES_DIR / f"{action_module_name}.py"
        assert module_path.is_file(), (
            f"{KICK_APP_ID}'s action entrypoint references bundles.{action_module_name}, "
            f"but no {module_path} file exists"
        )


class TestUpgradeActivatesTenantWithConfigDefaults:
    def test_inserts_app_tenant_availability_row_for_global_tenant(
        self, upgrade_sql
    ) -> None:
        assert upgrade_sql.count("INSERT INTO app_tenant_availability") == 1
        assert f"'{KICK_APP_ID}'" in upgrade_sql
        assert f"WHERE t.slug = '{TENANT_SLUG}'" in upgrade_sql

    def test_activation_sets_all_four_secret_refs(self, upgrade_sql) -> None:
        assert '"access_token_ref": "KICK_ACCESS_TOKEN"' in upgrade_sql
        assert '"client_id_ref": "KICK_CLIENT_ID"' in upgrade_sql
        assert '"client_secret_ref": "KICK_CLIENT_SECRET"' in upgrade_sql
        assert '"webhook_secret_ref": "KICK_WEBHOOK_SECRET"' in upgrade_sql

    def test_activation_insert_is_upsert_merging_config_defaults(
        self, upgrade_sql
    ) -> None:
        assert upgrade_sql.count("ON CONFLICT (tenant_id, app_id) DO UPDATE SET") == 1
        assert upgrade_sql.count("COALESCE(app_tenant_availability.config_defaults") == 1


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
    def test_deletes_app_tenant_availability_row(self, downgrade_sql) -> None:
        assert "DELETE FROM app_tenant_availability" in downgrade_sql
        assert f"'{KICK_APP_ID}'" in downgrade_sql

    def test_deletes_app_catalog_row(self, downgrade_sql) -> None:
        assert f"DELETE FROM app_catalog WHERE app_id = '{KICK_APP_ID}'" in downgrade_sql

    def test_deletes_app_tenant_availability_before_app_catalog(self, downgrade_sql) -> None:
        # app_tenant_availability.app_id has an FK onto app_catalog.app_id --
        # deleting catalog rows first would violate the constraint.
        assert (
            downgrade_sql.index(
                f"DELETE FROM app_tenant_availability\n        WHERE app_id = '{KICK_APP_ID}'"
            )
            < downgrade_sql.index(f"DELETE FROM app_catalog WHERE app_id = '{KICK_APP_ID}'")
        )

    def test_downgrade_removes_exactly_the_seeded_app_id(self, downgrade_sql) -> None:
        assert f"'{KICK_APP_ID}'" in downgrade_sql
        assert downgrade_sql.count("DELETE FROM app_catalog") == 1
        assert downgrade_sql.count("DELETE FROM app_tenant_availability") == 1
