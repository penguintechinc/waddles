"""`config.py::HubAPIConfig.from_env()` -- keystore DSN fail-closed behavior.

Security-review HIGH finding (PR #442): `KEYSTORE_DATABASE_URL` must
never fall back to the main `DATABASE_URL` -- doing so would defeat the
entire separate-key-store design (tenant-envelope-encryption-design.md
Sec4: a compromised `hub_api` application-data credential must not also
unlock the key store). This file locks in the fix: `from_env()` reads
`KEYSTORE_DATABASE_URL` with an empty-string default, never `database_url`.
"""

from __future__ import annotations

import pytest

from config import HubAPIConfig


@pytest.fixture(autouse=True)
def _clean_keystore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("WADDLES_TENANT_ENVELOPE_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_keystore_database_url_defaults_empty_when_unset() -> None:
    cfg = HubAPIConfig.from_env()
    assert cfg.keystore_database_url == ""


def test_keystore_database_url_never_falls_back_to_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core regression this test guards.

    DATABASE_URL being set must NOT leak into keystore_database_url just
    because KEYSTORE_DATABASE_URL is unset -- that was the exact bug
    (`os.getenv("KEYSTORE_DATABASE_URL", database_url)`) flagged by the
    security review.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://hub-api-rw:secret@main-db/waddlebot")
    cfg = HubAPIConfig.from_env()
    assert cfg.keystore_database_url == ""
    assert cfg.keystore_database_url != cfg.database_url


def test_keystore_database_url_explicit_value_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://hub-api-rw:secret@main-db/waddlebot")
    monkeypatch.setenv(
        "KEYSTORE_DATABASE_URL", "postgres://hub_api_keystore:secret@keystore-db/keystore"
    )
    cfg = HubAPIConfig.from_env()
    assert cfg.keystore_database_url == "postgres://hub_api_keystore:secret@keystore-db/keystore"
    assert cfg.keystore_database_url != cfg.database_url


def test_tenant_envelope_encryption_defaults_off() -> None:
    cfg = HubAPIConfig.from_env()
    assert cfg.tenant_envelope_encryption_enabled is False


def test_tenant_envelope_encryption_enabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLES_TENANT_ENVELOPE_ENCRYPTION_ENABLED", "true")
    cfg = HubAPIConfig.from_env()
    assert cfg.tenant_envelope_encryption_enabled is True
