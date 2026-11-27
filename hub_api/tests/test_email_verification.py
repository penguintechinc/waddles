"""Account email verification + the shared SMTP email service, over REAL SMTP.

The units under test (`services/email_service.py`, `services/email_verification.py`, the
`auth_service` / `oauth_service` flows and the `/api/v1/auth` routes) are never mocked. The only
fake is the remote mail server: the in-process `aiosmtpd` sink from `test_dsar_email_sender.py`
(STARTTLS with a throw-away CA, optional AUTH, scripted 4xx/5xx), so certificate verification
stays ON. The kill-switch flag lookup is pinned OFF (= verification enforced) for determinism.
"""

# ruff: noqa: F811 - reused fixtures re-declared as args

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import email_service, email_verification, oauth_service
from services.errors import ApiError
from tests.test_dsar_email_sender import Sink, make_sink, pki  # noqa: F401 - fixtures
from tests.test_v1_auth_blueprint import (  # noqa: F401 - fixtures + helpers reused as-is
    _seed_settings,
    _test_config,
    app,
    client,
)

EHLO = "hub-api.waddles.example"
USER = "new.person@example.com"
PASSWORD = "hunter22-long-enough"
TOKEN_RE = re.compile(r"#token=([A-Za-z0-9_-]+)")


@pytest.fixture(autouse=True)
def kill_switch_off(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """`waddles.disable-email-verification` unseen/OFF -> verification enforced."""
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(email_verification, "feature_enabled", gate)
    return gate


@pytest.fixture(autouse=True)
def no_smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with email unconfigured; `smtp` below opts in."""
    for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "DSAR_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch, pki: dict[str, Path], make_sink: Any) -> Any:
    """Factory: start a sink and point `SMTP_*` at it (env only -- the real config path)."""

    def configure(*, auth: tuple[str, str] | None = None, mode: str = "starttls") -> Sink:
        sink: Sink = make_sink(mode=mode, auth=auth)
        values = {
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(sink.port),
            "SMTP_TLS": "starttls",
            "SMTP_TLS_CA_FILE": str(pki["ca"]),
            "SMTP_FROM": "no-reply@waddles.example",
            "SMTP_FROM_NAME": "Waddles",
            "SMTP_EHLO_NAME": EHLO,
            "SMTP_BACKOFF_SECONDS": "0.01",
            "SMTP_MAX_RETRIES": "1",
            "SMTP_CONNECT_TIMEOUT": "2",
            "SMTP_SEND_TIMEOUT": "5",
        }
        if auth is not None:
            values.update(SMTP_USERNAME=auth[0], SMTP_PASSWORD=auth[1])
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return sink

    return configure


def _token_from(sink: Sink, index: int = -1) -> str:
    body = sink.mail(index).get_body(preferencelist=("plain",))
    assert body is not None
    match = TOKEN_RE.search(body.get_content())
    assert match, "verification link missing from mail"
    return match.group(1)


def _user(auth_db: Any, email: str = USER) -> Any:
    return auth_db.dal(auth_db.dal.hub_users.email == email).select().first()


async def _register(client: Any, email: str = USER) -> Any:
    return await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})


async def _login(client: Any, email: str = USER) -> Any:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})


async def _verify(client: Any, token: str) -> Any:
    return await client.post("/api/v1/auth/verify-email", json={"token": token})


def _open_signup(auth_db: Any, domains: str = "") -> None:
    _seed_settings(
        auth_db, signup_enabled="true", email_configured="true", signup_allowed_domains=domains
    )


class TestLocalSignup:
    async def test_signup_is_blocked_until_the_emailed_token_is_used_once(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 202
        body = await response.get_json()
        assert body["requiresVerification"] is True and "token" not in body

        assert len(sink.handler.messages) == 1
        mail = sink.mail()
        assert mail["To"] == USER and "Verify" in mail["Subject"]
        assert sink.handler.ehlo_names and set(sink.handler.ehlo_names) == {EHLO}
        token = _token_from(sink)
        row = _user(auth_db)
        assert row.email_verified is False
        # hashed at rest: the DB holds the SHA-256 digest, never the raw token
        assert row.email_verification_token == email_verification.hash_token(token)
        assert token not in row.email_verification_token

        login = await _login(client)
        assert login.status_code == 403 and (await login.get_json())["requiresVerification"]

        verified = await _verify(client, token)
        assert verified.status_code == 200
        assert (await verified.get_json())["token"]
        assert _user(auth_db).email_verified is True
        assert (await _login(client)).status_code == 200

        reused = await _verify(client, token)
        assert reused.status_code == 400

    async def test_wrong_password_never_reveals_verification_state(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        smtp()
        _open_signup(auth_db)
        await _register(client)
        response = await client.post(
            "/api/v1/auth/login", json={"email": USER, "password": "wrong-password"}
        )
        assert response.status_code == 401

    async def test_tampered_and_expired_tokens_are_refused(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        token = _token_from(sink)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert (await _verify(client, tampered)).status_code == 400
        assert (await _verify(client, "x" * 300)).status_code == 400
        assert (await _verify(client, "")).status_code == 400

        auth_db.dal(auth_db.dal.hub_users.email == USER).update(
            email_verification_expires=datetime.now(UTC) - timedelta(minutes=1)
        )
        auth_db.dal.commit()
        expired = await _verify(client, token)
        assert expired.status_code == 400
        assert "expired" in (await expired.get_json())["error"]["message"]
        assert _user(auth_db).email_verified is False

    async def test_wrong_domain_is_rejected_without_mail_or_account(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "allowed.example, other.example")
        response = await _register(client, "someone@evil.example")
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == email_verification.ERR_DOMAIN
        assert sink.handler.messages == [] and _user(auth_db, "someone@evil.example") is None

        ok = await _register(client, "someone@allowed.example")
        assert ok.status_code == 202 and len(sink.handler.messages) == 1

    async def test_domain_is_rechecked_at_verification(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        auth_db.dal(auth_db.dal.hub_settings.setting_key == "signup_allowed_domains").update(
            setting_value="allowed.example"
        )
        auth_db.dal.commit()
        response = await _verify(client, _token_from(sink))
        assert response.status_code == 403
        assert _user(auth_db).email_verified is False


class TestResend:
    async def test_resend_is_rate_limited_then_rotates_the_token(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        first = _token_from(sink)

        limited = await client.post("/api/v1/auth/resend-verification", json={"email": USER})
        assert limited.status_code == 429
        err = (await limited.get_json())["error"]
        assert err["code"] == email_verification.ERR_RATE_LIMITED
        assert err["details"]["retryAfterSeconds"] > 0
        assert len(sink.handler.messages) == 1

        # age the token past the cooldown (issued = expires - TTL)
        auth_db.dal(auth_db.dal.hub_users.email == USER).update(
            email_verification_expires=datetime.now(UTC)
            + email_verification.token_ttl()
            - timedelta(minutes=5)
        )
        auth_db.dal.commit()
        again = await client.post("/api/v1/auth/resend-verification", json={"email": USER})
        assert again.status_code == 200 and len(sink.handler.messages) == 2
        second = _token_from(sink)
        assert second != first
        assert (await _verify(client, first)).status_code == 400  # rotated out
        assert (await _verify(client, second)).status_code == 200

    async def test_unknown_address_gets_the_same_answer_and_no_mail(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        response = await client.post(
            "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
        )
        assert response.status_code == 200 and sink.handler.messages == []


class TestSmtpFailuresAreLoud:
    async def test_unconfigured_smtp_refuses_signup_before_creating_the_account(
        self, client: Any, auth_db: Any
    ) -> None:
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "email_not_configured"
        assert _user(auth_db) is None

    async def test_relay_failure_surfaces_retry_error_and_never_auto_verifies(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        sink.handler.script = ["554 5.7.1 rejected"]
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 503
        err = (await response.get_json())["error"]
        assert err["code"] == email_verification.ERR_SEND_FAILED
        assert "try again" in err["message"]
        row = _user(auth_db)
        assert row is not None and row.email_verified is False
        assert (await _login(client)).status_code == 403

    async def test_relay_without_starttls_fails_loud_never_plaintext(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp(mode="plain")
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 503
        assert sink.handler.messages == []


class TestEmailServiceInterface:
    async def test_no_auth_starttls_relay_delivers(self, smtp: Any) -> None:
        """IP-allowlisted relay (e.g. Gmail SMTP relay): STARTTLS, no AUTH, custom EHLO."""
        sink = smtp()
        await email_service.send_email(
            to_email="otp.user@example.com", subject="Code", text_body="123456", kind="test_mail"
        )
        assert len(sink.handler.messages) == 1 and sink.handler.ehlo_names[-1] == EHLO

    async def test_authenticated_relay_delivers_with_secret_credentials(self, smtp: Any) -> None:
        sink = smtp(auth=("relay-user", "relay-pass-9f3a"))
        await email_service.send_email(
            to_email="otp.user@example.com", subject="Code", text_body="123456", kind="test_mail"
        )
        assert len(sink.handler.messages) == 1

    async def test_unconfigured_raises_not_configured(self) -> None:
        with pytest.raises(email_service.EmailNotConfiguredError):
            await email_service.send_email(
                to_email="a@example.com", subject="s", text_body="b", kind="test_mail"
            )

    async def test_kind_label_is_bounded(self) -> None:
        with pytest.raises(ValueError):
            await email_service.send_email(
                to_email="a@example.com", subject="s", text_body="b", kind="Bad Kind!"
            )


class TestEhloValidation:
    @pytest.mark.parametrize(
        ("name", "ok"),
        [
            ("waddles-alpha.penguintech.cloud", True),
            ("mail.example.org", True),
            ("", False),
            ("localhost", False),
            ("localhost.localdomain", False),
            ("127.0.0.1", False),
            ("10.0.0.5", False),
            ("hub-api-7d9f8c", False),  # bare pod hostname
            ("bad_host.example.com", False),
            ("-x.example.com", False),
        ],
    )
    def test_is_fqdn(self, name: str, ok: bool) -> None:
        assert email_service.is_fqdn(name) is ok

    @pytest.mark.parametrize("ehlo", ["", "localhost", "192.168.1.10", "podname"])
    def test_configured_host_with_bad_ehlo_fails_startup(self, ehlo: str) -> None:
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "a@example.com"}
        if ehlo:
            env["SMTP_EHLO_NAME"] = ehlo
        with pytest.raises(email_service.SmtpConfigError, match="SMTP_EHLO_NAME"):
            email_service.validate_startup(env)

    def test_valid_config_and_unset_host_pass_startup(self) -> None:
        email_service.validate_startup({})
        email_service.validate_startup(
            {
                "SMTP_HOST": "smtp-relay.gmail.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "no-reply@penguintech.io",
                "SMTP_EHLO_NAME": "waddles-alpha.penguintech.cloud",
            }
        )

    def test_password_never_appears_in_settings_repr(self) -> None:
        settings = email_service.settings_from_env(
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_FROM": "a@example.com",
                "SMTP_EHLO_NAME": "hub.example.com",
                "SMTP_USERNAME": "u",
                "SMTP_PASSWORD": "pw-must-not-leak",
            }
        )
        assert settings is not None and "pw-must-not-leak" not in repr(settings)


def _discord(email: str | None, ident: str = "1001") -> dict[str, Any]:
    return {"id": ident, "username": f"user{ident}", "email": email, "avatar_url": None}


class TestOAuthSignupPolicy:
    async def _signup(self, auth_db: Any, data: dict[str, Any]) -> Any:
        return await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db, auth_db.dal, platform="discord", user_data=data, cfg=_test_config()
        )

    async def test_unrestricted_platform_needs_no_email(self, auth_db: Any, smtp: Any) -> None:
        sink = smtp()
        _open_signup(auth_db)
        user = await self._signup(auth_db, _discord("gamer@example.com"))
        assert user.id and sink.handler.messages == []

    async def test_restricted_platform_requires_verification_then_allows_login(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        with pytest.raises(ApiError) as err:
            await self._signup(auth_db, _discord("worker@corp.example"))
        assert err.value.code == email_verification.ERR_REQUIRED and err.value.status_code == 403
        assert len(sink.handler.messages) == 1 and _user(auth_db, "worker@corp.example")

        with pytest.raises(ApiError) as again:  # linked but still pending: no session
            await self._signup(auth_db, _discord("worker@corp.example"))
        assert again.value.code == email_verification.ERR_REQUIRED

        assert (await _verify(client, _token_from(sink))).status_code == 200
        user = await self._signup(auth_db, _discord("worker@corp.example"))
        assert user.email == "worker@corp.example"

    @pytest.mark.parametrize("email", ["worker@evil.example", None])
    async def test_restricted_platform_rejects_wrong_or_missing_domain(
        self, auth_db: Any, smtp: Any, email: str | None
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        with pytest.raises(ApiError) as err:
            await self._signup(auth_db, _discord(email))
        assert err.value.code == email_verification.ERR_DOMAIN
        assert sink.handler.messages == []
        assert auth_db.dal(auth_db.dal.hub_users.id > 0).count() == 0

    async def test_restricted_platform_without_smtp_fails_loud(self, auth_db: Any) -> None:
        _open_signup(auth_db, "corp.example")
        with pytest.raises(ApiError) as err:
            await self._signup(auth_db, _discord("worker@corp.example"))
        assert err.value.code == "email_not_configured"
        assert auth_db.dal(auth_db.dal.hub_users.id > 0).count() == 0

    async def test_callback_redirect_carries_the_typed_error(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def pending(*_a: Any, **_k: Any) -> str:
            raise ApiError("check inbox", 403, email_verification.ERR_REQUIRED)

        monkeypatch.setattr(oauth_service, "oauth_callback", pending)
        response = await client.get("/api/v1/auth/oauth/discord/callback?code=c&state=s")
        assert response.status_code in (302, 303)
        assert "error=email_verification_required" in response.headers["Location"]


class TestKillSwitch:
    async def test_kill_switch_on_restores_immediate_signup(
        self, client: Any, auth_db: Any, kill_switch_off: AsyncMock
    ) -> None:
        kill_switch_off.return_value = True  # waddles.disable-email-verification ON
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 200 and (await response.get_json())["token"]
        assert kill_switch_off.await_args.args[0] == "waddles.disable-email-verification"
