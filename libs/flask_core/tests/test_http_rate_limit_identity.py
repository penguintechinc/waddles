"""Identity bucketing + lifecycle for flask_core.http_rate_limit (real Quart app, real JWTs).

The Redis server is not available, so the installed RateLimiter runs in its real in-memory
mode; the sliding-window enforcement, bucket selection and 429 contract are all real.
"""

import pytest
from quart import Quart

from flask_core import http_rate_limit as hrl
from flask_core.auth import create_jwt_token
from flask_core.http_rate_limit import RATE_LIMITER_CONFIG_KEY, install_rate_limiting

SECRET = "test-secret-key-not-for-production-use-only"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    for k in ("RATE_LIMIT_MAX_REQUESTS", "RATE_LIMIT_WINDOW_SECONDS",
              "RATE_LIMIT_AUTH_MAX_REQUESTS", "RATE_LIMIT_AUTH_WINDOW_SECONDS"):
        monkeypatch.delenv(k, raising=False)


def bearer(sub: str) -> dict[str, str]:
    tok = create_jwt_token(sub, "name", "n@e.invalid", [], SECRET, tenant="t")
    return {"Authorization": f"Bearer {tok}"}


def make_app(**kwargs) -> Quart:
    app = Quart("rl-identity")

    @app.route("/x")
    async def x():
        return {"ok": True}

    limiter = install_rate_limiting(app, namespace="svc", max_requests=2, window_seconds=60, **kwargs)
    limiter._fallback_enabled = True  # in-memory sliding window (no Redis in unit tests)
    return app


class TestPerUserBuckets:
    async def test_same_user_shares_a_bucket_across_ips(self):
        client = make_app(trusted_proxy_hops=1).test_client()
        h = lambda ip: {**bearer("alice"), "X-Forwarded-For": ip}  # noqa: E731
        assert (await client.get("/x", headers=h("1.1.1.1"))).status_code == 200
        assert (await client.get("/x", headers=h("2.2.2.2"))).status_code == 200
        assert (await client.get("/x", headers=h("3.3.3.3"))).status_code == 429  # IP rotation does not help

    async def test_different_users_have_independent_buckets(self):
        client = make_app().test_client()
        for _ in range(2):
            assert (await client.get("/x", headers=bearer("alice"))).status_code == 200
        assert (await client.get("/x", headers=bearer("alice"))).status_code == 429
        assert (await client.get("/x", headers=bearer("bob"))).status_code == 200

    async def test_invalid_token_falls_back_to_ip_bucket(self):
        client = make_app().test_client()
        bad = {"Authorization": "Bearer not-a-jwt"}
        assert (await client.get("/x", headers=bad)).status_code == 200
        assert (await client.get("/x", headers=bad)).status_code == 200
        assert (await client.get("/x", headers=bad)).status_code == 429
        # an authenticated user is a different bucket from that anonymous IP
        assert (await client.get("/x", headers=bearer("carol"))).status_code == 200

    async def test_non_bearer_header_is_anonymous(self):
        client = make_app().test_client()
        for _ in range(2):
            await client.get("/x", headers={"Authorization": "Basic abc"})
        assert (await client.get("/x")).status_code == 429  # same anonymous IP bucket


class TestClientIpHelpers:
    async def _ident(self, headers, hops):
        app = Quart("ip")
        seen = {}

        @app.route("/ip")
        async def ip():
            seen["ip"] = hrl._client_ip(hops)
            return "ok"

        await app.test_client().get("/ip", headers=headers)
        return seen["ip"]

    async def test_header_absent_uses_remote_addr(self):
        assert await self._ident({}, hops=1) == "<local>"  # Quart test client peer

    async def test_more_trusted_hops_than_header_entries_falls_back(self):
        assert await self._ident({"X-Forwarded-For": "9.9.9.9"}, hops=3) == "<local>"  # Quart test client peer

    async def test_selects_hop_from_the_right(self):
        assert await self._ident({"X-Forwarded-For": "6.6.6.6, 7.7.7.7, 8.8.8.8"}, hops=2) == "7.7.7.7"

    async def test_zero_hops_ignores_header(self):
        assert await self._ident({"X-Forwarded-For": "6.6.6.6"}, hops=0) == "<local>"  # Quart test client peer


class TestLifecycleAndConfig:
    async def test_limiter_connects_and_disconnects_with_the_app(self, monkeypatch):
        events = []

        async def fake_connect(self):
            events.append("connect")

        async def fake_disconnect(self):
            events.append("disconnect")

        monkeypatch.setattr(hrl.RateLimiter, "connect", fake_connect)
        monkeypatch.setattr(hrl.RateLimiter, "disconnect", fake_disconnect)
        app = Quart("lifecycle")
        limiter = install_rate_limiting(app, namespace="svc")
        assert app.config[RATE_LIMITER_CONFIG_KEY] is limiter
        async with app.test_app():
            assert events == ["connect"]
        assert events == ["connect", "disconnect"]

    async def test_namespace_and_env_defaults(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "1")
        monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "30")
        app = Quart("env")

        @app.route("/x")
        async def x():
            return "ok"

        limiter = install_rate_limiting(app, namespace="mymod")
        limiter._fallback_enabled = True
        assert limiter.namespace == "mymod_rate_limit"
        client = app.test_client()
        assert (await client.get("/x")).status_code == 200
        blocked = await client.get("/x")
        assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "30"

    async def test_auth_env_overrides_apply_to_auth_prefix(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_AUTH_MAX_REQUESTS", "1")
        monkeypatch.setenv("RATE_LIMIT_AUTH_WINDOW_SECONDS", "15")
        app = Quart("authenv")

        @app.route("/auth/login")
        async def login():
            return "ok"

        limiter = install_rate_limiting(app, namespace="svc", auth_path_prefixes=["/auth"])
        limiter._fallback_enabled = True
        client = app.test_client()
        assert (await client.get("/auth/login")).status_code == 200
        blocked = await client.get("/auth/login")
        assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "15"
