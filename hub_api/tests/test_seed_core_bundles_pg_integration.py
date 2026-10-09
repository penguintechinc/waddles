"""Seeder tenant-wide grants against a REAL, fully-migrated Postgres (FK enforced).

Regression for the #480 kind-e2e deploy blocker: `community_permission_grants.community_id`
FKs `communities(id)`, the seeder grants tenant-wide with sentinel 0, and no row 0 existed ->
`Key (community_id)=(0) is not present` IntegrityError (36/40 core bundles failed to grant).
The sqlite unit tests cannot catch this (no FK enforcement). Skipped without `docker`.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from cli import seed_core_bundles as seeder  # noqa: E402
from services.bundle_install_dal import build_install_dal  # noqa: E402
from tests.test_seed_core_bundles import (  # noqa: E402
    _MANIFEST,
    _patch_validator_and_storage,
    _write_bundle,
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to alembic `head`."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-seed-grants-sentinel") as db:
        yield db


@pytest_asyncio.fixture
async def pg_dal(pg_db: PgTestDatabase) -> AsyncIterator[Any]:
    """`install_dal` over the migrated container, with the production `communities` columns."""
    async_url = pg_db.dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    dal = await build_install_dal(async_url, pool_size=2)
    async with dal.engine.begin() as conn:
        # `pg_docker`'s bootstrap `communities` is `(id, name)` only; add the production
        # columns the sentinel insert relies on.
        for ddl in (
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER",
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT TRUE",
            # bootstrap `app_catalog` is `(app_id)` only; the seeder's catalog INSERT needs these.
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS manifest_version TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS module TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS feature TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS provider TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS execution_model TEXT",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS is_default BOOLEAN",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS platform_compatibility JSONB",
            "ALTER TABLE app_catalog ADD COLUMN IF NOT EXISTS status TEXT",
        ):
            await conn.execute(text(ddl))
    await dal.reflect()
    yield dal
    await dal.engine.dispose()


async def test_tenant_wide_core_grants_succeed_with_fk_enforced(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh DB -> seeder -> tenant-wide grant row inserted, sentinel community 0 present."""
    from tests.conftest import TENANT_SLUG

    _patch_validator_and_storage(monkeypatch)
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (slug) VALUES (:s) ON CONFLICT (slug) DO NOTHING"),
            {"s": TENANT_SLUG},
        )
    entry = _write_bundle(tmp_path)  # community_id=None -> tenant-wide

    await seeder.seed_one(pg_dal, entry, tmp_path, valkey_client=AsyncMock())

    async with pg_dal.engine.connect() as conn:
        sentinel = (
            (await conn.execute(text("SELECT name FROM communities WHERE id = 0"))).scalars().all()
        )
        grants = (
            (
                await conn.execute(
                    text(
                        "SELECT permission_id FROM community_permission_grants "
                        "WHERE community_id = 0 AND app_id = :a AND revoked_at IS NULL"
                    ),
                    {"a": _MANIFEST["app_id"]},
                )
            )
            .scalars()
            .all()
        )
    assert sentinel == [seeder._SENTINEL_COMMUNITY_NAME]
    assert grants == ["storage.kv"], "tenant-wide grant must INSERT (no FK IntegrityError)"
