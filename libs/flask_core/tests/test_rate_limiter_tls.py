"""Tests for `RateLimiter.connect()`'s TLS wiring and fallback logging.

regression: rate limiter ignored Valkey CA, silent in-memory fallback (alpha
2026-10-02) -- `connect()` called `redis.from_url()` with no `ssl_ca_certs`,
so alpha hub-api failed with `CERTIFICATE_VERIFY_FAILED` against the chart's
self-signed Valkey CA and silently degraded to a per-replica in-memory
limiter (`enable_fallback=True`) instead of surfacing the real cause loudly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flask_core.rate_limiter import RateLimiter


async def test_connect_passes_ssl_ca_certs_for_rediss_url(tmp_path: Path, monkeypatch):
    ca_file = tmp_path / "valkey-ca.crt"
    ca_file.write_text("fake-ca")
    monkeypatch.setenv("VALKEY_CA_FILE", str(ca_file))

    mock_client = MagicMock()
    mock_client.ping = AsyncMock(return_value=True)

    with patch("flask_core.rate_limiter.redis.from_url", return_value=mock_client) as mock_from_url:
        limiter = RateLimiter(redis_url="rediss://valkey:6380/0")
        await limiter.connect()

    assert limiter._connected is True
    _, kwargs = mock_from_url.call_args
    assert kwargs["ssl_ca_certs"] == str(ca_file)
    assert kwargs["ssl_cert_reqs"] == "required"


async def test_connect_plain_redis_url_gets_no_tls_kwargs():
    mock_client = MagicMock()
    mock_client.ping = AsyncMock(return_value=True)

    with patch("flask_core.rate_limiter.redis.from_url", return_value=mock_client) as mock_from_url:
        limiter = RateLimiter(redis_url="redis://valkey:6379/0")
        await limiter.connect()

    _, kwargs = mock_from_url.call_args
    assert "ssl_ca_certs" not in kwargs
    assert "ssl_cert_reqs" not in kwargs


async def test_connect_failure_falls_back_and_logs_error(caplog: pytest.LogCaptureFixture):
    with patch(
        "flask_core.rate_limiter.redis.from_url",
        side_effect=ConnectionError("CERTIFICATE_VERIFY_FAILED: self-signed certificate"),
    ):
        limiter = RateLimiter(redis_url="rediss://valkey:6380/0", enable_fallback=True)
        with caplog.at_level("ERROR", logger="flask_core.rate_limiter"):
            await limiter.connect()

    assert limiter._fallback_enabled is True
    error_messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    # Both the connect failure AND the fallback engagement must be ERROR, not
    # a swallowed/low-level message -- degradations must be loud.
    assert any("Failed to connect to Redis" in m for m in error_messages)
    assert any("Falling back to in-memory rate limiter" in m for m in error_messages)


async def test_connect_failure_without_fallback_raises():
    with patch(
        "flask_core.rate_limiter.redis.from_url",
        side_effect=ConnectionError("boom"),
    ):
        limiter = RateLimiter(redis_url="rediss://valkey:6380/0", enable_fallback=False)
        with pytest.raises(ConnectionError):
            await limiter.connect()
