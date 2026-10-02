"""Tests for `flask_core.valkey_tls.build_tls_kwargs()`.

regression: rate limiter ignored Valkey CA, silent in-memory fallback (alpha
2026-10-02) -- `RateLimiter.connect()` (and the sibling `cache.py`/
`message_queue.py`/`stream_pipeline.py` clients) called `redis.from_url()`
with no `ssl_ca_certs`, so it verified the chart's self-signed Valkey CA
against the system trust store and failed with `CERTIFICATE_VERIFY_FAILED`;
the non-fatal fallback masked the real cause in alpha logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import flask_core.valkey_tls as valkey_tls
from flask_core.valkey_tls import DEFAULT_CA_FILE, build_tls_kwargs


@pytest.fixture(autouse=True)
def _reset_warned_flag():
    """Each test gets a clean module-level warn-once flag."""
    valkey_tls._warned_missing_ca = False
    yield
    valkey_tls._warned_missing_ca = False


def test_plain_redis_url_gets_no_tls_kwargs():
    assert build_tls_kwargs("redis://valkey:6379/0") == {}


def test_falsy_url_gets_no_tls_kwargs():
    assert build_tls_kwargs(None) == {}
    assert build_tls_kwargs("") == {}


def test_rediss_url_with_existing_ca_file_passes_ssl_ca_certs(tmp_path: Path):
    ca_file = tmp_path / "valkey-ca.crt"
    ca_file.write_text("fake-ca")

    kwargs = build_tls_kwargs("rediss://valkey:6380/0", ca_file=str(ca_file))

    assert kwargs == {"ssl_cert_reqs": "required", "ssl_ca_certs": str(ca_file)}


def test_rediss_url_with_missing_ca_file_omits_ssl_ca_certs_but_still_requires_verification(
    tmp_path: Path,
):
    missing = tmp_path / "does-not-exist.crt"

    kwargs = build_tls_kwargs("rediss://valkey:6380/0", ca_file=str(missing))

    # Never disables verification, even with no CA file on disk.
    assert kwargs == {"ssl_cert_reqs": "required"}
    assert "ssl_ca_certs" not in kwargs


def test_rediss_url_defaults_ca_file_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    ca_file = tmp_path / "valkey-ca.crt"
    ca_file.write_text("fake-ca")
    monkeypatch.setenv("VALKEY_CA_FILE", str(ca_file))

    kwargs = build_tls_kwargs("rediss://valkey:6380/0")

    assert kwargs["ssl_ca_certs"] == str(ca_file)


def test_rediss_url_falls_back_to_default_ca_path_constant(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VALKEY_CA_FILE", raising=False)

    # No file at DEFAULT_CA_FILE in a test sandbox -- verification stays on,
    # CA pinning is just absent (system trust store fallback).
    kwargs = build_tls_kwargs("rediss://valkey:6380/0")

    assert kwargs == {"ssl_cert_reqs": "required"}
    assert DEFAULT_CA_FILE == "/etc/waddles/ca/valkey-ca.crt"


def test_missing_ca_file_logs_one_warning_with_no_credentials_or_url_content(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
):
    missing = tmp_path / "does-not-exist.crt"
    url = "rediss://:s3cret@valkey:6380/0"

    with caplog.at_level("WARNING", logger="flask_core.valkey_tls"):
        build_tls_kwargs(url, ca_file=str(missing))
        build_tls_kwargs(url, ca_file=str(missing))  # second call: no duplicate WARN

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    rendered = warnings[0].getMessage()
    # No part of the URL (host, credentials, or otherwise) is ever
    # interpolated into the log line -- CodeQL's clear-text-logging query
    # treats any derivative of a credential-bearing URL as tainted
    # regardless of redaction logic, so the only safe fix is zero
    # interpolation of URL-derived content.
    assert "s3cret" not in rendered
    assert "valkey:6380" not in rendered
