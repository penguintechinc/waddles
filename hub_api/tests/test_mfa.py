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

import jwt
import pytest
from quart import Quart
from quart_schema import QuartSchema

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from blueprints.v1.mfa import mfa_bp
from services import dsar_rate_limit, mfa_email, mfa_service, totp
from services.envelope import KmsProviderRegistry, TenantEnvelopeService
from services.session_cookie import SESSION_COOKIE_NAME
from tests.conftest import make_user_token
from tests.envelope.fakes import CountingPlatformKek, FakeGate, InMemoryEnvelopeRepo
from tests.test_session_lifecycle import PASSWORD, _config, _seed_user

LOGIN = "/api/v1/auth/login"
MFA = "/api/v1/auth/mfa"


class FakeTransport:
    """Stands in for `services.email_service` (the SMTP boundary): records, never sends."""

    def __init__(self, *, configured: bool = True) -> None:
        """Start with an empty outbox."""
        self.configured = configured
        self.sent: list[dict[str, str]] = []

    def is_configured(self) -> bool:
        """Whether SMTP is configured."""
        return self.configured

    async def send_email(self, *, to: str, subject: str, text: str) -> None:
        """Record the message."""
        self.sent.append({"to": to, "subject": subject, "text": text})

    def last_code(self) -> str:
        """The 6-digit code in the newest message."""
        return next(w.rstrip(".") for w in self.sent[-1]["text"].split() if w.rstrip(".").isdigit())


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

    async def test_kill_switch_restores_single_factor_login(
        self, client: Any, user: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _enroll_totp(client, user.headers)
        monkeypatch.setenv("FLAG_WADDLES_DISABLE_MFA", "true")
        login = await client.post(LOGIN, json={"email": "alice@example.com", "password": PASSWORD})
        assert login.status_code == 200
        enroll = await client.post(f"{MFA}/email/enroll", headers=user.headers)
        assert (await enroll.get_json())["error"]["code"] == "MFA_DISABLED"


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
        assert len(transport.sent) == 1 + mfa_service.MAX_EMAIL_SENDS_PER_CHALLENGE  # enroll + cap

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
