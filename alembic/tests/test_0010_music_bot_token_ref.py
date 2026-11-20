"""Regression test for 0010_music_bot_token_ref.

0009_music_catalog activated `penguintechinc.waddles.music-pipeline` tenant-wide via
`app_tenant_availability` but only ever set `(tenant_id, app_id, available)`
-- `config_defaults` was left at `{}`, so `social_music_action.
enqueue_song_request`'s config never carried `bot_token_ref` and every
`!sr`/`!songrequest` failed with `NonRetryableTransportError("social music
bundle config missing required 'bot_token_ref'")`. Verified against the
live cluster: `penguintechinc.waddles.music-pipeline`'s `global`-tenant
`config_defaults` was `{}` while `penguintechinc.waddles.discord-pipeline`'s was
`{"bot_token_ref": "DISCORD_BOT_TOKEN", ...}`.

These tests inspect the migration's SQL directly (no local Postgres
fixture in this repo for `op.execute()`-based migrations) -- they assert
the `upgrade()`/`downgrade()` bodies target the right row and set/unset
the same `bot_token_ref` reference already live for the sibling
`penguintechinc.waddles.discord-pipeline` bundle.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0010_music_bot_token_ref.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0010_music_bot_token_ref", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration()


class TestMigrationMetadata:
    def test_chains_directly_off_0009_music_catalog(self, migration) -> None:
        assert migration.revision == "0010_music_bot_token_ref"
        assert migration.down_revision == "0009_music_catalog"

    def test_constants_match_the_music_app_and_shared_discord_bot_token(self, migration) -> None:
        assert migration.APP_ID == "penguintechinc.waddles.music-pipeline"
        assert migration.TENANT_SLUG == "global"
        # Matches the live value already set for penguintechinc.waddles.discord-pipeline's
        # own global-tenant config_defaults -- same shared bot connection.
        assert migration.BOT_TOKEN_REF == "DISCORD_BOT_TOKEN"


class TestUpgradeSetsBotTokenRef:
    def test_upgrade_updates_app_tenant_availability_config_defaults(self, migration) -> None:
        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()

        mock_execute.assert_called_once()
        sql = mock_execute.call_args[0][0]
        assert "UPDATE app_tenant_availability" in sql
        assert "config_defaults" in sql
        assert "penguintechinc.waddles.music-pipeline" in sql
        assert "'global'" in sql
        assert '"bot_token_ref": "DISCORD_BOT_TOKEN"' in sql

    def test_upgrade_merges_rather_than_overwrites_existing_config_defaults(self, migration) -> None:
        """Uses `||` (jsonb merge) over the COALESCEd existing value, not a
        bare assignment -- must not clobber a `config_defaults` an admin
        already hand-set for this row."""
        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()

        sql = mock_execute.call_args[0][0]
        assert "COALESCE(ata.config_defaults" in sql
        assert "||" in sql


class TestDowngradeRemovesBotTokenRef:
    def test_downgrade_removes_only_the_bot_token_ref_key(self, migration) -> None:
        with patch("alembic.op.execute") as mock_execute:
            migration.downgrade()

        mock_execute.assert_called_once()
        sql = mock_execute.call_args[0][0]
        assert "penguintechinc.waddles.music-pipeline" in sql
        assert "- 'bot_token_ref'" in sql
