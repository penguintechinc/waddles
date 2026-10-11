"""Login-session lifecycle end to end: short access tokens, rotating refresh, revocation.

Drives the REAL routes (`blueprints/v1/auth.py`, the superadmin user routes, the passkey route)
through the REAL `flask_core.auth` verifier and revocation store, against real pydal tables
(`auth_db`). Browsers are modelled as separate test clients (each with its own cookie jar); a bare
`use_cookies=False` client is the "attacker with a stolen token" / "is this token alive" probe.

Covers the four acceptance behaviours of the session-revocation finding -- a revoked token is
rejected before `exp`, revoke-all cuts off every prior-`iat` token, the short TTL is enforced,
and a revocation-store outage behaves as `docs/TOKEN_REVOCATION.md` documents -- plus refresh
rotation, reuse detection, the parallel-refresh grace window and every revocation trigger.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
import pytest
from flask_core import token_revocation as tr
from flask_core.auth import ACCESS_TOKEN_TTL_SECONDS, verify_jwt_token
from quart import Quart
from quart_schema import QuartSchema

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from blueprints.v1.passkey import passkey_bp
from blueprints.v1.user_management import user_management_bp
from config import HubAPIConfig
from services import audit_http, session_service
from services.auth_service import SessionUser, create_session_token
from services.session_cookie import REFRESH_COOKIE_NAME, SESSION_COOKIE_NAME
from tests.conftest import SECRET_KEY, TENANT_SLUG, make_user_token

PASSWORD = "correcthorse"


def _config(**overrides: Any) -> HubAPIConfig:
    base = HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key=SECRET_KEY,
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.register_blueprint(user_management_bp)
    quart_app.register_blueprint(passkey_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _config()
    quart_app.before_request(bridge_session_cookie_to_bearer)
    return quart_app


@pytest.fixture
def browser(app: Quart) -> Any:
    """One browser: a test client with its own cookie jar."""
    return app.test_client()


@pytest.fixture
def probe(app: Quart) -> Any:
    """A cookie-less client: whatever it sends is exactly what a thief replaying a token sends."""
    return app.test_client(use_cookies=False)


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


def _seed_user(
    auth_db: Any,
    *,
    email: str = "alice@example.com",
    password: str | None = PASSWORD,
    is_super_admin: bool = False,
    is_active: bool = True,
) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=email.split("@")[0],
        password_hash=_hash(password) if password else None,
        is_active=is_active,
        email_verified=True,
        is_super_admin=is_super_admin,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _set_cookies(response: Any) -> dict[str, str]:
    """`name -> full Set-Cookie header` for every cookie the response set."""
    return {raw.split("=", 1)[0]: raw for raw in response.headers.getlist("Set-Cookie")}


def _cookie_value(response: Any, name: str) -> str | None:
    raw = _set_cookies(response).get(name)
    return raw.split("=", 1)[1].split(";", 1)[0] if raw else None


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})  # type: ignore[no-any-return]


def _sessions(auth_db: Any) -> list[Any]:
    return list(
        auth_db.dal(auth_db.dal.hub_sessions.id > 0).select(orderby=auth_db.dal.hub_sessions.id)
    )


async def _login(
    client: Any,
    email: str = "alice@example.com",
    password: str = PASSWORD,
    tenant: str | None = None,
) -> Any:
    body: dict[str, str] = {"email": email, "password": password}
    if tenant is not None:
        body["tenantSlug"] = tenant
    response = await client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200, await response.get_data()
    return response


async def _login_token(client: Any, **kwargs: Any) -> str:
    return str((await (await _login(client, **kwargs)).get_json())["token"])


async def _alive(probe: Any, token: str) -> bool:
    """Return whether `/auth/me` accepts this bearer token (`user: null` means it did not)."""
    response = await probe.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return (await response.get_json())["user"] is not None


def _refresh_headers(refresh_token: str) -> dict[str, str]:
    return {"Cookie": f"{REFRESH_COOKIE_NAME}={refresh_token}"}


class TestLoginIssuesSessionCredentials:
    async def test_login_sets_a_short_lived_access_cookie_and_a_path_scoped_refresh_cookie(
        self, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        cookies = _set_cookies(await _login(browser))
        access, refresh = cookies[SESSION_COOKIE_NAME], cookies[REFRESH_COOKIE_NAME]
        assert f"Max-Age={ACCESS_TOKEN_TTL_SECONDS}" in access
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/;"):
            assert attr in access + ";"
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/api/v1/auth"):
            assert attr in refresh
        assert f"Max-Age={_config().refresh_token_ttl_seconds}" in refresh

    async def test_access_token_is_short_lived_unique_and_bound_to_its_session(
        self, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        first = await _login_token(browser)
        second = await _login_token(browser)
        a, b = _claims(first), _claims(second)
        assert a["exp"] - a["iat"] == ACCESS_TOKEN_TTL_SECONDS <= 15 * 60
        assert a["jti"] != b["jti"]
        sessions = _sessions(auth_db)
        assert [str(s.id) for s in sessions] == [a["sid"], b["sid"]]
        assert verify_jwt_token(first, SECRET_KEY) is not None

    async def test_the_database_never_holds_a_bearer_credential(
        self, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        response = await _login(browser)
        access = (await response.get_json())["token"]
        refresh = _cookie_value(response, REFRESH_COOKIE_NAME)
        assert refresh is not None
        (row,) = _sessions(auth_db)
        stored = str(row.session_token)
        assert stored.startswith("rt1:") and len(stored) == len("rt1:") + 64
        nonce = refresh.split(".")[2]
        assert access not in stored and refresh not in stored and nonce not in stored

    async def test_registration_also_delivers_the_refresh_cookie(
        self, browser: Any, auth_db: Any
    ) -> None:
        for key, value in (("signup_enabled", "true"), ("email_configured", "true")):
            auth_db.dal.hub_settings.insert(setting_key=key, setting_value=value)
        auth_db.dal.commit()
        response = await browser.post(
            "/api/v1/auth/register", json={"email": "new@example.com", "password": "hunter22"}
        )
        assert response.status_code == 200
        assert REFRESH_COOKIE_NAME in _set_cookies(response)

    async def test_a_pending_link_session_has_no_refresh_and_no_session_row(
        self, app: Quart, auth_db: Any
    ) -> None:
        """Temp-password sessions (user=None) live only their short access TTL."""
        async with app.test_request_context("/"):
            token = await create_session_token(
                auth_db, auth_db.dal, _config(), user=None, username_override="guest"
            )
        assert "sid" not in _claims(token) and _claims(token)["sub"] == "0"
        assert _sessions(auth_db) == []

    async def test_no_request_context_still_issues_a_token_without_a_cookie(
        self, auth_db: Any
    ) -> None:
        user = SessionUser(id=_seed_user(auth_db), email="a@b.c", username="alice")
        token = await create_session_token(auth_db, auth_db.dal, _config(), user=user)
        assert _claims(token)["sid"] == str(_sessions(auth_db)[0].id)


class TestRefreshRotation:
    async def test_refresh_rotates_the_token_and_mints_a_fresh_access_token_in_the_session(
        self, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        login = await _login(browser)
        old_access = (await login.get_json())["token"]
        old_refresh = _cookie_value(login, REFRESH_COOKIE_NAME)
        (before,) = _sessions(auth_db)

        response = await browser.post("/api/v1/auth/refresh")
        assert response.status_code == 200
        new_access = (await response.get_json())["token"]
        new_refresh = _cookie_value(response, REFRESH_COOKIE_NAME)

        assert new_refresh and new_refresh != old_refresh
        assert _claims(new_access)["sid"] == _claims(old_access)["sid"]
        assert _claims(new_access)["jti"] != _claims(old_access)["jti"]
        assert _cookie_value(response, SESSION_COOKIE_NAME) == new_access
        (after,) = _sessions(auth_db)
        assert after.session_token != before.session_token and after.is_active
        assert verify_jwt_token(new_access, SECRET_KEY) is not None

    async def test_a_session_survives_many_rotations_without_a_forced_logout(
        self, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        await _login(browser)
        for _ in range(6):
            response = await browser.post("/api/v1/auth/refresh")
            assert response.status_code == 200
            assert verify_jwt_token((await response.get_json())["token"], SECRET_KEY) is not None
        assert len(_sessions(auth_db)) == 1

    async def test_refresh_without_a_cookie_is_400(self, probe: Any) -> None:
        assert (await probe.post("/api/v1/auth/refresh")).status_code == 400

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "x",
            "rt1.1.a.b.c",
            "rt1." + "9" * 20 + ".abc.abc.abc",
            "a" * 600,
            "rt1.1.%%%.dGVuYW50.AAAA",
        ],
    )
    async def test_garbage_refresh_tokens_are_401(self, probe: Any, junk: str) -> None:
        response = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(junk))
        assert response.status_code in (400, 401)

    async def test_a_forged_tag_is_refused_and_cannot_revoke_a_real_session(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        """A known session id is not enough to attack it; only a genuine token can reuse-revoke."""
        _seed_user(auth_db)
        login = await _login(browser)
        genuine = _cookie_value(login, REFRESH_COOKIE_NAME)
        assert genuine is not None
        prefix, sid, nonce, tenant, _tag = genuine.split(".")
        forged = ".".join([prefix, sid, "A" * 43, tenant, "B" * 43])
        wrong_tenant = ".".join([prefix, sid, nonce, "b3RoZXItY29ycA", _tag])

        for bad in (forged, wrong_tenant):
            response = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(bad))
            assert response.status_code == 401
        (row,) = _sessions(auth_db)
        assert row.is_active and row.revoked_at is None
        ok = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(genuine))
        assert ok.status_code == 200  # the real token is untouched by the forgery attempts

    async def test_a_valid_tag_for_a_missing_session_is_401(self, probe: Any, auth_db: Any) -> None:
        token = session_service.build_refresh_token(
            SECRET_KEY, session_id=424242, nonce="n" * 43, tenant_slug=TENANT_SLUG
        )
        response = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        assert response.status_code == 401

    async def test_refresh_re_reads_roles_so_a_promotion_and_demotion_apply(
        self, browser: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        await _login(browser)
        plain = (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        assert "users:admin" not in _claims(plain)["scope"]

        auth_db.dal(auth_db.dal.hub_users.id == user_id).update(is_super_admin=True)
        auth_db.dal.commit()
        promoted = (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        assert "users:admin" in _claims(promoted)["scope"].split()

        auth_db.dal(auth_db.dal.hub_users.id == user_id).update(is_super_admin=False)
        auth_db.dal.commit()
        demoted = (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        assert "users:admin" not in _claims(demoted)["scope"].split()

    async def test_refresh_keeps_a_tenant_owners_tenant_scopes(
        self, browser: Any, auth_db: Any
    ) -> None:
        """The old refresh dropped `tenant_id`, silently downgrading tenant owners to viewers."""
        user_id = _seed_user(auth_db)
        tenant = auth_db.dal(auth_db.dal.tenants.slug == TENANT_SLUG).select().first()
        auth_db.dal.tenant_admins.insert(
            tenant_id=tenant.id, user_id=user_id, role="tenant-owner", created_at=datetime.now(UTC)
        )
        auth_db.dal.commit()
        response = await browser.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": PASSWORD, "tenantSlug": TENANT_SLUG},
        )
        assert response.status_code == 200
        login_claims = _claims((await response.get_json())["token"])
        assert login_claims["tenant"] == TENANT_SLUG
        refreshed = _claims(
            (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        )
        assert refreshed["tenant"] == TENANT_SLUG
        assert "tenant:admin" in refreshed["scope"].split()
        assert refreshed["scope"] == login_claims["scope"]

    async def test_an_inactive_user_cannot_refresh_and_the_session_is_revoked(
        self, browser: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        await _login(browser)
        auth_db.dal(auth_db.dal.hub_users.id == user_id).update(is_active=False)
        auth_db.dal.commit()
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 401
        (row,) = _sessions(auth_db)
        assert not row.is_active and row.revoked_at is not None

    async def test_an_idle_expired_session_cannot_refresh(self, browser: Any, auth_db: Any) -> None:
        _seed_user(auth_db)
        await _login(browser)
        auth_db.dal(auth_db.dal.hub_sessions.id > 0).update(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        auth_db.dal.commit()
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 401

    async def test_the_absolute_cap_ends_a_session_no_matter_how_active(
        self, browser: Any, auth_db: Any, app: Quart
    ) -> None:
        _seed_user(auth_db)
        await _login(browser)
        cap = app.config["HUB_API_CONFIG"].session_absolute_ttl_seconds
        auth_db.dal(auth_db.dal.hub_sessions.id > 0).update(
            created_at=datetime.now(UTC) - timedelta(seconds=cap + 5),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        auth_db.dal.commit()
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 401

    async def test_the_sliding_window_never_extends_past_the_absolute_cap(
        self, browser: Any, auth_db: Any, app: Quart
    ) -> None:
        _seed_user(auth_db)
        await _login(browser)
        cfg = app.config["HUB_API_CONFIG"]
        created = datetime.now(UTC) - timedelta(seconds=cfg.session_absolute_ttl_seconds - 3600)
        auth_db.dal(auth_db.dal.hub_sessions.id > 0).update(created_at=created)
        auth_db.dal.commit()
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 200
        (row,) = _sessions(auth_db)
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        assert expires <= created + timedelta(seconds=cfg.session_absolute_ttl_seconds) + timedelta(
            seconds=2
        )


class TestReuseDetection:
    async def test_replaying_a_rotated_token_after_the_grace_window_revokes_the_session(
        self,
        app: Quart,
        browser: Any,
        probe: Any,
        auth_db: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        app.config["HUB_API_CONFIG"] = _config(refresh_reuse_grace_seconds=0)
        _seed_user(auth_db)
        login = await _login(browser)
        stolen = _cookie_value(login, REFRESH_COOKIE_NAME)
        assert stolen is not None
        rotated = await browser.post("/api/v1/auth/refresh")  # the legitimate client moves on
        live_access = (await rotated.get_json())["token"]
        live_refresh = _cookie_value(rotated, REFRESH_COOKIE_NAME)
        assert await _alive(probe, live_access)

        with caplog.at_level(logging.ERROR, logger="services.session_service"):
            replay = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(stolen))
        assert replay.status_code == 401
        assert any("reuse detected" in r.getMessage() for r in caplog.records)

        (row,) = _sessions(auth_db)
        assert not row.is_active and row.revoked_at is not None
        # the legitimate client's NEWER credentials died with the session (chain revoked) ...
        again = await probe.post(
            "/api/v1/auth/refresh", headers=_refresh_headers(live_refresh or "")
        )
        assert again.status_code == 401
        # ... and so did the access token it was holding, immediately, long before its exp.
        assert not await _alive(probe, live_access)

    async def test_reuse_does_not_touch_the_users_other_sessions(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        app.config["HUB_API_CONFIG"] = _config(refresh_reuse_grace_seconds=0)
        _seed_user(auth_db)
        phone = app.test_client()
        phone_access = await _login_token(phone)
        stolen = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        await browser.post("/api/v1/auth/refresh")
        assert (
            await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(stolen))
        ).status_code == 401
        assert await _alive(probe, phone_access)
        assert (await phone.post("/api/v1/auth/refresh")).status_code == 200

    async def test_parallel_refreshes_from_one_page_do_not_read_as_theft(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        """Five requests 401 at the same expiry and all refresh with the same cookie."""
        _seed_user(auth_db)
        token = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        results = await asyncio.gather(
            *(probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token)) for _ in range(5))
        )
        assert [r.status_code for r in results] == [200] * 5
        rotated = [r for r in results if REFRESH_COOKIE_NAME in _set_cookies(r)]
        assert len(rotated) == 1  # exactly one caller won the rotation; the rest got access only
        (row,) = _sessions(auth_db)
        assert row.is_active and row.revoked_at is None
        winner_cookie = _cookie_value(rotated[0], REFRESH_COOKIE_NAME) or ""
        assert (
            await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(winner_cookie))
        ).status_code == 200

    async def test_a_grace_replay_hands_out_access_only_and_never_a_second_refresh_token(
        self, probe: Any, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        token = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        first = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        replay = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        assert first.status_code == replay.status_code == 200
        assert REFRESH_COOKIE_NAME in _set_cookies(first)
        assert REFRESH_COOKIE_NAME not in _set_cookies(replay)
        assert SESSION_COOKIE_NAME in _set_cookies(replay)

    async def test_an_undecidable_replay_is_refused_without_revoking_the_session(
        self,
        app: Quart,
        browser: Any,
        probe: Any,
        auth_db: Any,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the marker store is down we cannot tell a racing page from a thief: 401, no revoke."""
        _seed_user(auth_db)
        token = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))

        def down(_name: str) -> bool:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation, "has_marker", down)
        replay = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        assert replay.status_code == 401
        (row,) = _sessions(auth_db)
        assert row.is_active and row.revoked_at is None


class TestLogout:
    async def test_logout_kills_the_presented_access_token_before_its_exp(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        access = await _login_token(browser)
        claims = _claims(access)
        assert claims["exp"] > time.time() + 60  # minutes of life left
        assert await _alive(probe, access)

        response = await browser.post("/api/v1/auth/logout")
        assert response.status_code == 200
        assert not await _alive(probe, access)
        assert verify_jwt_token(access, SECRET_KEY) is None

    async def test_logout_clears_both_cookies_and_ends_refresh(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        refresh = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        response = await browser.post("/api/v1/auth/logout")
        cookies = _set_cookies(response)
        for name in (SESSION_COOKIE_NAME, REFRESH_COOKIE_NAME):
            assert "Max-Age=0" in cookies[name] or "expires=" in cookies[name].lower()
        (row,) = _sessions(auth_db)
        assert not row.is_active and row.revoked_at is not None
        stale = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(refresh))
        assert stale.status_code == 401

    async def test_logout_also_kills_the_sessions_earlier_access_tokens(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        """A token from before the last rotation still had minutes left -- not any more."""
        _seed_user(auth_db)
        earlier = await _login_token(browser)
        later = (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        assert await _alive(probe, earlier) and await _alive(probe, later)
        await browser.post("/api/v1/auth/logout")
        assert not await _alive(probe, earlier)
        assert not await _alive(probe, later)

    async def test_logout_works_from_the_refresh_cookie_alone(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        """The access cookie has already expired in the browser; the user can still sign out."""
        _seed_user(auth_db)
        login = await _login(browser)
        access = (await login.get_json())["token"]
        refresh = _cookie_value(login, REFRESH_COOKIE_NAME) or ""
        response = await probe.post("/api/v1/auth/logout", headers=_refresh_headers(refresh))
        assert response.status_code == 200
        assert not await _alive(probe, access)  # killed by session id, though never presented
        (row,) = _sessions(auth_db)
        assert not row.is_active

    async def test_logging_out_one_device_leaves_the_other_signed_in(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        phone = app.test_client()
        phone_access = await _login_token(phone)
        await _login(browser)
        await browser.post("/api/v1/auth/logout")
        assert await _alive(probe, phone_access)
        assert (await phone.post("/api/v1/auth/refresh")).status_code == 200

    async def test_logout_with_no_or_junk_credentials_is_a_harmless_success(
        self, probe: Any, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        access = await _login_token(browser)
        assert (await probe.post("/api/v1/auth/logout")).status_code == 200
        junk = await probe.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer not.a.jwt", **_refresh_headers("rt1.1.x.y.z")},
        )
        assert junk.status_code == 200
        assert await _alive(probe, access)
        assert _sessions(auth_db)[0].is_active

    async def test_a_dead_revocation_store_never_blocks_logout_and_refresh_still_dies(
        self,
        browser: Any,
        probe: Any,
        auth_db: Any,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Database half still stops refresh; the access token merely runs out its <= 15 minutes."""
        _seed_user(auth_db)
        login = await _login(browser)
        refresh = _cookie_value(login, REFRESH_COOKIE_NAME) or ""

        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation._backend, "put_max", down)
        response = await browser.post("/api/v1/auth/logout")
        assert response.status_code == 200
        (row,) = _sessions(auth_db)
        assert not row.is_active
        stale = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(refresh))
        assert stale.status_code == 401


class TestLogoutAll:
    async def test_signs_out_every_device_and_cuts_off_every_token_issued_so_far(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        other = _seed_user(auth_db, email="bob@example.com")
        phone = app.test_client()
        tokens = [await _login_token(phone), await _login_token(browser)]
        bob = await _login_token(app.test_client(), email="bob@example.com")
        refresh = _cookie_value(await _login(app.test_client()), REFRESH_COOKIE_NAME) or ""
        await asyncio.sleep(1.1)  # sessions opened strictly before the revoke second

        response = await browser.post("/api/v1/auth/logout-all")
        assert response.status_code == 200
        for token in tokens:
            assert not await _alive(probe, token)
        stale = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(refresh))
        assert stale.status_code == 401
        assert await _alive(probe, bob)  # another user is untouched
        mine = [s for s in _sessions(auth_db) if s.user_id != other]
        assert mine and all(not s.is_active for s in mine)

    async def test_requires_authentication(self, probe: Any) -> None:
        assert (await probe.post("/api/v1/auth/logout-all")).status_code == 401


class TestRevokeAllCutsOffPriorTokens:
    async def test_every_prior_iat_token_dies_and_a_fresh_login_in_the_same_second_lives(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        minted = [make_user_token(user_id=user_id) for _ in range(5)]  # no session row at all
        assert all([await _alive(probe, t) for t in minted])

        outcome = await session_service.revoke_user_sessions(
            auth_db, auth_db.dal, user_id=user_id, reason="test"
        )
        assert outcome.access_tokens_recorded
        assert not any([await _alive(probe, t) for t in minted])

        # No sleep: the very next login, in the revoke second, is stamped past the cutoff.
        fresh = await _login_token(browser)
        assert await _alive(probe, fresh)
        refreshed = (await (await browser.post("/api/v1/auth/refresh")).get_json())["token"]
        assert await _alive(probe, refreshed)


class TestPasswordChange:
    async def test_a_password_change_ends_every_session_including_this_one(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        phone = app.test_client()
        phone_access = await _login_token(phone)
        access = await _login_token(browser)
        phone_refresh = _cookie_value(await _login(app.test_client()), REFRESH_COOKIE_NAME) or ""

        response = await browser.post(
            "/api/v1/auth/password",
            json={"currentPassword": PASSWORD, "newPassword": "a-brand-new-password"},
        )
        assert response.status_code == 200
        for name in (SESSION_COOKIE_NAME, REFRESH_COOKIE_NAME):
            assert name in _set_cookies(response)  # cleared
        assert not await _alive(probe, access)
        assert not await _alive(probe, phone_access)
        stale = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(phone_refresh))
        assert stale.status_code == 401
        assert all(not s.is_active for s in _sessions(auth_db))

    async def test_signing_back_in_straight_away_with_the_new_password_works(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        """Same-second login after the change: the new token must not be swept by the cutoff."""
        _seed_user(auth_db)
        await _login(browser)
        await browser.post(
            "/api/v1/auth/password",
            json={"currentPassword": PASSWORD, "newPassword": "a-brand-new-password"},
        )
        fresh = await _login_token(browser, password="a-brand-new-password")
        assert await _alive(probe, fresh)

    async def test_a_wrong_current_password_revokes_nothing(
        self, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        access = await _login_token(browser)
        response = await browser.post(
            "/api/v1/auth/password",
            json={"currentPassword": "not-the-password", "newPassword": "a-brand-new-password"},
        )
        assert response.status_code == 401
        assert await _alive(probe, access)


class TestAuthenticatorChangesKeepTheCurrentSession:
    async def test_removing_a_passkey_signs_out_the_other_devices_only(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        passkey_id = auth_db.dal.user_passkeys.insert(
            user_id=user_id, credential_id="cred-1", public_key="pk", device_name="key"
        )
        auth_db.dal.commit()
        phone = app.test_client()
        phone_access = await _login_token(phone, tenant=TENANT_SLUG)
        mine = await _login_token(browser, tenant=TENANT_SLUG)

        response = await browser.delete(
            f"/api/v1/user/passkey/credentials/{passkey_id}",
            headers={"Authorization": f"Bearer {mine}"},
        )
        assert response.status_code == 200
        assert await _alive(probe, mine)  # the device doing the removal stays signed in
        assert not await _alive(probe, phone_access)
        assert (await phone.post("/api/v1/auth/refresh")).status_code == 401
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 200

    async def test_removing_nothing_revokes_nothing(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        phone_access = await _login_token(app.test_client(), tenant=TENANT_SLUG)
        mine = await _login_token(browser, tenant=TENANT_SLUG)
        response = await browser.delete(
            "/api/v1/user/passkey/credentials/999", headers={"Authorization": f"Bearer {mine}"}
        )
        assert response.status_code == 200
        assert await _alive(probe, phone_access)

    async def test_unlinking_an_identity_signs_out_the_other_devices_only(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        auth_db.dal.hub_user_identities.insert(
            hub_user_id=user_id,
            platform="discord",
            platform_user_id="d-1",
            linked_at=datetime.now(UTC),
        )
        auth_db.dal.commit()
        phone_access = await _login_token(app.test_client())
        mine = await _login_token(browser)
        response = await browser.delete(
            "/api/v1/auth/oauth/discord", headers={"Authorization": f"Bearer {mine}"}
        )
        assert response.status_code == 200
        assert await _alive(probe, mine)
        assert not await _alive(probe, phone_access)


class TestMeSilentRenewal:
    async def test_an_expired_access_cookie_is_renewed_from_the_refresh_cookie(
        self, app: Quart, browser: Any, auth_db: Any
    ) -> None:
        """SPA load after 15 minutes: the access cookie is gone, the refresh cookie is not."""
        _seed_user(auth_db)
        refresh = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        returning = app.test_client(use_cookies=False)
        response = await returning.get("/api/v1/auth/me", headers=_refresh_headers(refresh))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["email"] == "alice@example.com"
        assert SESSION_COOKIE_NAME in _set_cookies(response)
        assert REFRESH_COOKIE_NAME in _set_cookies(response)

    async def test_a_revoked_refresh_cookie_is_simply_logged_out(
        self, app: Quart, browser: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db)
        refresh = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        await browser.post("/api/v1/auth/logout")
        returning = app.test_client(use_cookies=False)
        response = await returning.get("/api/v1/auth/me", headers=_refresh_headers(refresh))
        assert response.status_code == 200
        assert (await response.get_json())["user"] is None
        assert SESSION_COOKIE_NAME not in _set_cookies(response)

    async def test_no_cookies_at_all_is_logged_out(self, probe: Any) -> None:
        response = await probe.get("/api/v1/auth/me")
        assert (await response.get_json())["user"] is None


def _admin_headers(*, user_id: int = 1) -> dict[str, str]:
    scope = "*:read *:write *:admin *:delete settings:write users:admin"
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope)}"}


class TestAdminRevocation:
    async def test_list_sessions_exposes_ids_and_timestamps_never_credentials(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        await _login(browser)
        response = await probe.get(
            f"/api/v1/superadmin/users/{user_id}/sessions", headers=_admin_headers(user_id=99)
        )
        assert response.status_code == 200
        (session,) = (await response.get_json())["sessions"]
        assert set(session) == {"id", "createdAt", "expiresAt", "isActive", "revokedAt"}
        assert session["isActive"] is True

    async def test_revoke_one_session_ends_that_session_only(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        phone = app.test_client()
        phone_access = await _login_token(phone)
        mine = await _login_token(browser)
        target = int(_claims(phone_access)["sid"])

        response = await probe.post(
            f"/api/v1/superadmin/users/{user_id}/sessions/{target}/revoke",
            headers=_admin_headers(user_id=99),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "sessionsRevoked": 1,
            "accessTokensRevokedImmediately": True,
        }
        assert not await _alive(probe, phone_access)  # before its exp, immediately
        assert (await phone.post("/api/v1/auth/refresh")).status_code == 401
        assert await _alive(probe, mine)

    async def test_a_session_of_another_user_is_404(
        self, app: Quart, probe: Any, auth_db: Any
    ) -> None:
        alice = _seed_user(auth_db)
        _seed_user(auth_db, email="bob@example.com")
        bob_access = await _login_token(app.test_client(), email="bob@example.com")
        response = await probe.post(
            f"/api/v1/superadmin/users/{alice}/sessions/{_claims(bob_access)['sid']}/revoke",
            headers=_admin_headers(user_id=99),
        )
        assert response.status_code == 404
        assert await _alive(probe, bob_access)

    async def test_revoke_user_ends_every_session_and_every_token(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        tokens = [await _login_token(browser), await _login_token(app.test_client())]
        minted_elsewhere = make_user_token(user_id=user_id)
        response = await probe.post(
            f"/api/v1/superadmin/users/{user_id}/revoke-sessions",
            headers=_admin_headers(user_id=99),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["sessionsRevoked"] == 2 and body["accessTokensRevokedImmediately"] is True
        for token in [*tokens, minted_elsewhere]:
            assert not await _alive(probe, token)

    async def test_with_the_store_down_refresh_still_dies_and_the_caller_is_told(
        self,
        browser: Any,
        probe: Any,
        auth_db: Any,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id = _seed_user(auth_db)
        await _login(browser)

        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation._backend, "put_max", down)
        response = await probe.post(
            f"/api/v1/superadmin/users/{user_id}/revoke-sessions",
            headers=_admin_headers(user_id=99),
        )
        body = await response.get_json()
        assert response.status_code == 200
        assert body["sessionsRevoked"] == 1 and body["accessTokensRevokedImmediately"] is False
        assert (await browser.post("/api/v1/auth/refresh")).status_code == 401

    async def test_unknown_user_is_404(self, probe: Any) -> None:
        response = await probe.post(
            "/api/v1/superadmin/users/424242/revoke-sessions", headers=_admin_headers(user_id=99)
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/superadmin/users/1/sessions"),
            ("POST", "/api/v1/superadmin/users/1/revoke-sessions"),
            ("POST", "/api/v1/superadmin/users/1/sessions/1/revoke"),
        ],
    )
    async def test_requires_the_users_admin_scope(
        self, probe: Any, auth_db: Any, method: str, path: str
    ) -> None:
        _seed_user(auth_db)
        no_token = await probe.open(path, method=method)
        assert no_token.status_code == 401
        weak = {"Authorization": f"Bearer {make_user_token(user_id=5, scope='community:read')}"}
        assert (await probe.open(path, method=method, headers=weak)).status_code == 403

    async def test_deactivating_a_user_cuts_off_their_tokens(
        self, app: Quart, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        access = await _login_token(app.test_client())
        response = await probe.put(
            f"/api/v1/superadmin/users/{user_id}",
            headers=_admin_headers(user_id=99),
            json={"isActive": False},
        )
        assert response.status_code == 200
        assert not await _alive(probe, access)

    async def test_deleting_a_user_cuts_off_their_tokens(
        self, app: Quart, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        access = await _login_token(app.test_client())
        response = await probe.delete(
            f"/api/v1/superadmin/users/{user_id}", headers=_admin_headers(user_id=99)
        )
        assert response.status_code == 200
        assert not await _alive(probe, access)

    async def test_demoting_a_super_admin_cuts_off_the_admin_scoped_tokens(
        self, app: Quart, probe: Any, auth_db: Any
    ) -> None:
        caller = _seed_user(auth_db, email="root@example.com", is_super_admin=True)
        target = _seed_user(auth_db, email="admin2@example.com", is_super_admin=True)
        admin_token = await _login_token(
            app.test_client(), email="admin2@example.com", tenant=TENANT_SLUG
        )
        assert "users:admin" in _claims(admin_token)["scope"]
        caller_headers = {
            "Authorization": "Bearer "
            + await _login_token(app.test_client(), email="root@example.com", tenant=TENANT_SLUG)
        }
        response = await probe.post(
            f"/api/v1/superadmin/users/{target}/super-admin-role",
            headers=caller_headers,
            json={"grant": False},
        )
        assert response.status_code == 200, caller
        assert not await _alive(probe, admin_token)
        assert await _alive(probe, caller_headers["Authorization"].removeprefix("Bearer "))


class TestShortTtlIsEnforcedAtTheApi:
    async def test_a_day_long_token_is_refused_even_though_its_signature_is_good(
        self, probe: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        now = int(time.time())
        legacy = jwt.encode(
            {
                "sub": str(user_id),
                "tenant": TENANT_SLUG,
                "scope": "*:read",
                "iss": "waddlebot",
                "aud": "waddlebot-services",
                "iat": now,
                "exp": now + 24 * 3600,
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        assert not await _alive(probe, legacy)


class TestSessionAudit:
    """Revocations reach the audit log with a closed vocabulary and never a credential."""

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def find_tenant(self, *, tenant_id: int | None, tenant_slug: str | None) -> Any:
            return (1, TENANT_SLUG) if tenant_slug == TENANT_SLUG else None

        async def record(self, event: Any) -> None:
            self.events.append(event)

    @pytest.fixture
    def recorder(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        recorder = self._Recorder()
        monkeypatch.setattr(audit_http, "get_service", lambda: recorder)
        return recorder

    async def test_logout_password_change_and_reuse_are_each_audited(
        self, app: Quart, browser: Any, probe: Any, auth_db: Any, recorder: Any
    ) -> None:
        app.config["HUB_API_CONFIG"] = _config(refresh_reuse_grace_seconds=0)
        user_id = _seed_user(auth_db)

        await _login(browser)
        await browser.post("/api/v1/auth/logout")

        stolen = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""
        await browser.post("/api/v1/auth/refresh")
        await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(stolen))

        await _login(browser)
        await browser.post(
            "/api/v1/auth/password",
            json={"currentPassword": PASSWORD, "newPassword": "a-brand-new-password"},
        )
        revoked = [e for e in recorder.events if e.action == "authn.session_revoked"]
        assert [e.details["reason"] for e in revoked] == [
            "logout",
            "refresh_reuse",
            "password_changed",
        ]
        assert revoked[0].actor_user_id == user_id and revoked[0].target_id is not None
        assert revoked[1].actor_user_id is None and revoked[1].actor_kind.value == "system"
        assert revoked[2].details["kind"] == "user"
        rendered = " ".join(repr(e) for e in revoked)
        assert "rt1." not in rendered and SECRET_KEY not in rendered

    async def test_an_unrecordable_audit_event_never_undoes_or_blocks_the_revocation(
        self,
        browser: Any,
        probe: Any,
        auth_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from services.audit_service import AuditWriteError

        class Down:
            async def find_tenant(self, **_k: Any) -> Any:
                return None

            async def record(self, _event: Any) -> None:
                raise AuditWriteError("audit down")

        _seed_user(auth_db)
        access = await _login_token(browser)  # issuance is fail-closed on audit; log in first
        monkeypatch.setattr(audit_http, "get_service", lambda: Down())
        with caplog.at_level(logging.ERROR, logger="services.audit_http"):
            response = await browser.post("/api/v1/auth/logout")
        assert response.status_code == 200
        assert not await _alive(probe, access)
        assert any("audit event NOT recorded" in r.getMessage() for r in caplog.records)

    async def test_no_audit_service_is_a_quiet_noop(self, browser: Any, auth_db: Any) -> None:
        _seed_user(auth_db)
        await _login(browser)
        assert (await browser.post("/api/v1/auth/logout")).status_code == 200


class TestRefreshTokenFormat:
    """The wire format is a contract with `docs/TOKEN_REVOCATION.md`; pin its properties."""

    def test_round_trip_and_tenant_binding(self) -> None:
        token = session_service.build_refresh_token(
            SECRET_KEY, session_id=7, nonce="n" * 43, tenant_slug="acme-corp"
        )
        parsed = session_service.parse_refresh_token(SECRET_KEY, token)
        assert parsed is not None
        assert (parsed.session_id, parsed.nonce, parsed.tenant_slug) == (7, "n" * 43, "acme-corp")

    def test_another_secret_cannot_authenticate_it(self) -> None:
        token = session_service.build_refresh_token(
            SECRET_KEY, session_id=7, nonce="n" * 43, tenant_slug="acme-corp"
        )
        assert session_service.parse_refresh_token("some-other-secret", token) is None

    def test_every_field_is_authenticated(self) -> None:
        token = session_service.build_refresh_token(
            SECRET_KEY, session_id=7, nonce="n" * 43, tenant_slug="acme-corp"
        )
        version, sid, nonce, tenant, tag = token.split(".")
        for tampered in (
            ".".join([version, "8", nonce, tenant, tag]),
            ".".join([version, sid, "m" * 43, tenant, tag]),
            ".".join([version, sid, nonce, "b3RoZXI", tag]),
            ".".join(["rt2", sid, nonce, tenant, tag]),
        ):
            assert session_service.parse_refresh_token(SECRET_KEY, tampered) is None

    def test_the_tag_key_is_not_the_jwt_signing_key(self) -> None:
        """A refresh tag must never be usable as (or confused with) an HS256 signature."""
        import hashlib
        import hmac

        token = session_service.build_refresh_token(
            SECRET_KEY, session_id=7, nonce="n" * 43, tenant_slug="acme-corp"
        )
        _v, sid, nonce, tenant, tag = token.split(".")
        naive = hmac.new(SECRET_KEY.encode(), f"{sid}.{nonce}.{tenant}".encode(), hashlib.sha256)
        import base64

        assert tag != base64.urlsafe_b64encode(naive.digest()).rstrip(b"=").decode()

    @pytest.mark.parametrize("raw", [None, 5, b"rt1.1.a.b.c", "", " ", "rt1"])
    def test_non_strings_and_fragments_do_not_parse(self, raw: Any) -> None:
        assert session_service.parse_refresh_token(SECRET_KEY, raw) is None

    def test_an_empty_secret_refuses_to_mint(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            session_service.build_refresh_token("", session_id=1, nonce="n" * 43, tenant_slug="t")


class TestSessionConfig:
    def test_defaults(self) -> None:
        cfg = _config()
        assert cfg.refresh_token_ttl_seconds == 7 * 24 * 3600
        assert cfg.session_absolute_ttl_seconds == 30 * 24 * 3600
        assert cfg.refresh_reuse_grace_seconds == 10

    def test_idle_window_cannot_exceed_the_absolute_cap(self) -> None:
        with pytest.raises(ValueError, match="cannot exceed"):
            _config(refresh_token_ttl_seconds=10_000, session_absolute_ttl_seconds=5_000)

    @pytest.mark.parametrize(
        ("raw", "ok"),
        [("900", True), ("604800", True), ("899", False), ("7776001", False), ("weekly", False)],
    )
    def test_env_values_are_range_checked(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, ok: bool
    ) -> None:
        from config import _bounded_int_env

        monkeypatch.setenv("REFRESH_TOKEN_TTL_SECONDS", raw)
        if ok:
            assert _bounded_int_env(
                "REFRESH_TOKEN_TTL_SECONDS", 1, minimum=900, maximum=7_776_000
            ) == int(raw)
        else:
            with pytest.raises(ValueError, match="REFRESH_TOKEN_TTL_SECONDS"):
                _bounded_int_env("REFRESH_TOKEN_TTL_SECONDS", 1, minimum=900, maximum=7_776_000)

    def test_blank_env_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from config import _bounded_int_env

        monkeypatch.setenv("REFRESH_REUSE_GRACE_SECONDS", "  ")
        assert _bounded_int_env("REFRESH_REUSE_GRACE_SECONDS", 10, minimum=0, maximum=60) == 10


class TestRefreshTokenEdgeCases:
    """The remaining branches of the token parser and the best-effort store interactions."""

    def test_a_tenant_field_that_is_not_utf8_or_is_oversized_does_not_authenticate(self) -> None:
        import base64
        import hashlib
        import hmac

        def forge(tenant_bytes: bytes) -> str:
            """A tag-VALID token whose tenant field is hostile (needs the real key to build)."""
            tenant_b64 = base64.urlsafe_b64encode(tenant_bytes).rstrip(b"=").decode()
            key = hmac.new(
                SECRET_KEY.encode(), b"waddles.refresh-token.v1", hashlib.sha256
            ).digest()
            msg = f"7.{'n' * 43}.{tenant_b64}".encode()
            tag = base64.urlsafe_b64encode(hmac.new(key, msg, hashlib.sha256).digest())
            return f"rt1.7.{'n' * 43}.{tenant_b64}.{tag.rstrip(b'=').decode()}"

        assert session_service.parse_refresh_token(SECRET_KEY, forge(b"\xff\xfe\xfd")) is None
        assert session_service.parse_refresh_token(SECRET_KEY, forge(b"t" * 129)) is None
        assert session_service.parse_refresh_token(SECRET_KEY, forge(b"acme")) is not None

    async def test_a_dead_marker_store_at_rotation_does_not_undo_the_rotation(
        self,
        browser: Any,
        probe: Any,
        auth_db: Any,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No marker means a parallel refresh is refused later; the rotation itself still stands."""
        _seed_user(auth_db)
        token = _cookie_value(await _login(browser), REFRESH_COOKIE_NAME) or ""

        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation, "put_marker", down)
        first = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        assert first.status_code == 200 and REFRESH_COOKIE_NAME in _set_cookies(first)
        monkeypatch.undo()
        # Without a marker the stale token cannot be told from theft: refused (and revoked).
        replay = await probe.post("/api/v1/auth/refresh", headers=_refresh_headers(token))
        assert replay.status_code == 401

    async def test_a_dead_store_while_revoking_a_single_token_is_reported_not_raised(
        self,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation._backend, "put_max", down)
        assert await session_service.revoke_access_token("a" * 32, None) is False

    async def test_revoke_other_sessions_with_a_dead_store_still_ends_them_in_the_database(
        self,
        app: Quart,
        auth_db: Any,
        isolated_token_revocation: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id = _seed_user(auth_db)
        phone_access = await _login_token(app.test_client())
        mine = int(_claims(await _login_token(app.test_client()))["sid"])

        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation._backend, "put_max", down)
        outcome = await session_service.revoke_other_sessions(
            auth_db, auth_db.dal, user_id=user_id, keep_session_id=mine, reason="test"
        )
        assert outcome.sessions_revoked == 1 and outcome.access_tokens_recorded is False
        by_id = {s.id: s for s in _sessions(auth_db)}
        assert not by_id[int(_claims(phone_access)["sid"])].is_active and by_id[mine].is_active

    async def test_revoking_other_sessions_when_there_are_none_is_a_clean_noop(
        self, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        outcome = await session_service.revoke_other_sessions(
            auth_db, auth_db.dal, user_id=user_id, keep_session_id=None, reason="test"
        )
        assert (outcome.sessions_revoked, outcome.access_tokens_recorded) == (0, True)

    async def test_a_broken_meter_never_breaks_a_login(
        self, browser: Any, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Boom:
            def add(self, *_a: Any, **_k: Any) -> None:
                raise RuntimeError("meter down")

        monkeypatch.setattr(session_service, "_session_events", Boom())
        _seed_user(auth_db)
        assert (await _login(browser)).status_code == 200

    async def test_a_session_without_an_owner_has_no_owner(self, auth_db: Any) -> None:
        assert await session_service.get_session_owner(auth_db, auth_db.dal, session_id=999) is None
        auth_db.dal.hub_sessions.insert(
            session_token="legacy", user_id=None, is_active=True, created_at=datetime.now(UTC)
        )
        auth_db.dal.commit()
        sid = int(_sessions(auth_db)[0].id)
        assert await session_service.get_session_owner(auth_db, auth_db.dal, session_id=sid) is None
