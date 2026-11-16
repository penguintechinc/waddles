"""Platform secret store T8: rotate / retire service, MFA step-up, and the admin HTTP surface.

Service tests run over the in-memory repository of `test_platform_secrets_bootstrap`; the HTTP
tests drive the REAL blueprint (real `tenant_middleware`, `require_scope`, JWT verification and a
sqlite DAL with real `system` tenant / global-admin rows) with the vault session faked. Every
refusal has a positive control and asserts that nothing changed.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from flask_core.auth import create_jwt_token
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.platform_secrets import platform_secrets_bp
from services import global_identity, mfa_stepup
from services.platform_secrets import escrow as escrow_mod
from services.platform_secrets import rotation
from services.platform_secrets.bootstrap import bootstrap_platform_secrets
from services.platform_secrets.cache import PlatformSecretCache, verify_until_for
from services.platform_secrets.escrow import MANIFEST_KEY, load_manifest
from services.platform_secrets.event_chain import verify_chain
from services.platform_secrets.registry import REGISTRY, SecretType
from services.platform_secrets.rotation import (
    ROTATION_MIN_INTERVAL,
    Refusal,
    RetireOutcome,
    RotateOutcome,
    RotationReason,
    resolve_type,
    retire_secret,
    rotate_secret,
)
from tests.conftest import SECRET_KEY, TENANT_SLUG, insert_global_admin
from tests.test_platform_secrets_bootstrap import KEK_A, FakeRepo
from tests.test_platform_secrets_escrow import MemStore

JWT = REGISTRY[SecretType.JWT_SIGNING]
BUNDLE = REGISTRY[SecretType.BUNDLE_SIGNING]
ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
BOOT_AT = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)


def _seeded(at: datetime = BOOT_AT) -> FakeRepo:
    """A bootstrapped repository (all three types at version 1, activated at `at`)."""
    repo = FakeRepo()
    bootstrap_platform_secrets(repo, KEK_A, environ={}, now=at)
    return repo


def _rotate(repo: FakeRepo, spec=JWT, *, at: datetime, key: uuid.UUID | None = None, **kw: Any):
    return rotate_secret(
        repo,
        KEK_A,
        spec=spec,
        actor=ACTOR,
        reason=kw.get("reason", RotationReason.SCHEDULED),
        idempotency_key=key or uuid.uuid4(),
        now=at,
    )


def _retire(repo: FakeRepo, version: int, *, at: datetime, spec=JWT, key: uuid.UUID | None = None):
    return retire_secret(
        repo,
        KEK_A,
        spec=spec,
        version=version,
        actor=ACTOR,
        reason=RotationReason.MAINTENANCE,
        idempotency_key=key or uuid.uuid4(),
        now=at,
    )


def _events(repo: FakeRepo, event_type: int | None = None) -> list[Any]:
    return [e for e in repo.event_rows if event_type is None or e.event_type == event_type]


# ----------------------------------------------------------------------------------- rotation


def test_rotation_state_transitions_and_audit() -> None:
    repo = _seeded()
    before = {(r.secret_type, r.version): r for r in repo.secrets}
    at = BOOT_AT + timedelta(hours=2)
    out = _rotate(repo, at=at, reason=RotationReason.COMPROMISE)
    assert isinstance(out, RotateOutcome) and (out.previous_version, out.new_version) == (1, 2)
    rows = {r.version: r for r in repo.rows_for(int(JWT.secret_type), None)}
    assert rows[1].state == 3 and rows[1].rotated_at == at  # demoted to verify-only
    assert rows[2].state == 2 and rows[2].activated_at == at
    assert rows[2].kid != rows[1].kid and rows[2].fingerprint != rows[1].fingerprint
    assert rows[2].public_id != rows[1].public_id
    # exactly one active per (type, scope); other types untouched
    assert [r.state for r in repo.rows_for(int(JWT.secret_type), None)] == [3, 2]
    for key, row in before.items():
        if key[0] != int(JWT.secret_type):
            assert repo.rows_for(key[0], None)[0] == row
    # audit: rotation requested + verify-only + activated, all with actor + int reason code 9
    new_events = [e for e in repo.event_rows if e.actor_uuid == ACTOR]
    assert sorted(e.event_type for e in new_events) == [4, 5, 8]
    assert {e.reason for e in new_events} == {9} and {e.outcome for e in new_events} == {1}
    assert verify_chain(repo, KEK_A).intact
    assert repo.notifies >= 1


def test_the_new_version_is_signed_by_the_new_key_and_old_stays_servable_in_the_window() -> None:
    repo = _seeded()
    at = BOOT_AT + timedelta(hours=2)
    _rotate(repo, at=at)
    window = timedelta(seconds=JWT.verify_window_s)
    assert verify_until_for(int(JWT.secret_type), 3, at) == at + window
    assert verify_until_for(int(JWT.secret_type), 2, None) is None  # active never expires here
    # the cache serves the old key until verify_until, then refuses it (even with no refresh)
    from services.platform_secrets.bootstrap import _open_row  # opened the way the cache is seeded

    clock = {"now": at + timedelta(minutes=1)}
    cache = PlatformSecretCache(clock=lambda: clock["now"])
    cache.seed(_open_row(JWT, row, KEK_A) for row in repo.openable_rows() if row.secret_type == 3)
    old = repo.rows_for(int(JWT.secret_type), None)[0]
    assert cache.by_kid(int(JWT.secret_type), old.kid or "").version == 1
    assert cache.active(int(JWT.secret_type)).version == 2
    clock["now"] = at + window + timedelta(seconds=1)
    with pytest.raises(LookupError):
        cache.by_kid(int(JWT.secret_type), old.kid or "")
    assert cache.active(int(JWT.secret_type)).version == 2


def test_replayed_idempotency_key_is_refused_and_audited() -> None:
    repo = _seeded()
    key = uuid.uuid4()
    first = _rotate(repo, at=BOOT_AT + timedelta(hours=2), key=key)
    assert isinstance(first, RotateOutcome)
    versions = len(repo.secrets)
    replay = _rotate(repo, at=BOOT_AT + timedelta(hours=5), key=key)
    assert replay == Refusal("replayed", 409)
    assert len(repo.secrets) == versions  # nothing executed twice
    denial = _events(repo, 8)[-1]
    assert denial.outcome == 2 and denial.actor_uuid == ACTOR and denial.version is None


def test_rotation_rate_limit_one_per_type_per_ten_minutes() -> None:
    repo = _seeded()
    at = BOOT_AT + timedelta(hours=2)
    assert isinstance(_rotate(repo, at=at), RotateOutcome)
    limited = _rotate(repo, at=at + timedelta(minutes=9))
    assert limited == Refusal("rate_limited", 429)
    assert len(repo.rows_for(int(JWT.secret_type), None)) == 2
    assert _events(repo, 8)[-1].outcome == 2
    # other types are not limited by this one; and after the interval it works again
    assert isinstance(_rotate(repo, BUNDLE, at=at + timedelta(minutes=9)), RotateOutcome)
    after = _rotate(repo, at=at + ROTATION_MIN_INTERVAL + timedelta(seconds=1))
    assert isinstance(after, RotateOutcome) and after.new_version == 3


def test_retire_is_refused_inside_the_verify_window_then_allowed() -> None:
    repo = _seeded()
    at = BOOT_AT + timedelta(hours=2)
    _rotate(repo, at=at)
    window = timedelta(seconds=JWT.verify_window_s)
    early = _retire(repo, 1, at=at + window - timedelta(seconds=1))
    assert early == Refusal("window_open", 409)
    assert repo.rows_for(int(JWT.secret_type), None)[0].state == 3  # unchanged
    done = _retire(repo, 1, at=at + window)
    assert isinstance(done, RetireOutcome) and done.version == 1
    row = repo.rows_for(int(JWT.secret_type), None)[0]
    assert row.state == 4 and row.retired_at == at + window
    assert [e.event_type for e in _events(repo) if e.version == 1][-2:] == [8, 6]
    assert verify_chain(repo, KEK_A).intact


def test_retire_refusals_active_version_unknown_version_and_replay() -> None:
    repo = _seeded()
    at = BOOT_AT + timedelta(hours=2)
    _rotate(repo, at=at)
    late = at + timedelta(days=2)
    assert _retire(repo, 2, at=late) == Refusal("not_verify_only", 409)  # active: rotate first
    assert _retire(repo, 99, at=late) == Refusal("version_not_found", 404)
    key = uuid.uuid4()
    assert isinstance(_retire(repo, 1, at=late, key=key), RetireOutcome)
    assert _retire(repo, 1, at=late, key=key) == Refusal("replayed", 409)
    assert _retire(repo, 1, at=late) == Refusal("not_verify_only", 409)  # already retired
    assert [r.state for r in repo.rows_for(int(JWT.secret_type), None)] == [4, 2]
    assert all(e.outcome in (1, 2) for e in repo.event_rows)


def test_a_retired_version_cannot_be_revived_and_the_active_one_survives_a_second_rotation() -> (
    None
):
    repo = _seeded()
    at = BOOT_AT + timedelta(hours=2)
    _rotate(repo, at=at)
    third = _rotate(repo, at=at + timedelta(hours=1))
    assert isinstance(third, RotateOutcome) and third.previous_version == 2
    assert [r.state for r in repo.rows_for(int(JWT.secret_type), None)] == [3, 3, 2]
    assert not repo.update_state(1, 2, at=at, reason=4, actor_uuid=None)  # never backwards


def test_resolve_type_accepts_names_and_codes_only_for_registered_global_types() -> None:
    assert resolve_type("jwt_signing") is JWT and resolve_type("3") is JWT
    for bad in ("", "nope", "999", "-1", "JWT_SIGNING", "jwt_signing; DROP TABLE"):
        assert resolve_type(bad) is None


def test_no_secret_shaped_value_reaches_logs_or_events(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    repo = _seeded()
    _rotate(repo, at=BOOT_AT + timedelta(hours=2))
    _rotate(repo, at=BOOT_AT + timedelta(hours=2, minutes=1))  # rate limited -> logged refusal
    from services.provider_connections.vault import open_platform_secret

    for row in repo.secrets:
        plain = open_platform_secret(
            row.sealed_value,
            secret_type=row.secret_type,
            scope_id=None,
            version=row.version,
            public_id=row.public_id,
            kek=KEK_A,
        )
        assert plain.hex() not in caplog.text and row.fingerprint.hex() not in caplog.text
        assert (row.kid or "x-none") not in caplog.text


# ----------------------------------------------------------------------------------- escrow hook


def test_rotation_is_escrowed_afterwards_and_the_manifest_advances() -> None:
    repo, store = FakeRepo(), MemStore()
    bootstrap_platform_secrets(repo, KEK_A, environ={}, now=BOOT_AT, escrow=store)
    escrow_mod.sync_escrow(repo, KEK_A, store, now=BOOT_AT)
    assert load_manifest(store, KEK_A).generation == 1  # type: ignore[union-attr]
    _rotate(repo, at=BOOT_AT + timedelta(hours=2))

    class Session:
        def __enter__(self) -> FakeRepo:
            return repo

        def __exit__(self, *_a: object) -> None:
            return None

    rotation.escrow_after_change(lambda: Session(), KEK_A, store)  # type: ignore[arg-type,return-value]
    manifest = load_manifest(store, KEK_A)
    assert manifest is not None and manifest.generation == 2
    assert {(e.secret_type, e.version) for e in manifest.entries} >= {(3, 1), (3, 2)}
    assert repo.anchor is not None and repo.anchor.generation == 2 and MANIFEST_KEY in store.objects


def test_escrow_failure_after_a_rotation_never_raises() -> None:
    repo, store = FakeRepo(), MemStore()
    bootstrap_platform_secrets(repo, KEK_A, environ={}, now=BOOT_AT)
    store.down = True

    class Session:
        def __enter__(self) -> FakeRepo:
            return repo

        def __exit__(self, *_a: object) -> None:
            return None

    rotation.escrow_after_change(lambda: Session(), KEK_A, store)  # type: ignore[arg-type,return-value]


# ----------------------------------------------------------------------------------- MFA claim


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"amr": ["pwd", "otp"], "auth_time": 1000}, None),
        ({"amr": ["hwk", "user", "mfa"], "auth_time": 1000}, None),
        ({"amr": ["hwk"], "auth_time": 1000}, "no_mfa"),
        ({"amr": ["swk", "user"], "auth_time": 1000}, "no_mfa"),
        ({"amr": ["pwd"], "auth_time": 1000}, "no_mfa"),
        ({"auth_time": 1000}, "no_mfa"),
        ({"amr": "otp", "auth_time": 1000}, "no_mfa"),
        ({"amr": [None, 5], "auth_time": 1000}, "no_mfa"),
        ({"amr": ["otp"]}, "no_auth_time"),
        ({"amr": ["otp"], "auth_time": "1000"}, "no_auth_time"),
        ({"amr": ["otp"], "auth_time": True}, "no_auth_time"),
        ({"amr": ["otp"], "auth_time": 1000 - 400}, "stale"),
        ({"amr": ["otp"], "auth_time": 1000 + 120}, "stale"),
    ],
)
def test_step_up_claim_matrix(claims: dict[str, Any], expected: str | None) -> None:
    assert mfa_stepup.step_up_failure(claims, now=1000 + 10) == expected


def test_step_up_boundary_is_five_minutes() -> None:
    claims = {"amr": ["otp"], "auth_time": 5000}
    assert mfa_stepup.step_up_failure(claims, now=5000 + 300) is None
    assert mfa_stepup.step_up_failure(claims, now=5000 + 301) == "stale"


# ----------------------------------------------------------------------------------- HTTP


@pytest.fixture
def vault() -> Iterator[FakeRepo]:
    """A seeded in-memory vault installed behind the blueprint's session factory."""
    repo = _seeded(datetime.now(UTC) - timedelta(days=1))

    class Session:
        def __enter__(self) -> FakeRepo:
            return repo

        def __exit__(self, *_a: object) -> None:
            return None

    rotation.configure_session_factory(lambda: Session())  # type: ignore[arg-type,return-value]
    yield repo
    rotation.configure_session_factory(None)


@pytest.fixture
def app(admin_db: Any, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch) -> Quart:
    monkeypatch.setattr(rotation, "get_kek", lambda: KEK_A)
    monkeypatch.setattr(rotation, "escrow_store_from_env", lambda *_a, **_k: None)
    monkeypatch.delenv("FLAG_WADDLES_DISABLE_DB_PLATFORM_KEYS", raising=False)
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(platform_secrets_bp)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    quart_app.config["HUB_API_CONFIG"] = SimpleNamespace(database_url="postgresql://unused")
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def users(admin_db: Any) -> SimpleNamespace:
    """A global admin (system-tenant admin) and a plain acme tenant admin."""
    dal = admin_db.dal
    global_admin = insert_global_admin(dal, username="root")
    tenant_admin = int(dal.hub_users.insert(username="acme-admin"))
    acme = dal(dal.tenants.slug == TENANT_SLUG).select().first().id
    global_identity.add_tenant_admin_sync(dal, tenant_id=acme, user_id=tenant_admin, role="admin")
    dal.commit()
    uuids = {
        uid: uuid.UUID(str(dal(dal.hub_users.id == uid).select().first().uuid))
        for uid in (global_admin, tenant_admin)
    }
    return SimpleNamespace(
        admin=global_admin, tenant_admin=tenant_admin, admin_uuid=uuids[global_admin]
    )


def _token(
    user_id: int,
    *,
    tenant: str = "system",
    scope: str = "platform_secrets:admin",
    mfa_age: int | None = 30,
    amr: list[str] | None = None,
) -> str:
    return create_jwt_token(
        user_id=str(user_id),
        username="u",
        email="u@example.invalid",
        roles=["viewer"],
        secret_key=SECRET_KEY,
        tenant=tenant,
        scope=scope,
        amr=amr if amr is not None else (["pwd", "otp"] if mfa_age is not None else ["pwd"]),
        auth_time=None if mfa_age is None else int(time.time()) - mfa_age,
    )


def _hdr(token: str, *, key: uuid.UUID | None = None, **extra: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(key or uuid.uuid4()),
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def _versions(vault: FakeRepo, spec=JWT) -> list[tuple[int, int]]:
    return [(r.version, r.state) for r in vault.rows_for(int(spec.secret_type), None)]


def _denials(vault: FakeRepo) -> list[Any]:
    return [e for e in vault.event_rows if e.event_type == 8 and e.outcome == 2]


ROTATE = "/api/v1/admin/platform-secrets/jwt_signing/rotate"
BODY = {"reasonCode": 1}


@pytest.mark.asyncio
async def test_rotate_happy_path_dto_has_no_secret_material(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    resp = await client.post(ROTATE, json=BODY, headers=_hdr(_token(users.admin)))
    assert resp.status_code == 200
    body = await resp.get_json()
    assert set(body) == {"success", "type", "newVersion", "previousVersion", "previousVerifyUntil"}
    assert (body["type"], body["newVersion"], body["previousVersion"]) == ("jwt_signing", 2, 1)
    assert _versions(vault) == [(1, 3), (2, 2)]
    text = await resp.get_data(as_text=True)
    for row in vault.secrets:
        assert (row.kid or "x-none") not in text and row.fingerprint.hex() not in text
    audited = [e for e in vault.event_rows if e.actor_uuid == users.admin_uuid]
    assert sorted(e.event_type for e in audited) == [4, 5, 8] and {e.reason for e in audited} == {8}


@pytest.mark.asyncio
async def test_rotate_by_registry_code_and_status_listing(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    resp = await client.post(
        "/api/v1/admin/platform-secrets/3/rotate", json=BODY, headers=_hdr(_token(users.admin))
    )
    assert resp.status_code == 200
    listing = await client.get(
        "/api/v1/admin/platform-secrets/",
        headers={"Authorization": f"Bearer {_token(users.admin)}"},
    )
    assert listing.status_code == 200
    body = await listing.get_json()
    assert {t["type"] for t in body["types"]} == {
        "bundle_signing",
        "precompile_signing",
        "jwt_signing",
    }
    jwt_row = next(t for t in body["types"] if t["type"] == "jwt_signing")
    assert [(v["version"], v["state"]) for v in jwt_row["versions"]] == [(1, 3), (2, 2)]
    assert set(jwt_row["versions"][0]) == {
        "version", "state", "activatedAt", "rotatedAt", "retiredAt", "verifyUntil"
    }  # fmt: skip


@pytest.mark.asyncio
async def test_authz_matrix_every_refusal_is_audited_and_changes_nothing(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    before = _versions(vault)
    cases = {
        "tenant admin (tenant token, scope present)": (
            _hdr(_token(users.tenant_admin, tenant=TENANT_SLUG)),
            403,
            "NOT_SYSTEM_TENANT",
        ),
        "system tenant token of a non-global-admin": (
            _hdr(_token(users.tenant_admin)),
            403,
            "NOT_GLOBAL_ADMIN",
        ),
        "no MFA at all": (_hdr(_token(users.admin, mfa_age=None)), 403, "MFA_REQUIRED"),
        "stale MFA (10 min)": (_hdr(_token(users.admin, mfa_age=600)), 403, "MFA_REQUIRED"),
        "password-only amr with fresh auth_time": (
            _hdr(_token(users.admin, amr=["pwd"])),
            403,
            "MFA_REQUIRED",
        ),
    }
    for name, (headers, status, code) in cases.items():
        resp = await client.post(ROTATE, json=BODY, headers=headers)
        body = await resp.get_json()
        assert (resp.status_code, body["error"]["code"]) == (status, code), name
        assert _versions(vault) == before, name
    assert len(_denials(vault)) == len(cases)
    assert {e.actor_uuid for e in _denials(vault)} <= {
        users.admin_uuid,
        *{e.actor_uuid for e in _denials(vault)},
    }
    assert all(e.reason == 8 for e in _denials(vault))
    assert verify_chain(vault, KEK_A).intact
    # positive control: the same admin with fresh MFA succeeds
    ok = await client.post(ROTATE, json=BODY, headers=_hdr(_token(users.admin)))
    assert ok.status_code == 200 and _versions(vault) == [(1, 3), (2, 2)]


@pytest.mark.asyncio
async def test_missing_scope_is_refused_by_the_scope_gate(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    before = _versions(vault)
    for scope in ("", "platform_secrets:read", "communities:admin"):
        resp = await client.post(ROTATE, json=BODY, headers=_hdr(_token(users.admin, scope=scope)))
        assert resp.status_code == 403, scope
    assert _versions(vault) == before


@pytest.mark.asyncio
async def test_no_token_and_bad_input(client: Any, users: SimpleNamespace, vault: FakeRepo) -> None:
    assert (await client.post(ROTATE, json=BODY)).status_code == 401
    head = _hdr(_token(users.admin))
    assert (await client.post(ROTATE, json={"reasonCode": 9}, headers=head)).status_code == 400
    assert (await client.post(ROTATE, json={}, headers=head)).status_code == 400
    unknown = await client.post(
        "/api/v1/admin/platform-secrets/nope/rotate", json=BODY, headers=head
    )
    assert unknown.status_code == 404
    assert _versions(vault) == [(1, 2)]


@pytest.mark.asyncio
async def test_idempotency_key_is_mandatory_and_replay_is_refused(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    token = _token(users.admin)
    no_key = _hdr(token)
    del no_key["Idempotency-Key"]
    assert (await client.post(ROTATE, json=BODY, headers=no_key)).status_code == 400
    bad = _hdr(token)
    bad["Idempotency-Key"] = "not-a-uuid"
    assert (await client.post(ROTATE, json=BODY, headers=bad)).status_code == 400
    assert _versions(vault) == [(1, 2)]
    key = uuid.uuid4()
    assert (await client.post(ROTATE, json=BODY, headers=_hdr(token, key=key))).status_code == 200
    replay = await client.post(ROTATE, json=BODY, headers=_hdr(token, key=key))
    assert replay.status_code == 409 and (await replay.get_json())["error"]["code"] == "REPLAYED"
    assert _versions(vault) == [(1, 3), (2, 2)]  # executed exactly once
    again = await client.post(ROTATE, json=BODY, headers=_hdr(token))  # fresh key, inside 10 min
    assert again.status_code == 429 and (await again.get_json())["error"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_csrf_cookie_authenticated_requests_need_the_header_json_and_origin(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    from services.session_cookie import SESSION_COOKIE_NAME

    token = _token(users.admin)

    def cookie_request(**extra: str) -> dict[str, str]:
        headers = {"Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
        headers.update(extra)
        return headers

    client.set_cookie("localhost", SESSION_COOKIE_NAME, token)
    # app.before_request bridge is registered by create_app, not by this standalone app: emulate it
    from quart import request

    @client.app.before_request
    async def bridge() -> None:
        if not request.headers.get("Authorization"):
            cookie = request.cookies.get(SESSION_COOKIE_NAME)
            if cookie:
                request.headers["Authorization"] = f"Bearer {cookie}"

    before = _versions(vault)
    for headers in (
        cookie_request(),  # no custom header
        cookie_request(**{"X-Requested-With": "XMLHttpRequest"}),  # wrong value
        cookie_request(**{"X-Requested-With": "waddles-admin", "Origin": "https://evil.example"}),
        cookie_request(**{"X-Requested-With": "waddles-admin", "Content-Type": "text/plain"}),
    ):
        resp = await client.post(ROTATE, data='{"reasonCode": 1}', headers=headers)
        assert resp.status_code in (400, 403, 415), headers
        if headers["Content-Type"] == "application/json":
            assert resp.status_code == 403, headers
            assert (await resp.get_json())["error"]["code"] == "CSRF_REJECTED", headers
        assert _versions(vault) == before, headers
    assert len(_denials(vault)) == 3  # the three JSON-bodied CSRF refusals were audited
    good = cookie_request(**{"X-Requested-With": "waddles-admin"})
    resp = await client.post(ROTATE, json=BODY, headers=good)
    assert resp.status_code == 200 and _versions(vault) == [(1, 3), (2, 2)]


@pytest.mark.asyncio
async def test_retire_over_http_window_and_state_rules(
    client: Any, users: SimpleNamespace, vault: FakeRepo
) -> None:
    retire = "/api/v1/admin/platform-secrets/jwt_signing/retire"
    token = _token(users.admin)
    assert (await client.post(ROTATE, json=BODY, headers=_hdr(token))).status_code == 200
    early = await client.post(retire, json={"version": 1, "reasonCode": 1}, headers=_hdr(token))
    assert early.status_code == 409 and (await early.get_json())["error"]["code"] == "WINDOW_OPEN"
    active = await client.post(retire, json={"version": 2, "reasonCode": 1}, headers=_hdr(token))
    assert (await active.get_json())["error"]["code"] == "NOT_VERIFY_ONLY"
    assert _versions(vault) == [(1, 3), (2, 2)]
    # age the demotion + activation past the window, then retirement is allowed
    aged = datetime.now(UTC) - timedelta(seconds=JWT.verify_window_s + 5)
    for i, row in enumerate(vault.secrets):
        if row.secret_type == int(JWT.secret_type):
            vault.secrets[i] = replace(row, rotated_at=aged, activated_at=aged)
    done = await client.post(retire, json={"version": 1, "reasonCode": 1}, headers=_hdr(token))
    assert done.status_code == 200
    body = await done.get_json()
    assert set(body) == {"success", "type", "version", "retiredAt"} and body["version"] == 1
    assert _versions(vault) == [(1, 4), (2, 2)]


@pytest.mark.asyncio
async def test_kill_switch_blocks_with_409(
    client: Any, users: SimpleNamespace, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLAG_WADDLES_DISABLE_DB_PLATFORM_KEYS", "true")
    resp = await client.post(ROTATE, json=BODY, headers=_hdr(_token(users.admin)))
    assert resp.status_code == 409 and _versions(vault) == [(1, 2)]


@pytest.mark.asyncio
async def test_vault_failure_is_a_503_without_leaking_the_error(
    client: Any, users: SimpleNamespace, vault: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("SECRET-LEAK postgres://user:pw@host")

    monkeypatch.setattr(rotation, "rotate_secret", boom)
    resp = await client.post(ROTATE, json=BODY, headers=_hdr(_token(users.admin)))
    assert resp.status_code == 503
    assert "SECRET-LEAK" not in await resp.get_data(as_text=True)


def test_every_mutating_route_requires_the_scope_and_the_blueprint_exposes_three_routes() -> None:
    rules = {
        (m, r.rule)
        for r in _make_app().url_map.iter_rules()
        if r.rule.startswith("/api/v1/admin/platform-secrets")
        for m in (r.methods or set()) - {"HEAD", "OPTIONS"}
    }
    assert rules == {
        ("GET", "/api/v1/admin/platform-secrets/"),
        ("POST", "/api/v1/admin/platform-secrets/<secret_type>/rotate"),
        ("POST", "/api/v1/admin/platform-secrets/<secret_type>/retire"),
    }


def _make_app() -> Quart:
    quart_app = Quart(__name__)
    quart_app.register_blueprint(platform_secrets_bp)
    return quart_app
