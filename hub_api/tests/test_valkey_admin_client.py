"""Tests for ensure_group()/destroy_group(), redis.asyncio client mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import redis.exceptions

from services.valkey_admin_client import build_client, destroy_group, ensure_group


async def test_ensure_group_calls_xgroup_create_with_mkstream() -> None:
    mock_client = AsyncMock()
    await ensure_group(
        mock_client, stream="waddles:t:acme:c:main:src:twitch:tw-a:events", group="app1"
    )
    mock_client.xgroup_create.assert_called_once_with(
        "waddles:t:acme:c:main:src:twitch:tw-a:events", "app1", id="$", mkstream=True
    )


async def test_ensure_group_is_busygroup_tolerant() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_create.side_effect = redis.exceptions.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )
    await ensure_group(mock_client, stream="s", group="g")  # must not raise


async def test_ensure_group_reraises_other_response_errors() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_create.side_effect = redis.exceptions.ResponseError(
        "WRONGTYPE Operation against a key"
    )
    with pytest.raises(redis.exceptions.ResponseError):
        await ensure_group(mock_client, stream="s", group="g")


async def test_destroy_group_calls_xgroup_destroy() -> None:
    mock_client = AsyncMock()
    await destroy_group(mock_client, stream="s", group="g")
    mock_client.xgroup_destroy.assert_called_once_with("s", "g")


async def test_destroy_group_tolerates_missing_stream() -> None:
    mock_client = AsyncMock()
    mock_client.xgroup_destroy.side_effect = redis.exceptions.ResponseError("no such key")
    await destroy_group(mock_client, stream="s", group="g")  # must not raise


def test_build_client_refuses_plaintext_url_when_tls_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
    monkeypatch.setenv("SECURITY_TRANSPORT_TLS", "true")
    with pytest.raises(ValueError, match="rediss://"):
        build_client()


def test_build_client_allows_plaintext_when_tls_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALKEY_URL", "redis://valkey:6379/0")
    monkeypatch.setenv("SECURITY_TRANSPORT_TLS", "false")
    client = build_client()
    assert client is not None
