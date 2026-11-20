"""Regression test for 0016_moderation_enforce_app (gh-304).

Same harness convention as `test_0009_music_catalog.py`-equivalent /
`test_0014_wave1a_bundle_seeds.py` -- this repo has no pytest-level
fixture that runs Alembic against a real Postgres in CI, so these tests
mock `alembic.op.execute` and assert the exact SQL text 0016's
`upgrade()`/`downgrade()` emit: (a) the `app_catalog` row is an
action-only upsert with the exact `stages` JSON the design/task spec
calls for; (b) the concatenated `stages` JSON blob is syntactically valid
once its `||`-joined literals are combined (the exact bug
`0014_wave1a_bundle_seeds`'s own test suite was authored to catch: a bare
`... || '...'::jsonb` casts only the LAST literal unless the whole
concatenation is wrapped in `(...)::jsonb`); (c) `app_tenant_availability`
is upserted with `config_defaults.bot_token_ref` set directly (no
follow-up migration needed, unlike music's `0009`/`0010` split); (d) both
upgrade()/downgrade() are idempotent (`ON CONFLICT ... DO UPDATE` /
`IF EXISTS`-equivalent deletes) and downgrade() removes exactly what
upgrade() added, in FK-safe order.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0016_moderation_enforce_app.py"
)

APP_ID = "penguintechinc.waddles.moderation-pipeline"
TENANT_SLUG = "global"
BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_0016_moderation_enforce_app", _MIGRATION_PATH
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
    def test_chains_directly_off_0015_loyalty_core_tables(self, migration) -> None:
        assert migration.revision == "0016_moderation_enforce_app"
        assert migration.down_revision == "0015_loyalty_core_tables"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration) -> None:
        # alembic_version.version_num is VARCHAR(32) -- a too-long revision
        # id fails silently truncated or raises at stamp time.
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """Every other version file's down_revision must not also point at
        0015 -- otherwise alembic has two heads and `alembic upgrade head`
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

        assert down_revisions.count("0015_loyalty_core_tables") == 1, (
            "more than one migration chains off 0015_loyalty_core_tables -- "
            "alembic would report multiple heads"
        )

    def test_constants_match_the_moderation_app_and_shared_discord_bot_token(
        self, migration
    ) -> None:
        assert migration.APP_ID == APP_ID
        assert migration.TENANT_SLUG == TENANT_SLUG
        assert migration.BOT_TOKEN_REF == BOT_TOKEN_REF


class TestUpgradeSeedsAppCatalog:
    def test_inserts_app_catalog_row_for_the_moderation_app_id(self, upgrade_sql) -> None:
        assert f"'{APP_ID}'" in upgrade_sql
        assert "INSERT INTO app_catalog" in upgrade_sql

    def test_app_catalog_insert_is_upsert_on_app_id(self, upgrade_sql) -> None:
        assert "ON CONFLICT (app_id) DO UPDATE SET" in upgrade_sql
        assert "stages = EXCLUDED.stages" in upgrade_sql

    def test_action_entrypoint_targets_moderation_enforce_action(self, upgrade_sql) -> None:
        assert "bundles.moderation_enforce_action:enforce" in upgrade_sql

    def test_no_process_stage_declared(self, upgrade_sql) -> None:
        """Action-only app -- the moderation gate, not a catalog process
        bundle, decides whether to route here (see module docstring)."""
        assert '"process"' not in upgrade_sql

    def test_module_and_feature_follow_the_app_id_namespace(self, upgrade_sql) -> None:
        assert "'community'" in upgrade_sql
        assert "'waddles.community.moderation'" in upgrade_sql


class TestSeededStagesJsonIsValid:
    """Guards the exact bug 0014_wave1a_bundle_seeds' own test suite was
    authored to catch: a bare `str1 || str2::jsonb` casts ONLY the last
    literal (`::` binds tighter than `||`) unless the whole concatenation
    is wrapped in `(...)::jsonb`."""

    def _stages_blob(self, upgrade_sql: str) -> str:
        match = re.search(
            r"\(\s*((?:'(?:[^'\\]|\\.)*'\s*\|\|\s*)*'(?:[^'\\]|\\.)*')\s*\)::jsonb",
            upgrade_sql,
            re.DOTALL,
        )
        assert match is not None, (
            "no wrapped (...)::jsonb stages blob found -- the concatenation "
            "cast may be missing its wrapping parens"
        )
        literals = re.findall(r"'((?:[^'\\]|\\.)*)'", match.group(1), re.DOTALL)
        return "".join(literals)

    def test_stages_blob_is_valid_json(self, upgrade_sql) -> None:
        blob = self._stages_blob(upgrade_sql)
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError as exc:
            pytest.fail(f"stages blob is not valid JSON: {exc}\n{blob!r}")
        else:
            assert parsed == {
                "action": {
                    "entrypoint": "bundles.moderation_enforce_action:enforce",
                    "config": {"api_base": "https://discord.com/api/v10"},
                    "spec": {"required_config": ["bot_token_ref"]},
                }
            }

    def test_no_bare_multi_literal_jsonb_cast_without_wrapping_parens(
        self, upgrade_sql
    ) -> None:
        assert not re.search(r"\|\|\s*'(?:[^'\\]|\\.)*'::jsonb", upgrade_sql), (
            "found a ::jsonb cast applied directly to the last literal of a "
            "|| chain with no wrapping parens -- only that literal would be "
            "cast, not the full concatenated stages JSON"
        )


class TestUpgradeActivatesTenantWithBotTokenRef:
    def test_inserts_app_tenant_availability_row_for_global_tenant(self, upgrade_sql) -> None:
        assert "INSERT INTO app_tenant_availability" in upgrade_sql
        assert f"'{APP_ID}'" in upgrade_sql
        assert f"WHERE t.slug = '{TENANT_SLUG}'" in upgrade_sql

    def test_sets_bot_token_ref_directly_no_followup_migration_needed(self, upgrade_sql) -> None:
        assert f'"bot_token_ref": "{BOT_TOKEN_REF}"' in upgrade_sql

    def test_activation_insert_is_upsert_merging_config_defaults(self, upgrade_sql) -> None:
        assert "ON CONFLICT (tenant_id, app_id) DO UPDATE SET" in upgrade_sql
        assert "COALESCE(app_tenant_availability.config_defaults" in upgrade_sql
        assert "||" in upgrade_sql


class TestDowngradeRemovesEverythingUpgradeAdds:
    def test_deletes_app_tenant_availability_row(self, downgrade_sql) -> None:
        assert "DELETE FROM app_tenant_availability" in downgrade_sql
        assert f"'{APP_ID}'" in downgrade_sql

    def test_deletes_app_catalog_row(self, downgrade_sql) -> None:
        assert f"DELETE FROM app_catalog WHERE app_id = '{APP_ID}'" in downgrade_sql

    def test_deletes_app_tenant_availability_before_app_catalog(self, downgrade_sql) -> None:
        # app_tenant_availability.app_id has an FK onto app_catalog.app_id --
        # deleting catalog rows first would violate the constraint.
        ata_index = downgrade_sql.index("DELETE FROM app_tenant_availability")
        catalog_index = downgrade_sql.index("DELETE FROM app_catalog")
        assert ata_index < catalog_index
