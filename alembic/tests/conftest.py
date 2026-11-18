"""Session-scoped real-Postgres fixture, used only by `test_0028_bundle_active_set_changelog.py`.

Every other `test_00NN_*.py` in this directory mocks `alembic.op.execute`
(no real database needed) -- see `pg_docker.py`'s own module docstring
for why 0028's own tests are the one exception.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

def pytest_configure(config: pytest.Config) -> None:
    """Keep each test file on ONE xdist worker (`--dist loadfile`).

    Real-Postgres modules here are order-dependent by design: a module/session-scoped
    container is mutated by `test_1`, `test_2`, ... in file order (apply -> idempotent ->
    changed secret; `TestRealPostgres` apply -> constraints). xdist's default `--dist load`
    scatters those tests across workers, each with its own fresh container, so a later test
    runs against state the earlier one never created -- a scheduling-dependent flake.
    """
    if getattr(config.option, "dist", "no") in ("load", "loadscope"):
        config.option.dist = "loadfile"


requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="session")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every test in this module."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0028-changelog") as db:
        yield db
