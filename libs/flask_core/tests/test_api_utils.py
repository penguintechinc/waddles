"""Tests for flask_core.api_utils against a real Quart app.

Auth tests mint real platform JWTs with ``auth.create_jwt_token`` and resolve
API keys through a REAL pydal/SQLite database (so the generated SQL -- not a
fake that ignores query expressions -- decides whether a key verifies).
"""

import logging
from datetime import datetime, timedelta

import pytest
from pydal import DAL, Field
from quart import Quart, request

from flask_core import api_utils as au
from flask_core import auth
from flask_core.rate_limiter import RateLimiter
from flask_core.api_utils import (
    async_endpoint,
    auth_required,
    cors_headers,
    create_health_blueprint,
    error_response,
    paginate_response,
    rate_limit,
    record_request_metrics,
    success_response,
)

SECRET = "test-secret-key-not-for-production-use-only"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)


@pytest.fixture
def clean_metrics():
    saved = {k: (list(v) if isinstance(v, list) else v) for k, v in au._metrics.items()}
    au._metrics.update(requests_total=0, requests_success=0, requests_error=0, request_duration_seconds=[])
    yield au._metrics
    au._metrics.clear()
    au._metrics.update(saved)


class RealDal:
    """Async facade over a real in-memory pydal DB (the shape verify_api_key_async expects)."""

    def __init__(self):
        self.db = DAL("sqlite:memory")
        self.db.define_table(
            "api_keys",
            Field("key_hash"),
            Field("is_active", "boolean", default=True),
            Field("user_id", "integer"),
            Field("name"),
            Field("expires_at", "datetime"),
            Field("last_used_at", "datetime"),
            Field("permissions", "list:string"),
        )
        self.db.define_table("auth_user", Field("username"), Field("email"))
        self.api_keys = self.db.api_keys
        self.auth_user = self.db.auth_user

    async def select_async(self, query):
        return self.db(query).select()

    async def update_async(self, query, **values):
        return self.db(query).update(**values)


@pytest.fixture
def dal():
    d = RealDal()
    uid = d.db.auth_user.insert(username="alice", email="alice@example.invalid")
    d.db.api_keys.insert(
        key_hash=auth.hash_api_key("wa-good"), is_active=True, user_id=uid, name="ci",
        permissions=["community:read"],
    )
    d.db.api_keys.insert(
        key_hash=auth.hash_api_key("wa-off"), is_active=False, user_id=uid, name="off", permissions=[]
    )
    d.db.api_keys.insert(
        key_hash=auth.hash_api_key("wa-old"), is_active=True, user_id=uid, name="old",
        expires_at=datetime.utcnow() - timedelta(days=1), permissions=[],
    )
    return d


def make_app() -> Quart:
    app = Quart("api-utils-test")

    @app.route("/protected")
    @auth_required
    async def protected():
        return success_response({"user": request.current_user["username"], "type": request.current_user["auth_type"]})

    @app.route("/limited")
    @rate_limit(requests_per_minute=2)
    async def limited():
        return success_response("ok")

    @app.route("/ep/<kind>")
    @async_endpoint
    async def ep(kind):
        if kind == "ok":
            return success_response({"ok": True})
        if kind == "value":
            raise ValueError("bad input")
        if kind == "perm":
            raise PermissionError("nope")
        raise RuntimeError("kaboom")

    @app.route("/cors/<kind>", methods=["GET", "OPTIONS"])
    @cors_headers(origins=["https://a.io", "https://b.io"], methods=["GET"], headers=["X-T"])
    async def cors(kind):
        if kind == "tuple":
            return success_response({"k": 1}, status_code=201)
        if kind == "dict":
            return {"plain": True}
        if kind == "str":
            return "just text"
        return await _resp()

    async def _resp():
        from quart import jsonify

        return jsonify({"resp": True})

    @app.route("/page")
    async def page():
        return paginate_response(["a", "b"], total=45, page=2, per_page=10, endpoint="page")

    @app.route("/page-plain")
    async def page_plain():
        return paginate_response([], total=0, page=1, per_page=10)

    app.register_blueprint(create_health_blueprint("modx", "9.9"))
    return app


def token(**kw) -> str:
    return auth.create_jwt_token("u1", "alice", "alice@example.invalid", ["admin"], SECRET, tenant="t1", **kw)


class TestResponseHelpers:
    async def test_success_response_shape(self):
        app = make_app()
        async with app.test_request_context("/"):
            resp, status = success_response({"a": 1}, status_code=201, message="made", meta={"m": 1})
            body = await resp.get_json()
        assert status == 201
        assert body["success"] is True and body["data"] == {"a": 1}
        assert body["message"] == "made" and body["meta"] == {"m": 1} and "timestamp" in body

    async def test_success_response_omits_optional(self):
        app = make_app()
        async with app.test_request_context("/"):
            resp, _ = success_response(None)
            body = await resp.get_json()
        assert "message" not in body and "meta" not in body

    async def test_error_response_defaults_and_details(self):
        app = make_app()
        async with app.test_request_context("/"):
            resp, status = error_response("bad", status_code=422, details={"f": "x"})
            body = await resp.get_json()
            resp2, status2 = error_response("coded", error_code="E1")
            body2 = await resp2.get_json()
        assert status == 422 and body["error"]["code"] == "ERROR_422" and body["error"]["details"] == {"f": "x"}
        assert status2 == 400 and body2["error"]["code"] == "E1" and "details" not in body2["error"]

    async def test_paginate_with_links(self):
        resp = await make_app().test_client().get("/page")
        body = await resp.get_json()
        p = body["meta"]["pagination"]
        assert (p["page"], p["per_page"], p["total"], p["total_pages"]) == (2, 10, 45, 5)
        assert p["has_next"] is True and p["has_prev"] is True
        assert p["links"]["next"].endswith("?page=3&per_page=10")
        assert p["links"]["prev"].endswith("?page=1&per_page=10")
        assert p["links"]["last"].endswith("?page=5&per_page=10")

    async def test_paginate_without_endpoint_has_no_links(self):
        resp = await make_app().test_client().get("/page-plain")
        p = (await resp.get_json())["meta"]["pagination"]
        assert "links" not in p and p["total_pages"] == 0
        assert p["has_next"] is False and p["has_prev"] is False


class TestMetricsRecording:
    def test_counts_and_trim(self, clean_metrics):
        record_request_metrics(True, 0.1)
        record_request_metrics(False, 0.2)
        assert (clean_metrics["requests_total"], clean_metrics["requests_success"], clean_metrics["requests_error"]) == (2, 1, 1)
        clean_metrics["request_duration_seconds"] = [0.0] * 1000
        record_request_metrics(True, 9.9)
        assert len(clean_metrics["request_duration_seconds"]) == 1000
        assert clean_metrics["request_duration_seconds"][-1] == 9.9


class TestHealthBlueprint:
    async def test_health(self):
        resp = await make_app().test_client().get("/health")
        body = await resp.get_json()
        assert resp.status_code == 200
        assert (body["status"], body["module"], body["version"]) == ("healthy", "modx", "9.9")

    async def test_healthz_ok(self, monkeypatch):
        class Mem:
            percent = 10

        monkeypatch.setattr(au.psutil, "virtual_memory", lambda: Mem())
        monkeypatch.setattr(au.psutil, "cpu_percent", lambda interval=None: 5)
        resp = await make_app().test_client().get("/healthz")
        body = await resp.get_json()
        assert resp.status_code == 200 and body["status"] == "healthy"
        assert body["checks"] == {"memory": "ok", "cpu": "ok"}

    async def test_healthz_degraded_on_memory_and_cpu(self, monkeypatch):
        class Mem:
            percent = 95

        monkeypatch.setattr(au.psutil, "virtual_memory", lambda: Mem())
        monkeypatch.setattr(au.psutil, "cpu_percent", lambda interval=None: 99)
        resp = await make_app().test_client().get("/healthz")
        body = await resp.get_json()
        assert resp.status_code == 503 and body["status"] == "degraded"
        assert body["checks"] == {"memory": "warning", "cpu": "warning"}

    async def test_healthz_probe_failure_is_unhealthy(self, monkeypatch):
        def boom():
            raise OSError("no /proc")

        monkeypatch.setattr(au.psutil, "virtual_memory", boom)
        resp = await make_app().test_client().get("/healthz")
        body = await resp.get_json()
        assert resp.status_code == 503 and body["status"] == "unhealthy" and "no /proc" in body["error"]

    async def test_metrics_prometheus_text(self, clean_metrics):
        record_request_metrics(True, 0.25)
        record_request_metrics(False, 0.75)
        resp = await make_app().test_client().get("/metrics")
        text = (await resp.get_data()).decode()
        assert resp.status_code == 200 and resp.mimetype == "text/plain"
        assert 'waddlebot_info{module="modx",version="9.9"} 1' in text
        assert 'waddlebot_requests_total{module="modx"} 2' in text
        assert 'waddlebot_requests_error_total{module="modx"} 1' in text
        assert 'waddlebot_request_duration_seconds{module="modx"} 0.500000' in text
        assert "waddlebot_memory_bytes" in text and "waddlebot_threads" in text

    async def test_metrics_failure_returns_500(self, monkeypatch):
        def boom(pid):
            raise RuntimeError("psutil gone")

        monkeypatch.setattr(au.psutil, "Process", boom)
        resp = await make_app().test_client().get("/metrics")
        assert resp.status_code == 500
        assert "psutil gone" in (await resp.get_json())["error"]


class TestAsyncEndpoint:
    async def test_success_passthrough(self):
        resp = await make_app().test_client().get("/ep/ok")
        assert resp.status_code == 200 and (await resp.get_json())["data"] == {"ok": True}

    @pytest.mark.parametrize(
        "kind,status,msg",
        [("value", 400, "bad input"), ("perm", 403, "nope"), ("boom", 500, "Internal server error")],
    )
    async def test_exception_mapping(self, kind, status, msg):
        resp = await make_app().test_client().get(f"/ep/{kind}")
        body = await resp.get_json()
        assert resp.status_code == status
        assert body["success"] is False and body["error"]["message"] == msg

    async def test_server_error_does_not_leak_detail(self):
        resp = await make_app().test_client().get("/ep/boom")
        assert "kaboom" not in (await resp.get_data()).decode()

    async def test_db_driver_error_logged_sanitised_not_leaked(self, caplog):
        """A DB driver error carrying a bound value must not reach the log message or response."""
        import sqlite3

        app = Quart("db-err")
        # Built outside the raising frame: a traceback renders the *source line*, so a literal
        # here would show up in the log for reasons unrelated to the exception message.
        bound_value = "victim" + "@example.invalid"
        message = f"UNIQUE failed: users.email = '{bound_value}'"

        @app.route("/x")
        @async_endpoint
        async def x():
            raise sqlite3.IntegrityError(message)

        with caplog.at_level(logging.DEBUG, logger=au.logger.name):
            resp = await app.test_client().get("/x")
        assert resp.status_code == 500
        assert "victim@example.invalid" not in (await resp.get_data()).decode()
        assert "victim@example.invalid" not in caplog.text
        assert any("sanitized traceback" in r.getMessage() for r in caplog.records)


class TestAuthRequired:
    async def test_no_credentials_401(self):
        resp = await make_app().test_client().get("/protected")
        body = await resp.get_json()
        assert resp.status_code == 401 and body["error"]["code"] == "UNAUTHORIZED"

    async def test_valid_jwt(self):
        resp = await make_app().test_client().get("/protected", headers={"Authorization": f"Bearer {token()}"})
        body = await resp.get_json()
        assert resp.status_code == 200
        assert body["data"] == {"user": "alice", "type": "jwt"}

    async def test_invalid_jwt_rejected(self):
        resp = await make_app().test_client().get("/protected", headers={"Authorization": "Bearer not.a.jwt"})
        assert resp.status_code == 401

    async def test_jwt_signed_with_other_key_rejected(self):
        bad = auth.create_jwt_token("u1", "alice", "a@e.invalid", [], "x" * 40, tenant="t1")
        resp = await make_app().test_client().get("/protected", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401

    async def test_non_bearer_authorization_without_api_key_rejected(self):
        resp = await make_app().test_client().get("/protected", headers={"Authorization": "Basic abc"})
        assert resp.status_code == 401

    async def test_api_key_resolved_through_real_sql(self, dal):
        app = make_app()
        app.config["dal"] = dal
        resp = await app.test_client().get("/protected", headers={"X-API-Key": "wa-good"})
        body = await resp.get_json()
        assert resp.status_code == 200, body
        assert body["data"] == {"user": "alice", "type": "api_key"}
        # last_used_at was written back
        assert dal.db(dal.db.api_keys.name == "ci").select().first().last_used_at is not None

    @pytest.mark.parametrize("key", ["wa-unknown", "wa-off", "wa-old"])
    async def test_api_key_unknown_inactive_expired_rejected(self, dal, key):
        app = make_app()
        app.config["dal"] = dal
        resp = await app.test_client().get("/protected", headers={"X-API-Key": key})
        assert resp.status_code == 401

    async def test_api_key_without_dal_rejected(self):
        resp = await make_app().test_client().get("/protected", headers={"X-API-Key": "wa-good"})
        assert resp.status_code == 401


def _real_limiter() -> RateLimiter:
    """The production RateLimiter in its in-memory mode (no Redis server in unit tests)."""
    rl = RateLimiter(redis_url=None)
    rl._fallback_enabled = True
    return rl


class TestRateLimitDecorator:
    async def test_no_limiter_configured_passes_through(self):
        resp = await make_app().test_client().get("/limited")
        assert resp.status_code == 200

    async def test_allows_then_blocks_with_429_using_real_rate_limiter(self):
        app = make_app()
        limiter = _real_limiter()
        app.config["rate_limiter"] = limiter
        client = app.test_client()
        assert (await client.get("/limited")).status_code == 200
        assert (await client.get("/limited")).status_code == 200
        blocked = await client.get("/limited")
        body = await blocked.get_json()
        assert blocked.status_code == 429 and body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        (key,) = limiter._fallback_cache
        assert key.startswith("rate_limit:rate_limit:") and key.endswith(":limited")
        assert len(limiter._fallback_cache[key]) == 2

    async def test_key_uses_authenticated_user_when_present(self):
        app = Quart("rl")
        limiter = _real_limiter()
        app.config["rate_limiter"] = limiter

        @app.route("/r")
        @rate_limit(5)
        async def r():
            return "x"

        @app.before_request
        async def attach():
            request.current_user = {"user_id": "user-42"}

        await app.test_client().get("/r")
        assert list(limiter._fallback_cache) == ["rate_limit:rate_limit:user-42:r"]


class TestCorsHeaders:
    async def test_tuple_response_gets_headers_and_keeps_status(self):
        resp = await make_app().test_client().get("/cors/tuple")
        assert resp.status_code == 201
        assert resp.headers["Access-Control-Allow-Origin"] == "https://a.io, https://b.io"
        assert resp.headers["Access-Control-Allow-Methods"] == "GET"
        assert resp.headers["Access-Control-Allow-Headers"] == "X-T"
        assert resp.headers["Access-Control-Max-Age"] == "3600"

    async def test_preflight_short_circuits_handler(self):
        resp = await make_app().test_client().options("/cors/tuple")
        assert resp.status_code == 200
        assert (await resp.get_json()) == {"status": "ok"}
        assert "Access-Control-Allow-Origin" in resp.headers

    async def test_dict_response_is_jsonified(self):
        resp = await make_app().test_client().get("/cors/dict")
        assert resp.status_code == 200 and (await resp.get_json()) == {"plain": True}
        assert "Access-Control-Allow-Origin" in resp.headers

    async def test_bare_response_object(self):
        resp = await make_app().test_client().get("/cors/resp")
        assert (await resp.get_json()) == {"resp": True}
        assert "Access-Control-Allow-Origin" in resp.headers

    async def test_string_response(self):
        # regression: a bare str body has no .headers and used to raise AttributeError (500)
        resp = await make_app().test_client().get("/cors/str")
        assert resp.status_code == 200
        assert (await resp.get_data()).decode() == "just text"
        assert "Access-Control-Allow-Origin" in resp.headers
