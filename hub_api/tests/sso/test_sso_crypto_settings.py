"""`services/sso_crypto.py` + `services/sso_settings.py` -- key handling and operator settings."""

from __future__ import annotations

import pytest

from services import sso_crypto
from services.sso_settings import SsoSettings, load_settings
from services.sso_types import SsoConfigError
from tests.sso.conftest import TEST_SSO_KEY


class TestSecretEncryption:
    def test_round_trip(self) -> None:
        token = sso_crypto.encrypt_secret("hunter2-client-secret", aad="conn-1")
        assert token.startswith("v1:")
        assert "hunter2" not in token
        assert sso_crypto.decrypt_secret(token, aad="conn-1") == "hunter2-client-secret"

    def test_nonce_is_random_per_encryption(self) -> None:
        a = sso_crypto.encrypt_secret("same", aad="c")
        b = sso_crypto.encrypt_secret("same", aad="c")
        assert a != b

    def test_aad_binds_ciphertext_to_its_row(self) -> None:
        token = sso_crypto.encrypt_secret("secret", aad="connection-A")
        with pytest.raises(SsoConfigError) as exc:
            sso_crypto.decrypt_secret(token, aad="connection-B")
        assert exc.value.code == "secret_decrypt"

    def test_tampered_ciphertext_is_rejected(self) -> None:
        token = sso_crypto.encrypt_secret("secret", aad="c")
        flipped = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(SsoConfigError):
            sso_crypto.decrypt_secret(flipped, aad="c")

    @pytest.mark.parametrize("bad", ["", "v2:abc", "plain", "v1:!!!notbase64!!!", "v1:QUJD"])
    def test_malformed_tokens_fail_loudly(self, bad: str) -> None:
        with pytest.raises(SsoConfigError) as exc:
            sso_crypto.decrypt_secret(bad, aad="c")
        assert exc.value.code in {"secret_format", "secret_decrypt"}

    def test_wrong_master_key_cannot_decrypt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        token = sso_crypto.encrypt_secret("secret", aad="c")
        monkeypatch.setenv("SSO_ENCRYPTION_KEY", "cd" * 32)
        with pytest.raises(SsoConfigError):
            sso_crypto.decrypt_secret(token, aad="c")


class TestMasterKeyLoading:
    def test_missing_key_is_a_loud_error_not_a_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SSO_ENCRYPTION_KEY")
        with pytest.raises(SsoConfigError) as exc:
            sso_crypto.encrypt_secret("x", aad="c")
        assert exc.value.code == "sso_key_missing"

    @pytest.mark.parametrize("bad", ["zz" * 32, "ab" * 31, "ab" * 33, "not-hex", "AB"])
    def test_malformed_key_is_rejected(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        monkeypatch.setenv("SSO_ENCRYPTION_KEY", bad)
        with pytest.raises(SsoConfigError) as exc:
            sso_crypto.encrypt_secret("x", aad="c")
        assert exc.value.code == "sso_key_malformed"


class TestBinder:
    def test_binder_is_deterministic_per_state_and_verifies(self) -> None:
        value = sso_crypto.binder_value("state-token")
        assert value == sso_crypto.binder_value("state-token")
        assert sso_crypto.verify_binder("state-token", value)

    def test_binder_differs_per_state(self) -> None:
        assert sso_crypto.binder_value("state-1") != sso_crypto.binder_value("state-2")

    def test_other_states_binder_is_rejected(self) -> None:
        assert not sso_crypto.verify_binder("state-1", sso_crypto.binder_value("state-2"))

    @pytest.mark.parametrize("presented", [None, "", "garbage"])
    def test_missing_or_garbage_binder_is_rejected(self, presented: str | None) -> None:
        assert not sso_crypto.verify_binder("state-1", presented)

    def test_binder_is_independent_of_the_encryption_subkey(self) -> None:
        # Domain separation: the binder must not be derivable from encrypt-key material.
        assert TEST_SSO_KEY not in sso_crypto.binder_value("s")


class TestSettings:
    def test_defaults(self) -> None:
        s = load_settings({})
        assert s == SsoSettings()
        assert s.state_ttl_s == 600
        assert s.clock_skew_s == 120
        assert not s.platform_google_configured

    def test_full_environment(self) -> None:
        s = load_settings(
            {
                "SSO_STATE_TTL_SECONDS": "300",
                "SSO_CLOCK_SKEW_SECONDS": "30",
                "SSO_HTTP_TIMEOUT_SECONDS": "5.5",
                "SSO_ALLOWED_PRIVATE_HOSTS": " Keycloak.Corp.Test , adfs.corp.test,",
                "SSO_GOOGLE_CLIENT_ID": "gid",
                "SSO_GOOGLE_CLIENT_SECRET": "gsecret",
            }
        )
        assert s.state_ttl_s == 300
        assert s.clock_skew_s == 30
        assert s.http_timeout_s == 5.5
        assert s.allowed_private_hosts == frozenset({"keycloak.corp.test", "adfs.corp.test"})
        assert s.platform_google_configured
        assert "gsecret" not in repr(s)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("SSO_STATE_TTL_SECONDS", "abc"),
            ("SSO_STATE_TTL_SECONDS", "5"),
            ("SSO_STATE_TTL_SECONDS", "999999"),
            ("SSO_CLOCK_SKEW_SECONDS", "-1"),
            ("SSO_CLOCK_SKEW_SECONDS", "9999"),
            ("SSO_HTTP_TIMEOUT_SECONDS", "nope"),
            ("SSO_HTTP_TIMEOUT_SECONDS", "0"),
        ],
    )
    def test_malformed_values_fail_loudly_instead_of_defaulting(
        self, name: str, value: str
    ) -> None:
        with pytest.raises(SsoConfigError) as exc:
            load_settings({name: value})
        assert exc.value.code == "bad_setting"

    def test_blank_values_use_defaults(self) -> None:
        assert load_settings({"SSO_STATE_TTL_SECONDS": "  "}).state_ttl_s == 600

    def test_reads_process_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSO_CLOCK_SKEW_SECONDS", "7")
        assert load_settings().clock_skew_s == 7
