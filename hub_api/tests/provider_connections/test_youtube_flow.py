"""YouTube end to end: OAuth connect (state + PKCE), refresh/rotation, binding, discovery, authz.

Real path: HTTP routes, tenant/scope/community-admin chain, install flow, credential resolver
(per-tenant Google app in ``tenant_platform_apps``), service, vault and store over sqlite. Faked:
the Google socket, DNS (public), Redis, entitlements. Regression/contract tests pin the exact
resolve shape the Rust executor (``outbound_creds_youtube.rs``) parses.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from services import youtube_install as install
from services.provider_connections import ProviderConnectionService
from services.provider_connections.errors import (
    ConnectionNotFound,
    ConnectionNotUsable,
    InvalidConnectionInput,
    ProviderUnavailable,
)
from services.provider_connections.youtube import GOOGLE_AUTHORIZE_URL, SCOPE_FORCE_SSL
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.fake_google import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    READONLY,
    FakeGoogle,
    broadcast,
)
from tests.provider_connections.support import open_row, raw_rows, set_column
from tests.provider_connections.test_api import _bound
from tests.provider_connections.youtube_fixtures import (
    YT_CHANNEL,
    YT_CHAT,
    YT_OTHER_CHANNEL,
    FakeRedis,
)

INTERNAL = "/api/v1/internal"
CALLBACK = "/api/v1/provider-connections/youtube/callback"
OWN_APP = {"client_id": APP_CLIENT_ID, "client_secret": APP_CLIENT_SECRET}


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    """A raw row's ``resource_metadata`` as a dict (sqlite hands raw JSON columns back as text)."""
    raw = row["resource_metadata"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _base(db: Any, community_id: int | None = None) -> str:
    return f"/api/v1/communities/{community_id or db.seed.community_id}/provider-connections"


async def _authorize(
    client: Any, db: Any, body: dict[str, Any] | None = None, **kwargs: Any
) -> Any:
    headers = kwargs.pop("headers", None) or admin_headers(db)
    community_id = kwargs.pop("community_id", None)
    return await client.post(
        _base(db, community_id) + "/youtube/authorize",
        headers=headers,
        data=json.dumps(body if body is not None else {}),
    )


def _params(authorize_url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}


async def _install(
    client: Any,
    db: Any,
    google: FakeGoogle,
    *,
    channel: Any = YT_CHANNEL,
    body: dict[str, Any] | None = None,
    with_refresh: bool = True,
    scopes: list[str] | None = None,
    community_id: int | None = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, str]]:
    """Authorize, let 'Google' issue a code for the consent, and hit the callback."""
    response = await _authorize(client, db, body, community_id=community_id, **kwargs)
    assert response.status_code == 200, await response.get_data()
    params = _params((await response.get_json())["authorizeUrl"])
    code = google.issue_code(
        channel=channel,
        challenge=params["code_challenge"],
        redirect_uri=params["redirect_uri"],
        with_refresh=with_refresh,
        scopes=scopes,
    )
    callback = await client.get(CALLBACK, query_string={"state": params["state"], "code": code})
    return callback, params


async def _connection_id(client: Any, db: Any, community_id: int | None = None) -> int:
    listed = await client.get(_base(db, community_id), headers=admin_headers(db))
    (connection,) = [
        c for c in (await listed.get_json())["connections"] if c["provider"] == "youtube"
    ]
    return int(connection["id"])


async def _ok(callback: Any) -> bool:
    html = (await callback.get_data(as_text=True)).split("var message = ")[1].split(";")[0]
    return bool(json.loads(html)["ok"])


async def _error(callback: Any) -> str | None:
    html = (await callback.get_data(as_text=True)).split("var message = ")[1].split(";")[0]
    return json.loads(html).get("error")  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------------


class TestAuthorize:
    async def test_the_consent_url_is_pkce_s256_offline_and_least_scope(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body) == {"success", "authorizeUrl"}
        url = body["authorizeUrl"]
        assert url.startswith(GOOGLE_AUTHORIZE_URL + "?")
        params = _params(url)
        assert params["client_id"] == APP_CLIENT_ID
        assert params["response_type"] == "code"
        assert params["scope"] == SCOPE_FORCE_SSL  # no readonly, no extras
        assert params["access_type"] == "offline" and params["prompt"] == "consent"
        assert params["code_challenge_method"] == "S256"
        assert len(params["code_challenge"]) == 43
        assert params["redirect_uri"].endswith(CALLBACK)
        assert "client_secret" not in params and APP_CLIENT_SECRET not in url

    async def test_the_state_is_single_use_ttl_bound_and_holds_no_plaintext_secret(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        state = _params((await response.get_json())["authorizeUrl"])["state"]
        ((key, (raw, expires)),) = fake_redis.store.items()
        assert key.endswith(state) and expires is not None
        assert APP_CLIENT_SECRET not in raw
        stored = json.loads(raw)
        assert stored["tenant_id"] == yt_db.seed.tenant_id
        assert stored["community_id"] == yt_db.seed.community_id
        assert stored["user_id"] == yt_db.seed.admin_user_id

    async def test_the_redirect_uri_cannot_be_chosen_by_the_client(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        response = await _authorize(yt_client, yt_db, {"redirect_uri": "https://evil/x"})
        assert response.status_code == 400

    async def test_a_tenant_without_an_app_gets_a_typed_422(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        yt_db.dal(yt_db.dal.tenant_platform_apps.id > 0).delete()
        yt_db.dal.commit()
        response = await _authorize(yt_client, yt_db)
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "YOUTUBE_APP_NOT_CONFIGURED"

    @pytest.mark.parametrize(
        "body",
        [
            OWN_APP,
            {"client_id": "other.apps.googleusercontent.com"},
            {"client_secret": "x"},
            {"client_id": "other.apps.googleusercontent.com", "client_secret": "evil-secret"},
        ],
    )
    async def test_a_community_admin_cannot_supply_or_overwrite_the_tenant_app(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis, body: dict[str, Any]
    ) -> None:
        before = yt_db.dal(yt_db.dal.tenant_platform_apps.id > 0).select().first()
        response = await _authorize(yt_client, yt_db, body)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "YOUTUBE_APP_TENANT_ADMIN_ONLY"
        assert fake_redis.store == {}  # no state minted, no cookie
        assert "Set-Cookie" not in response.headers
        yt_db.dal.rollback()
        after = yt_db.dal(yt_db.dal.tenant_platform_apps.id > 0).select().first()
        assert after.credentials_ciphertext == before.credentials_ciphertext

    async def test_the_global_tenant_uses_the_platform_app_and_cannot_bring_its_own(
        self, yt_client: Any, yt_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = admin_headers(yt_db, tenant="global", user_id=yt_db.global_admin_id)
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "platform-app.apps.googleusercontent.com")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "platform-secret")
        ok = await _authorize(
            yt_client, yt_db, {}, headers=headers, community_id=yt_db.global_community_id
        )
        assert ok.status_code == 200
        assert (
            _params((await ok.get_json())["authorizeUrl"])["client_id"]
            == "platform-app.apps.googleusercontent.com"
        )
        refused = await _authorize(
            yt_client, yt_db, OWN_APP, headers=headers, community_id=yt_db.global_community_id
        )
        assert refused.status_code == 403

    async def test_the_global_tenant_without_a_platform_app_is_refused(
        self, yt_client: Any, yt_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
        headers = admin_headers(yt_db, tenant="global", user_id=yt_db.global_admin_id)
        response = await _authorize(
            yt_client, yt_db, {}, headers=headers, community_id=yt_db.global_community_id
        )
        assert response.status_code == 422

    async def test_admin_only_scope_community_admin_tenant_and_feature(
        self, yt_client: Any, yt_db: Any, entitlements: Any
    ) -> None:
        assert (
            await yt_client.post(_base(yt_db) + "/youtube/authorize", data="{}")
        ).status_code == 401
        no_scope = admin_headers(yt_db, scope="*:read")
        assert (await _authorize(yt_client, yt_db, headers=no_scope)).status_code == 403
        # has the scope but administers no community
        rando = int(yt_db.dal.hub_users.insert(username="rando"))
        yt_db.dal.commit()
        outsider = admin_headers(yt_db, user_id=rando)
        assert (await _authorize(yt_client, yt_db, headers=outsider)).status_code == 403
        # an admin of ANOTHER tenant cannot touch this community
        foreign = admin_headers(yt_db, tenant="other-corp")
        assert (await _authorize(yt_client, yt_db, headers=foreign)).status_code in (403, 404)
        # the second community of the same tenant is the admin's too: allowed (control)
        assert (
            await _authorize(yt_client, yt_db, community_id=yt_db.seed.second_community_id)
        ).status_code == 200
        entitlements.flag_off("waddles.integrations.provider_connections")
        assert (await _authorize(yt_client, yt_db)).status_code == 402


class TestCallback:
    async def test_the_full_flow_connects_the_channel_sealed_at_rest(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, fake_redis: FakeRedis
    ) -> None:
        callback, params = await _install(yt_client, yt_db, fake_google)
        assert callback.status_code == 200 and await _ok(callback)
        html = await callback.get_data(as_text=True)
        assert params["state"] not in html and "ya29" not in html and "1//" not in html
        assert callback.headers["Cache-Control"] == "no-store"
        assert "default-src 'none'" in callback.headers["Content-Security-Policy"]

        (row,) = await raw_rows(yt_db, "platform_connections")
        assert row["tenant_id"] == yt_db.seed.tenant_id
        assert row["platform"] == "youtube" and row["resource_type"] == "youtube_channel"
        assert row["resource_id"] == YT_CHANNEL.channel_id and row["status"] == "active"
        assert row["connection_kind"] == 1
        # sealed: the DB holds ciphertext, and it opens only with the row's own identity (AAD)
        access, refresh = open_row(row), open_row(row, "refresh_token")
        assert access.startswith("ya29.") and refresh.startswith("1//")
        assert access not in row["access_token"] and refresh not in row["refresh_token"]
        facts = _meta(row)
        assert facts["tenant_id"] == yt_db.seed.tenant_id
        assert facts["client_id"] == APP_CLIENT_ID and facts["channel_id"] == YT_CHANNEL.channel_id
        assert "live_chat_id" not in facts  # not known yet: binding incomplete
        # the tenant's own Google app was stored (sealed) by the same flow
        app_row = (
            yt_db.dal(
                (yt_db.dal.tenant_platform_apps.tenant_id == yt_db.seed.tenant_id)
                & (yt_db.dal.tenant_platform_apps.platform == "youtube")
            )
            .select()
            .first()
        )
        assert app_row is not None and APP_CLIENT_SECRET not in app_row.credentials_ciphertext
        assert fake_redis.store == {}  # the state was consumed

    async def test_the_pkce_verifier_is_really_checked_by_google(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        code = fake_google.issue_code(
            channel=YT_CHANNEL,
            challenge="not-the-real-challenge",
            redirect_uri=params["redirect_uri"],
        )
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": code}
        )
        assert await _error(callback) == "exchange_failed"
        assert await raw_rows(yt_db, "platform_connections") == []
        # and the exchange really carried the verifier + the client secret
        assert {"code_verifier", "client_secret", "redirect_uri"} <= set(fake_google.bodies[-1])

    async def test_a_state_is_single_use(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        callback, params = await _install(yt_client, yt_db, fake_google)
        assert await _ok(callback)
        code = fake_google.issue_code(
            channel=YT_CHANNEL,
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        replay = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": code}
        )
        assert replay.status_code == 400 and await _error(replay) == "invalid_or_expired_state"

    @pytest.mark.parametrize("state", ["", "forged", "x" * 500, "../../etc"])
    async def test_forged_or_absent_state_is_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, state: str
    ) -> None:
        response = await yt_client.get(CALLBACK, query_string={"state": state, "code": "c"})
        assert response.status_code == 400 and await _error(response) == "invalid_or_expired_state"
        assert fake_google.calls == []  # never reached Google

    async def test_an_expired_state_is_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        (key,) = fake_redis.store
        fake_redis.force_expire(key)
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": "c"}
        )
        assert callback.status_code == 400

    async def test_a_tampered_state_payload_fails_authentication(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        ((key, (raw, ttl)),) = fake_redis.store.items()
        payload = json.loads(raw)
        payload["tenant_id"] = yt_db.seed.other_tenant_id  # retarget the install at another tenant
        fake_redis.store[key] = (json.dumps(payload), ttl)
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": "c"}
        )
        assert callback.status_code == 400 and await _error(callback) == "invalid_or_expired_state"

    async def test_tenant_and_community_come_from_the_state_never_the_query(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        code = fake_google.issue_code(
            channel=YT_CHANNEL,
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        callback = await yt_client.get(
            CALLBACK,
            query_string={
                "state": params["state"],
                "code": code,
                "tenant_id": yt_db.seed.other_tenant_id,
                "community_id": yt_db.seed.other_community_id,
            },
        )
        assert await _ok(callback)
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert row["tenant_id"] == yt_db.seed.tenant_id
        grants = await raw_rows(yt_db, "community_connection_access")
        assert [g["community_id"] for g in grants] == [yt_db.seed.community_id]

    async def test_a_denied_consent_burns_the_state_and_stores_nothing(
        self, yt_client: Any, yt_db: Any, fake_redis: FakeRedis
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "error": "access_denied"}
        )
        assert await _error(callback) == "access_denied"
        assert fake_redis.store == {} and await raw_rows(yt_db, "platform_connections") == []

    async def test_a_missing_code_is_a_400(self, yt_client: Any, yt_db: Any) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        callback = await yt_client.get(CALLBACK, query_string={"state": params["state"]})
        assert callback.status_code == 400 and await _error(callback) == "missing_code"

    async def test_no_refresh_token_means_no_connection(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        callback, _ = await _install(yt_client, yt_db, fake_google, with_refresh=False)
        assert await _error(callback) == "no_refresh_token"
        assert await raw_rows(yt_db, "platform_connections") == []

    async def test_a_token_without_the_required_scope_is_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        callback, _ = await _install(yt_client, yt_db, fake_google, scopes=[READONLY])
        assert await _error(callback) == "insufficient_scopes"
        assert await raw_rows(yt_db, "platform_connections") == []

    async def test_a_google_account_without_a_channel_is_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        callback, _ = await _install(yt_client, yt_db, fake_google, channel=None)
        assert await _error(callback) == "channel_not_found"

    async def test_google_down_during_the_exchange_is_a_typed_failure(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        response = await _authorize(yt_client, yt_db)
        params = _params((await response.get_json())["authorizeUrl"])
        fake_google.down = True
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": "c"}
        )
        assert await _error(callback) == "exchange_failed"

    async def test_the_stored_app_changing_between_authorize_and_callback_is_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, acme_google_app: None
    ) -> None:
        response = await _authorize(yt_client, yt_db, {})
        params = _params((await response.get_json())["authorizeUrl"])
        code = fake_google.issue_code(
            channel=YT_CHANNEL,
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        from services.credential_resolver import store_tenant_credentials

        store_tenant_credentials(
            yt_db.dal,
            tenant_id=yt_db.seed.tenant_id,
            is_global_tenant=False,
            platform="youtube",
            payload={"client_id": "swapped.apps.googleusercontent.com", "client_secret": "s"},
            installed_by_user_id=1,
        )
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": code}
        )
        assert await _error(callback) == "app_not_configured"

    async def test_reconnecting_the_same_channel_rotates_credentials_in_place(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, acme_google_app: None
    ) -> None:
        first, _ = await _install(yt_client, yt_db, fake_google, body={})
        assert await _ok(first)
        (before,) = await raw_rows(yt_db, "platform_connections")
        await set_column(
            yt_db,
            "platform_connections",
            before["id"],
            status="expired",
            status_reason="refresh_rejected",
        )
        second, _ = await _install(yt_client, yt_db, fake_google, body={})
        assert await _ok(second)
        (after,) = await raw_rows(yt_db, "platform_connections")
        assert after["id"] == before["id"] and after["status"] == "active"
        assert after["status_reason"] is None
        assert open_row(after) != open_row(before)

    async def test_the_same_channel_cannot_be_hijacked_into_another_community(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, acme_google_app: None
    ) -> None:
        assert await _ok((await _install(yt_client, yt_db, fake_google, body={}))[0])
        other = await _install(
            yt_client, yt_db, fake_google, body={}, community_id=yt_db.seed.second_community_id
        )
        assert await _error(other[0]) == "save_failed"
        assert len(await raw_rows(yt_db, "platform_connections")) == 1


class TestBinding:
    async def _connected(self, client: Any, db: Any, google: FakeGoogle) -> int:
        assert await _ok((await _install(client, db, google))[0])
        return await _connection_id(client, db)

    async def test_an_unbound_connection_is_incomplete_and_never_served(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        listed = await yt_client.get(
            f"{INTERNAL}/provider-connections?provider=youtube",
            headers=_list_headers(service_issuer),
        )
        assert (await listed.get_json())["items"] == []
        resolved = await yt_client.post(
            f"{INTERNAL}/communities/{yt_db.seed.community_id}/provider-connections/"
            f"{connection_id}/resolve",
            headers=_bound(service_issuer, yt_db.seed.community_id, connection_id),
        )
        assert resolved.status_code == 409
        assert (await resolved.get_json())["reason"] == "metadata_incomplete"

    async def test_the_manual_binding_is_served_in_the_exact_shape_the_executor_parses(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        patched = await yt_client.patch(
            f"{_base(yt_db)}/{connection_id}",
            headers=admin_headers(yt_db),
            data=json.dumps({"metadata": {"live_chat_id": YT_CHAT}}),
        )
        assert patched.status_code == 200
        admin_view = (await patched.get_json())["connection"]
        assert admin_view["metadata"]["live_chat_id"] == YT_CHAT
        assert admin_view["hasRefreshToken"] is True
        assert (
            "tenant_id" not in admin_view["metadata"] and "client_id" not in admin_view["metadata"]
        )

        listed = await yt_client.get(
            f"{INTERNAL}/provider-connections?provider=youtube",
            headers=_list_headers(service_issuer),
        )
        (item,) = (await listed.get_json())["items"]
        assert item["metadata"] == {"live_chat_id": YT_CHAT} and item["kind"] == 1
        assert item["resourceId"] == YT_CHANNEL.channel_id

        resolved = await yt_client.post(
            f"{INTERNAL}/communities/{yt_db.seed.community_id}/provider-connections/"
            f"{connection_id}/resolve",
            headers=_bound(service_issuer, yt_db.seed.community_id, connection_id),
        )
        assert resolved.status_code == 200
        assert resolved.headers["Cache-Control"] == "no-store"
        body = await resolved.get_json()
        # regression: the exact field set -- anything extra is a leak, anything missing breaks Rust
        assert set(body) == {
            "success",
            "connectionId",
            "communityId",
            "provider",
            "kind",
            "accessToken",
            "expiresAt",
            "endpoint",
            "secret",
            "metadata",
        }
        assert (
            body["kind"] == 1
        )  # ConnectionKind::PlatformOauth -> ConnectionSecret::OAuthAccessToken
        assert body["secret"] is None and body["accessToken"].startswith("ya29.")
        assert body["metadata"] == {"live_chat_id": YT_CHAT}
        assert body["expiresAt"] is not None and body["expiresAt"] > clock.now.isoformat()
        serialized = json.dumps(body)
        assert APP_CLIENT_SECRET not in serialized and "1//" not in serialized  # no refresh token

    @pytest.mark.parametrize("bad", ["", "a b", "a/b", "x" * 129, "bad!", "ünï"])
    async def test_invalid_ids_are_refused_over_http_and_store_nothing(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, bad: str
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        response = await yt_client.patch(
            f"{_base(yt_db)}/{connection_id}",
            headers=admin_headers(yt_db),
            data=json.dumps({"metadata": {"live_chat_id": bad}}),
        )
        (row,) = await raw_rows(yt_db, "platform_connections")
        if bad.strip() == "":
            # blank == "unset": the update is a no-op re-verification, never a stored blank
            assert "live_chat_id" not in _meta(row)
        else:
            assert response.status_code == 400
            assert "live_chat_id" not in _meta(row)

    async def test_non_admin_writes_are_refused(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        rando = int(yt_db.dal.hub_users.insert(username="rando2"))
        yt_db.dal.commit()
        outsider = admin_headers(yt_db, user_id=rando)
        viewer = admin_headers(yt_db, scope="*:read")
        url = f"{_base(yt_db)}/{connection_id}"
        patch_body = json.dumps({"metadata": {"live_chat_id": YT_CHAT}})
        for headers in (outsider, viewer):
            assert (await yt_client.patch(url, headers=headers, data=patch_body)).status_code == 403
            assert (await yt_client.delete(url, headers=headers)).status_code == 403
            assert (await yt_client.post(url + "/discover", headers=headers)).status_code == 403
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert "live_chat_id" not in _meta(row)

    async def test_another_community_cannot_see_bind_or_discover_this_connection(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, service_issuer: ServiceJwtIssuer
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        wrong = yt_db.seed.second_community_id  # same tenant, same admin, different community
        url = f"{_base(yt_db, wrong)}/{connection_id}"
        h = admin_headers(yt_db)
        assert (await yt_client.get(url, headers=h)).status_code == 404
        assert (
            await yt_client.patch(url, headers=h, data=json.dumps({"enabled": False}))
        ).status_code == 404
        assert (await yt_client.post(url + "/discover", headers=h)).status_code == 404
        assert (await yt_client.delete(url, headers=h)).status_code == 404
        resolved = await yt_client.post(
            f"{INTERNAL}/communities/{wrong}/provider-connections/{connection_id}/resolve",
            headers=_bound(service_issuer, wrong, connection_id),
        )
        assert resolved.status_code in (403, 404)

    async def test_another_tenant_sees_nothing(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        foreign = admin_headers(yt_db, tenant="other-corp")
        for method in ("get", "delete"):
            response = await getattr(yt_client, method)(
                f"{_base(yt_db)}/{connection_id}", headers=foreign
            )
            assert response.status_code in (403, 404)


def _list_headers(issuer: ServiceJwtIssuer) -> dict[str, str]:
    from tests.provider_connections.conftest import SYSTEM_SERVICE_ID

    return {"Authorization": f"Bearer {issuer.issue(SYSTEM_SERVICE_ID, 'connections:list')}"}


class TestDiscovery:
    async def _connected(self, client: Any, db: Any, google: FakeGoogle) -> int:
        assert await _ok((await _install(client, db, google))[0])
        return await _connection_id(client, db)

    async def test_discovery_writes_the_binding_and_nothing_else(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        fake_google.broadcasts[YT_CHANNEL.channel_id] = [
            broadcast(YT_CHAT, channel_id=YT_CHANNEL.channel_id)
        ]
        fake_google.calls.clear()
        response = await yt_client.post(
            f"{_base(yt_db)}/{connection_id}/discover", headers=admin_headers(yt_db)
        )
        assert response.status_code == 200
        view = (await response.get_json())["connection"]
        assert view["metadata"]["live_chat_id"] == YT_CHAT and view["usable"] is True
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert _meta(row)["live_chat_id"] == YT_CHAT
        assert _meta(row)["channel_id"] == YT_CHANNEL.channel_id
        # bounded: one discovery call + the framework's re-verification (tokeninfo + channels)
        assert [c[1] for c in fake_google.calls].count(
            "www.googleapis.com/youtube/v3/liveBroadcasts"
        ) == 1
        assert len(fake_google.calls) <= 3

    async def test_discovery_is_cooled_down_per_connection(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        url = f"{_base(yt_db)}/{connection_id}/discover"
        first = await yt_client.post(url, headers=admin_headers(yt_db))
        assert first.status_code == 422  # no live broadcast yet; the call was still spent
        calls = len(fake_google.calls)
        second = await yt_client.post(url, headers=admin_headers(yt_db))
        assert second.status_code == 429
        assert (await second.get_json())["error"]["code"] == "TOO_MANY_REQUESTS"
        assert len(fake_google.calls) == calls  # the cooled-down request cost no quota

    async def test_no_active_broadcast_is_a_422_and_writes_nothing(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        response = await yt_client.post(
            f"{_base(yt_db)}/{connection_id}/discover", headers=admin_headers(yt_db)
        )
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "RESOURCE_NOT_FOUND"
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert "live_chat_id" not in _meta(row)

    async def test_quota_exhaustion_is_a_typed_429(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        connection_id = await self._connected(yt_client, yt_db, fake_google)
        fake_google.youtube_status, fake_google.youtube_reason = 403, "quotaExceeded"
        response = await yt_client.post(
            f"{_base(yt_db)}/{connection_id}/discover", headers=admin_headers(yt_db)
        )
        assert response.status_code == 429
        assert (await response.get_json())["error"]["code"] == "PROVIDER_QUOTA_EXCEEDED"

    async def test_a_provider_without_discovery_is_a_400(
        self, yt_client: Any, yt_db: Any, yt_service: ProviderConnectionService, bot_tokens: Any
    ) -> None:
        from tests.provider_connections.support import create_twitch

        view = await create_twitch(yt_service, yt_db, bot_tokens)
        response = await yt_client.post(
            f"{_base(yt_db)}/{view.binding.connection.id}/discover", headers=admin_headers(yt_db)
        )
        assert response.status_code == 400

    async def test_a_shared_connection_cannot_be_rebound_by_one_community(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
    ) -> None:
        from tests.provider_connections.support import grant_to

        connection_id = await self._connected(yt_client, yt_db, fake_google)
        await grant_to(
            yt_db, community_id=yt_db.seed.second_community_id, connection_id=connection_id
        )
        response = await yt_client.post(
            f"{_base(yt_db)}/{connection_id}/discover", headers=admin_headers(yt_db)
        )
        assert response.status_code == 409


class TestRefresh:
    async def _bound(
        self, client: Any, db: Any, google: FakeGoogle, issuer: ServiceJwtIssuer
    ) -> tuple[int, dict[str, str]]:
        assert await _ok((await _install(client, db, google))[0])
        connection_id = await _connection_id(client, db)
        patched = await client.patch(
            f"{_base(db)}/{connection_id}",
            headers=admin_headers(db),
            data=json.dumps({"metadata": {"live_chat_id": YT_CHAT}}),
        )
        assert patched.status_code == 200
        return connection_id, _bound(issuer, db.seed.community_id, connection_id)

    def _resolve(self, client: Any, db: Any, connection_id: int, headers: dict[str, str]) -> Any:
        return client.post(
            f"{INTERNAL}/communities/{db.seed.community_id}/provider-connections/"
            f"{connection_id}/resolve",
            headers=headers,
        )

    async def test_a_stale_token_is_refreshed_before_it_is_served(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        connection_id, headers = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        (before,) = await raw_rows(yt_db, "platform_connections")
        clock.advance(seconds=3599 - 60)  # inside the 120 s refresh skew
        response = await self._resolve(yt_client, yt_db, connection_id, headers)
        body = await response.get_json()
        assert response.status_code == 200 and fake_google.refresh_calls == 1
        assert body["accessToken"] != open_row(before)
        assert body["expiresAt"] > clock.now.isoformat()
        # still good for the data plane's 30 s rule right after the refresh
        (after,) = await raw_rows(yt_db, "platform_connections")
        assert open_row(after) == body["accessToken"]
        assert open_row(after, "refresh_token") == open_row(before, "refresh_token")  # not rotated

    async def test_a_fresh_token_is_not_refreshed(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle, service_issuer: ServiceJwtIssuer
    ) -> None:
        connection_id, headers = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        for _ in range(3):
            assert (
                await self._resolve(yt_client, yt_db, connection_id, headers)
            ).status_code == 200
        assert fake_google.refresh_calls == 0

    async def test_a_rotated_refresh_token_is_stored_sealed_and_used_next_time(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        connection_id, headers = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        fake_google.rotate_refresh = True
        (before,) = await raw_rows(yt_db, "platform_connections")
        old_refresh = open_row(before, "refresh_token")
        for round_ in (1, 2):
            clock.advance(seconds=3599)
            assert (
                await self._resolve(yt_client, yt_db, connection_id, headers)
            ).status_code == 200
            assert fake_google.refresh_calls == round_
        (after,) = await raw_rows(yt_db, "platform_connections")
        new_refresh = open_row(after, "refresh_token")
        assert new_refresh != old_refresh and new_refresh in fake_google.refresh_tokens
        assert old_refresh not in fake_google.refresh_tokens
        assert new_refresh not in after["refresh_token"]  # still ciphertext

    async def test_a_rejected_refresh_marks_the_connection_for_reauth_and_keeps_the_row(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        connection_id, headers = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        (row,) = await raw_rows(yt_db, "platform_connections")
        secrets = (open_row(row), open_row(row, "refresh_token"))
        fake_google.refresh_tokens.clear()  # the user revoked access in their Google account
        clock.advance(seconds=3599)
        with caplog.at_level(logging.DEBUG):
            response = await self._resolve(yt_client, yt_db, connection_id, headers)
        assert response.status_code == 409
        assert await response.get_json() == {
            "success": False,
            "error": "not_usable",
            "reason": "refresh_rejected",
        }
        (after,) = await raw_rows(yt_db, "platform_connections")
        assert (after["status"], after["status_reason"]) == ("expired", "refresh_rejected")
        listed = await yt_client.get(_base(yt_db), headers=admin_headers(yt_db))
        (view,) = (await listed.get_json())["connections"]
        assert view["status"] == "expired" and view["usable"] is False
        assert all(secret not in caplog.text for secret in secrets)
        # a second resolve is refused WITHOUT hitting Google again
        calls = fake_google.refresh_calls
        again = await self._resolve(yt_client, yt_db, connection_id, headers)
        assert again.status_code == 409 and fake_google.refresh_calls == calls

    async def test_a_transient_failure_changes_nothing(
        self,
        yt_client: Any,
        yt_db: Any,
        yt_service: ProviderConnectionService,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        connection_id, _ = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        (before,) = await raw_rows(yt_db, "platform_connections")
        clock.advance(seconds=3599)
        fake_google.down = True
        with pytest.raises(ProviderUnavailable):
            await yt_service.resolve(
                community_id=yt_db.seed.community_id, connection_id=connection_id
            )
        (after,) = await raw_rows(yt_db, "platform_connections")
        assert after["status"] == "active" and after["access_token"] == before["access_token"]
        fake_google.down = False  # and it recovers
        resolved = await yt_service.resolve(
            community_id=yt_db.seed.community_id, connection_id=connection_id
        )
        assert resolved.access_token

    async def test_no_app_configured_is_refresh_not_configured_not_a_dead_token(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        connection_id, headers = await self._bound(yt_client, yt_db, fake_google, service_issuer)
        yt_db.dal(yt_db.dal.tenant_platform_apps.id > 0).delete()
        yt_db.dal.commit()
        clock.advance(seconds=3599)
        response = await self._resolve(yt_client, yt_db, connection_id, headers)
        assert response.status_code == 409
        assert (await response.get_json())["reason"] == "refresh_not_configured"
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert row["status"] == "active"  # the token is fine; our config is what is missing
        assert fake_google.refresh_calls == 0


class TestFrameworkAdditions:
    async def test_the_tenant_stamp_is_framework_owned_and_never_served(
        self, yt_service: ProviderConnectionService, yt_db: Any, yt_tokens: Any
    ) -> None:
        view = await yt_service.create(
            tenant_id=yt_db.seed.tenant_id,
            community_id=yt_db.seed.community_id,
            actor_user_id=yt_db.seed.admin_user_id,
            provider="youtube",
            access_token=yt_tokens[0],
            refresh_token=yt_tokens[1],
            metadata={"live_chat_id": YT_CHAT},
            enabled=True,
        )
        (row,) = await raw_rows(yt_db, "platform_connections")
        assert _meta(row)["tenant_id"] == yt_db.seed.tenant_id
        assert "tenant_id" not in view.public_metadata
        resolved = await yt_service.resolve(
            community_id=yt_db.seed.community_id, connection_id=view.binding.connection.id
        )
        assert resolved.metadata.keys() <= {"live_chat_id", "channel_id", "scopes", "capabilities"}
        assert yt_service.binding_metadata(
            "youtube", _meta(row), connection_id=view.binding.connection.id
        ) == {"live_chat_id": YT_CHAT}

    async def test_service_level_wrong_community_and_tenant_are_not_found(
        self, yt_service: ProviderConnectionService, yt_db: Any, yt_tokens: Any
    ) -> None:
        view = await yt_service.create(
            tenant_id=yt_db.seed.tenant_id,
            community_id=yt_db.seed.community_id,
            actor_user_id=None,
            provider="youtube",
            access_token=yt_tokens[0],
            refresh_token=yt_tokens[1],
            metadata={},
            enabled=True,
        )
        cid = view.binding.connection.id
        with pytest.raises(ConnectionNotFound):
            await yt_service.discover_metadata(
                tenant_id=yt_db.seed.tenant_id,
                community_id=yt_db.seed.second_community_id,
                connection_id=cid,
            )
        with pytest.raises(ConnectionNotFound):
            await yt_service.discover_metadata(
                tenant_id=yt_db.seed.other_tenant_id,
                community_id=yt_db.seed.community_id,
                connection_id=cid,
            )
        with pytest.raises(ConnectionNotFound):
            await yt_service.resolve(community_id=yt_db.seed.second_community_id, connection_id=cid)
        with pytest.raises(ConnectionNotUsable):  # right community, but no binding yet
            await yt_service.resolve(community_id=yt_db.seed.community_id, connection_id=cid)

    async def test_discovery_of_an_unknown_connection_is_not_found(
        self, yt_service: ProviderConnectionService, yt_db: Any
    ) -> None:
        with pytest.raises(ConnectionNotFound):
            await yt_service.discover_metadata(
                tenant_id=yt_db.seed.tenant_id,
                community_id=yt_db.seed.community_id,
                connection_id=999,
            )

    async def test_pasting_a_token_pair_is_still_validated(
        self, yt_service: ProviderConnectionService, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        readonly, refresh = fake_google.mint(channel=YT_CHANNEL, scopes=[READONLY])
        with pytest.raises(Exception, match="scope"):
            await yt_service.create(
                tenant_id=yt_db.seed.tenant_id,
                community_id=yt_db.seed.community_id,
                actor_user_id=None,
                provider="youtube",
                access_token=readonly,
                refresh_token=refresh,
                metadata={},
                enabled=True,
            )
        with pytest.raises(InvalidConnectionInput):
            await yt_service.create(
                tenant_id=yt_db.seed.tenant_id,
                community_id=yt_db.seed.community_id,
                actor_user_id=None,
                provider="youtube",
                access_token="tok",
                refresh_token=None,
                metadata={"live_chat_id": "bad id"},
                enabled=True,
            )
        assert await raw_rows(yt_db, "platform_connections") == []


class TestSecretHygiene:
    async def test_no_secret_reaches_a_log_line_across_the_whole_flow(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            callback, params = await _install(yt_client, yt_db, fake_google)
            assert await _ok(callback)
            connection_id = await _connection_id(yt_client, yt_db)
            await yt_client.patch(
                f"{_base(yt_db)}/{connection_id}",
                headers=admin_headers(yt_db),
                data=json.dumps({"metadata": {"live_chat_id": YT_CHAT}}),
            )
            clock.advance(seconds=3599)
            await yt_client.post(
                f"{INTERNAL}/communities/{yt_db.seed.community_id}/provider-connections/"
                f"{connection_id}/resolve",
                headers=_bound(service_issuer, yt_db.seed.community_id, connection_id),
            )
        (row,) = await raw_rows(yt_db, "platform_connections")
        needles = [
            APP_CLIENT_SECRET,
            open_row(row),
            open_row(row, "refresh_token"),
            params["state"],
            params["code_challenge"],
            *list(fake_google.tokens),
            *list(fake_google.refresh_tokens),
        ]
        for needle in needles:
            assert needle not in caplog.text

    def test_the_pkce_challenge_matches_the_rfc_7636_vector(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        assert install.pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    async def test_an_unreadable_user_token_cannot_authorize(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        bad = {"Authorization": "Bearer not-a-jwt", "Content-Type": "application/json"}
        assert (await _authorize(yt_client, yt_db, headers=bad)).status_code == 401


class TestInstallEdges:
    """Failure branches of the callback: every one is a fixed code and stores nothing."""

    async def _pending(
        self, client: Any, db: Any, google: FakeGoogle
    ) -> tuple[dict[str, str], str]:
        response = await _authorize(client, db)
        params = _params((await response.get_json())["authorizeUrl"])
        code = google.issue_code(
            channel=YT_CHANNEL,
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        return params, code

    async def test_an_incomplete_stored_app_is_not_configured(
        self, yt_client: Any, yt_db: Any
    ) -> None:
        from services.credential_resolver import store_tenant_credentials

        store_tenant_credentials(
            yt_db.dal,
            tenant_id=yt_db.seed.tenant_id,
            is_global_tenant=False,
            platform="youtube",
            payload={"client_id": APP_CLIENT_ID, "client_secret": ""},
            installed_by_user_id=1,
        )
        response = await _authorize(yt_client, yt_db, {})
        assert response.status_code == 422

    async def test_a_non_json_or_tokenless_exchange_answer_is_exchange_failed(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import httpx

        for answer in (
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json={"expires_in": 5}),
            httpx.Response(200, json=["nope"]),
        ):
            params, code = await self._pending(yt_client, yt_db, fake_google)
            monkeypatch.setattr(
                install, "_transport", httpx.MockTransport(lambda request, a=answer: a)
            )
            callback = await yt_client.get(
                CALLBACK, query_string={"state": params["state"], "code": code}
            )
            assert await _error(callback) == "exchange_failed"
        assert await raw_rows(yt_db, "platform_connections") == []

    async def test_a_private_token_endpoint_is_blocked_before_any_request(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import socket

        params, code = await self._pending(yt_client, yt_db, fake_google)
        monkeypatch.setattr(
            "services.url_guard.socket.getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
        )
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": code}
        )
        assert await _error(callback) == "exchange_failed"
        assert fake_google.calls == []

    async def test_google_unavailable_after_the_exchange_is_provider_unavailable(
        self,
        yt_client: Any,
        yt_db: Any,
        fake_google: FakeGoogle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        access, refresh = fake_google.mint(channel=YT_CHANNEL)

        async def exchanged(**kwargs: Any) -> tuple[str, str | None]:
            fake_google.down = True  # the token endpoint worked, the API behind it does not
            return access, refresh

        monkeypatch.setattr(install, "exchange_code", exchanged)
        callback, _ = await _install(yt_client, yt_db, fake_google)
        assert await _error(callback) == "provider_unavailable"
        assert await raw_rows(yt_db, "platform_connections") == []

    async def test_a_community_deleted_mid_flow_is_save_failed(
        self, yt_client: Any, yt_db: Any, fake_google: FakeGoogle
    ) -> None:
        params, code = await self._pending(yt_client, yt_db, fake_google)
        yt_db.dal(yt_db.dal.communities.id == yt_db.seed.community_id).delete()
        yt_db.dal.commit()
        callback = await yt_client.get(
            CALLBACK, query_string={"state": params["state"], "code": code}
        )
        assert await _error(callback) == "save_failed"

    async def test_reconnect_skips_other_providers_and_other_channels(
        self,
        yt_client: Any,
        yt_db: Any,
        yt_service: ProviderConnectionService,
        fake_google: FakeGoogle,
        bot_tokens: Any,
    ) -> None:
        from tests.provider_connections.support import create_twitch

        await create_twitch(yt_service, yt_db, bot_tokens)  # a non-YouTube neighbour
        # channel B lives in the SECOND community, channel A in the first
        second = yt_db.seed.second_community_id
        assert await _ok(
            (
                await _install(
                    yt_client, yt_db, fake_google, channel=YT_OTHER_CHANNEL, community_id=second
                )
            )[0]
        )
        assert await _ok((await _install(yt_client, yt_db, fake_google))[0])
        # channel A again, but from the second community: it already belongs to the first, and
        # the second's own YouTube connection (channel B) refuses to be rotated onto A's account
        again = await _install(yt_client, yt_db, fake_google, community_id=second)
        assert await _error(again[0]) == "save_failed"
        rows = [
            r for r in await raw_rows(yt_db, "platform_connections") if r["platform"] == "youtube"
        ]
        assert sorted(r["resource_id"] for r in rows) == sorted(
            [YT_CHANNEL.channel_id, YT_OTHER_CHANNEL.channel_id]
        )

    async def test_reconnect_skips_a_non_youtube_neighbour_then_rotates(
        self,
        yt_client: Any,
        yt_db: Any,
        yt_service: ProviderConnectionService,
        fake_google: FakeGoogle,
        bot_tokens: Any,
    ) -> None:
        from tests.provider_connections.support import create_twitch

        assert await _ok((await _install(yt_client, yt_db, fake_google))[0])
        await create_twitch(yt_service, yt_db, bot_tokens)
        (before,) = [
            r for r in await raw_rows(yt_db, "platform_connections") if r["platform"] == "youtube"
        ]
        assert await _ok((await _install(yt_client, yt_db, fake_google))[0])
        (after,) = [
            r for r in await raw_rows(yt_db, "platform_connections") if r["platform"] == "youtube"
        ]
        assert after["id"] == before["id"] and open_row(after) != open_row(before)

    async def test_the_state_store_failing_closed(
        self, yt_client: Any, yt_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Broken:
            async def getdel(self, key: str) -> None:
                raise ConnectionError("redis down")

        monkeypatch.setattr(install, "_redis_client", lambda: Broken())
        callback = await yt_client.get(CALLBACK, query_string={"state": "abc", "code": "c"})
        assert callback.status_code == 400 and await _error(callback) == "invalid_or_expired_state"
