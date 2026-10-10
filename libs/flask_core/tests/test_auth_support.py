"""Tests for the non-JWT-verification helpers in flask_core.auth.

They sit in the same module as the hardened JWT functions, so they are covered here rather than
left as the one reason the module's coverage reads low: API-key helpers, the service-key
comparison, and `setup_auth` provider wiring (no network - authlib only registers metadata).
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from flask_core import auth


class TestApiKeys:
    def test_create_api_key_format_and_entropy(self) -> None:
        key = auth.create_api_key()
        prefix, _, body = key.partition("-")
        assert prefix == "wa"
        assert len(body) == 64 and int(body, 16) >= 0
        assert auth.create_api_key() != key

    def test_create_api_key_custom_prefix_and_length(self) -> None:
        key = auth.create_api_key(prefix="svc", length=16)
        assert key.startswith("svc-") and len(key) == len("svc-") + 16

    def test_hash_api_key_is_stable_sha256_hex(self) -> None:
        digest = auth.hash_api_key("wa-abc")
        assert digest == auth.hash_api_key("wa-abc")
        assert len(digest) == 64 and digest != auth.hash_api_key("wa-abd")
        assert digest != "wa-abc"


class _Rows(list):  # minimal pydal-Rows stand-in
    def first(self) -> Any:
        return self[0] if self else None


class _FakeDal:
    """Async DAL double: attribute tables whose column comparisons are inert."""

    def __init__(self, keys: list[Any], users: list[Any]) -> None:
        col = SimpleNamespace(key_hash=object(), is_active=True, id=object())
        self.api_keys = col
        self.auth_user = SimpleNamespace(id=object())
        self._keys, self._users = keys, users
        self.updated: list[dict[str, Any]] = []

    async def select_async(self, query: Any) -> _Rows:
        # the fake distinguishes the two lookups by call order
        self._calls = getattr(self, "_calls", 0) + 1
        return _Rows(self._keys if self._calls % 2 == 1 else self._users)

    async def update_async(self, query: Any, **values: Any) -> None:
        self.updated.append(values)


def _key_row(**over: Any) -> SimpleNamespace:
    row = dict(id=1, user_id=7, name="ci-key", expires_at=None, permissions=["community:read"])
    row.update(over)
    return SimpleNamespace(**row)


USER = SimpleNamespace(id=7, username="alice", email="alice@example.invalid")


class TestVerifyApiKeyAsync:
    async def test_unknown_key(self, caplog: pytest.LogCaptureFixture) -> None:
        assert await auth.verify_api_key_async("wa-x", _FakeDal([], [])) is None
        assert "Invalid API key" in caplog.text

    async def test_expired_key(self) -> None:
        expired = _key_row(expires_at=datetime.utcnow() - timedelta(days=1))
        assert await auth.verify_api_key_async("wa-x", _FakeDal([expired], [USER])) is None

    async def test_key_for_a_vanished_user(self) -> None:
        assert await auth.verify_api_key_async("wa-x", _FakeDal([_key_row()], [])) is None

    async def test_valid_key_returns_user_and_stamps_last_used(self) -> None:
        dal = _FakeDal([_key_row(expires_at=datetime.utcnow() + timedelta(days=1))], [USER])
        result = await auth.verify_api_key_async("wa-x", dal)
        assert result == {
            "user_id": 7,
            "username": "alice",
            "email": "alice@example.invalid",
            "api_key_name": "ci-key",
            "permissions": ["community:read"],
        }
        assert "last_used_at" in dal.updated[0]

    async def test_missing_permissions_default_to_empty(self) -> None:
        result = await auth.verify_api_key_async("wa-x", _FakeDal([_key_row(permissions=None)], [USER]))
        assert result is not None and result["permissions"] == []


class TestVerifyServiceKey:
    def test_unconfigured_key_rejects_everything(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="flask_core.auth"):
            assert auth.verify_service_key("anything", None) is False
            assert auth.verify_service_key("", "") is False
        assert "not configured" in caplog.text

    def test_missing_provided_key(self) -> None:
        assert auth.verify_service_key("", "expected") is False
        assert auth.verify_service_key(None, "expected") is False  # type: ignore[arg-type]

    def test_wrong_and_right_key(self) -> None:
        assert auth.verify_service_key("nope", "expected") is False
        assert auth.verify_service_key("expected", "expected") is True


class _FakeSetupDal:
    """Records `define_table` calls; `Field` returns a tuple so the schema is inspectable."""

    def __init__(self) -> None:
        self.tables: dict[str, tuple[Any, ...]] = {}

    @staticmethod
    def Field(*args: Any, **kwargs: Any) -> tuple[Any, ...]:  # noqa: N802 - mirrors pydal's DAL.Field
        return (args, kwargs)

    def define_table(self, name: str, *fields: Any) -> None:
        self.tables[name] = fields


class TestSetupAuth:
    @pytest.fixture(autouse=True)
    def _isolate_provider_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """setup_auth writes credentials into the module-global provider table."""
        monkeypatch.setattr(auth, "OAUTH_PROVIDERS", copy.deepcopy(auth.OAUTH_PROVIDERS))

    def test_defines_the_auth_tables_and_generates_secrets(self) -> None:
        from flask import Flask

        app, dal = Flask("t"), _FakeSetupDal()
        auth.setup_auth(app, dal)
        assert set(dal.tables) == {"auth_user", "auth_role", "auth_user_roles"}
        assert len(app.config["SECRET_KEY"]) == 64  # token_hex(32) when not supplied
        assert app.config["SECURITY_PASSWORD_HASH"] == "bcrypt"

    def test_config_overrides_are_honoured(self) -> None:
        from flask import Flask

        app = Flask("t")
        auth.setup_auth(app, _FakeSetupDal(), {"SECRET_KEY": "k" * 40, "REGISTERABLE": False})
        assert app.config["SECRET_KEY"] == "k" * 40
        assert app.config["SECURITY_REGISTERABLE"] is False

    def test_only_providers_with_credentials_are_registered(self, caplog: pytest.LogCaptureFixture) -> None:
        from flask import Flask

        app = Flask("t")
        with caplog.at_level(logging.INFO, logger="flask_core.auth"):
            oauth = auth.setup_auth(
                app, _FakeSetupDal(), {"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "csecret"}
            )
        assert oauth.create_client("twitch") is not None
        assert oauth.create_client("discord") is None
        assert auth.OAUTH_PROVIDERS["twitch"].client_id == "cid"
        assert "csecret" not in caplog.text  # credentials are never logged

    def test_half_configured_provider_is_skipped(self) -> None:
        from flask import Flask

        oauth = auth.setup_auth(Flask("t"), _FakeSetupDal(), {"SLACK_CLIENT_ID": "only-an-id"})
        assert oauth.create_client("slack") is None
        assert auth.OAUTH_PROVIDERS["slack"].client_id == ""
