"""Account MFA end to end: TOTP (preferred), email one-time code, recovery codes, login step.

Real paths throughout -- the auth + mfa blueprints, password login, the MFA-pending challenge
table, the envelope service sealing TOTP secrets (in-memory key repository, real AES-GCM), JWT
mint/verify and the session cookies. The ONE faked seam is the email transport module
(`mfa_email._load_transport`), i.e. exactly where an SMTP message would leave the process; the
clock seen by TOTP is pinned through `mfa_service.time` for the drift cases.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import bcrypt
import jwt
import pytest
from flask_core.auth import create_jwt_token as jwt_mint
from quart import Quart
from quart_schema import QuartSchema

from app import bridge_session_cookie_to_bearer
from blueprints.v1 import mfa as mfa_bp_module
from blueprints.v1.auth import auth_bp
from blueprints.v1.mfa import mfa_bp
from services import dsar_rate_limit, mfa_email, mfa_service, totp
from services.envelope import KmsProviderRegistry, TenantEnvelopeService
from services.errors import ApiError
from services.session_cookie import SESSION_COOKIE_NAME
from tests.conftest import TENANT_SLUG, make_user_token
from tests.envelope.fakes import CountingPlatformKek, FakeGate, InMemoryEnvelopeRepo
from tests.test_session_lifecycle import PASSWORD, _config, _seed_user

LOGIN = "/api/v1/auth/login"
MFA = "/api/v1/auth/mfa"


class EmailUnavailableError(RuntimeError):
    """Mirror of `services.email_service.EmailUnavailableError` (the transport's failure base)."""

    reason = "unavailable"


class FakeTransport:
    """Stands in for `services.email_service` (the SMTP boundary): records, never sends.

    Exactly the transport's public contract (PR #1541): `is_configured()`,
    `async send_email(*, to_email, subject, text_body, kind, html_body=None)`, and the
    `EmailUnavailableError` it raises.
    """

    EmailUnavailableError = EmailUnavailableError

    def __init__(self, *, configured: bool = True) -> None:
        """Start with an empty outbox."""
        self.configured = configured
        self.fail_with: BaseException | None = None
        self.sent: list[dict[str, str]] = []

    def is_configured(self) -> bool:
        """Whether SMTP is configured."""
        return self.configured

    async def send_email(
        self,
        *,
        to_email: str,
        subject: str,
        text_body: str,
        kind: str,
        html_body: str | None = None,
    ) -> None:
        """Record the message (or raise the configured transport failure)."""
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"to": to_email, "subject": subject, "text": text_body, "kind": kind})

    def of_kind(self, kind: str) -> list[dict[str, str]]:
        """Messages of one `kind` label."""
        return [m for m in self.sent if m["kind"] == kind]

    def last_code(self) -> str:
        """The 6-digit code in the newest verification-code message."""
        text = self.of_kind("mfa_code")[-1]["text"]
        return next(w.rstrip(".") for w in text.split() if w.rstrip(".").isdigit())


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    dsar_rate_limit.reset_local()
    monkeypatch.delenv("FLAG_WADDLES_DISABLE_MFA", raising=False)
    monkeypatch.delenv("FLAG_WADDLES_DISABLE_MFA_EMAIL", raising=False)
    yield
    dsar_rate_limit.reset_local()


@pytest.fixture
def no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """The email-verification transport is not installed (today's release branch)."""
    monkeypatch.setattr(mfa_email, "_load_transport", lambda: None)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    fake = FakeTransport()
    monkeypatch.setattr(mfa_email, "_load_transport", lambda: fake)
    return fake


@pytest.fixture
def app(auth_db: Any) -> Quart:
    auth_db.dal.tenants.insert(slug="system", display_name="System", is_active=True)
    auth_db.dal.commit()
    repo = InMemoryEnvelopeRepo()
    kek = CountingPlatformKek(os.urandom(32))
    envelope = TenantEnvelopeService(
        keys=repo,
        configs=repo,
        registry=KmsProviderRegistry(),
        platform_kek=lambda: kek,
        gate=FakeGate(),
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.register_blueprint(mfa_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _config()
    quart_app.config["envelope_runtime"] = SimpleNamespace(service=envelope)
    quart_app.before_request(bridge_session_cookie_to_bearer)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client(use_cookies=False)


@pytest.fixture
def user(auth_db: Any) -> SimpleNamespace:
    user_id = _seed_user(auth_db)
    return SimpleNamespace(id=user_id, headers=_auth(user_id))


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


async def _enroll_totp(client: Any, headers: dict[str, str]) -> tuple[str, list[str]]:
    """Enroll + confirm TOTP; returns (secret, recovery codes).

    Confirms at the PREVIOUS step so the current step is still fresh for the test's first login.
    """
    resp = await client.post(f"{MFA}/totp/enroll", headers=headers)
    assert resp.status_code == 200, await resp.get_data()
    secret = (await resp.get_json())["secret"]
    code = totp.code_at_step(secret, totp.step_for(time.time()) - 1)
    resp = await client.post(f"{MFA}/totp/confirm", headers=headers, json={"code": code})
    assert resp.status_code == 200, await resp.get_data()
    return secret, (await resp.get_json())["recoveryCodes"]


async def _start_login(client: Any) -> dict[str, Any]:
    resp = await client.post(LOGIN, json={"email": "alice@example.com", "password": PASSWORD})
    assert resp.status_code == 202, await resp.get_data()
    assert resp.headers.getlist("Set-Cookie") == []  # no session before the second factor
    body: dict[str, Any] = await resp.get_json()
    return body


async def _verify(client: Any, token: str, method: str, code: str) -> Any:
    return await client.post(
        f"{MFA}/login/verify", json={"mfaToken": token, "method": method, "code": code}
    )


def _now_code(secret: str, offset: int = 0) -> str:
    return totp.code_at_step(secret, totp.step_for(time.time()) + offset)


class TestTotpPrimitives:
    def test_rfc6238_vector(self) -> None:
        secret = totp.base64.b32encode(b"12345678901234567890").decode()
        assert totp.code_at_step(secret, 59 // 30) == "287082"  # RFC 6238 App. B, T=59, 6 digits
        assert totp.code_at_step(secret, 1111111109 // 30) == "081804"

    def test_drift_window_and_replay_floor(self) -> None:
        secret = totp.new_secret()
        now = 1_700_000_000.0
        step = totp.step_for(now)
        for offset in (-1, 0, 1):
            code = totp.code_at_step(secret, step + offset)
            assert totp.matching_step(secret, code, now=now, after_step=None) == step + offset
        assert (
            totp.matching_step(
                secret, totp.code_at_step(secret, step - 2), now=now, after_step=None
            )
            is None
        )
        assert (
            totp.matching_step(secret, totp.code_at_step(secret, step), now=now, after_step=step)
            is None
        )
        assert totp.matching_step(secret, "12a456", now=now, after_step=None) is None

    def test_provisioning_uri_shape(self) -> None:
        uri = totp.provisioning_uri("ABCDEF", account_label="a@b.c")
        assert uri.startswith("otpauth://totp/Waddles%3Aa%40b.c?")
        for part in ("secret=ABCDEF", "issuer=Waddles", "digits=6", "period=30"):
            assert part in uri

    def test_recovery_codes_are_unique_and_normalized_hash(self) -> None:
        codes = totp.new_recovery_codes()
        assert len(codes) == 10 and len(set(codes)) == 10
        assert totp.hash_recovery_code(
            codes[0].lower().replace("-", " ")
        ) == totp.hash_recovery_code(codes[0])


class TestTotpEnrollment:
    async def test_enroll_returns_uri_and_seals_secret_until_confirmed(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        resp = await client.post(f"{MFA}/totp/enroll", headers=user.headers)
        body = await resp.get_json()
        assert (
            body["otpauthUri"].startswith("otpauth://totp/")
            and body["secret"] in body["otpauthUri"]
        )
        row = auth_db.dal(auth_db.dal.hub_user_mfa.user_id == user.id).select().first()
        assert row.totp_status == mfa_service.STATUS_PENDING
        assert (
            row.totp_secret_enc.startswith("wenv1.") and body["secret"] not in row.totp_secret_enc
        )
        # Pending is not enrolled: login still completes with the password alone.
        login = await client.post(LOGIN, json={"email": "alice@example.com", "password": PASSWORD})
        assert login.status_code == 200

    async def test_confirm_requires_a_valid_first_code(
        self, client: Any, user: SimpleNamespace
    ) -> None:
        resp = await client.post(f"{MFA}/totp/enroll", headers=user.headers)
        secret = (await resp.get_json())["secret"]
        bad = await client.post(
            f"{MFA}/totp/confirm", headers=user.headers, json={"code": "000000"}
        )
        if _now_code(secret) == "000000":  # pragma: no cover - 1e-6 chance
            pytest.skip("random secret produced 000000")
        assert (
            bad.status_code == 401 and (await bad.get_json())["error"]["code"] == "MFA_INVALID_CODE"
        )
        ok = await client.post(
            f"{MFA}/totp/confirm", headers=user.headers, json={"code": _now_code(secret)}
        )
        codes = (await ok.get_json())["recoveryCodes"]
        assert ok.status_code == 200 and len(codes) == 10
        status = await (await client.get(f"{MFA}/status", headers=user.headers)).get_json()
        assert status["totp"]["status"] == mfa_service.STATUS_ACTIVE
        assert status["preferredMethod"] == "totp" and status["recoveryCodesRemaining"] == 10

    async def test_ciphertext_bound_to_its_user(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        await _enroll_totp(client, user.headers)
        other = _seed_user(auth_db, email="bob@example.com")
        dal = auth_db.dal
        sealed = dal(dal.hub_user_mfa.user_id == user.id).select().first().totp_secret_enc
        dal.hub_user_mfa.insert(
            user_id=other, totp_secret_enc=sealed, totp_status=2, email_status=0
        )
        dal.commit()
        login = await client.post(LOGIN, json={"email": "bob@example.com", "password": PASSWORD})
        token = (await login.get_json())["mfaToken"]
        resp = await _verify(client, token, "totp", "123456")
        assert resp.status_code == 500
        assert (await resp.get_json())["error"]["code"] == "MFA_SECRET_UNREADABLE"


class TestLoginStep:
    async def test_pending_token_carries_no_session_or_scope(
        self, client: Any, user: SimpleNamespace
    ) -> None:
        await _enroll_totp(client, user.headers)
        body = await _start_login(client)
        assert body["mfaRequired"] is True and body["methods"] == ["totp", "recovery"]
        token = body["mfaToken"]
        with pytest.raises(jwt.exceptions.DecodeError):
            jwt.decode(token, options={"verify_signature": False})
        # Presented as a bearer token it opens nothing: MFA routes reject it, status is 401.
        status = await client.get(f"{MFA}/status", headers={"Authorization": f"Bearer {token}"})
        assert status.status_code in (401, 403)

    async def test_totp_completes_login_with_mfa_amr_and_cookie(
        self, client: Any, user: SimpleNamespace
    ) -> None:
        secret, _codes = await _enroll_totp(client, user.headers)
        token = (await _start_login(client))["mfaToken"]
        resp = await _verify(client, token, "totp", _now_code(secret))
        assert resp.status_code == 200, await resp.get_data()
        claims = jwt.decode((await resp.get_json())["token"], options={"verify_signature": False})
        assert {"pwd", "otp", "mfa"} <= set(claims["amr"]) and claims["auth_time"]
        assert any(
            c.startswith(f"{SESSION_COOKIE_NAME}=") for c in resp.headers.getlist("Set-Cookie")
        )
        # The challenge is single use.
        again = await _verify(client, token, "totp", _now_code(secret, 1))
        assert (await again.get_json())["error"]["code"] == "MFA_CHALLENGE_INVALID"

    async def test_totp_code_cannot_be_replayed(self, client: Any, user: SimpleNamespace) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        code = _now_code(secret)
        first = await _verify(client, (await _start_login(client))["mfaToken"], "totp", code)
        assert first.status_code == 200
        replay = await _verify(client, (await _start_login(client))["mfaToken"], "totp", code)
        assert replay.status_code == 401
        assert (await replay.get_json())["error"]["code"] == "MFA_INVALID_CODE"

    async def test_drift_plus_one_accepted_plus_two_rejected(
        self, client: Any, user: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        frozen = time.time()
        monkeypatch.setattr(
            mfa_service,
            "time",
            SimpleNamespace(time=lambda: frozen, perf_counter=time.perf_counter),
        )
        step = totp.step_for(frozen)
        too_new = await _verify(
            client,
            (await _start_login(client))["mfaToken"],
            "totp",
            totp.code_at_step(secret, step + 2),
        )
        assert too_new.status_code == 401
        ok = await _verify(
            client,
            (await _start_login(client))["mfaToken"],
            "totp",
            totp.code_at_step(secret, step + 1),
        )
        assert ok.status_code == 200

    async def test_recovery_code_is_single_use(self, client: Any, user: SimpleNamespace) -> None:
        _secret, codes = await _enroll_totp(client, user.headers)
        ok = await _verify(
            client, (await _start_login(client))["mfaToken"], "recovery", codes[0].lower()
        )
        assert ok.status_code == 200
        reuse = await _verify(
            client, (await _start_login(client))["mfaToken"], "recovery", codes[0]
        )
        assert reuse.status_code == 401
        status = await (await client.get(f"{MFA}/status", headers=user.headers)).get_json()
        assert status["recoveryCodesRemaining"] == 9

    async def test_five_wrong_codes_burn_the_challenge(
        self, client: Any, user: SimpleNamespace
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        token = (await _start_login(client))["mfaToken"]
        wrong = "000000" if _now_code(secret) != "000000" else "111111"
        codes = [
            (await (await _verify(client, token, "totp", wrong)).get_json())["error"]["code"]
            for _ in range(5)
        ]
        assert codes == ["MFA_INVALID_CODE"] * 4 + ["MFA_CHALLENGE_INVALID"]
        late = await _verify(client, token, "totp", _now_code(secret))
        assert (await late.get_json())["error"]["code"] == "MFA_CHALLENGE_INVALID"

    async def test_per_user_verify_rate_limit(self, client: Any, user: SimpleNamespace) -> None:
        await _enroll_totp(client, user.headers)
        last = None
        for _ in range(mfa_service.USER_VERIFY_ATTEMPTS // mfa_service.MAX_ATTEMPTS + 1):
            token = (await _start_login(client))["mfaToken"]
            for _ in range(mfa_service.MAX_ATTEMPTS):
                last = await _verify(client, token, "recovery", "AAAAA-AAAAA")
                if last.status_code == 429:
                    break
            if last is not None and last.status_code == 429:
                break
        assert last is not None and last.status_code == 429
        assert (await last.get_json())["error"]["code"] == "MFA_RATE_LIMITED"

    async def test_kill_switch_still_challenges_enrolled_users(
        self,
        client: Any,
        user: SimpleNamespace,
        auth_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`waddles.disable-mfa` ON blocks NEW enrollment only -- never a silent downgrade."""
        secret, _ = await _enroll_totp(client, user.headers)
        monkeypatch.setenv("FLAG_WADDLES_DISABLE_MFA", "true")
        body = await _start_login(client)  # 202 + no cookie: the second factor is still required
        assert body["mfaRequired"] is True and body["methods"] == ["totp", "recovery"]
        ok = await _verify(client, body["mfaToken"], "totp", _now_code(secret))
        assert ok.status_code == 200, await ok.get_data()
        enroll = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert (await enroll.get_json())["error"]["code"] == "MFA_DISABLED"
        other = _auth(_seed_user(auth_db, email="carol@example.com"))
        totp_enroll = await client.post(f"{MFA}/totp/enroll", headers=other)
        assert (await totp_enroll.get_json())["error"]["code"] == "MFA_DISABLED"


class TestEmailOtp:
    async def test_unavailable_without_transport_fails_loud(
        self, client: Any, user: SimpleNamespace, no_transport: None
    ) -> None:
        status = await (await client.get(f"{MFA}/status", headers=user.headers)).get_json()
        assert status["email"]["available"] is False and status["totp"]["available"] is True
        resp = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert resp.status_code == 503
        assert (await resp.get_json())["error"]["code"] == "EMAIL_MFA_UNAVAILABLE"
        with pytest.raises(Exception) as raised:
            await mfa_email.send_email(to="x@example.com", subject="s", text="t")
        assert getattr(raised.value, "code", "") == "EMAIL_MFA_UNAVAILABLE"

    async def test_enrollment_requires_verified_email(
        self, client: Any, auth_db: Any, transport: FakeTransport
    ) -> None:
        uid = _seed_user(auth_db, email="new@example.com")
        auth_db.dal(auth_db.dal.hub_users.id == uid).update(email_verified=False)
        auth_db.dal.commit()
        resp = await client.post(f"{MFA}/email/enroll", headers=_auth(uid))
        assert (await resp.get_json())["error"]["code"] == "EMAIL_NOT_VERIFIED"
        assert transport.sent == []

    async def _enroll_email(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> list[str]:
        resp = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert resp.status_code == 200, await resp.get_data()
        assert transport.sent[-1]["to"] == "alice@example.com"
        confirm = await client.post(
            f"{MFA}/email/confirm", headers=user.headers, json={"code": transport.last_code()}
        )
        assert confirm.status_code == 200, await confirm.get_data()
        codes: list[str] = (await confirm.get_json())["recoveryCodes"]
        return codes

    async def test_email_login_flow(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        assert len(await self._enroll_email(client, user, transport)) == 10
        body = await _start_login(client)
        assert body["methods"] == ["email", "recovery"]
        sent = await client.post(f"{MFA}/login/email", json={"mfaToken": body["mfaToken"]})
        assert sent.status_code == 200
        ok = await _verify(client, body["mfaToken"], "email", transport.last_code())
        assert ok.status_code == 200

    async def test_email_code_expires(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport, auth_db: Any
    ) -> None:
        await self._enroll_email(client, user, transport)
        token = (await _start_login(client))["mfaToken"]
        await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        dal = auth_db.dal
        dal(dal.hub_mfa_challenges.purpose == mfa_service.PURPOSE_LOGIN).update(
            email_code_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        dal.commit()
        expired = await _verify(client, token, "email", transport.last_code())
        assert expired.status_code == 401

    async def test_send_rate_limits(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport, auth_db: Any
    ) -> None:
        await self._enroll_email(client, user, transport)
        token = (await _start_login(client))["mfaToken"]
        first = await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        assert first.status_code == 200
        too_soon = await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        assert too_soon.status_code == 429
        dal = auth_db.dal
        for _ in range(mfa_service.MAX_EMAIL_SENDS_PER_CHALLENGE):
            dal(dal.hub_mfa_challenges.purpose == mfa_service.PURPOSE_LOGIN).update(
                email_last_sent_at=datetime.now(UTC) - timedelta(minutes=5)
            )
            dal.commit()
            await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        capped = await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        assert capped.status_code == 429
        # enroll + the per-challenge cap (the factor-added notice is a different kind)
        assert len(transport.of_kind("mfa_code")) == 1 + mfa_service.MAX_EMAIL_SENDS_PER_CHALLENGE

    async def test_wrong_email_codes_count_attempts(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        await self._enroll_email(client, user, transport)
        token = (await _start_login(client))["mfaToken"]
        await client.post(f"{MFA}/login/email", json={"mfaToken": token})
        good = transport.last_code()
        wrong = "000000" if good != "000000" else "111111"
        for _ in range(mfa_service.MAX_ATTEMPTS):
            await _verify(client, token, "email", wrong)
        late = await _verify(client, token, "email", good)
        assert (await late.get_json())["error"]["code"] == "MFA_CHALLENGE_INVALID"


class TestDisable:
    async def test_disable_needs_reauth_and_valid_code(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        url = f"{MFA}/disable"
        bad_pw = await client.post(
            url,
            headers=user.headers,
            json={"method": "totp", "code": _now_code(secret), "password": "nope-nope"},
        )
        assert (await bad_pw.get_json())["error"]["code"] == "REAUTH_FAILED"
        wrong = "000000" if _now_code(secret) != "000000" else "111111"
        bad_code = await client.post(
            url, headers=user.headers, json={"method": "totp", "code": wrong, "password": PASSWORD}
        )
        assert (await bad_code.get_json())["error"]["code"] == "MFA_INVALID_CODE"
        ok = await client.post(
            url,
            headers=user.headers,
            json={"method": "totp", "code": _now_code(secret), "password": PASSWORD},
        )
        assert ok.status_code == 200, await ok.get_data()
        dal = auth_db.dal
        row = dal(dal.hub_user_mfa.user_id == user.id).select().first()
        assert row.totp_status == 0 and row.totp_secret_enc is None
        assert dal(dal.hub_user_mfa_recovery_codes.user_id == user.id).count() == 0
        login = await client.post(LOGIN, json={"email": "alice@example.com", "password": PASSWORD})
        assert login.status_code == 200

    async def test_passwordless_account_needs_a_fresh_sign_in(
        self, client: Any, auth_db: Any
    ) -> None:
        uid = _seed_user(auth_db, email="oauth@example.com", password=None)
        headers = _auth(uid)  # token without auth_time: not a fresh sign-in
        secret, _ = await _enroll_totp(client, headers)
        resp = await client.post(
            f"{MFA}/disable", headers=headers, json={"method": "totp", "code": _now_code(secret)}
        )
        assert (await resp.get_json())["error"]["code"] == "REAUTH_REQUIRED"

    async def test_regenerate_recovery_codes(self, client: Any, user: SimpleNamespace) -> None:
        _secret, old = await _enroll_totp(client, user.headers)
        resp = await client.post(
            f"{MFA}/recovery-codes/regenerate",
            headers=user.headers,
            json={"code": old[0], "password": PASSWORD},
        )
        new = (await resp.get_json())["recoveryCodes"]
        assert resp.status_code == 200 and len(new) == 10 and not set(new) & set(old)
        stale = await _verify(client, (await _start_login(client))["mfaToken"], "recovery", old[1])
        assert stale.status_code == 401


# -- security review (PR #1540) regressions ----------------------------------------------------


def _wrong(secret: str) -> str:
    """A 6-digit code that is not the current TOTP code."""
    return "000000" if _now_code(secret) != "000000" else "111111"


async def _enroll_email_only(
    client: Any, headers: dict[str, str], transport: FakeTransport
) -> list[str]:
    """Enroll + confirm the email method as the FIRST factor; returns its recovery codes."""
    resp = await client.post(f"{MFA}/email/enroll", headers=headers)
    assert resp.status_code == 200, await resp.get_data()
    confirm = await client.post(
        f"{MFA}/email/confirm", headers=headers, json={"code": transport.last_code()}
    )
    assert confirm.status_code == 200, await confirm.get_data()
    codes: list[str] = (await confirm.get_json())["recoveryCodes"]
    return codes


def _fresh_signin_headers(user_id: int, *, amr: list[str]) -> dict[str, str]:
    """A session minted moments ago (auth_time = now) with the given `amr`."""
    token = jwt_mint(
        user_id=str(user_id),
        username="oauth",
        email="oauth@example.com",
        roles=["viewer"],
        tenant=TENANT_SLUG,
        scope="",
        amr=amr,
        auth_time=int(time.time()),
    )
    return {"Authorization": f"Bearer {token}"}


class TestKillSwitchScope:
    """Core kill switches are read at GLOBAL scope only; request tenants never flip them."""

    async def test_tenant_slug_cannot_flip_the_kill_switch(
        self, client: Any, user: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _enroll_totp(client, user.headers)
        seen: list[str] = []

        async def tenant_scoped_flag(flag: str, *, tenant: str, **_: Any) -> bool:
            # The flag is ON for every tenant EXCEPT the global/system scope.
            seen.append(tenant)
            return flag == mfa_service.FLAG_DISABLE_MFA and tenant != "system"

        monkeypatch.setattr(mfa_service, "feature_enabled", tenant_scoped_flag)
        resp = await client.post(
            LOGIN,
            json={"email": "alice@example.com", "password": PASSWORD, "tenantSlug": TENANT_SLUG},
        )
        assert resp.status_code == 202, await resp.get_data()
        assert (await resp.get_json())["mfaRequired"] is True
        assert seen and set(seen) == {"system"}

    async def test_unknown_or_inactive_tenant_rejected_before_mfa(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        await _enroll_totp(client, user.headers)
        dal = auth_db.dal
        dal.tenants.insert(slug="dormant", display_name="Dormant", is_active=False)
        dal.commit()
        for slug in ("no-such-tenant", "dormant"):
            resp = await client.post(
                LOGIN,
                json={"email": "alice@example.com", "password": PASSWORD, "tenantSlug": slug},
            )
            assert resp.status_code == 401, (slug, await resp.get_data())
        purpose = dal.hub_mfa_challenges.purpose == mfa_service.PURPOSE_LOGIN
        assert dal(purpose).count() == 0  # no MFA decision was ever made


class TestEnrollmentGate:
    async def test_stolen_session_cannot_add_totp_when_email_active(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        await _enroll_email_only(client, user.headers, transport)
        # A stolen session alone (no password, no code): refused, no secret handed out.
        bare = await client.post(f"{MFA}/totp/enroll", headers=user.headers)
        bare_body = await bare.get_json()
        assert bare.status_code == 401 and "secret" not in bare_body
        assert bare_body["error"]["code"] == "REAUTH_FAILED"
        # Password but no valid code from the existing (email) factor: still refused.
        no_code = await client.post(
            f"{MFA}/totp/enroll", headers=user.headers, json={"password": PASSWORD, "code": ""}
        )
        assert (await no_code.get_json())["error"]["code"] == "MFA_INVALID_CODE"
        # Re-auth + a valid emailed code: allowed.
        sent = await client.post(f"{MFA}/email/send-code", headers=user.headers)
        assert sent.status_code == 200, await sent.get_data()
        ok = await client.post(
            f"{MFA}/totp/enroll",
            headers=user.headers,
            json={"password": PASSWORD, "code": transport.last_code()},
        )
        assert ok.status_code == 200, await ok.get_data()

    async def test_stolen_session_cannot_add_email_when_totp_active(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        bare = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert (await bare.get_json())["error"]["code"] == "REAUTH_FAILED"
        assert transport.of_kind("mfa_code") == []
        ok = await client.post(
            f"{MFA}/email/enroll",
            headers=user.headers,
            json={"password": PASSWORD, "code": _now_code(secret)},
        )
        assert ok.status_code == 200, await ok.get_data()

    async def test_fresh_oauth_session_alone_cannot_add_or_disable_a_factor(
        self, client: Any, auth_db: Any, transport: FakeTransport
    ) -> None:
        uid = _seed_user(auth_db, email="oauth@example.com", password=None)
        secret, _ = await _enroll_totp(client, _auth(uid))
        no_mfa = _fresh_signin_headers(uid, amr=["ext"])  # fresh IdP sign-in, no MFA in it
        resp = await client.post(
            f"{MFA}/disable",
            headers=no_mfa,
            json={"method": "totp", "code": _now_code(secret)},
        )
        assert (await resp.get_json())["error"]["code"] == "REAUTH_REQUIRED"
        add = await client.post(
            f"{MFA}/email/enroll", headers=no_mfa, json={"code": _now_code(secret)}
        )
        assert (await add.get_json())["error"]["code"] == "REAUTH_REQUIRED"
        with_mfa = _fresh_signin_headers(uid, amr=["ext", "otp", "mfa"])
        ok = await client.post(
            f"{MFA}/disable",
            headers=with_mfa,
            json={"method": "totp", "code": _now_code(secret, 1)},
        )
        assert ok.status_code == 200, await ok.get_data()

    async def test_recovery_codes_preserved_when_second_factor_confirmed(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        originals = await _enroll_email_only(client, user.headers, transport)
        assert len(originals) == 10
        await client.post(f"{MFA}/email/send-code", headers=user.headers)
        start = await client.post(
            f"{MFA}/totp/enroll",
            headers=user.headers,
            json={"password": PASSWORD, "code": transport.last_code()},
        )
        assert start.status_code == 200, await start.get_data()
        secret = (await start.get_json())["secret"]
        confirm = await client.post(
            f"{MFA}/totp/confirm",
            headers=user.headers,
            json={"code": totp.code_at_step(secret, totp.step_for(time.time()) - 1)},
        )
        assert confirm.status_code == 200, await confirm.get_data()
        assert (await confirm.get_json())["recoveryCodes"] == []
        token = (await _start_login(client))["mfaToken"]
        ok = await _verify(client, token, "recovery", originals[0])
        assert ok.status_code == 200, await ok.get_data()

    async def test_factor_changes_notify_verified_email(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        added = transport.of_kind("mfa_factor_change")
        assert len(added) == 1 and added[0]["to"] == "alice@example.com"
        assert "added" in added[0]["text"] and secret not in added[0]["text"]
        resp = await client.post(
            f"{MFA}/disable",
            headers=user.headers,
            json={"method": "totp", "code": _now_code(secret), "password": PASSWORD},
        )
        assert resp.status_code == 200
        changes = transport.of_kind("mfa_factor_change")
        assert len(changes) == 2 and "removed" in changes[1]["text"]

    async def test_notification_failure_never_blocks_the_change(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        transport.fail_with = EmailUnavailableError("smtp down")
        _secret, codes = await _enroll_totp(client, user.headers)  # asserts 200 inside
        assert len(codes) == 10 and transport.sent == []

    async def test_email_confirm_respects_the_kill_switch(
        self,
        client: Any,
        user: SimpleNamespace,
        transport: FakeTransport,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        resp = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert resp.status_code == 200
        monkeypatch.setenv("FLAG_WADDLES_DISABLE_MFA", "true")
        confirm = await client.post(
            f"{MFA}/email/confirm", headers=user.headers, json={"code": transport.last_code()}
        )
        assert (await confirm.get_json())["error"]["code"] == "MFA_DISABLED"


class TestBruteForce:
    async def test_per_ip_limit_on_login_verify(self, client: Any) -> None:
        limit = mfa_bp_module.IP_VERIFY_LIMIT[0]
        statuses = [
            (await _verify(client, f"bogus-{i}", "totp", "123456")).status_code
            for i in range(limit + 1)
        ]
        assert statuses[:limit] == [401] * limit and statuses[-1] == 429

    async def test_per_ip_limit_on_login_email(self, client: Any) -> None:
        limit = mfa_bp_module.IP_EMAIL_LIMIT[0]
        statuses = [
            (await client.post(f"{MFA}/login/email", json={"mfaToken": f"x{i}"})).status_code
            for i in range(limit + 1)
        ]
        assert statuses[:limit] == [401] * limit and statuses[-1] == 429

    async def test_backoff_and_failed_attempt_notice(
        self, client: Any, user: SimpleNamespace, transport: FakeTransport
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        token = (await _start_login(client))["mfaToken"]
        for _ in range(mfa_service.FAILED_ATTEMPTS_NOTIFY):
            await _verify(client, token, "totp", _wrong(secret))
        notices = transport.of_kind("mfa_failed_attempts")
        assert len(notices) == 1 and notices[0]["to"] == "alice@example.com"
        # Backoff is now armed: even the right code on a fresh challenge waits it out.
        fresh = (await _start_login(client))["mfaToken"]
        held = await _verify(client, fresh, "totp", _now_code(secret))
        assert held.status_code == 429

    async def test_login_and_account_buckets_are_separate(
        self, client: Any, user: SimpleNamespace
    ) -> None:
        _secret, codes = await _enroll_totp(client, user.headers)
        login_statuses: list[int] = []
        while len(login_statuses) < mfa_service.USER_VERIFY_ATTEMPTS + 1:
            token = (await _start_login(client))["mfaToken"]
            for _ in range(mfa_service.MAX_ATTEMPTS - 1):  # never burns the challenge
                resp = await _verify(client, token, "recovery", "AAAAA-AAAAA")
                login_statuses.append(resp.status_code)
        assert login_statuses[-1] == 429  # login verification is locked out
        regen = await client.post(
            f"{MFA}/recovery-codes/regenerate",
            headers=user.headers,
            json={"code": codes[0], "password": PASSWORD},
        )
        assert regen.status_code == 200, await regen.get_data()


class TestUnguardedSessionPaths:
    async def test_verify_email_requires_the_second_factor(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        dal = auth_db.dal
        dal(dal.hub_users.id == user.id).update(
            email_verification_token="verify-tok-1",  # noqa: S106 -- test fixture value
            email_verification_expires=datetime.now(UTC) + timedelta(hours=1),
        )
        dal.commit()
        resp = await client.get("/api/v1/auth/verify-email?token=verify-tok-1")
        assert resp.status_code == 202, await resp.get_data()
        assert resp.headers.getlist("Set-Cookie") == []
        body = await resp.get_json()
        assert body["mfaRequired"] is True and "token" not in body
        ok = await _verify(client, body["mfaToken"], "totp", _now_code(secret))
        assert ok.status_code == 200, await ok.get_data()

    async def test_temp_password_requires_the_second_factor(
        self, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        dal = auth_db.dal
        dal.hub_temp_passwords.insert(
            user_identifier="alice@example.com",
            password_hash=bcrypt.hashpw(b"temp-1234", bcrypt.gensalt(rounds=4)).decode(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            force_oauth_link=True,
            created_at=datetime.now(UTC),
        )
        dal.commit()
        resp = await client.post(
            "/api/v1/auth/temp-password",
            json={"identifier": "alice@example.com", "password": "temp-1234"},
        )
        assert resp.status_code == 202, await resp.get_data()
        assert resp.headers.getlist("Set-Cookie") == []
        token = (await resp.get_json())["mfaToken"]
        ok = await _verify(client, token, "totp", _now_code(secret))
        assert ok.status_code == 200, await ok.get_data()
        claims = jwt.decode((await ok.get_json())["token"], options={"verify_signature": False})
        assert claims["sub"] == "0"  # still the pending-link temp session, never a full one
        assert {"otp", "mfa"} <= set(claims["amr"])


class TestRecoveryCodeHashing:
    async def test_codes_stored_as_peppered_hmac(
        self, app: Quart, client: Any, user: SimpleNamespace, auth_db: Any
    ) -> None:
        _secret, codes = await _enroll_totp(client, user.headers)
        dal = auth_db.dal
        rows = dal(dal.hub_user_mfa_recovery_codes.user_id == user.id).select()
        assert len(rows) == 10
        stored = {r.code_hash for r in rows}
        for code in codes:
            assert totp.hash_recovery_code(code) not in stored  # never the plain SHA-256
        system_id = dal(dal.tenants.slug == "system").select().first().id
        envelope = app.config["envelope_runtime"].service
        _v, data_key = await envelope.data_key(int(system_id), int(rows.first().key_version))
        pepper = totp.recovery_pepper(data_key)
        assert {totp.hmac_recovery_code(pepper, c) for c in codes} == stored
        assert totp.hmac_recovery_code(pepper, codes[0].lower().replace("-", " ")) in stored

    async def test_fails_closed_without_the_pepper(
        self, app: Quart, client: Any, user: SimpleNamespace
    ) -> None:
        _secret, codes = await _enroll_totp(client, user.headers)
        app.config["envelope_runtime"] = None
        resp = await client.post(
            f"{MFA}/recovery-codes/regenerate",
            headers=user.headers,
            json={"code": codes[0], "password": PASSWORD},
        )
        assert resp.status_code == 503
        assert (await resp.get_json())["error"]["code"] == "MFA_UNAVAILABLE"


class TestEmailAdapter:
    async def test_transport_unavailable_error_maps_to_503(self, transport: FakeTransport) -> None:
        transport.fail_with = EmailUnavailableError("down")
        with pytest.raises(ApiError) as raised:
            await mfa_email.send_email(to="x@example.com", subject="s", text="t")
        assert raised.value.status_code == 503 and raised.value.code == "EMAIL_MFA_UNAVAILABLE"

    async def test_passes_the_transport_contract(self, transport: FakeTransport) -> None:
        await mfa_email.send_email(
            to="x@example.com", subject="s", text="body", kind=mfa_email.KIND_FACTOR_CHANGE
        )
        assert transport.sent == [
            {"to": "x@example.com", "subject": "s", "text": "body", "kind": "mfa_factor_change"}
        ]
