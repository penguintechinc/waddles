"""Account email verification + the shared SMTP email service, over REAL SMTP.

The units under test (`services/email_service.py`, `services/email_verification.py`, the
`auth_service` / `oauth_service` flows and the `/api/v1/auth` routes) are never mocked. The only
fake is the remote mail server: the in-process `aiosmtpd` sink from `test_dsar_email_sender.py`
(STARTTLS with a throw-away CA, optional AUTH, scripted 4xx/5xx), so certificate verification
stays ON. The kill-switch flag lookup is pinned OFF (= verification enforced) for determinism.
"""

# ruff: noqa: F811 - reused fixtures re-declared as args

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import (
    email_service,
    email_verification,
    oauth_service,
    rate_limiting,
    session_service,
    user_management_service,
)
from services.errors import ApiError
from tests.conftest import seed_super_admin
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


async def _settle() -> int:
    """Wait for the off-request-path account/mail jobs the last request scheduled."""
    return await email_verification.drain_pending_sends(grace_seconds=15)


def _session_cookies(response: Any) -> list[str]:
    return [c for c in response.headers.getlist("Set-Cookie") if c.strip()]


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
        assert await _settle() == 1

        assert len(sink.handler.messages) == 1
        mail = sink.mail()
        assert mail["To"] == USER and "account request" in mail["Subject"]
        text = mail.get_body(preferencelist=("plain",)).get_content()
        assert "Someone requested an account with this address" in text
        assert "&action=cancel" in text
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
        verified_body = await verified.get_json()
        assert "token" not in verified_body and "user" not in verified_body
        assert _session_cookies(verified) == []
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
        await _settle()
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
        await _settle()
        assert ok.status_code == 202 and len(sink.handler.messages) == 1

    async def test_domain_is_rechecked_at_verification(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        auth_db.dal(auth_db.dal.hub_settings.setting_key == "signup_allowed_domains").update(
            setting_value="allowed.example"
        )
        auth_db.dal.commit()
        response = await _verify(client, _token_from(sink))
        assert response.status_code == 403
        assert _user(auth_db).email_verified is False


class TestResend:
    async def test_resend_cooldown_is_silent_then_rotates_the_token(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        first = _token_from(sink)
        unknown = await client.post(
            "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
        )

        limited = await client.post("/api/v1/auth/resend-verification", json={"email": USER})
        assert limited.status_code == unknown.status_code == 200
        assert await limited.get_json() == await unknown.get_json()  # cooldown is invisible
        await _settle()
        assert len(sink.handler.messages) == 1  # ...but still enforced

        # age the token past the cooldown (issued = expires - TTL)
        auth_db.dal(auth_db.dal.hub_users.email == USER).update(
            email_verification_expires=datetime.now(UTC)
            + email_verification.token_ttl()
            - timedelta(minutes=5)
        )
        auth_db.dal.commit()
        again = await client.post("/api/v1/auth/resend-verification", json={"email": USER})
        await _settle()
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
        await _settle()
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

    async def test_relay_failure_is_logged_loudly_and_never_auto_verifies(
        self, client: Any, auth_db: Any, smtp: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = smtp()
        sink.handler.script = ["554 5.7.1 rejected"]
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 202  # uniform answer: delivery is off the request path
        with caplog.at_level("ERROR", logger="services.email_verification"):
            await _settle()
        failures = [r for r in caplog.records if "background_failed" in r.getMessage()]
        assert len(failures) == 1
        assert email_verification.ERR_SEND_FAILED in failures[0].getMessage()
        assert USER not in caplog.text  # no address in logs
        row = _user(auth_db)
        assert row is not None and row.email_verified is False
        assert (await _login(client)).status_code == 403

    async def test_relay_without_starttls_fails_loud_never_plaintext(
        self, client: Any, auth_db: Any, smtp: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = smtp(mode="plain")
        _open_signup(auth_db)
        response = await _register(client)
        assert response.status_code == 202
        with caplog.at_level("ERROR", logger="services.email_verification"):
            await _settle()
        assert sink.handler.messages == []
        assert any("background_failed" in r.getMessage() for r in caplog.records)


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


def _discord(email: str | None, ident: str = "1001", *, verified: bool = False) -> dict[str, Any]:
    return {
        "id": ident,
        "username": f"user{ident}",
        "email": email,
        "email_verified": verified,
        "avatar_url": None,
    }


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
        assert kill_switch_off.await_args.kwargs == {"tenant": email_verification.FLAG_SCOPE_TENANT}


# ---------------------------------------------------------------------------------------------
# Security-review fixes (PR #1541). Each test fails without its fix.
# ---------------------------------------------------------------------------------------------


def _mail_text(sink: Sink, index: int = -1) -> str:
    body = sink.mail(index).get_body(preferencelist=("plain",))
    assert body is not None
    return str(body.get_content())


async def _oauth(auth_db: Any, data: dict[str, Any]) -> Any:
    return await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
        auth_db, auth_db.dal, platform="discord", user_data=data, cfg=_test_config()
    )


class TestProviderEmailVerification:
    """HIGH: the provider's `email_verified` assertion is honoured; verify mints no session."""

    async def test_unverified_provider_email_under_restricted_tenant_is_held(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        with pytest.raises(ApiError) as err:
            await _oauth(auth_db, _discord("worker@corp.example", verified=False))
        assert err.value.code == email_verification.ERR_REQUIRED
        row = _user(auth_db, "worker@corp.example")
        assert row is not None and row.email_verified is False
        text = _mail_text(sink)
        assert "Someone requested an account with this address" in text
        assert "this wasn't you" in text and "&action=cancel" in text

        verified = await _verify(client, _token_from(sink))
        assert verified.status_code == 200
        assert _session_cookies(verified) == []
        assert "token" not in await verified.get_json()
        # ...the identity holder then logs in normally (a fresh provider login).
        assert (await _oauth(auth_db, _discord("worker@corp.example"))).id == row.id

    async def test_unverified_provider_email_is_never_attached_in_an_open_tenant(
        self, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        user = await _oauth(auth_db, _discord("victim@example.com", "2002", verified=False))
        assert user.email == "user2002@discord.local"
        assert _user(auth_db, "victim@example.com") is None  # no squatting on the address
        trusted = await _oauth(auth_db, _discord("gamer@example.com", "2003", verified=True))
        assert trusted.email == "gamer@example.com"
        assert sink.handler.messages == []


class TestVerifyEmailMintsNoSession:
    async def test_verify_email_returns_no_session_cookie_or_token(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        response = await _verify(client, _token_from(sink))
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body) == {"success", "message"}
        assert _session_cookies(response) == []


class TestNotMeCancel:
    async def _cancel(self, client: Any, token: str) -> Any:
        return await client.post("/api/v1/auth/cancel-signup", json={"token": token})

    async def test_not_me_link_cancels_and_deletes_the_pending_account(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        token = _token_from(sink)
        assert f"#token={token}&action=cancel" in _mail_text(sink)

        response = await self._cancel(client, token)
        assert response.status_code == 200
        assert _user(auth_db) is None
        assert (await _verify(client, token)).status_code == 400
        assert (await self._cancel(client, token)).status_code == 400

    async def test_not_me_link_removes_a_held_oauth_identity(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        with pytest.raises(ApiError):
            await _oauth(auth_db, _discord("worker@corp.example"))
        user_id = _user(auth_db, "worker@corp.example").id
        assert (await self._cancel(client, _token_from(sink))).status_code == 200
        identities = auth_db.dal(auth_db.dal.hub_user_identities.hub_user_id == user_id).count()
        assert identities == 0 and _user(auth_db, "worker@corp.example") is None

    async def test_a_verified_account_is_never_deleted(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        token = _token_from(sink)
        assert (await _verify(client, token)).status_code == 200
        assert (await self._cancel(client, token)).status_code == 400
        assert _user(auth_db) is not None


class TestKillSwitchIsPlatformScoped:
    async def test_a_per_tenant_flag_override_has_no_effect(
        self, auth_db: Any, smtp: Any, kill_switch_off: AsyncMock
    ) -> None:
        # The kill switch is ON for every real tenant, OFF only at the platform scope.
        kill_switch_off.side_effect = lambda _flag, *, tenant, **_k: (
            tenant != email_verification.FLAG_SCOPE_TENANT
        )
        smtp()
        _open_signup(auth_db, "corp.example")
        assert await email_verification.enforced() is True
        with pytest.raises(ApiError) as err:
            await _oauth(auth_db, _discord("worker@corp.example"))
        assert err.value.code == email_verification.ERR_REQUIRED
        assert {c.kwargs["tenant"] for c in kill_switch_off.await_args_list} == {
            email_verification.FLAG_SCOPE_TENANT
        }


class TestInactiveAccounts:
    async def test_an_inactive_user_cannot_verify(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        token = _token_from(sink)
        auth_db.dal(auth_db.dal.hub_users.email == USER).update(is_active=False)
        auth_db.dal.commit()
        assert (await _verify(client, token)).status_code == 400
        assert _user(auth_db).email_verified is False

    async def test_deactivation_clears_verification_tokens(
        self, client: Any, auth_db: Any, smtp: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(session_service, "revoke_user_sessions", AsyncMock(return_value=None))
        sink = smtp()
        seed_super_admin(auth_db, user_id=1)
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        token = _token_from(sink)
        target = _user(auth_db)
        await user_management_service.update_user(
            auth_db, auth_db.dal, user_id=int(target.id), email=None, is_active=False, caller_id=1
        )
        row = _user(auth_db)
        assert row.email_verification_token is None and row.email_verification_expires is None
        # Reactivated later: the stale link still never verifies.
        await user_management_service.update_user(
            auth_db, auth_db.dal, user_id=int(target.id), email=None, is_active=True, caller_id=1
        )
        assert (await _verify(client, token)).status_code == 400


class TestEmailChange:
    async def test_email_change_resets_verification_and_rechecks_the_domain(
        self, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        seed_super_admin(auth_db, user_id=1)
        _open_signup(auth_db, "corp.example")
        user_id = auth_db.dal.hub_users.insert(
            email="old@corp.example",
            username="old@corp.example",
            is_active=True,
            email_verified=True,
            email_verification_token=email_verification.hash_token("stale"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        await user_management_service.update_user(
            auth_db,
            auth_db.dal,
            user_id=int(user_id),
            email="  New@CORP.example. ",
            is_active=None,
            caller_id=1,
            cfg=_test_config(),
        )
        await _settle()
        row = auth_db.dal(auth_db.dal.hub_users.id == user_id).select().first()
        assert row.email == "new@corp.example" and row.email_verified is False
        assert len(sink.handler.messages) == 1 and sink.mail()["To"] == "new@corp.example"
        assert row.email_verification_token == email_verification.hash_token(_token_from(sink))

        with pytest.raises(ApiError) as err:
            await user_management_service.update_user(
                auth_db,
                auth_db.dal,
                user_id=int(user_id),
                email="x@evil.example",
                is_active=None,
                caller_id=1,
                cfg=_test_config(),
            )
        assert err.value.code == email_verification.ERR_DOMAIN
        again = auth_db.dal(auth_db.dal.hub_users.id == user_id).select().first()
        assert again.email == "new@corp.example"


class _SlowMail:
    """Holds every send until released, to prove delivery happens off the request path."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.kinds: list[str] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.kinds.append(str(kwargs["kind"]))
        await self.release.wait()


class TestConstantShapeAndAsyncSend:
    async def test_register_and_resend_answer_identically_and_send_off_path(
        self, client: Any, auth_db: Any, smtp: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        smtp()
        _open_signup(auth_db)
        slow = _SlowMail()
        monkeypatch.setattr(email_service, "send_email", slow)

        new = await asyncio.wait_for(_register(client), timeout=5)  # returns before the send
        assert email_verification._pending  # noqa: SLF001 - the mail is still in flight
        known = await asyncio.wait_for(_register(client), timeout=5)
        assert new.status_code == known.status_code == 202
        assert await new.get_json() == await known.get_json()

        resend_known = await asyncio.wait_for(
            client.post("/api/v1/auth/resend-verification", json={"email": USER}), timeout=5
        )
        resend_unknown = await asyncio.wait_for(
            client.post("/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}),
            timeout=5,
        )
        assert resend_known.status_code == resend_unknown.status_code == 200
        assert await resend_known.get_json() == await resend_unknown.get_json()

        slow.release.set()
        await _settle()
        assert sorted(slow.kinds) == sorted(
            [email_verification.EMAIL_KIND, email_verification.NOTICE_KIND]
        )

    async def test_existing_owner_is_told_someone_tried_to_register(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db)
        await _register(client)
        await _settle()
        response = await _register(client)
        await _settle()
        assert response.status_code == 202
        assert len(sink.handler.messages) == 2
        assert sink.mail()["Subject"] == email_verification.NOTICE_SUBJECT
        assert "Someone tried to register" in _mail_text(sink)
        assert auth_db.dal(auth_db.dal.hub_users.email == USER).count() == 1


class _RecordingLimiter:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.keys: list[str] = []

    async def check_rate_limit(self, key: str, limit: int, window: int) -> bool:
        self.keys.append(key)
        return self.allow


class TestSendBudgetSharesThePerIpKey:
    async def test_oauth_hold_and_resend_charge_the_same_ip_bucket(
        self,
        app: Any,
        client: Any,
        auth_db: Any,
        smtp: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        limiter = _RecordingLimiter()
        app.config[rate_limiting.RATE_LIMITER_CONFIG_KEY] = limiter
        # One caller address (documentation range) for both requests.
        monkeypatch.setattr(rate_limiting, "_client_ip", lambda _hops: "203.0.113.7")
        expected = "auth:ip:203.0.113.7"
        async with app.test_request_context("/api/v1/auth/oauth/discord/callback"):
            assert rate_limiting.auth_ip_bucket_key(0) == expected
            with pytest.raises(ApiError):
                await _oauth(auth_db, _discord("worker@corp.example"))
        assert limiter.keys == [expected] and len(sink.handler.messages) == 1

        await client.post("/api/v1/auth/resend-verification", json={"email": "a@corp.example"})
        assert limiter.keys == [expected, expected]

    async def test_an_exhausted_bucket_holds_without_mailing(
        self, app: Any, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        _open_signup(auth_db, "corp.example")
        app.config[rate_limiting.RATE_LIMITER_CONFIG_KEY] = _RecordingLimiter(allow=False)
        async with app.test_request_context("/api/v1/auth/oauth/discord/callback"):
            with pytest.raises(ApiError) as err:
                await _oauth(auth_db, _discord("worker@corp.example"))
        assert err.value.code == email_verification.ERR_REQUIRED
        assert sink.handler.messages == []


class TestDomainNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Bücher.Example.", "xn--bcher-kva.example"),
            ("@CORP.example.", "corp.example"),
            ("xn--bcher-kva.example", "xn--bcher-kva.example"),
            ("a" * 64 + ".example", ""),  # label too long for IDNA: never matches
        ],
    )
    def test_normalize_domain(self, raw: str, expected: str) -> None:
        assert email_verification.normalize_domain(raw) == expected

    def test_policy_matches_idn_and_trailing_dot_forms(self) -> None:
        policy = email_verification.combine_allowlists(
            email_verification.parse_domains("bücher.example, corp.example."), []
        )
        assert policy.domains == ("xn--bcher-kva.example", "corp.example")
        for ok in ("a@BÜCHER.example", "a@xn--bcher-kva.example", "a@corp.example."):
            assert policy.allows(ok), ok
        assert not policy.allows("a@evil.example")
        assert email_verification.normalize_tenant_domains(["Bücher.example."]) == [
            "xn--bcher-kva.example"
        ]

    async def test_idn_signup_is_stored_normalized(
        self, client: Any, auth_db: Any, smtp: Any
    ) -> None:
        smtp()
        _open_signup(auth_db, "bücher.example")
        response = await _register(client, "Person@Bücher.Example.")
        await _settle()
        assert response.status_code == 202
        assert _user(auth_db, "person@xn--bcher-kva.example") is not None
