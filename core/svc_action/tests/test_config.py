"""config.py -- DATABASE_URL scheme normalization for pydal.

Regression coverage for the startup crash where a directly-supplied
`DATABASE_URL` (the shared Helm secret, SQLAlchemy's `postgresql://`
scheme) bypassed `_build_db_url`'s own scheme translation and reached
pydal as-is, raising `SyntaxError: Adapter not found for postgresql` at
`DAL()` construction time -- surfaced as a Quart lifespan startup failure.
"""

from __future__ import annotations

import pytest

from config import ActionConfig, _normalize_pydal_scheme


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key in (
        "DATABASE_URL",
        "DB_TYPE",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRET_KEY", "x" * 32)


class TestNormalizePydalScheme:
    """Unit coverage for the standalone chokepoint function."""

    def test_rewrites_postgresql_scheme(self) -> None:
        assert _normalize_pydal_scheme("postgresql://u:p@h:5432/db") == "postgres://u:p@h:5432/db"

    def test_rewrites_postgresql_plus_driver_scheme(self) -> None:
        assert _normalize_pydal_scheme("postgresql+psycopg2://u@h/db") == "postgres://u@h/db"

    def test_leaves_already_correct_scheme_untouched(self) -> None:
        assert _normalize_pydal_scheme("postgres://u:p@h:5432/db") == "postgres://u:p@h:5432/db"

    def test_leaves_non_postgres_scheme_untouched(self) -> None:
        assert _normalize_pydal_scheme("sqlite:memory") == "sqlite:memory"


class TestActionConfigDatabaseUrl:
    """`ActionConfig.from_env()` -- both the direct-`DATABASE_URL` and component-built paths."""

    def test_direct_database_url_with_sqlalchemy_scheme_is_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression: alpha svc-action crash -- see module docstring."""
        _env(
            monkeypatch,
            DATABASE_URL="postgresql://waddlebot:secret@infra-postgres:5432/waddlebot",
        )
        config = ActionConfig.from_env()
        assert config.database_url == "postgres://waddlebot:secret@infra-postgres:5432/waddlebot"

    def test_component_built_url_accepts_db_type_postgres_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The configmap ships DB_TYPE=postgres (not postgresql) -- must not raise."""
        _env(
            monkeypatch,
            DB_TYPE="postgres",
            DB_HOST="infra-postgres",
            DB_PORT="5432",
            DB_NAME="waddlebot",
            DB_USER="svc-action-rw",
            DB_PASS="secret",
        )
        config = ActionConfig.from_env()
        assert config.database_url.startswith("postgres://svc-action-rw:secret@")

    def test_component_built_url_accepts_db_type_postgresql(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, DB_TYPE="postgresql", DB_USER="svc-action-rw")
        config = ActionConfig.from_env()
        assert config.database_url.startswith("postgres://")

    def test_sqlite_url_is_unaffected_by_normalization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, DB_TYPE="sqlite", DB_NAME="memory")
        config = ActionConfig.from_env()
        assert config.database_url == "sqlite:memory:"
