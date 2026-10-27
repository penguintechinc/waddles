"""`services/sso_state.py` (single-use state, replay cache) and `services/sso_http.py` (SSRF guard)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from quart import Quart

from config import HubAPIConfig
from services import sso_state
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY
from services.sso_http import MAX_RESPONSE_BYTES, SsoHttp, check_outbound_url
from services.sso_settings import SsoSettings
from services.sso_types import SsoConfigError, SsoIdpUnavailableError
from tests.sso.conftest import FakeRedis, hub_config


@pytest.fixture
def state_app(fake_redis: FakeRedis) -> Quart:
    app = Quart(__name__)
    app.config[sso_state.SSO_REDIS_CONFIG_KEY] = fake_redis
    app.config["HUB_API_CONFIG"] = hub_config()
    return app


def _payload(**overrides: Any) -> sso_state.SsoStatePayload:
    base: dict[str, Any] = {
        "connection_public_id": "conn-1",
        "protocol": "oidc",
        "nonce": "n",
        "code_verifier": "v",
    }
    base.update(overrides)
    return sso_state.SsoStatePayload(**base)


class TestState:
    async def test_state_round_trips_and_is_single_use(self, state_app: Quart) -> None:
        async with state_app.app_context():
            token = await sso_state.create_state(_payload(), ttl_s=60)
            first = await sso_state.consume_state(token)
            second = await sso_state.consume_state(token)
        assert first == _payload()
        assert second is None  # GETDEL: a replay always misses

    async def test_saml_payload_carries_request_id(self, state_app: Quart) -> None:
        async with state_app.app_context():
            token = await sso_state.create_state(
                _payload(protocol="saml", nonce=None, code_verifier=None, request_id="_abc"),
                ttl_s=60,
            )
            got = await sso_state.consume_state(token)
        assert got is not None
        assert got.request_id == "_abc"
        assert got.nonce is None

    async def test_tokens_are_unguessable_and_distinct(self, state_app: Quart) -> None:
        async with state_app.app_context():
            tokens = {await sso_state.create_state(_payload(), ttl_s=60) for _ in range(20)}
        assert len(tokens) == 20
        assert all(len(t) >= 40 for t in tokens)

    @pytest.mark.parametrize("state", ["", "nope", "x" * 200])
    async def test_unknown_state_is_none(self, state_app: Quart, state: str) -> None:
        async with state_app.app_context():
            assert await sso_state.consume_state(state) is None

    async def test_expired_state_is_none(self, state_app: Quart, fake_redis: FakeRedis) -> None:
        async with state_app.app_context():
            token = await sso_state.create_state(_payload(), ttl_s=60)
            key = f"sso:state:{token}"
            value, _ = fake_redis.store[key]
            fake_redis.store[key] = (value, 0.0)  # already expired
            assert await sso_state.consume_state(token) is None

    async def test_malformed_payload_is_treated_as_missing(
        self, state_app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with state_app.app_context():
            await fake_redis.set("sso:state:bad", "{not json")
            assert await sso_state.consume_state("bad") is None
            await fake_redis.set("sso:state:partial", '{"protocol": "oidc"}')
            assert await sso_state.consume_state("partial") is None

    async def test_store_outage_fails_loudly_without_leaking_driver_text(
        self, state_app: Quart, fake_redis: FakeRedis
    ) -> None:
        fake_redis.fail = True
        async with state_app.app_context():
            with pytest.raises(SsoIdpUnavailableError) as create_exc:
                await sso_state.create_state(_payload(), ttl_s=60)
            with pytest.raises(SsoIdpUnavailableError) as consume_exc:
                await sso_state.consume_state("whatever")
            with pytest.raises(SsoIdpUnavailableError):
                await sso_state.remember_assertion("a1", ttl_s=60)
        assert "redis down" not in create_exc.value.message
        assert consume_exc.value.code == "state_store_unavailable"


class TestAssertionReplayCache:
    async def test_first_use_wins_second_is_a_replay(self, state_app: Quart) -> None:
        async with state_app.app_context():
            assert await sso_state.remember_assertion("conn:_a1", ttl_s=60) is True
            assert await sso_state.remember_assertion("conn:_a1", ttl_s=60) is False
            assert await sso_state.remember_assertion("conn:_a2", ttl_s=60) is True

    async def test_non_positive_ttl_is_clamped_not_rejected(
        self, state_app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with state_app.app_context():
            assert await sso_state.remember_assertion("conn:_a1", ttl_s=-50) is True
        assert fake_redis.keys_with_prefix("sso:saml:assertion:")


class TestRedisClientResolution:
    async def test_reuses_rate_limiter_connection(self) -> None:
        class _Limiter:
            _redis = object()

        app = Quart(__name__)
        app.config["HUB_API_CONFIG"] = hub_config()
        app.config[RATE_LIMITER_CONFIG_KEY] = _Limiter()
        async with app.app_context():
            assert sso_state._redis_client() is _Limiter._redis

    async def test_lazily_opens_and_caches_a_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opened: list[str] = []

        class _Client:
            pass

        def _from_url(url: str, **_kwargs: Any) -> _Client:
            opened.append(url)
            return _Client()

        import redis.asyncio as redis_asyncio

        monkeypatch.setattr(redis_asyncio, "from_url", _from_url)
        app = Quart(__name__)
        cfg: HubAPIConfig = hub_config()
        app.config["HUB_API_CONFIG"] = cfg
        async with app.app_context():
            first = sso_state._redis_client()
            second = sso_state._redis_client()
        assert first is second
        assert opened == [cfg.valkey_url]


def _http(handler: Any, *, settings: SsoSettings | None = None) -> SsoHttp:
    return SsoHttp(
        settings or SsoSettings(), protocol="oidc", transport=httpx.MockTransport(handler)
    )


class TestOutboundUrlGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://idp.example.com/x",
            "ftp://idp.example.com/x",
            "file:///etc/passwd",
            "javascript:alert(1)",
        ],
    )
    async def test_non_https_schemes_are_rejected(self, url: str) -> None:
        with pytest.raises(SsoConfigError) as exc:
            await check_outbound_url(url, SsoSettings())
        assert exc.value.code in {"idp_url_insecure", "idp_url_invalid"}

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/x",
            "https://169.254.169.254/latest/meta-data",
            "https://10.1.2.3/x",
            "https://[::1]/x",
            "https://private-idp.corp.test/x",  # resolves to 10.0.0.5
            "https://never-resolves.invalid/x",
        ],
    )
    async def test_private_loopback_metadata_and_unresolvable_hosts_are_blocked(
        self, url: str
    ) -> None:
        with pytest.raises(SsoConfigError) as exc:
            await check_outbound_url(url, SsoSettings())
        assert exc.value.code == "idp_url_blocked"

    async def test_credentials_in_url_are_rejected(self) -> None:
        with pytest.raises(SsoConfigError) as exc:
            await check_outbound_url("https://user:pw@idp.example.com/x", SsoSettings())
        assert exc.value.code == "idp_url_invalid"

    async def test_hostless_url_is_rejected(self) -> None:
        with pytest.raises(SsoConfigError):
            await check_outbound_url("https:///path", SsoSettings())

    async def test_public_https_host_is_allowed(self) -> None:
        await check_outbound_url("https://idp.example.com/.well-known/x", SsoSettings())

    async def test_operator_allowlist_permits_a_private_host_over_http_or_https(self) -> None:
        settings = SsoSettings(allowed_private_hosts=frozenset({"private-idp.corp.test"}))
        await check_outbound_url("https://private-idp.corp.test/x", settings)
        await check_outbound_url("http://private-idp.corp.test/x", settings)

    async def test_allowlist_is_exact_host_only(self) -> None:
        settings = SsoSettings(allowed_private_hosts=frozenset({"private-idp.corp.test"}))
        with pytest.raises(SsoConfigError):
            await check_outbound_url("https://evil.private-idp.corp.test/x", settings)


class TestGuardedClient:
    async def test_get_json_happy_path(self) -> None:
        http = _http(lambda r: httpx.Response(200, json={"ok": True}))
        assert await http.get_json("https://idp.example.com/a", operation="discovery") == {
            "ok": True
        }

    async def test_post_form_sends_form_and_basic_auth(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("authorization")
            seen["ctype"] = request.headers.get("content-type")
            return httpx.Response(200, json={"id_token": "x"})

        out = await _http(handler).post_form(
            "https://idp.example.com/token",
            {"a": "1"},
            operation="token",
            basic_auth=("cid", "sec"),
        )
        assert out == {"id_token": "x"}
        assert seen["body"] == "a=1"
        assert seen["auth"].startswith("Basic ")
        assert "x-www-form-urlencoded" in seen["ctype"]

    async def test_guard_runs_before_the_socket_is_touched(self) -> None:
        touched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            touched.append(str(request.url))
            return httpx.Response(200, json={})

        with pytest.raises(SsoConfigError):
            await _http(handler).get_json("https://169.254.169.254/x", operation="discovery")
        assert touched == []

    async def test_redirects_are_not_followed(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(handler).get_json("https://idp.example.com/a", operation="discovery")
        assert exc.value.code == "idp_http_status"
        assert calls == ["https://idp.example.com/a"]

    async def test_non_200_reports_status_and_whitelisted_oauth_error_only(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "<script>"}
            )

        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(handler).post_form("https://idp.example.com/t", {}, operation="token")
        assert "HTTP 400" in exc.value.message
        assert "invalid_grant" in exc.value.message
        assert "<script>" not in exc.value.message

    async def test_hostile_error_value_is_not_echoed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Robert'); DROP TABLE x;--"})

        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(handler).get_json("https://idp.example.com/a", operation="jwks")
        assert "DROP" not in exc.value.message

    async def test_non_json_error_body(self) -> None:
        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(
                lambda r: httpx.Response(502, content=b"<html>bad gateway</html>")
            ).get_json("https://idp.example.com/a", operation="jwks")
        assert exc.value.message == "IdP returned HTTP 502"

    async def test_invalid_json_body(self) -> None:
        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(lambda r: httpx.Response(200, content=b"not json")).get_json(
                "https://idp.example.com/a", operation="discovery"
            )
        assert exc.value.code == "idp_bad_json"

    async def test_oversized_response_is_cut_off(self) -> None:
        big = b"{" + b" " * (MAX_RESPONSE_BYTES + 10) + b"}"
        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(lambda r: httpx.Response(200, content=big)).get_json(
                "https://idp.example.com/a", operation="discovery"
            )
        assert exc.value.code == "idp_response_too_large"

    async def test_transport_errors_never_leak_the_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "connect failed for https://idp.example.com/secret-path?token=abc"
            )

        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(handler).get_json("https://idp.example.com/a", operation="discovery")
        assert exc.value.code == "idp_unreachable"
        assert "secret-path" not in exc.value.message
        assert "token=abc" not in exc.value.message
        assert "ConnectError" in exc.value.message

    async def test_timeout_is_an_idp_unavailable_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        with pytest.raises(SsoIdpUnavailableError) as exc:
            await _http(handler).get_json("https://idp.example.com/a", operation="jwks")
        assert "ReadTimeout" in exc.value.message
