"""`services/odoo_sync_service.py` tests -- real sync path against an in-memory fake Odoo.

The fake Odoo is an `httpx.MockTransport` implementing the real `/jsonrpc`
wire contract (`common.authenticate`, `object.execute_kw` search_read/
create/write), so `OdooClient`, retry/backoff, upsert and PII-boundary code
all run for real -- only the network socket is replaced. DB state uses the
`bar_citizen_db` fixture (real pydal sqlite).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from services import odoo_sync_service as svc

_KEY = "ref-key-not-a-secret"  # gitleaks:allow - test fixture value
_ENV = {
    "ODOO_URL": "https://odoo.test/",
    "ODOO_DB": "waddledb",
    "ODOO_LOGIN": "svc-waddles",
    "ODOO_API_KEY": "odoo-api-key-test",  # gitleaks:allow
    "ODOO_SYNC_REF_KEY": _KEY,
    "ODOO_BACKOFF_BASE_SECONDS": "0",
}


class FakeOdoo:
    """In-memory Odoo speaking the real JSON-RPC envelope; records every request."""

    def __init__(self, *, fail_first: int = 0, bad_auth: bool = False, status: int = 200) -> None:
        """Configure failure injection (503s, bad auth, fixed HTTP status)."""
        self.records: dict[int, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.bad_auth = bad_auth
        self.status = status
        self.rpc_error: str | None = None
        self._next = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        """MockTransport handler."""
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503)
        if self.status != 200:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        self.requests.append(body)
        p = body["params"]
        if self.rpc_error:
            return httpx.Response(
                200, json={"error": {"message": "x", "data": {"message": self.rpc_error}}}
            )
        if p["service"] == "common":
            return httpx.Response(200, json={"result": False if self.bad_auth else 7})
        db, uid, key, model, method, args, _kw = p["args"]
        assert (db, uid, key) == ("waddledb", 7, "odoo-api-key-test")
        if method == "search_read":
            refs = args[0][0][2]
            rows = [
                {"id": i, "x_waddles_ref": r["x_waddles_ref"]}
                for i, r in self.records.items()
                if r["x_waddles_ref"] in refs
            ]
            return httpx.Response(200, json={"result": rows})
        if method == "create":
            rid = self._next
            self._next += 1
            self.records[rid] = dict(args[0][0])
            return httpx.Response(200, json={"result": [rid]})
        if method == "write":
            self.records[args[0][0]].update(args[1])
            return httpx.Response(200, json={"result": True})
        raise AssertionError(method)


def _client(fake: FakeOdoo, **overrides: str) -> tuple[svc.OdooClient, svc.OdooConfig]:
    cfg = svc.OdooConfig.from_env({**_ENV, **overrides})
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return svc.OdooClient(cfg, http=http), cfg


def _add_member(
    dal: Any, community_id: int, user: str, role: str = "member", rep: int = 600
) -> None:
    dal.community_members.insert(
        community_id=community_id,
        user_id=user,
        platform="twitch",
        platform_user_id=f"tw-{user}",
        display_name=f"RealName-{user}",
        role=role,
        reputation=rep,
        is_active=True,
    )
    dal.commit()


class TestConfig:
    """Env loading fails loud."""

    def test_missing_vars_named(self) -> None:
        with pytest.raises(svc.OdooConfigError, match="ODOO_URL.*ODOO_API_KEY"):
            svc.OdooConfig.from_env({"ODOO_DB": "d", "ODOO_LOGIN": "l", "ODOO_SYNC_REF_KEY": "k"})

    def test_bad_url(self) -> None:
        with pytest.raises(svc.OdooConfigError, match="http"):
            svc.OdooConfig.from_env({**_ENV, "ODOO_URL": "ftp://x"})

    def test_repr_hides_secrets(self) -> None:
        cfg = svc.OdooConfig.from_env(_ENV)
        assert "odoo-api-key-test" not in repr(cfg) and _KEY not in repr(cfg)
        assert cfg.url == "https://odoo.test" and cfg.model == "res.partner"

    def test_from_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("ODOO_SYNC_MODEL", "x.model")
        assert svc.OdooConfig.from_env().model == "x.model"


class TestMemberRef:
    """Opaque reference properties."""

    def test_stable_and_scoped(self) -> None:
        a = svc.member_ref(b"k", 1, "u")
        assert a == svc.member_ref(b"k", 1, "u")
        assert a != svc.member_ref(b"k", 2, "u")
        assert a != svc.member_ref(b"k2", 1, "u")
        assert len(a) == 36


class TestClient:
    """Wire behaviour: auth, retry/backoff, error taxonomy."""

    async def test_auth_cached(self) -> None:
        fake = FakeOdoo()
        client, _ = _client(fake)
        assert await client.authenticate() == 7
        assert await client.authenticate() == 7
        assert len(fake.requests) == 1
        await client.aclose()

    async def test_bad_credentials_not_retried(self) -> None:
        fake = FakeOdoo(bad_auth=True)
        client, _ = _client(fake)
        with pytest.raises(svc.OdooAuthError):
            await client.authenticate()
        assert len(fake.requests) == 1

    async def test_transient_retries_then_succeeds(self) -> None:
        fake = FakeOdoo(fail_first=2)
        client, _ = _client(fake)
        assert await client.authenticate() == 7

    async def test_transient_exhausted_raises(self) -> None:
        fake = FakeOdoo(fail_first=99)
        client, _ = _client(fake, ODOO_MAX_ATTEMPTS="2")
        with pytest.raises(svc.OdooTransientError, match="2 attempts"):
            await client.authenticate()

    async def test_transport_error_retried(self) -> None:
        calls = {"n": 0}

        def boom(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("down", request=request)

        cfg = svc.OdooConfig.from_env({**_ENV, "ODOO_MAX_ATTEMPTS": "3"})
        client = svc.OdooClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
        with pytest.raises(svc.OdooTransientError):
            await client.authenticate()
        assert calls["n"] == 3

    async def test_4xx_not_retried(self) -> None:
        fake = FakeOdoo(status=403)
        client, _ = _client(fake)
        with pytest.raises(svc.OdooRpcError, match="403"):
            await client.authenticate()

    async def test_429_is_retried(self) -> None:
        seen = {"n": 0}

        def h(request: httpx.Request) -> httpx.Response:
            seen["n"] += 1
            return (
                httpx.Response(429) if seen["n"] == 1 else httpx.Response(200, json={"result": 7})
            )

        cfg = svc.OdooConfig.from_env(_ENV)
        client = svc.OdooClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
        assert await client.authenticate() == 7

    async def test_rpc_error_not_retried(self) -> None:
        fake = FakeOdoo()
        fake.rpc_error = "Access Denied"
        client, _ = _client(fake)
        with pytest.raises(svc.OdooRpcError, match="Access Denied"):
            await client.authenticate()
        assert len(fake.requests) == 1

    async def test_rpc_error_without_data_message(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": {"message": "plain"}})

        cfg = svc.OdooConfig.from_env(_ENV)
        client = svc.OdooClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
        with pytest.raises(svc.OdooRpcError, match="plain"):
            await client.authenticate()

    async def test_create_accepts_bare_int(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            p = json.loads(request.content)["params"]
            return httpx.Response(200, json={"result": 7 if p["service"] == "common" else 42})

        cfg = svc.OdooConfig.from_env(_ENV)
        client = svc.OdooClient(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
        assert await client.create({"a": 1}) == 42

    async def test_owned_http_closed(self) -> None:
        client = svc.OdooClient(svc.OdooConfig.from_env(_ENV))
        await client.aclose()
        assert client._http.is_closed


class TestSync:
    """End-to-end sync_community / batch against fake Odoo + real DB."""

    async def test_create_then_idempotent_update(self, bar_citizen_db: Any) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "1", "admin", 900)
        _add_member(dal, community_id, "2")
        fake = FakeOdoo()
        client, cfg = _client(fake)

        assert await svc.sync_community(dal, client, cfg, community_id) == (2, 2, 0)
        assert len(fake.records) == 2
        # second run: no duplicates, all updates; reputation change propagates
        dal(dal.community_members.user_id == "2").update(reputation=777)
        dal.commit()
        assert await svc.sync_community(dal, client, cfg, community_id) == (2, 0, 2)
        assert len(fake.records) == 2
        assert sorted(r["x_waddles_reputation"] for r in fake.records.values()) == [777, 900]
        assert {r["x_waddles_role"] for r in fake.records.values()} == {"admin", "member"}

    async def test_no_pii_crosses_to_odoo(self, bar_citizen_db: Any) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "55")
        fake = FakeOdoo()
        client, cfg = _client(fake)
        await svc.sync_community(dal, client, cfg, community_id)
        wire = json.dumps(fake.requests)
        for needle in ("RealName-55", "tw-55", "twitch", '"55"'):
            assert needle not in wire
        rec = next(iter(fake.records.values()))
        assert set(rec) == {
            "name",
            "x_waddles_ref",
            "x_waddles_community_ref",
            "x_waddles_role",
            "x_waddles_reputation",
            "active",
        }
        assert rec["name"].startswith("Waddles member ")

    async def test_inactive_members_excluded_and_empty_community(self, bar_citizen_db: Any) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "9")
        dal(dal.community_members.user_id == "9").update(is_active=False)
        dal.commit()
        fake = FakeOdoo()
        client, cfg = _client(fake)
        assert await svc.sync_community(dal, client, cfg, community_id) == (0, 0, 0)
        assert fake.requests == []

    async def test_flag_off_by_default(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "1")
        monkeypatch.delenv(svc.ENV_BASELINE_VAR, raising=False)
        monkeypatch.setattr(svc, "feature_enabled", _flag(False))
        fake = FakeOdoo()
        client, cfg = _client(fake)
        summary = await svc.run_odoo_sync_batch(dal, client, cfg)
        assert summary.communities_examined >= 1
        assert summary.communities_flag_off == summary.communities_examined
        assert summary.communities_synced == 0 and fake.requests == []

    async def test_posthog_flag_on(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "1")
        monkeypatch.delenv(svc.ENV_BASELINE_VAR, raising=False)
        monkeypatch.setattr(svc, "feature_enabled", _flag(True))
        fake = FakeOdoo()
        client, cfg = _client(fake)
        summary = await svc.run_odoo_sync_batch(dal, client, cfg)
        assert summary.communities_synced == 1
        assert (summary.members_examined, summary.records_created) == (1, 1)

    async def test_env_baseline_enables_without_posthog(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, _, _ = bar_citizen_db
        _add_member(dal, community_id, "1")
        monkeypatch.setenv(svc.ENV_BASELINE_VAR, "true")
        monkeypatch.setattr(svc, "feature_enabled", None)
        fake = FakeOdoo()
        client, cfg = _client(fake)
        summary = await svc.run_odoo_sync_batch(dal, client, cfg)
        assert summary.communities_synced == 1

    async def test_community_failure_isolated_and_logged(
        self,
        bar_citizen_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dal, community_id, tenant_id, _ = bar_citizen_db
        other = dal.communities.insert(name="second", tenant_id=tenant_id)
        dal.commit()
        _add_member(dal, community_id, "1")
        _add_member(dal, int(other), "2")
        monkeypatch.setenv(svc.ENV_BASELINE_VAR, "1")
        fake = FakeOdoo(bad_auth=True)
        client, cfg = _client(fake)
        with caplog.at_level(logging.ERROR):
            summary = await svc.run_odoo_sync_batch(dal, client, cfg)
        assert summary.communities_failed == 2 and summary.communities_synced == 0
        err = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert err and err[0].exc_info is not None
        assert "odoo-api-key-test" not in caplog.text and "RealName" not in caplog.text

    async def test_community_without_tenant(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, _, _, _ = bar_citizen_db
        dal.communities.insert(name="orphan")
        dal.commit()
        monkeypatch.delenv(svc.ENV_BASELINE_VAR, raising=False)
        monkeypatch.setattr(svc, "feature_enabled", _flag(False))
        client, cfg = _client(FakeOdoo())
        summary = await svc.run_odoo_sync_batch(dal, client, cfg)
        assert summary.communities_flag_off == summary.communities_examined >= 2


def _flag(value: bool) -> Any:
    async def fake(flag: str, *, tenant: str, **_: Any) -> bool:
        assert flag == svc.FEATURE_BAR_CITIZEN_ODOO_SYNC
        return value

    return fake


class TestMain:
    """CronJob entrypoint exit codes + printed denominators."""

    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, dal: Any, summary: svc.SyncSummary) -> None:
        import services.bundle_install_dal as bid

        class _Inst:
            pass

        inst = _Inst()
        inst.dal = dal  # type: ignore[attr-defined]

        async def build(url: str, pool_size: int = 1) -> Any:
            return inst

        async def run(*_a: Any, **_k: Any) -> svc.SyncSummary:
            return summary

        monkeypatch.setattr(bid, "build_install_dal", build)
        monkeypatch.setattr(svc, "run_odoo_sync_batch", run)
        for k, v in {**_ENV, "DATABASE_URL": "sqlite://x"}.items():
            monkeypatch.setenv(k, v)

    async def test_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._patch(
            monkeypatch, object(), svc.SyncSummary(communities_examined=2, communities_synced=2)
        )
        assert await svc.main() == 0
        assert "communities_examined=2" in capsys.readouterr().out

    async def test_zero_examined_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, object(), svc.SyncSummary())
        assert await svc.main() == 1

    async def test_failures_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(
            monkeypatch,
            object(),
            svc.SyncSummary(communities_examined=1, communities_failed=1),
        )
        assert await svc.main() == 1

    async def test_missing_config_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in _ENV:
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(svc.OdooConfigError):
            await svc.main()
