"""config.py -- DATABASE_URL scheme normalization for pydal.

Mirrors `core/svc_action/tests/test_config.py` exactly -- svc-process's DB
config was added as a line-for-line mirror of svc-action's (see
`config.py`'s own module docstring), so the regression coverage mirrors
too: a directly-supplied `DATABASE_URL` (the shared Helm secret's
`postgresql://` scheme) must not bypass `_build_db_url`'s scheme
translation and reach pydal as-is (`SyntaxError: Adapter not found for
postgresql` at `DAL()` construction).
"""

from __future__ import annotations

import importlib

import pytest


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
        import config

        assert (
            config._normalize_pydal_scheme("postgresql://u:p@h:5432/db")  # noqa: SLF001
            == "postgres://u:p@h:5432/db"
        )

    def test_rewrites_postgresql_plus_driver_scheme(self) -> None:
        import config

        assert (
            config._normalize_pydal_scheme("postgresql+psycopg2://u@h/db")  # noqa: SLF001
            == "postgres://u@h/db"
        )

    def test_leaves_already_correct_scheme_untouched(self) -> None:
        import config

        assert (
            config._normalize_pydal_scheme("postgres://u:p@h:5432/db")  # noqa: SLF001
            == "postgres://u:p@h:5432/db"
        )

    def test_leaves_non_postgres_scheme_untouched(self) -> None:
        import config

        assert config._normalize_pydal_scheme("sqlite:memory") == "sqlite:memory"  # noqa: SLF001

    def test_unsupported_db_type_raises(self) -> None:
        import config

        with pytest.raises(ValueError, match="unsupported DB_TYPE"):
            config._build_db_url(  # noqa: SLF001
                db_type="oracle", host="h", port="1", name="n", user="u", password=""
            )


class TestConfigDatabaseUrl:
    """`Config.DATABASE_URL` -- both the direct-`DATABASE_URL` and component-built paths.

    `Config` is a plain class (not `ActionConfig`'s `from_env()`
    classmethod), so each test reloads the `config` module after setting
    env vars to re-evaluate the class body against the new environment.
    """

    def test_direct_database_url_with_sqlalchemy_scheme_is_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression: alpha svc-action crash (same fix mirrored here) -- see module docstring."""
        _env(
            monkeypatch,
            DATABASE_URL="postgresql://waddlebot:secret@infra-postgres:5432/waddlebot",
        )
        import config

        importlib.reload(config)
        assert (
            config.Config.DATABASE_URL
            == "postgres://waddlebot:secret@infra-postgres:5432/waddlebot"
        )

    def test_component_built_url_defaults_to_svc_process_rw_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`DB_USER` defaults to `svc-process-rw`, distinct from svc-action's `svc-action-rw`."""
        _env(monkeypatch, DB_PASS="secret")
        import config

        importlib.reload(config)
        assert "svc-process-rw" in config.Config.DATABASE_URL
        assert config.Config.DATABASE_URL.startswith("postgres://")

    def test_sqlite_db_type_builds_memory_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, DB_TYPE="sqlite", DB_NAME="memory")
        import config

        importlib.reload(config)
        assert config.Config.DATABASE_URL == "sqlite:memory:"

    def test_db_pool_size_defaults_and_is_overridable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, DB_TYPE="sqlite", DB_NAME="memory")
        monkeypatch.setenv("DB_POOL_SIZE", "17")
        import config

        importlib.reload(config)
        assert config.Config.DB_POOL_SIZE == 17
