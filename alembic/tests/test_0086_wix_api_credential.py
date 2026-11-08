"""Tests for 0086_wix_api_credential: chain, sealed column + kind CHECK, resource types, downgrade guard.

Static checks pin the chain and single head; the real-Postgres test (docker; skipped where the CLI is
absent, same convention as 0081) proves the column exists, is refused on a static-kind row, the two
music resource types are accepted, an unknown type is still refused, and downgrade refuses while a
row that the previous release cannot represent exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0086_wix_api_credential.py"
_PREVIOUS = "0085_bundle_community_config"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0086", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == _PREVIOUS
    assert len(module.revision) <= 32


def test_history_has_exactly_one_head_and_0086_is_in_it() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0086_wix_api_credential" in {r.revision for r in script.walk_revisions()}


def _tenant(cur: psycopg2.extensions.cursor, slug: str) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert(
    cur: psycopg2.extensions.cursor, tenant: int, platform: str, rtype: str, rid: str
) -> None:
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, %s, %s, %s, 'sealed')",
        (tenant, platform, rtype, rid),
    )


@requires_docker
def test_column_check_types_and_guarded_downgrade() -> None:
    with migrated_postgres("0086-wix-api") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-0086")
                _insert(
                    cur, tenant, "soundcloud", "soundcloud_app", "SCID0123456789abcdef"
                )
                _insert(cur, tenant, "youtube_api", "youtube_api_key", "main")
                _insert(cur, tenant, "wix", "wix_site", "WIX-INSTANCE-1")
                with pytest.raises(psycopg2.errors.CheckViolation):
                    _insert(cur, tenant, "x", "carrier_pigeon", "x")
                cur.execute(
                    "UPDATE platform_connections SET sealed_api_credential = 'ct' "
                    "WHERE resource_id = 'WIX-INSTANCE-1'"
                )
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, connection_kind, sealed_secret, sealed_api_credential) "
                        "VALUES (%s, 'external_http', 'external_http', 'u', 2, 's', 'ct')",
                        (tenant,),
                    )
            with pytest.raises(RuntimeError, match="0086 downgrade refused"):
                alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM platform_connections "
                    "WHERE resource_type IN ('soundcloud_app', 'youtube_api_key', 'wix_site')"
                )
            alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
                _insert(
                    cur, tenant, "soundcloud", "soundcloud_app", "SCID0123456789abcdef"
                )
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                _insert(
                    cur, tenant, "soundcloud", "soundcloud_app", "SCID0123456789abcdef"
                )
        finally:
            conn.close()
