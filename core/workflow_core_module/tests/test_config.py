"""`config.py` -- credential DB loading + Redis-backed credential-refresh listener."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from config import Config


class TestLoadCredentialsFromDb:
    def test_success_marks_credentials_loaded(self) -> None:
        db = MagicMock()
        db.executesql.return_value = [("token", "{}")]
        Config._credentials_loaded = False
        result = Config.load_credentials_from_db(db)
        assert result is True
        assert Config._credentials_loaded is True

    def test_empty_result_returns_false(self) -> None:
        db = MagicMock()
        db.executesql.return_value = []
        assert Config.load_credentials_from_db(db) is False

    def test_db_error_returns_false(self) -> None:
        db = MagicMock()
        db.executesql.side_effect = Exception("db down")
        assert Config.load_credentials_from_db(db) is False


class TestStartCredentialListener:
    def test_no_redis_url_returns_none(self) -> None:
        with patch.object(Config, "REDIS_URL", ""):
            assert Config.start_credential_listener(MagicMock()) is None

    def test_starts_thread_and_processes_message(self) -> None:
        redis_client = MagicMock()
        pubsub = MagicMock()
        pubsub.listen.return_value = iter([
            {"type": "subscribe"},
            {"type": "message"},
        ])
        redis_client.pubsub.return_value = pubsub

        Config._credentials_loaded = True
        with patch.object(Config, "REDIS_URL", "redis://localhost:6379/0"):
            thread = Config.start_credential_listener(redis_client)
        assert thread is not None
        thread.join(timeout=5)
        assert Config._credentials_loaded is False
        pubsub.subscribe.assert_called_once()

    def test_listener_swallows_exceptions(self) -> None:
        redis_client = MagicMock()
        redis_client.pubsub.side_effect = Exception("connection refused")
        with patch.object(Config, "REDIS_URL", "redis://localhost:6379/0"):
            thread = Config.start_credential_listener(redis_client)
        thread.join(timeout=5)  # must not raise / hang
