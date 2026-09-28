"""Unit tests for community_module's `Config` class.

`load_credentials_from_db` and `start_credential_listener` are not called
anywhere in `app.py`'s startup path today (grep audit: no call site in this
module, nor in any sibling `core/*_module/app.py` that also carries this
same boilerplate `Config` shape) -- effectively dead code carried over from
a shared template. That is a real gap worth flagging (a module claiming
Redis-backed credential-refresh support that never actually wires it up),
but wiring it into `startup()` is a functional/feature change outside this
suite's scope; these tests instead pin down the two methods' *own*
behavior directly so the dead code is at least verified correct and has a
regression net for whenever it is wired up.
"""

from __future__ import annotations

import importlib
import sys
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

MODULE_NAME = "config"


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Re-import `config` fresh so each test's monkeypatched env actually takes."""
    monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    yield module
    sys.modules.pop(MODULE_NAME, None)


class TestConfigDefaults:
    def test_module_name_and_version(self, fresh_config: Any) -> None:
        assert fresh_config.Config.MODULE_NAME == "community_module"
        assert fresh_config.Config.MODULE_VERSION == "2.0.0"

    def test_default_module_port(self, fresh_config: Any) -> None:
        assert fresh_config.Config.MODULE_PORT == 8020

    def test_module_port_reads_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
        monkeypatch.setenv("MODULE_PORT", "9099")
        sys.modules.pop(MODULE_NAME, None)
        module = importlib.import_module(MODULE_NAME)
        try:
            assert module.Config.MODULE_PORT == 9099
        finally:
            sys.modules.pop(MODULE_NAME, None)

    def test_default_database_url(self, fresh_config: Any) -> None:
        assert fresh_config.Config.DATABASE_URL == (
            "postgresql://waddlebot:password@localhost:5432/waddlebot"
        )

    def test_default_redis_url_is_empty(self, fresh_config: Any) -> None:
        assert fresh_config.Config.REDIS_URL == ""


class TestLoadCredentialsFromDb:
    def test_returns_true_and_sets_flag_when_row_found(self, fresh_config: Any) -> None:
        config_cls = fresh_config.Config
        config_cls._credentials_loaded = False
        db_connection = MagicMock()
        db_connection.executesql.return_value = [("token-value", {"foo": "bar"})]

        result = config_cls.load_credentials_from_db(db_connection)

        assert result is True
        assert config_cls._credentials_loaded is True
        db_connection.executesql.assert_called_once()
        (sql,), _ = db_connection.executesql.call_args
        assert "platform_integrations" in sql
        assert "platform = 'community'" in sql

    def test_returns_false_when_no_rows(self, fresh_config: Any) -> None:
        config_cls = fresh_config.Config
        config_cls._credentials_loaded = False
        db_connection = MagicMock()
        db_connection.executesql.return_value = []

        result = config_cls.load_credentials_from_db(db_connection)

        assert result is False
        assert config_cls._credentials_loaded is False

    def test_falls_back_to_env_on_exception(self, fresh_config: Any) -> None:
        config_cls = fresh_config.Config
        config_cls._credentials_loaded = False
        db_connection = MagicMock()
        db_connection.executesql.side_effect = RuntimeError("connection lost")

        result = config_cls.load_credentials_from_db(db_connection)

        assert result is False
        assert config_cls._credentials_loaded is False


class TestStartCredentialListener:
    def test_returns_none_when_redis_not_configured(self, fresh_config: Any) -> None:
        config_cls = fresh_config.Config
        config_cls.REDIS_URL = ""

        thread = config_cls.start_credential_listener(redis_client=MagicMock())

        assert thread is None

    def test_starts_daemon_thread_and_clears_flag_on_message(self, fresh_config: Any) -> None:
        config_cls = fresh_config.Config
        config_cls.REDIS_URL = "redis://localhost:6379/0"
        config_cls._credentials_loaded = True

        pubsub = MagicMock()
        pubsub.listen.return_value = iter(
            [{"type": "subscribe", "data": 1}, {"type": "message", "data": b"refresh"}]
        )
        redis_client = MagicMock()
        redis_client.pubsub.return_value = pubsub

        thread = config_cls.start_credential_listener(redis_client)

        assert isinstance(thread, threading.Thread)
        assert thread.daemon is True
        thread.join(timeout=2)
        assert not thread.is_alive()
        pubsub.subscribe.assert_called_once_with("credentials:community:bot:refreshed")
        assert config_cls._credentials_loaded is False

    def test_listener_thread_survives_redis_error(self, fresh_config: Any) -> None:
        """A broken Redis connection must not crash the daemon thread."""
        config_cls = fresh_config.Config
        config_cls.REDIS_URL = "redis://localhost:6379/0"

        redis_client = MagicMock()
        redis_client.pubsub.side_effect = RuntimeError("redis unreachable")

        thread = config_cls.start_credential_listener(redis_client)
        assert thread is not None
        thread.join(timeout=2)
        assert not thread.is_alive()
