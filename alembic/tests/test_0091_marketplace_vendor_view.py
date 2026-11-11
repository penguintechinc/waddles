"""Tests for 0091_marketplace_vendor_view: chain, bundle branch, vendor derivation, backfill, grants.

The real-Postgres test (docker; skipped where the CLI is absent) rebuilds the legacy 059 view over
minimal legacy tables, runs the migration, and proves: every active `app_catalog` row appears as a
`bundle` row, `vendor_key`/`source_repo` are derived (author first, source URL second), a NULL
`app_catalog.author` is backfilled from the newest `app_versions` row, the two legacy branches are
untouched, SELECT grants survive, and `downgrade` restores the old column set.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0091_marketplace_vendor_view.py"
_VIEW_059 = (
    _ROOT / "config" / "postgres" / "migrations" / "059_marketplace_consolidation.sql"
)
_PREVIOUS = "0090_kick_connection"

_LEGACY_DDL = """
CREATE TABLE hub_modules (
    id SERIAL PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL, display_name VARCHAR(255),
    description TEXT, version VARCHAR(50), author VARCHAR(255), category VARCHAR(100),
    icon_url TEXT, is_published BOOLEAN DEFAULT false, is_core BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now());
CREATE TABLE hub_module_reviews (id SERIAL PRIMARY KEY, module_id INT, rating INT);
CREATE TABLE hub_module_installations (id SERIAL PRIMARY KEY, module_id INT);
CREATE TABLE marketplace_sellers (id SERIAL PRIMARY KEY, display_name VARCHAR(255));
CREATE TABLE marketplace_modules (
    id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL, description TEXT,
    category VARCHAR(100), icon_url VARCHAR(500), pricing_type VARCHAR(50) DEFAULT 'free',
    price_cents INT DEFAULT 0, pricing_model VARCHAR(50) DEFAULT 'flat', version VARCHAR(50),
    seller_id INT, webhook_url VARCHAR(500), communication_model VARCHAR(50),
    integration_type VARCHAR(50), rating_count INT DEFAULT 0, rating_sum INT DEFAULT 0,
    install_count INT DEFAULT 0, status VARCHAR(50), deleted_at TIMESTAMPTZ, tenant_id INT,
    created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS manifest_version TEXT;
ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS installed_at TIMESTAMPTZ DEFAULT now();
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0091", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_view_sql() -> str:
    """The real 059 view definition, extracted verbatim so the test cannot drift from it."""
    text = _VIEW_059.read_text()
    match = re.search(
        r"CREATE OR REPLACE VIEW marketplace_catalog AS.*?;\n", text, re.DOTALL
    )
    assert match is not None
    return match.group(0)


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == _PREVIOUS
    assert len(module.revision) <= 32


def test_history_has_exactly_one_head_and_0091_is_in_it() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0091_marketplace_vendor_view" in {
        r.revision for r in script.walk_revisions()
    }


def _rows(cur: psycopg2.extensions.cursor, sql: str) -> list[tuple[object, ...]]:
    cur.execute(sql)
    return cur.fetchall()


@requires_docker
def test_view_rebuild_backfill_vendor_derivation_and_grants() -> None:
    with migrated_postgres("0091-vendor") as db:
        # Bootstrap has no legacy view, so 0091 was skipped; step back, build the legacy schema.
        alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(_LEGACY_DDL)
        cur.execute(_legacy_view_sql())
        cur.execute("CREATE ROLE mc_reader_test NOLOGIN")
        cur.execute("GRANT SELECT ON marketplace_catalog TO mc_reader_test")
        cur.execute(
            "INSERT INTO hub_modules (name, display_name, author, is_published) "
            "VALUES ('demo-core', 'Demo Core', 'PenguinTech', true)"
        )
        cur.execute(
            "INSERT INTO marketplace_sellers (display_name) VALUES ('Some Seller')"
        )
        cur.execute(
            "INSERT INTO marketplace_modules (name, seller_id, status) "
            "VALUES ('demo-vendor', 1, 'approved')"
        )
        for app_id, author, url in (
            (
                "waddles.core.a.one",
                "github:Acme-Co",
                "https://github.com/Acme-Co/social/tree/main/a",
            ),
            (
                "waddles.core.a.two",
                "github:acme-co",
                "https://github.com/acme-co/business/x",
            ),
            (
                "waddles.core.a.three",
                "Plain Name",
                "https://github.com/Url-Org/repo.git",
            ),
            ("waddles.core.a.orphan", "Plain Name", None),
        ):
            cur.execute(
                "INSERT INTO app_catalog (app_id, name, manifest_version, status, author, "
                "source_url, category) VALUES (%s, %s, '1.0.0', 'active', %s, %s, 'alternatives')",
                (app_id, app_id, author, url),
            )
        # NULL attribution on the catalog row, but the newest published version carries it.
        cur.execute(
            "INSERT INTO app_catalog (app_id, name, manifest_version, status) "
            "VALUES ('waddles.core.a.legacy', 'legacy', '1.0.0', 'active')"
        )
        for version, author in (
            ("1.0.0", "github:old-org"),
            ("1.0.1", "github:new-org"),
        ):
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind, author, "
                "source_url) VALUES ('waddles.core.a.legacy', %s, 'rust', 'prebuilt', %s, "
                "'https://github.com/new-org/repo/tree/main/l')",
                (version, author),
            )
        cur.execute(
            "INSERT INTO app_catalog (app_id, name, manifest_version, status, author) "
            "VALUES ('waddles.core.a.yanked', 'y', '1.0.0', 'yanked', 'github:acme-co')"
        )

        alembic_cli("upgrade", "head", dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)  # idempotent re-run is a no-op

        got = {
            r[0]: r[1:]
            for r in _rows(
                cur,
                "SELECT source_ref, vendor_key, source_repo, version, source_id, source "
                "FROM marketplace_catalog WHERE source = 'bundle'",
            )
        }
        assert set(got) == {
            "waddles.core.a.one",
            "waddles.core.a.two",
            "waddles.core.a.three",
            "waddles.core.a.orphan",
            "waddles.core.a.legacy",
        }, "yanked bundle must be excluded; every active bundle present"
        assert got["waddles.core.a.one"][:2] == ("acme-co", "acme-co/social")
        assert got["waddles.core.a.two"][:2] == ("acme-co", "acme-co/business")
        # No github author -> vendor falls back to the source URL's org.
        assert got["waddles.core.a.three"][:2] == (
            "url-org",
            "url-org/repo",
        )  # `.git` stripped
        assert got["waddles.core.a.orphan"][:2] == (
            None,
            None,
        )  # surfaced, read path raises
        assert got["waddles.core.a.legacy"][:2] == (
            "new-org",
            "new-org/repo",
        )  # newest version
        assert (
            got["waddles.core.a.legacy"][2] == "1.0.1"
        )  # newest `app_versions` row wins
        assert all(r[3] is None for r in got.values()), "bundle source_id is NULL"

        legacy = _rows(
            cur,
            "SELECT source, name, vendor_key FROM marketplace_catalog WHERE source <> 'bundle'",
        )
        assert sorted(legacy) == [
            ("core", "demo-core", None),
            ("marketplace", "demo-vendor", None),
        ]

        cur.execute(
            "SELECT has_table_privilege('mc_reader_test', 'marketplace_catalog', 'SELECT')"
        )
        assert cur.fetchone() == (True,), "SELECT grant must survive the view rebuild"

        alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'marketplace_catalog' AND column_name = 'vendor_key'"
        )
        assert cur.fetchone() == (0,)
        assert _rows(cur, "SELECT count(*) FROM marketplace_catalog") == [(2,)]
        conn.close()
