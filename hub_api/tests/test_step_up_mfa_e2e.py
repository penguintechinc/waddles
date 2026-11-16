"""MFA step-up end to end: real login -> passkey step-up -> platform secret rotation.

The authenticator's cryptographic ceremony needs a browser, so `webauthn.verify_authentication_
response` is the ONE faked seam (its call arguments are asserted: user verification is required,
the challenge and credential are this user's); everything else is real -- password login (which now
mints `amr=["pwd"]`), the step-up blueprint and service, JWT issue/verify, `tenant_middleware`,
`require_scope`, the system-tenant / global-admin gates, the replay ledger and the rotation itself
(over the in-memory vault).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from flask_core.auth import create_jwt_token
from quart import Quart
from quart_schema import QuartSchema
from webauthn.helpers.structs import CredentialDeviceType

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from blueprints.v1.platform_secrets import platform_secrets_bp
from blueprints.v1.step_up import step_up_bp
from services import mfa_stepup, passkey_service, step_up_service
from services.auth_amr import passkey_amr
from services.platform_secrets import rotation
from services.platform_secrets.bootstrap import bootstrap_platform_secrets
from tests.conftest import SECRET_KEY, TENANT_SLUG, make_user_token
from tests.test_platform_secrets_bootstrap import KEK_A, FakeRepo
from tests.test_session_lifecycle import _config, _login_token, _seed_user

ROTATE = "/api/v1/admin/platform-secrets/jwt_signing/rotate"
START = "/api/v1/auth/step-up/passkey/start"
FINISH = "/api/v1/auth/step-up/passkey/finish"
CRED_ID = "Y3JlZC1hZG1pbg"  # base64url("cred-admin")
OTHER_CRED = "Y3JlZC1vdGhlcg"
PUBLIC_KEY = "cHVibGljLWtleQ"  # never parsed: verification is faked


@pytest.fixture
def vault() -> Any:
    repo = FakeRepo()
    bootstrap_platform_secrets(repo, KEK_A, environ={}, now=datetime.now(UTC) - timedelta(days=1))

    class Session:
        def __enter__(self) -> FakeRepo:
            return repo

        def __exit__(self, *_a: object) -> None:
            return None

    rotation.configure_session_factory(lambda: Session())  # type: ignore[arg-type,return-value]
    yield repo
    rotation.configure_session_factory(None)


@pytest.fixture
def app(auth_db: Any, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch) -> Quart:
    monkeypatch.setattr(rotation, "get_kek", lambda: KEK_A)
    monkeypatch.setattr(rotation, "escrow_store_from_env", lambda *_a, **_k: None)
    monkeypatch.delenv("FLAG_WADDLES_DISABLE_DB_PLATFORM_KEYS", raising=False)
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    for bp in (auth_bp, step_up_bp, platform_secrets_bp):
        quart_app.register_blueprint(bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    config = _config()
    assert config.secret_key == SECRET_KEY
    quart_app.config["HUB_API_CONFIG"] = config
    quart_app.before_request(bridge_session_cookie_to_bearer)
    step_up_service._challenges.clear()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """A cookie-less client: every request carries exactly the bearer token the test chooses."""
    return app.test_client(use_cookies=False)


@pytest.fixture
def admin(auth_db: Any) -> SimpleNamespace:
    """A global admin with one registered passkey, and an unrelated user with another."""
    admin_id = _seed_user(auth_db, email="root@example.com", is_super_admin=True)
    other_id = _seed_user(auth_db, email="mallory@example.com")
    for uid, cred in ((admin_id, CRED_ID), (other_id, OTHER_CRED)):
        auth_db.dal.user_passkeys.insert(
            user_id=uid,
            credential_id=cred,
            public_key=PUBLIC_KEY,
            sign_count=1,
            device_name="key",
            created_at=datetime.now(UTC),
        )
    auth_db.dal.commit()
    return SimpleNamespace(id=admin_id, other_id=other_id)


def _bearer(token: str, *, key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(key or uuid.uuid4()),
    }


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})  # type: ignore[no-any-return]


class _FakeVerify:
    """Stands in for `webauthn.verify_authentication_response`; records how it was called."""

    def __init__(self, *, fail: bool = False, device: CredentialDeviceType | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.device = device or CredentialDeviceType.SINGLE_DEVICE

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail or not kwargs.get("require_user_verification"):
            raise ValueError("verification failed")
        return SimpleNamespace(
            new_sign_count=7, user_verified=True, credential_device_type=self.device
        )


def _patch_verify(monkeypatch: pytest.MonkeyPatch, fake: _FakeVerify) -> None:
    monkeypatch.setattr(step_up_service.webauthn, "verify_authentication_response", fake)


async def _admin_token(client: Any, admin: SimpleNamespace) -> str:
    return await _login_token(client, email="root@example.com", tenant="system")


async def _step_up(client: Any, token: str, cred: str = CRED_ID) -> Any:
    started = await client.post(START, headers=_bearer(token))
    assert started.status_code == 200, await started.get_data()
    assert (await started.get_json())["options"]["challenge"]
    return await client.post(FINISH, json={"credential": {"id": cred}}, headers=_bearer(token))


@pytest.mark.asyncio
async def test_password_login_is_not_mfa_rotation_is_refused(
    client: Any, admin: SimpleNamespace, vault: FakeRepo
) -> None:
    token = await _admin_token(client, admin)
    claims = _claims(token)
    assert claims["amr"] == ["pwd"] and abs(claims["auth_time"] - time.time()) < 30
    assert claims["tenant"] == "system"
    resp = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(token))
    assert resp.status_code == 403
    assert (await resp.get_json())["error"]["code"] == "MFA_REQUIRED"
    assert [r.version for r in vault.rows_for(3, None)] == [1]  # nothing rotated


@pytest.mark.asyncio
async def test_login_then_passkey_step_up_then_rotate_succeeds(
    client: Any, admin: SimpleNamespace, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeVerify()
    _patch_verify(monkeypatch, fake)
    login_token = await _admin_token(client, admin)
    resp = await _step_up(client, login_token)
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["mfaValidForSeconds"] == 300
    stepped = _claims(body["token"])
    original = _claims(login_token)
    assert stepped["amr"] == ["hwk", "user", "mfa"] and abs(stepped["auth_time"] - time.time()) < 30
    for same in ("sub", "tenant", "scope", "sid", "roles"):  # nothing escalated or swapped
        assert stepped[same] == original[same], same
    assert stepped["jti"] != original["jti"]
    # the verifier was told to REQUIRE user verification, with this user's challenge + key
    (call,) = fake.calls
    assert call["require_user_verification"] is True
    assert call["credential_current_sign_count"] == 1 and len(call["expected_challenge"]) >= 16
    # and the new token drives the rotation end to end
    rotated = await client.post(ROTATE, json={"reasonCode": 2}, headers=_bearer(body["token"]))
    assert rotated.status_code == 200, await rotated.get_data()
    assert (await rotated.get_json())["newVersion"] == 2
    assert [(r.version, r.state) for r in vault.rows_for(3, None)] == [(1, 3), (2, 2)]
    assert sum(1 for e in vault.event_rows if e.actor_uuid is not None) == 3
    # the old (password-only) token is still refused: step-up does not upgrade it retroactively
    again = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(login_token))
    assert (await again.get_json())["error"]["code"] == "MFA_REQUIRED"


@pytest.mark.asyncio
async def test_a_synced_passkey_is_swk_and_still_mfa_when_user_verified(
    client: Any, admin: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, _FakeVerify(device=CredentialDeviceType.MULTI_DEVICE))
    resp = await _step_up(client, await _admin_token(client, admin))
    assert _claims((await resp.get_json())["token"])["amr"] == ["swk", "user", "mfa"]


@pytest.mark.asyncio
async def test_stale_step_up_is_refused_after_five_minutes(
    client: Any, admin: SimpleNamespace, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_verify(monkeypatch, _FakeVerify())
    token = (await (await _step_up(client, await _admin_token(client, admin))).get_json())["token"]
    real = time.time
    monkeypatch.setattr(mfa_stepup.time, "time", lambda: real() + 301)
    stale = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(token))
    assert stale.status_code == 403
    assert (await stale.get_json())["error"]["code"] == "MFA_REQUIRED"
    assert [r.version for r in vault.rows_for(3, None)] == [1]


@pytest.mark.asyncio
async def test_a_hand_made_old_auth_time_is_refused(
    client: Any, admin: SimpleNamespace, vault: FakeRepo
) -> None:
    token = create_jwt_token(
        user_id=str(admin.id),
        username="root",
        email="root@example.com",
        roles=["viewer"],
        secret_key=SECRET_KEY,
        tenant="system",
        scope="platform_secrets:admin",
        amr=["hwk", "user", "mfa"],
        auth_time=int(time.time()) - 900,
    )
    resp = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(token))
    assert (await resp.get_json())["error"]["code"] == "MFA_REQUIRED"


@pytest.mark.asyncio
async def test_step_up_refusals_leave_no_mfa_and_consume_the_challenge(
    client: Any, admin: SimpleNamespace, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await _admin_token(client, admin)
    # finish without start
    assert (
        await client.post(FINISH, json={"credential": {"id": CRED_ID}}, headers=_bearer(token))
    ).status_code == 400
    # another user's credential id
    _patch_verify(monkeypatch, _FakeVerify())
    wrong = await _step_up(client, token, cred=OTHER_CRED)
    assert wrong.status_code == 401
    # verification failure -> 401, and the challenge is single use (a retry needs a new start)
    _patch_verify(monkeypatch, _FakeVerify(fail=True))
    failed = await _step_up(client, token)
    assert failed.status_code == 401
    _patch_verify(monkeypatch, _FakeVerify())
    replay = await client.post(FINISH, json={"credential": {"id": CRED_ID}}, headers=_bearer(token))
    assert replay.status_code == 400
    # a user with no passkey cannot even start
    # (and nothing above produced a usable MFA token: rotation is still refused)
    resp = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(token))
    assert (await resp.get_json())["error"]["code"] == "MFA_REQUIRED"


@pytest.mark.asyncio
async def test_step_up_needs_a_valid_token_and_a_registered_passkey(
    client: Any, auth_db: Any
) -> None:
    assert (await client.post(START)).status_code == 401
    assert (await client.post(FINISH, json={"credential": {"id": CRED_ID}})).status_code == 401
    user_id = _seed_user(auth_db, email="nokey@example.com")
    token = make_user_token(user_id=user_id, tenant=TENANT_SLUG, scope="platform_secrets:admin")
    assert user_id and (await client.post(START, headers=_bearer(token))).status_code == 400


@pytest.mark.asyncio
async def test_step_up_by_a_tenant_admin_still_cannot_rotate(
    client: Any, auth_db: Any, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MFA is necessary, not sufficient: the system-tenant / global-admin gates still apply."""
    uid = _seed_user(auth_db, email="plain@example.com")
    auth_db.dal.user_passkeys.insert(
        user_id=uid,
        credential_id=CRED_ID,
        public_key=PUBLIC_KEY,
        sign_count=1,
        device_name="key",
        created_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    _patch_verify(monkeypatch, _FakeVerify())
    token = (
        await (
            await _step_up(
                client,
                make_user_token(user_id=uid, tenant=TENANT_SLUG, scope="platform_secrets:admin"),
            )
        ).get_json()
    )["token"]
    resp = await client.post(ROTATE, json={"reasonCode": 1}, headers=_bearer(token))
    assert resp.status_code == 403
    assert [r.version for r in vault.rows_for(3, None)] == [1]


# ------------------------------------------------------------------------ passkey LOGIN amr


class _LoginDal:
    """Just enough async DAL for `passkey_service.finish_login`."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def executesql_async(self, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
        return [(9, CRED_ID, PUBLIC_KEY, 3, 5, "a@example.com", "a", None, False, False)]

    async def update_async(self, _query: Any, **values: Any) -> None:
        self.updates.append(values)

    async def select_async(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verified", "device", "expected"),
    [
        (True, CredentialDeviceType.SINGLE_DEVICE, ["hwk", "user", "mfa"]),
        (True, CredentialDeviceType.MULTI_DEVICE, ["swk", "user", "mfa"]),
        (False, CredentialDeviceType.SINGLE_DEVICE, ["hwk"]),
        (False, CredentialDeviceType.MULTI_DEVICE, ["swk"]),
    ],
)
async def test_passkey_login_mints_mfa_only_when_the_authenticator_verified_the_user(
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
    device: CredentialDeviceType,
    expected: list[str],
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(*_a: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "token"

    async def not_admin(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr("services.auth_service.create_session_token", fake_create)
    monkeypatch.setattr(passkey_service, "is_global_admin", not_admin)
    monkeypatch.setattr(
        passkey_service.webauthn,
        "verify_authentication_response",
        lambda **_k: SimpleNamespace(
            new_sign_count=4, user_verified=verified, credential_device_type=device
        ),
    )
    passkey_service._authentication_challenges["aa" * 16] = time.time() + 60
    await passkey_service.finish_login(
        _LoginDal(),
        SimpleNamespace(user_passkeys=SimpleNamespace(id=1)),
        _config(),
        credential={"id": CRED_ID},
    )
    assert captured["amr"] == expected
    assert ("mfa" in captured["amr"]) is verified
    assert mfa_stepup.step_up_failure({"amr": captured["amr"], "auth_time": int(time.time())}) == (
        None if verified else "no_mfa"
    )


def test_passkey_amr_values() -> None:
    assert passkey_amr(user_verified=True, single_device=True) == ["hwk", "user", "mfa"]
    assert passkey_amr(user_verified=False, single_device=False) == ["swk"]
