"""`services/marketplace_crypto.py` -- AES-256-GCM webhook-secret encryption."""

from __future__ import annotations

from typing import Any

import pytest

from services import marketplace_crypto as crypto


class TestRoundTrip:
    def test_encrypt_then_decrypt_recovers_plaintext(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "a" * 64)
        stored = crypto.encrypt_webhook_secret("my-webhook-secret")
        assert stored != "my-webhook-secret"
        assert crypto.decrypt_webhook_secret(stored) == "my-webhook-secret"

    def test_two_encryptions_of_same_plaintext_differ(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "b" * 64)
        first = crypto.encrypt_webhook_secret("same-secret")
        second = crypto.encrypt_webhook_secret("same-secret")
        assert first != second  # random IV per call


class TestFailsClosed:
    def test_missing_key_raises(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MARKETPLACE_ENCRYPTION_KEY", raising=False)
        with pytest.raises(crypto.MarketplaceEncryptionKeyError):
            crypto.encrypt_webhook_secret("x")

    def test_short_key_raises(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "tooshort")
        with pytest.raises(crypto.MarketplaceEncryptionKeyError):
            crypto.encrypt_webhook_secret("x")


class TestPaymentDetails:
    """Field-level encryption of `vendor_submissions.payment_details` (security-audit MED)."""

    _DETAILS = {"paypal_email": "vendor@example.com", "bank_account": "DE89370400440532013000"}

    def test_round_trip(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "c" * 64)
        env = crypto.encrypt_payment_details(self._DETAILS)
        assert crypto.is_encrypted_payment_details(env)
        assert crypto.decrypt_payment_details(env) == self._DETAILS

    def test_ciphertext_is_not_plaintext(self, monkeypatch: Any) -> None:
        import json

        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "c" * 64)
        blob = json.dumps(crypto.encrypt_payment_details(self._DETAILS))
        for secret in (
            "vendor@example.com",
            "DE89370400440532013000",
            "paypal_email",
            "bank_account",
        ):
            assert secret not in blob

    def test_key_from_env_only_wrong_key_fails(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "c" * 64)
        env = crypto.encrypt_payment_details(self._DETAILS)
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "d" * 64)
        with pytest.raises(Exception):  # noqa: B017  # cryptography InvalidTag
            crypto.decrypt_payment_details(env)

    def test_missing_key_fails_closed(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MARKETPLACE_ENCRYPTION_KEY", raising=False)
        with pytest.raises(crypto.MarketplaceEncryptionKeyError):
            crypto.encrypt_payment_details(self._DETAILS)

    def test_aad_binds_to_column(self, monkeypatch: Any) -> None:
        """A webhook-secret ciphertext (no AAD) must not decrypt as payment_details."""
        monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "c" * 64)
        forged = {"v": 1, "enc": crypto.encrypt_webhook_secret('{"a":1}')}
        with pytest.raises(Exception):  # noqa: B017
            crypto.decrypt_payment_details(forged)

    def test_erased_value_decrypts_to_empty(self) -> None:
        assert crypto.decrypt_payment_details(None) == {}
        assert crypto.decrypt_payment_details({}) == {}

    def test_legacy_plaintext_rejected_not_returned(self) -> None:
        with pytest.raises(ValueError):
            crypto.decrypt_payment_details({"paypal_email": "x@example.com"})
