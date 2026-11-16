"""Seeder: music-requests provisions its db-v2 tables and its consumers resolve (real Postgres).

# regression: alpha helm upgrade 2026-11-16 -- the post-upgrade core-bundle seeder failed 3/164:
# `waddles.core.service.music-requests` -> `bundle_data_provisioning_unavailable` (the
# waddles_bundle_migrator role had NO password and BUNDLE_MIGRATOR_PASSWORD was empty), then its
# consumers `waddles.core.example.music` / `waddles.core.example.dj` -> 422
# `tool_target_cross_tenant` (cascade: the service was never made available).

Drives the REAL catalog rows (`bundles/core-bundles.yaml`) and REAL hub manifests through
`seed_one()` against a fully-migrated Postgres in exactly alpha's state (0030 ran without a
migrator password). The fix path is then exercised end to end: the chart's db-migrate hook value
reaches the role via `scripts/db/bundle_storage_roles.py` (what migrations/run-alembic.sh runs),
the seeder pod gets the same value as BUNDLE_MIGRATOR_PASSWORD, approval provisions every declared
table as `waddles_bundle_migrator`, and both consumers activate. Skipped without `docker`.
"""

from __future__ import annotations

import secrets
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy import text

_REPO = Path(__file__).resolve().parents[2]
_ALEMBIC_TESTS_DIR = _REPO / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))
if str(_REPO / "scripts" / "db") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "db"))

import bundle_storage_roles  # noqa: E402  # type: ignore[import-not-found]
from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from cli import seed_core_bundles as seeder  # noqa: E402
from services.errors import ApiError  # noqa: E402
from tests.test_seed_core_bundles import (  # noqa: E402
    _COMPONENT_BYTES,
    _patch_validator_and_storage,
)
from tests.test_seed_core_bundles_pg_integration import pg_dal  # noqa: E402,F401 -- fixture

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

SERVICE = "waddles.core.service.music-requests"
CONSUMERS = ("waddles.core.example.music", "waddles.core.example.dj")
#: Throwaway value standing in for the chart-generated waddlebot-bundle-migrator Secret.
MIGRATOR_PW = secrets.token_urlsafe(24)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Fresh Postgres at alembic head WITHOUT a migrator password -- alpha's exact state."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-seed-music-requests") as db:
        yield db


def _tool_targets(manifest_path: Path) -> set[str]:
    """Every service app_id a manifest calls tools on (requested_tools + tool.call permissions)."""
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    targets = {t["target_app_id"] for t in raw.get("requested_tools") or [] if "target_app_id" in t}
    for perm in raw.get("permissions") or []:
        pid = perm.get("id", "") if isinstance(perm, dict) else str(perm)
        if pid.startswith("tool.call:"):
            targets.add(pid.split(":")[1])
    return targets


def _bundles_dir(tmp_path: Path) -> tuple[seeder.CatalogEntry, ...]:
    """Lay out the real catalog rows like the built core-bundles image (+ bundle.yaml agreement).

    Seeds the service, both consumers, and the transitive closure of every other service they
    call (e.g. the music app also targets the provider services) -- as the real catalog does.
    """
    catalog = {e.app_id: e for e in seeder.load_catalog(_REPO / "bundles" / "core-bundles.yaml")}
    wanted: set[str] = set()
    todo = [SERVICE, *CONSUMERS]
    while todo:
        app_id = todo.pop()
        if app_id in wanted:
            continue
        assert app_id in catalog, f"{app_id} missing from bundles/core-bundles.yaml"
        wanted.add(app_id)
        todo.extend(
            _tool_targets(_REPO / "bundles" / catalog[app_id].source_dir / "hub-manifest.yaml")
        )
    entries = tuple(e for e in catalog.values() if e.app_id in wanted)
    assert {SERVICE, *CONSUMERS} <= {e.app_id for e in entries}
    for e in entries:
        src = _REPO / "bundles" / e.source_dir
        shutil.copyfile(src / "hub-manifest.yaml", tmp_path / e.manifest_path)
        (tmp_path / e.artifact_path).write_bytes(_COMPONENT_BYTES)
        (tmp_path / e.source_dir).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "bundle.yaml", tmp_path / e.source_dir / "bundle.yaml")
    return seeder.order_services_first(entries, tmp_path)


async def _prepare_tenants(dal: Any, entries: tuple[seeder.CatalogEntry, ...]) -> None:
    """Create every tenant the real catalog rows target (alpha: the `default` tenant)."""
    slugs = {t.tenant_slug for e in entries for t in e.activation_targets}
    assert slugs, "catalog rows declare no activation targets"
    async with dal.engine.begin() as conn:
        for slug in sorted(slugs):
            await conn.execute(
                text("INSERT INTO tenants (slug) VALUES (:s) ON CONFLICT (slug) DO NOTHING"),
                {"s": slug},
            )


def _apply_migrator_password(db: PgTestDatabase, password: str) -> None:
    """What the db-migrate hook now does every run (migrations/run-alembic.sh)."""
    engine = sa.create_engine(db.dsn.replace("postgresql://", "postgresql+psycopg2://", 1))
    try:
        with engine.begin() as conn:
            bundle_storage_roles.reconcile(
                conn, {bundle_storage_roles.MIGRATOR_PASSWORD_ENV: password}
            )
    finally:
        engine.dispose()


async def _provisioned_tables(dal: Any) -> list[str]:
    async with dal.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT logical_name FROM bundle_data.table_registry WHERE app_id = :a ORDER BY 1"
            ),
            {"a": SERVICE},
        )
        return list(rows.scalars().all())


async def _available(dal: Any, app_id: str) -> bool:
    async with dal.engine.connect() as conn:
        row = await conn.execute(
            text("SELECT bool_or(available) FROM bundle_tenant_availability WHERE app_id = :a"),
            {"a": app_id},
        )
        return bool(row.scalar())


async def test_music_requests_provisions_and_consumers_resolve(
    pg_dal: Any,  # noqa: F811
    pg_db: PgTestDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_validator_and_storage(monkeypatch)
    entries = _bundles_dir(tmp_path)
    await _prepare_tenants(pg_dal, entries)
    order = [e.app_id for e in entries]
    assert all(order.index(SERVICE) < order.index(c) for c in CONSUMERS), order
    service_entry = entries[order.index(SERVICE)]
    monkeypatch.setenv("DATABASE_URL", pg_db.dsn)

    # Before the fix: role has no password, seeder env empty -> fail closed, never available.
    monkeypatch.delenv("BUNDLE_MIGRATOR_PASSWORD", raising=False)
    with pytest.raises(ApiError) as unavailable:
        await seeder.seed_one(pg_dal, service_entry, tmp_path, valkey_client=AsyncMock())
    assert unavailable.value.code == "bundle_data_provisioning_unavailable"
    assert not await _available(pg_dal, SERVICE)

    # The fix: hook value applied to the role, same value in the seeder pod's env.
    _apply_migrator_password(pg_db, MIGRATOR_PW)
    monkeypatch.setenv("BUNDLE_MIGRATOR_PASSWORD", MIGRATOR_PW)
    for entry in entries:
        results = await seeder.seed_one(pg_dal, entry, tmp_path, valkey_client=AsyncMock())
        assert results, f"{entry.app_id}: no seed results"

    assert await _provisioned_tables(pg_dal) == sorted(
        ["requests", "history", "settings", "artist_plays", "song_plays"]
    )
    for app_id in (SERVICE, *CONSUMERS):
        assert await _available(pg_dal, app_id), f"{app_id} not available to the tenant"
    async with pg_dal.engine.connect() as conn:
        owners = (
            (
                await conn.execute(
                    text(
                        "SELECT DISTINCT t.tableowner FROM pg_tables t"
                        " JOIN bundle_data.table_registry r ON r.physical_table = t.tablename"
                        " WHERE r.app_id = :a"
                    ),
                    {"a": SERVICE},
                )
            )
            .scalars()
            .all()
        )
    assert list(owners) == [bundle_storage_roles.MIGRATOR_ROLE]
