"""Tests for ensure_group()/destroy_group(), redis.asyncio client mocked."""

from __future__ import annotations

from pathlib import Path
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


# regression: seeder VALKEY_URL REPLACE_ME / missing TLS wiring -- the chart's
# VALKEY_URL_TLS secret key is credential-free (password comes from a separate
# VALKEY_PASSWORD env var, same split the Rust data-plane pods use), and the CA for
# server-cert verification is mounted as a file, not an env var -- both must reach
# redis.asyncio.from_url() for a real rediss:// connection to work.


def test_build_client_injects_password_into_url_userinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALKEY_URL", "rediss://valkey:6380/0")
    monkeypatch.setenv("VALKEY_PASSWORD", "s3cret")
    monkeypatch.setenv("VALKEY_CA_FILE", "/nonexistent/ca.crt")
    captured: dict[str, object] = {}

    def _fake_from_url(url: str, **kwargs: object) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "client"

    monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)
    assert build_client() == "client"
    assert captured["url"] == "rediss://:s3cret@valkey:6380/0"
    assert "ssl_ca_certs" not in captured["kwargs"]  # CA file doesn't exist on disk


def test_build_client_passes_ssl_ca_certs_when_ca_file_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ca_file = tmp_path / "valkey-ca.crt"
    ca_file.write_text("fake-ca")
    monkeypatch.setenv("VALKEY_URL", "rediss://valkey:6380/0")
    monkeypatch.setenv("VALKEY_CA_FILE", str(ca_file))
    captured: dict[str, object] = {}

    def _fake_from_url(url: str, **kwargs: object) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "client"

    monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)
    assert build_client() == "client"
    assert captured["kwargs"] == {"ssl_ca_certs": str(ca_file)}


def test_build_client_never_overrides_url_with_embedded_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALKEY_URL", "rediss://:already-set@valkey:6380/0")
    monkeypatch.setenv("VALKEY_PASSWORD", "ignored")
    captured: dict[str, object] = {}

    def _fake_from_url(url: str, **kwargs: object) -> str:
        captured["url"] = url
        return "client"

    monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)
    build_client()
    assert captured["url"] == "rediss://:already-set@valkey:6380/0"
