"""AES-256-GCM round-trip + key-validation tests for webhook-secret at-rest encryption."""

from __future__ import annotations

import os

import pytest

from services.bundle_secret_crypto import EncryptionKeyError, decrypt, encrypt

_TEST_KEY = "a" * 64  # 32 bytes hex


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", _TEST_KEY)


def test_round_trip() -> None:
    ciphertext, iv = encrypt("my-webhook-secret")
    assert decrypt(ciphertext, iv) == "my-webhook-secret"


def test_ciphertext_differs_from_plaintext() -> None:
    ciphertext, _ = encrypt("my-webhook-secret")
    assert b"my-webhook-secret" not in ciphertext


def test_two_encryptions_use_different_ivs() -> None:
    _, iv1 = encrypt("same-value")
    _, iv2 = encrypt("same-value")
    assert iv1 != iv2


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_SECRET_ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionKeyError):
        encrypt("x")


def test_short_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "tooshort")
    with pytest.raises(EncryptionKeyError):
        encrypt("x")


def test_decrypt_with_wrong_iv_fails() -> None:
    ciphertext, iv = encrypt("my-webhook-secret")
    wrong_iv = os.urandom(12)
    with pytest.raises(Exception):  # noqa: PT011, B017 -- cryptography raises InvalidTag
        decrypt(ciphertext, wrong_iv)
