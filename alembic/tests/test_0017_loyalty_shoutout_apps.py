"""Regression test for 0017_loyalty_shoutout_apps (gh-316, gh-317).

Same harness convention as `test_0016_moderation_enforce_app.py`/
`test_0014_wave1a_bundle_seeds.py` -- this repo has no pytest-level
fixture that runs Alembic against a real Postgres in CI, so these tests
mock `alembic.op.execute` and assert the exact SQL text 0017's
`upgrade()`/`downgrade()` emit: (a) both `app_catalog` rows are
action-only upserts with the exact `stages` JSON the task spec calls for;
(b) each concatenated `stages` JSON blob is syntactically valid once its
`||`-joined literals are combined (the exact bug `0014_wave1a_bundle_seeds`'
own test suite was authored to catch: a bare `... || '...'::jsonb` casts
only the LAST literal unless the whole concatenation is wrapped in
`(...)::jsonb`); (c) both action entrypoints resolve to a real,
already-shipped module file under `core/svc_action/builtin_handlers/` (the
"coded but not routable" guard `0014`'s own test establishes); (d) both
`app_tenant_availability` rows are upserted with `config_defaults.
bot_token_ref` set directly; (e) both upgrade()/downgrade() are
idempotent (`ON CONFLICT ... DO UPDATE`) and downgrade() removes exactly
what upgrade() added, in FK-safe order.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0017_loyalty_shoutout_apps.py"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ACTION_BUNDLES_DIR = _REPO_ROOT / "core" / "svc_action" / "builtin_handlers"

LOYALTY_APP_ID = "waddles.community.loyalty.default"
SHOUTOUT_APP_ID = "waddles.bot.shoutout.default"
TENANT_SLUG = "global"
BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"

# app_id -> (expected action entrypoint module name, expected stages dict).
EXPECTED_STAGES = {
    LOYALTY_APP_ID: (
        "community_loyalty_action",
        {
            "action": {
                "entrypoint": "bundles.community_loyalty_action:loyalty",
                "config": {"api_base": "https://discord.com/api/v10"},
                "spec": {"required_config": ["bot_token_ref"]},
            }
        },
    ),
    SHOUTOUT_APP_ID: (
        "twitch_shoutout_action",
        {
            "action": {
                "entrypoint": "bundles.twitch_shoutout_action:shoutout",
                "config": {"api_base": "https://discord.com/api/v10"},
                "spec": {"required_config": ["bot_token_ref"]},
            }
        },
    ),
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_0017_loyalty_shoutout_apps", _MIGRATION_PATH
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
    def test_chains_directly_off_0016_moderation_enforce_app(self, migration) -> None:
        assert migration.revision == "0017_loyalty_shoutout_apps"
        assert migration.down_revision == "0016_moderation_enforce_app"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """Every other version file's down_revision must not also point at
        0016 -- otherwise alembic has two heads and `alembic upgrade head`
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

        assert down_revisions.count("0016_moderation_enforce_app") == 1, (
            "more than one migration chains off 0016_moderation_enforce_app -- "
            "alembic would report multiple heads"
        )

    def test_constants_match_both_apps_and_shared_discord_bot_token(self, migration) -> None:
        assert migration.LOYALTY_APP_ID == LOYALTY_APP_ID
        assert migration.SHOUTOUT_APP_ID == SHOUTOUT_APP_ID
        assert migration.TENANT_SLUG == TENANT_SLUG
        assert migration.BOT_TOKEN_REF == BOT_TOKEN_REF


class TestUpgradeSeedsAppCatalog:
    @pytest.mark.parametrize("app_id", [LOYALTY_APP_ID, SHOUTOUT_APP_ID])
    def test_inserts_app_catalog_row_for_app_id(self, upgrade_sql, app_id) -> None:
        assert f"'{app_id}'" in upgrade_sql
        assert upgrade_sql.count("INSERT INTO app_catalog") == 2

    def test_both_app_catalog_inserts_are_upserts_on_app_id(self, upgrade_sql) -> None:
        assert upgrade_sql.count("ON CONFLICT (app_id) DO UPDATE SET") == 2
        assert upgrade_sql.count("stages = EXCLUDED.stages") == 2

    def test_loyalty_action_entrypoint_targets_community_loyalty_action(
        self, upgrade_sql
    ) -> None:
        assert "bundles.community_loyalty_action:loyalty" in upgrade_sql

    def test_shoutout_action_entrypoint_targets_twitch_shoutout_action(
        self, upgrade_sql
    ) -> None:
        assert "bundles.twitch_shoutout_action:shoutout" in upgrade_sql

    def test_no_process_stage_declared_for_either_app(self, upgrade_sql) -> None:
        """Both apps are action-only in the catalog -- !<command> dispatch
        for both runs in-process inside bot_process.py's own
        _FEATURE_MODULES router (see module docstring)."""
        assert '"process"' not in upgrade_sql

    def test_module_and_feature_follow_each_app_ids_namespace(self, upgrade_sql) -> None:
        assert "'community'" in upgrade_sql
        assert "'waddles.community.loyalty'" in upgrade_sql
        assert "'bot'" in upgrade_sql
        assert "'waddles.bot.shoutout'" in upgrade_sql


class TestSeededStagesJsonIsValid:
    """Guards the exact bug 0014_wave1a_bundle_seeds' own test suite was
    authored to catch: a bare `str1 || str2::jsonb` casts ONLY the last
    literal (`::` binds tighter than `||`) unless the whole concatenation
    is wrapped in `(...)::jsonb`."""

    def _stages_blobs(self, upgrade_sql: str) -> list[str]:
        matches = re.findall(
            r"\(\s*((?:'(?:[^'\\]|\\.)*'\s*\|\|\s*)*'(?:[^'\\]|\\.)*')\s*\)::jsonb",
            upgrade_sql,
            re.DOTALL,
        )
        assert len(matches) == 2, (
            "expected exactly two wrapped (...)::jsonb stages blobs (one per "
            f"app) -- found {len(matches)}"
        )
        blobs = []
        for group in matches:
            literals = re.findall(r"'((?:[^'\\]|\\.)*)'", group, re.DOTALL)
            blobs.append("".join(literals))
        return blobs

    @pytest.mark.parametrize("app_id", [LOYALTY_APP_ID, SHOUTOUT_APP_ID])
    def test_stages_blob_is_valid_json_and_matches_expected_shape(
        self, upgrade_sql, app_id
    ) -> None:
        _, expected_stages = EXPECTED_STAGES[app_id]
        blobs = self._stages_blobs(upgrade_sql)
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

    def test_no_bare_multi_literal_jsonb_cast_without_wrapping_parens(
        self, upgrade_sql
    ) -> None:
        assert not re.search(r"\|\|\s*'(?:[^'\\]|\\.)*'::jsonb", upgrade_sql), (
            "found a ::jsonb cast applied directly to the last literal of a "
            "|| chain with no wrapping parens -- only that literal would be "
            "cast, not the full concatenated stages JSON"
        )


class TestActionEntrypointsResolveToRealModules:
    """The 'coded but not routable' guard 0014_wave1a_bundle_seeds' own
    test suite establishes: every seeded entrypoint must resolve to a
    real, already-shipped module file, not a stale/typo'd path."""

    @pytest.mark.parametrize(
        "app_id,module_name",
        [(app_id, mod) for app_id, (mod, _) in EXPECTED_STAGES.items()],
    )
    def test_action_module_file_exists(self, app_id, module_name) -> None:
        module_path = _ACTION_BUNDLES_DIR / f"{module_name}.py"
        assert module_path.is_file(), (
            f"{app_id}'s action entrypoint references bundles.{module_name}, "
            f"but no {module_path} file exists"
        )


class TestUpgradeActivatesTenantWithBotTokenRef:
    @pytest.mark.parametrize("app_id", [LOYALTY_APP_ID, SHOUTOUT_APP_ID])
    def test_inserts_app_tenant_availability_row_for_global_tenant(
        self, upgrade_sql, app_id
    ) -> None:
        assert upgrade_sql.count("INSERT INTO app_tenant_availability") == 2
        assert f"'{app_id}'" in upgrade_sql
        assert f"WHERE t.slug = '{TENANT_SLUG}'" in upgrade_sql

    def test_both_activations_set_bot_token_ref_directly(self, upgrade_sql) -> None:
        assert upgrade_sql.count(f'"bot_token_ref": "{BOT_TOKEN_REF}"') == 2

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
    @pytest.mark.parametrize("app_id", [LOYALTY_APP_ID, SHOUTOUT_APP_ID])
    def test_deletes_app_tenant_availability_row(self, downgrade_sql, app_id) -> None:
        assert "DELETE FROM app_tenant_availability" in downgrade_sql
        assert f"'{app_id}'" in downgrade_sql

    @pytest.mark.parametrize("app_id", [LOYALTY_APP_ID, SHOUTOUT_APP_ID])
    def test_deletes_app_catalog_row(self, downgrade_sql, app_id) -> None:
        assert f"DELETE FROM app_catalog WHERE app_id = '{app_id}'" in downgrade_sql

    def test_deletes_app_tenant_availability_before_app_catalog_for_each_app(
        self, downgrade_sql
    ) -> None:
        # app_tenant_availability.app_id has an FK onto app_catalog.app_id --
        # deleting catalog rows first would violate the constraint.
        ata_indexes = [
            m.start() for m in re.finditer("DELETE FROM app_tenant_availability", downgrade_sql)
        ]
        catalog_indexes = [
            m.start()
            for m in re.finditer(r"DELETE FROM app_catalog WHERE", downgrade_sql)
        ]
        assert len(ata_indexes) == 2
        assert len(catalog_indexes) == 2
        assert max(ata_indexes) < min(catalog_indexes) or (
            # Per-app ordering also holds: each app's own ata delete
            # precedes its own catalog delete.
            all(
                downgrade_sql.index(
                    f"DELETE FROM app_tenant_availability\n        WHERE app_id = '{app_id}'"
                )
                < downgrade_sql.index(f"DELETE FROM app_catalog WHERE app_id = '{app_id}'")
                for app_id in (LOYALTY_APP_ID, SHOUTOUT_APP_ID)
            )
        )

    def test_downgrade_removes_exactly_the_two_seeded_app_ids(self, downgrade_sql) -> None:
        for app_id in (LOYALTY_APP_ID, SHOUTOUT_APP_ID):
            assert f"'{app_id}'" in downgrade_sql
        assert downgrade_sql.count("DELETE FROM app_catalog") == 2
        assert downgrade_sql.count("DELETE FROM app_tenant_availability") == 2
