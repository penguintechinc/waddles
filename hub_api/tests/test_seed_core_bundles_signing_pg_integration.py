"""Seeder signs end-to-end against a FRESH, fully-migrated Postgres (real platform_vault).

Regression: alpha deploy run 43819105673 (2026-11-16) -- the seeder Job's process cache was never
seeded, so every activation failed `platform_vault has no active bundle_signing key`. Here:
hub-api's real bootstrap (legacy Secret in env -> imported), a FRESH process cache as the seeder
pod has (no KEK), `load_offline_signer_cache` as the real `waddles_hub_api` role, then
`seed_one()` signs and the stored signature verifies against the legacy public key the executors
trust.
Also covers the vault's create-if-absent rules on real PG: empty -> created, present -> untouched.
Skipped without `docker`.
"""

from __future__ import annotations

import base64
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
from services.bundle_signing_service import build_signing_payload  # noqa: E402
from services.envelope.platform_kek import PlatformKekAdapter  # noqa: E402
from services.platform_secrets import bootstrap as vault_bootstrap  # noqa: E402
from services.platform_secrets.bootstrap import (  # noqa: E402
    get_process_cache,
    run_platform_secrets_bootstrap,
)
from services.platform_secrets.cache import PlatformSecretCache  # noqa: E402
from tests.test_seed_core_bundles import (  # noqa: E402
    _MANIFEST,
    _patch_validator_and_storage,
    _write_bundle,
)
from tests.test_seed_core_bundles_pg_integration import pg_dal  # noqa: E402,F401 -- fixture

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

#: Captured before conftest's autouse stub replaces the module attribute.
_REAL_LOAD = vault_bootstrap.load_offline_signer_cache

KEK = PlatformKekAdapter(bytes.fromhex("c3" * 32))  # throwaway test key
LEGACY_SEED = os.urandom(32)  # throwaway per-run test seed, never a real key
LEGACY_KID = "waddlebot-1"
HUB_ROLE = "waddles_hub_api"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """A fresh Postgres migrated to head -- an empty platform_vault, like a new install."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-seed-signing") as db:
        yield db


def _hub_engine(db: PgTestDatabase) -> sqlalchemy.engine.Engine:
    password = db.service_role_passwords.get(HUB_ROLE)
    if password is None:
        pytest.skip("the migrated container has no waddles_hub_api login password")
    url = f"postgresql+psycopg2://{HUB_ROLE}:{password}@{db.host}:{db.port}/{db.dbname}"
    return sqlalchemy.create_engine(url, hide_parameters=True)


def _legacy_env() -> dict[str, str]:
    return {
        "BUNDLE_SIGNING_PRIVATE_KEY": base64.b64encode(LEGACY_SEED).decode(),
        "BUNDLE_SIGNING_KEY_ID": LEGACY_KID,
    }


def _bundle_rows(db: PgTestDatabase) -> list[tuple[int, int, str, int]]:
    """(type, version, kid, reason) of every vault row, as the owner."""
    engine = sqlalchemy.create_engine(
        f"postgresql+psycopg2://{db.user}:{db.password}@{db.host}:{db.port}/{db.dbname}"
    )
    try:
        with engine.connect() as conn:
            return [
                tuple(r)  # type: ignore[misc]
                for r in conn.execute(
                    text(
                        "SELECT secret_type, version, kid, reason FROM platform_vault.secrets "
                        "ORDER BY secret_type, version"
                    )
                )
            ]
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def vault_ready(pg_db: PgTestDatabase) -> AsyncIterator[sqlalchemy.engine.Engine]:
    """hub-api's real first-run bootstrap with the legacy Secret present (idempotent re-runs)."""
    engine = _hub_engine(pg_db)
    run_platform_secrets_bootstrap(
        engine,
        kill_switch=lambda: False,
        kek=KEK,
        environ=_legacy_env(),
        cache=PlatformSecretCache(),
        escrow=None,
    )
    yield engine
    engine.dispose()


def test_fresh_vault_imports_the_legacy_key_and_a_reboot_leaves_it_untouched(
    pg_db: PgTestDatabase, vault_ready: sqlalchemy.engine.Engine
) -> None:
    """Empty -> created (legacy imported, reason 2); a reboot with ANOTHER env key: no change."""
    first = _bundle_rows(pg_db)
    signing = [r for r in first if r[0] == 1]
    assert signing == [(1, 1, LEGACY_KID, 2)]
    assert {r[0] for r in first} == {1, 2, 3}  # every global type created once
    other = {
        "BUNDLE_SIGNING_PRIVATE_KEY": base64.b64encode(os.urandom(32)).decode(),
        "BUNDLE_SIGNING_KEY_ID": "other",
    }
    report = run_platform_secrets_bootstrap(
        vault_ready,
        kill_switch=lambda: False,
        kek=KEK,
        environ=other,
        cache=PlatformSecretCache(),
        escrow=None,
    )
    assert not report.generated and not report.imported
    assert _bundle_rows(pg_db) == first


async def test_seeder_signs_end_to_end_from_a_fresh_process_cache(
    pg_db: PgTestDatabase,
    pg_dal: Any,  # noqa: F811
    vault_ready: sqlalchemy.engine.Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import TENANT_SLUG

    _patch_validator_and_storage(monkeypatch)
    for key, value in _legacy_env().items():
        monkeypatch.setenv(key, value)
    cache = get_process_cache()
    cache.seed(())  # the seeder pod: nothing seeded by any bootstrap
    password = pg_db.service_role_passwords[HUB_ROLE]
    url = f"postgresql://{HUB_ROLE}:{password}@{pg_db.host}:{pg_db.port}/{pg_db.dbname}"
    try:
        loaded = _REAL_LOAD(url, environ=_legacy_env(), kill_switch=lambda: False)
        assert loaded == 1
        async with pg_dal.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (slug) VALUES (:s) ON CONFLICT (slug) DO NOTHING"),
                {"s": TENANT_SLUG},
            )
        entry = _write_bundle(tmp_path)
        await seeder.seed_one(pg_dal, entry, tmp_path, valkey_client=AsyncMock())
        async with pg_dal.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT v.version, v.artifact_digest, v.artifact_signature, "
                        "v.artifact_signature_key_id, v.artifact_signed_approval_id "
                        "FROM app_versions v WHERE v.app_id = :a"
                    ),
                    {"a": _MANIFEST["app_id"]},
                )
            ).one()
    finally:
        cache.seed(())
    version, digest, signature, key_id, approval_id = row
    assert key_id == LEGACY_KID and signature
    Ed25519PrivateKey.from_private_bytes(LEGACY_SEED).public_key().verify(
        base64.b64decode(signature),
        build_signing_payload(
            app_id=_MANIFEST["app_id"], version=version, digest=digest, approval_id=approval_id
        ),
    )
