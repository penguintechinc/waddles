"""`config.py::HubAPIConfig.from_env()` -- `WADDLES_AI_ENABLED` deploy-time kill-switch parsing.

Only the AI kill-switch is covered here (the rest of `from_env()`'s ~25
other env vars have no dedicated test file today -- out of scope for this
PR, which touches only `ai_enabled`). Every test explicitly `delenv`s
`WADDLES_AI_ENABLED` first so ambient shell/CI environment state never
leaks between parametrize cases.
"""

from __future__ import annotations

import pytest

from config import HubAPIConfig


@pytest.fixture(autouse=True)
def _clean_ai_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WADDLES_AI_ENABLED", raising=False)


def test_unset_defaults_to_enabled() -> None:
    """DESIGN: `DEFAULT = TRUE (enabled) when unset` -- current full-feature behavior, unchanged."""
    cfg = HubAPIConfig.from_env()
    assert cfg.ai_enabled is True


def test_constructed_directly_also_defaults_to_enabled() -> None:
    """The dataclass field default itself (not just `from_env()`'s explicit pass-through)."""
    cfg = HubAPIConfig(
        module_name="x",
        module_version="0.0.0",
        module_port=1,
        grpc_port=1,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="s",
        jwt_algorithm="HS256",
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:1",
        frontend_origin="http://localhost:1",
        log_level="INFO",
    )
    assert cfg.ai_enabled is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "NO"])
def test_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WADDLES_AI_ENABLED", value)
    cfg = HubAPIConfig.from_env()
    assert cfg.ai_enabled is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
def test_truthy_values_stay_enabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WADDLES_AI_ENABLED", value)
    cfg = HubAPIConfig.from_env()
    assert cfg.ai_enabled is True
