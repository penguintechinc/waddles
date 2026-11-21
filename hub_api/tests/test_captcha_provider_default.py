"""Captcha provider resolution: default altcha, third parties opt-in, misconfiguration fails LOUD.

The public DSAR form is a statutory right, so an unset `DSAR_CAPTCHA_PROVIDER` must resolve to the
self-hosted altcha provider (its HMAC key is a chart-generated internal secret). A provider that
IS configured but lacks its key/secret (or an unknown name) never silently bypasses the captcha: it
logs an ERROR naming the missing setting (never a value) and the caller gets `CaptchaUnavailable`.
"""

from __future__ import annotations

import logging

import pytest

from services import captcha_service as captcha

KEY = "k" * 48


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DSAR_CAPTCHA_PROVIDER",
        "DSAR_ALTCHA_HMAC_KEY",
        "DSAR_CAPTCHA_SECRET",
        "DSAR_CAPTCHA_SITE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_unset_provider_defaults_to_altcha_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", KEY)
    assert captcha.provider() == captcha.PROVIDER_ALTCHA
    assert captcha.public_config()["provider"] == "altcha"
    challenge = captcha.create_altcha_challenge()
    assert challenge["algorithm"] == "SHA-256" and challenge["signature"]


def test_blank_provider_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "  ")
    monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", KEY)
    assert captcha.provider() == captcha.PROVIDER_ALTCHA


@pytest.mark.parametrize("key", [None, "short"])
def test_altcha_without_a_usable_key_fails_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, key: str | None
) -> None:
    if key is not None:
        monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", key)
    caplog.set_level(logging.ERROR, logger="services.captcha_service")
    assert captcha.provider() is None
    errors = [r for r in caplog.records if r.getMessage() == "dsar.captcha_misconfigured"]
    assert len(errors) == 1
    assert "DSAR_ALTCHA_HMAC_KEY" in errors[0].missing  # type: ignore[attr-defined]
    assert "short" not in caplog.text  # the value is never logged
    with pytest.raises(captcha.CaptchaUnavailable, match="missing or invalid"):
        captcha.public_config()
    with pytest.raises(captcha.CaptchaUnavailable):
        captcha.create_altcha_challenge()


@pytest.mark.parametrize("name", ["turnstile", "recaptcha"])
def test_third_party_without_operator_keys_fails_loud_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", name)
    monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", KEY)  # present, but must NOT be used as a fallback
    caplog.set_level(logging.ERROR, logger="services.captcha_service")
    assert captcha.provider() is None
    errors = [r for r in caplog.records if r.getMessage() == "dsar.captcha_misconfigured"]
    assert len(errors) == 1
    assert errors[0].missing == "DSAR_CAPTCHA_SECRET,DSAR_CAPTCHA_SITE_KEY"  # type: ignore[attr-defined]


def test_third_party_with_operator_keys_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "turnstile")
    monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s")
    monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "site")
    assert captcha.provider() == "turnstile"


def test_unknown_provider_fails_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "hcaptcha")
    caplog.set_level(logging.ERROR, logger="services.captcha_service")
    assert captcha.provider() is None
    assert any(r.getMessage() == "dsar.captcha_unknown_provider" for r in caplog.records)


async def test_verify_with_missing_key_raises_unavailable_not_a_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(_challenge: str, _ttl: int) -> bool:  # pragma: no cover - must not run
        raise AssertionError("replay guard reached without a configured provider")

    with pytest.raises(captcha.CaptchaUnavailable):
        await captcha.verify("anything", remote_ip=None, replay_guard=never)
