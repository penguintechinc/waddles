"""YouTube install security: tenant-level app ownership and browser-bound OAuth state.

Two review findings, pinned as failing-first tests:

* the Google OAuth application is TENANT-level -- a community admin must not be able to write or
  overwrite it (only ``tenant:admin`` via ``PUT /tenants/youtube/app``);
* the public callback must only complete in the browser that started the flow (``__Host-`` cookie
  nonce, hash in the server-side state), without an oracle and without burning the state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from flask_core.auth import DEFAULT_TENANT_SLUG

from tests.provider_connections.conftest import admin_headers
from tests.provider_connections.fake_google import APP_CLIENT_ID, APP_CLIENT_SECRET, FakeGoogle
from tests.provider_connections.test_youtube_flow import (
    CALLBACK,
    OWN_APP,
    _authorize,
    _error,
    _install,
    _ok,
    _params,
)
from tests.provider_connections.youtube_fixtures import YT_CHANNEL, FakeRedis

COOKIE = "__Host-yt_install"


class TestTenantApp:
    """The OAuth application is tenant-level: only a tenant admin may write it."""

    URL = "/api/v1/tenants/youtube/app"

    @staticmethod
    def _tenant_admin(db: Any, **kwargs: Any) -> dict[str, str]:
        return admin_headers(db, scope="tenant:admin", **kwargs)

    async def _put(self, client: Any, headers: dict[str, str], body: dict[str, Any]) -> Any:
        return await client.put(self.URL, headers=headers, data=json.dumps(body))

    @staticmethod
    def _row(db: Any, tenant_id: int | None = None) -> Any:
        db.dal.rollback()
        table = db.dal.tenant_platform_apps
        wanted = tenant_id or db.seed.tenant_id
        return db.dal((table.tenant_id == wanted) & (table.platform == "youtube")).select().first()

    async def test_a_tenant_admin_stores_the_app_sealed(self, yt_client: Any, yt_db: Any) -> None:
        yt_db.dal(yt_db.dal.tenant_platform_apps.id > 0).delete()
        yt_db.dal.commit()
        response = await self._put(yt_client, self._tenant_admin(yt_db), OWN_APP)
        assert response.status_code == 200
        assert await response.get_json() == {"success": True, "invalidatedConnections": 0}
        row = self._row(yt_db)
        assert row is not None and APP_CLIENT_SECRET not in row.credentials_ciphertext

    async def test_a_community_admin_without_tenant_admin_is_refused_and_nothing_changes(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        before = self._row(yt_db).credentials_ciphertext
        community_only = admin_headers(yt_db, scope="connections:admin")
        response = await self._put(
            yt_client,
            community_only,
            {"client_id": "evil.apps.googleusercontent.com", "client_secret": "evil"},
        )
        assert response.status_code == 403
        assert self._row(yt_db).credentials_ciphertext == before
        assert (await yt_client.put(self.URL, data="{}")).status_code == 401

    async def test_an_existing_app_is_never_replaced_by_a_different_client_id(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        before = self._row(yt_db).credentials_ciphertext
        response = await self._put(
            yt_client,
            self._tenant_admin(yt_db),
            {"client_id": "different.apps.googleusercontent.com", "client_secret": "s"},
        )
        assert response.status_code == 409
        assert (await response.get_json())["error"]["code"] == "YOUTUBE_APP_CONFLICT"
        assert self._row(yt_db).credentials_ciphertext == before

    async def test_the_same_application_may_rotate_its_secret(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        before = self._row(yt_db).credentials_ciphertext
        response = await self._put(
            yt_client,
            self._tenant_admin(yt_db),
            {"client_id": APP_CLIENT_ID, "client_secret": "rotated-secret"},
        )
        assert response.status_code == 200
        assert self._row(yt_db).credentials_ciphertext != before

    async def test_the_global_tenant_cannot_store_an_app(self, yt_client: Any, yt_db: Any) -> None:
        headers = self._tenant_admin(
            yt_db, tenant=DEFAULT_TENANT_SLUG, user_id=yt_db.global_admin_id
        )
        response = await self._put(yt_client, headers, OWN_APP)
        assert response.status_code == 422
        assert self._row(yt_db, yt_db.global_tenant_id) is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"client_id": "has space", "client_secret": "x"},
            {"client_id": "x" * 513, "client_secret": "x"},
            {"client_id": "x", "client_secret": "new\nline"},
            {"client_id": "", "client_secret": "x"},
            {"client_id": "x"},
            {"client_id": "x", "client_secret": "y", "extra": "z"},
        ],
    )
    async def test_input_is_validated_server_side(
        self, yt_client: Any, yt_db: Any, bad: dict[str, Any]
    ) -> None:
        response = await self._put(yt_client, self._tenant_admin(yt_db), bad)
        assert response.status_code == 400
        assert "x" * 513 not in json.dumps(await response.get_json())

    async def test_the_feature_gate_applies(
        self, yt_client: Any, yt_db: Any, entitlements: Any
    ) -> None:
        entitlements.flag_off("waddles.integrations.provider_connections")
        response = await self._put(yt_client, self._tenant_admin(yt_db), OWN_APP)
        assert response.status_code == 402


def _set_cookie(response: Any) -> str:
    (header,) = response.headers.getlist("Set-Cookie")
    return str(header)


class TestBrowserBinding:
    """Account-linking CSRF: the callback only completes in the browser that started the flow."""

    async def test_authorize_sets_a_host_prefixed_httponly_secure_lax_cookie(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        header = _set_cookie(response)
        assert header.startswith(COOKIE + "=")
        lowered = header.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
        assert "path=/" in lowered and "domain" not in lowered  # __Host- rules: host-only
        nonce = header.split("=", 1)[1].split(";", 1)[0]
        ((_, (raw, _ttl)),) = fake_redis.store.items()
        assert nonce not in raw  # only a hash is kept server-side
        assert json.loads(raw)["binding"] == hashlib.sha256(nonce.encode()).hexdigest()

    async def test_another_browser_without_the_cookie_is_refused_and_the_state_survives(
        self,
        yt_app: Any,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        fake_redis: FakeRedis,
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        code = fake_google.issue_code(
            channel=YT_CHANNEL,
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        victim = yt_app.test_client()  # a different browser: no cookie in its jar
        refused = await victim.get(CALLBACK, query_string={"state": params["state"], "code": code})
        assert refused.status_code == 400
        assert await _error(refused) == "invalid_or_expired_state"
        assert len(fake_redis.store) == 1  # NOT consumed: nobody can burn a pending install
        assert fake_google.calls == []  # no token exchange attempted
        assert await _count_connections(yt_db) == 0
        # the initiating browser still completes it
        done = await yt_client.get(CALLBACK, query_string={"state": params["state"], "code": code})
        assert await _ok(done)

    async def test_a_wrong_cookie_is_refused_exactly_like_a_missing_or_unknown_state(
        self,
        yt_app: Any,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        fake_redis: FakeRedis,
    ) -> None:
        mine = await _authorize(yt_client, yt_db)
        params = _params((await mine.get_json())["authorizeUrl"])
        other = yt_app.test_client()
        attacker = await _authorize(other, yt_db)  # the attacker's OWN flow -> their own cookie
        assert attacker.status_code == 200
        query = {"state": params["state"], "code": "c"}
        cross = await other.get(CALLBACK, query_string=query)  # attacker cookie, victim state
        missing = await yt_app.test_client().get(CALLBACK, query_string=query)
        unknown = await other.get(CALLBACK, query_string={"state": "nope", "code": "c"})
        answers = {
            (r.status_code, await _error(r), await r.get_data(as_text=True))
            for r in (cross, missing, unknown)
        }
        assert len(answers) == 1, answers  # identical: no oracle
        assert next(iter(answers))[:2] == (400, "invalid_or_expired_state")
        assert fake_google.calls == [] and len(fake_redis.store) == 2

    async def test_the_cookie_is_cleared_and_a_consumed_state_cannot_be_replayed(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        callback, params = await _install(yt_client, yt_db, fake_google)
        assert await _ok(callback)
        assert "Expires=Thu, 01 Jan 1970" in _set_cookie(callback)
        replay = await yt_client.get(
            CALLBACK,
            query_string={"state": params["state"], "code": "c"},
            headers={"Cookie": f"{COOKIE}=whatever"},
        )
        assert replay.status_code == 400
        assert await _error(replay) == "invalid_or_expired_state"


async def _count_connections(db: Any) -> int:
    from tests.provider_connections.support import raw_rows

    return len(await raw_rows(db, "platform_connections"))
