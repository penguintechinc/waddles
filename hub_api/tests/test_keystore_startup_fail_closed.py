"""`app.py::startup()` -- keystore fail-closed behavior (security review HIGH, PR #442).

`WADDLES_TENANT_ENVELOPE_ENCRYPTION_ENABLED=true` with no
`KEYSTORE_DATABASE_URL` must abort startup outright, never silently
degrade (the pre-fix behavior for every other keystore-connect failure).
Uses the same `async with app.test_app():` pattern as
`test_app_factory.py` so the real `before_serving` hook runs.
"""

from __future__ import annotations

import pytest
from quart import Quart
from quart.testing.app import LifespanError

from app import create_app
from config import HubAPIConfig


def _base_config(**overrides: object) -> HubAPIConfig:
    defaults: dict[str, object] = dict(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )
    defaults.update(overrides)
    return HubAPIConfig(**defaults)  # type: ignore[arg-type]


class TestKeystoreFailClosed:
    async def test_enabled_without_keystore_url_aborts_startup(self) -> None:
        app: Quart = create_app(
            _base_config(
                tenant_envelope_encryption_enabled=True,
                keystore_database_url="",
            )
        )
        with pytest.raises(LifespanError, match="KEYSTORE_DATABASE_URL"):
            async with app.test_app():
                pass

    async def test_disabled_without_keystore_url_boots_cleanly(self) -> None:
        """Unchanged pre-existing behavior: feature OFF, no DSN -- degrade, don't crash."""
        app: Quart = create_app(
            _base_config(
                tenant_envelope_encryption_enabled=False,
                keystore_database_url="",
            )
        )
        async with app.test_app():
            assert app.config.get("tenant_keystore") is None

    async def test_enabled_with_unreachable_keystore_url_aborts_startup(self) -> None:
        """Enabled + DSN set but unreachable must also fail closed, not degrade."""
        app: Quart = create_app(
            _base_config(
                tenant_envelope_encryption_enabled=True,
                keystore_database_url="postgres://nobody:nothing@127.0.0.1:1/doesnotexist",
            )
        )
        with pytest.raises(LifespanError):
            async with app.test_app():
                pass
