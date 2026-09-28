"""Session-scoped real-Postgres fixture, used only by `test_0026_bundle_active_set_changelog.py`.

Every other `test_00NN_*.py` in this directory mocks `alembic.op.execute`
(no real database needed) -- see `pg_docker.py`'s own module docstring
for why 0026's own tests are the one exception.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="session")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every test in this module."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0026-changelog") as db:
        yield db
