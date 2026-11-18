"""Per-tenant license key + operator pool routes (issue #901) on a real sqlite DAL + real vault.

The key never appears in any response, audit event or captured log. The resolver integration test
runs the REAL `LicenseServerPoolProvider` (httpx) against a local stdlib HTTP server.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from flask_core.license_pool import LicenseServerPoolProvider, PoolUnavailable
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin_license_pools import superadmin_license_pools_bp
from blueprints.v1.tenant_license import tenant_license_bp
from services import community_tier_service as pools
from services import tenant_license_service as svc
from services.provider_connections import vault
from services.schema import bind_community_tier_tables, bind_tenant_license_key_tables
from tests.conftest import TENANT_SLUG, make_user_token

KEY_A = "PENG-AAAA-1111-BBBB-2222-SECRETA"
KEY_B = "PENG-CCCC-3333-DDDD-4444-SECRETB"
OTHER = "other-tenant"
BYPASS_HOST = "alpha.penguintech.cloud"


class RecordingAudit:
    """In-memory sink with the `AuditService.record` surface."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _seal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "cd" * 32)


@pytest.fixture
def env(tenant_admin_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    dal = tenant_admin_db.dal
    bind_tenant_license_key_tables(dal, migrate=True)
    bind_community_tier_tables(dal, migrate=True)
    other_id = int(dal.tenants.insert(slug=OTHER, display_name="Other", is_active=True))
    dal.tenants.insert(slug="system", display_name="System", is_active=True)  # operator plane
    dal.commit()
    for name in ("tenant_license_keys", "tenant_license_pools"):
        dal(dal[name]).count()
    tenant_id = int(dal(dal.tenants.slug == TENANT_SLUG).select().first().id)
    audit = RecordingAudit()
    import blueprints.v1.superadmin_license_pools as ops_mod
    import blueprints.v1.tenant_license as tl_mod

    monkeypatch.setattr(tl_mod, "get_audit_service", lambda: audit)
    monkeypatch.setattr(ops_mod, "get_audit_service", lambda: audit)
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(tenant_license_bp)
    app.register_blueprint(superadmin_license_pools_bp)
    app.config["dal"] = dal
    app.config["async_dal"] = tenant_admin_db
    return type(
        "Env",
        (),
        {
            "app": app,
            "client": app.test_client(),
            "adal": tenant_admin_db,
            "dal": dal,
            "tenant": tenant_id,
            "other": other_id,
            "audit": audit,
        },
    )


def _hdr(
    scope: str = "tenant:admin", tenant: str = TENANT_SLUG, user_id: int = 1
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope, tenant=tenant)}"
    }


def _ops(host: str = "localhost") -> dict[str, str]:
    return {**_hdr("tenants:admin", tenant="system"), "Host": host}


async def test_round_trip_status_set_clear_and_vault_version(env: Any) -> None:
    c = env.client
    r = await c.get(f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr())
    assert r.status_code == 200
    assert (await r.get_json())["license"] == {
        "configured": False,
        "fingerprint": None,
        "setAt": None,
    }

    r = await c.put(
        f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr(), json={"licenseKey": KEY_A}
    )
    body = await r.get_json()
    assert r.status_code == 200
    assert body["license"]["configured"] is True
    assert body["license"]["fingerprint"] == svc.fingerprint_of(KEY_A)
    assert KEY_A not in json.dumps(body)

    r = await c.get(f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr())
    assert KEY_A not in await r.get_data(as_text=True)

    row = env.dal(env.dal.tenant_license_keys.tenant_id == env.tenant).select().first()
    assert row.seal_version == vault.SEAL_AAD
    assert KEY_A not in row.license_key_sealed
    assert row.license_key_set_by == 1
    assert await svc.get_tenant_license_key(env.adal, env.dal, env.tenant) == KEY_A

    r = await c.delete(f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr())
    assert (await r.get_json()) == {"success": True, "cleared": True}
    assert await svc.get_tenant_license_key(env.adal, env.dal, env.tenant) is None
    r = await c.delete(f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr())
    assert (await r.get_json())["cleared"] is False

    actions = [e.action for e in env.audit.events]
    assert actions == ["license.tenant_key_set", "license.tenant_key_cleared"]
    assert KEY_A not in repr([e.details for e in env.audit.events])


async def test_key_never_in_logs(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await env.client.put(
        f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr(), json={"licenseKey": KEY_A}
    )
    # corrupt the row so the resolver's error path runs too
    env.dal(env.dal.tenant_license_keys.tenant_id == env.tenant).update(license_key_sealed="zz")
    env.dal.commit()
    assert await svc.get_tenant_license_key(env.adal, env.dal, env.tenant) is None
    assert "tenant_license.key_unavailable" in caplog.text
    assert KEY_A not in caplog.text and "SECRETA" not in caplog.text


@pytest.mark.parametrize(
    "bad", ["", "short", "has space in the key 1234567", "x" * 300, "ключ" * 8]
)
async def test_invalid_key_rejected_and_not_echoed(env: Any, bad: str) -> None:
    r = await env.client.put(
        f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr(), json={"licenseKey": bad}
    )
    assert r.status_code == 400
    assert bad not in await r.get_data(as_text=True) or bad == ""
    assert env.dal(env.dal.tenant_license_keys).count() == 0


async def test_tenant_isolation_and_scope(env: Any) -> None:
    other_hdr = _hdr(tenant=OTHER)
    await env.client.put(
        f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr(), json={"licenseKey": KEY_A}
    )
    for method, kw in (("get", {}), ("put", {"json": {"licenseKey": KEY_B}}), ("delete", {})):
        r = await getattr(env.client, method)(
            f"/api/v1/tenant/{TENANT_SLUG}/license", headers=other_hdr, **kw
        )
        assert r.status_code == 403
    assert await svc.get_tenant_license_key(env.adal, env.dal, env.tenant) == KEY_A
    r = await env.client.get(
        f"/api/v1/tenant/{TENANT_SLUG}/license", headers=_hdr(scope="tenant:read")
    )
    assert r.status_code == 403
    r = await env.client.get(f"/api/v1/tenant/{TENANT_SLUG}/license")
    assert r.status_code in (401, 403)


async def test_blob_copied_to_other_tenant_does_not_open(env: Any) -> None:
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None
    )
    sealed = env.dal(env.dal.tenant_license_keys.tenant_id == env.tenant).select().first()
    env.dal.tenant_license_keys.insert(
        tenant_id=env.other,
        license_key_sealed=sealed.license_key_sealed,
        seal_version=1,
        license_key_fingerprint="deadbeef",
        license_key_set_at=sealed.license_key_set_at,
    )
    env.dal.commit()
    assert await svc.get_tenant_license_key(env.adal, env.dal, env.other) is None


async def test_operator_pool_put_on_bypass_host_audited(env: Any) -> None:
    c = env.client
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2"
    r = await c.put(url, headers=_ops(BYPASS_HOST), json={"limit": 7})
    body = await r.get_json()
    assert r.status_code == 200, body
    assert body["bypassDomain"] is True
    assert body["pools"] == [
        {
            "tierCode": 2,
            "limit": 7,
            "source": "operator_set",
            "observedAt": body["pools"][0]["observedAt"],
        }
    ]
    row = await pools.PoolStore(env.adal, env.dal).get(env.tenant, 2)
    assert row is not None and row.source_code == pools.POOL_SOURCE_OPERATOR_SET and row.set_by == 1
    r = await c.put(url, headers=_ops(BYPASS_HOST), json={"limit": -1})
    assert [p["limit"] for p in (await r.get_json())["pools"]] == [-1]
    r = await c.put(url, headers=_ops(BYPASS_HOST), json={"limit": None})
    assert [p["limit"] for p in (await r.get_json())["pools"]] == [None]
    r = await c.get(
        f"/api/v1/superadmin/tenants/{env.tenant}/license-pools", headers=_ops(BYPASS_HOST)
    )
    assert r.status_code == 200
    ev = [e for e in env.audit.events if e.action == "license.tenant_pool_set"]
    assert len(ev) == 3 and ev[0].details == {"tier_code": 2, "limit": 7}


async def test_operator_pool_put_conflict_off_bypass_host(env: Any) -> None:
    r = await env.client.put(
        f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2",
        headers=_ops("example.com"),
        json={"limit": 3},
    )
    assert r.status_code == 409
    assert (await r.get_json())["error"]["code"] == "POOL_OPERATOR_SET_NOT_ALLOWED"
    assert await pools.PoolStore(env.adal, env.dal).get(env.tenant, 2) is None
    assert env.audit.events == []


@pytest.mark.parametrize(
    ("tier", "payload", "status"),
    [
        (1, {"limit": 3}, 400),
        (3, {"limit": 3}, 400),  # no Enterprise pool any more
        (4, {"limit": 3}, 400),
        (2, {"limit": -2}, 400),
        (2, {"limit": "x"}, 400),
        (2, {}, 400),  # omitted limit must NOT silently wipe the pool
        (2, {"limit": 2_147_483_648}, 400),  # beyond int32
        (2, {"limit": True}, 400),
    ],
)
async def test_operator_pool_validation(
    env: Any, tier: int, payload: dict[str, Any], status: int
) -> None:
    r = await env.client.put(
        f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/{tier}",
        headers=_ops(BYPASS_HOST),
        json=payload,
    )
    assert r.status_code == status


async def test_operator_pool_unknown_tenant_and_scope(env: Any) -> None:
    r = await env.client.put(
        "/api/v1/superadmin/tenants/99999/license-pools/2",
        headers=_ops(BYPASS_HOST),
        json={"limit": 1},
    )
    assert r.status_code == 404
    r = await env.client.put(
        f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2",
        headers={**_hdr("tenant:admin"), "Host": BYPASS_HOST},
        json={"limit": 1},
    )
    assert r.status_code == 403


class _Entitlements(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        out = json.dumps({"team": {"premium": {"limit": 4}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_a: Any) -> None:
        return None


@pytest.fixture
def license_server() -> Iterator[str]:
    _Entitlements.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Entitlements)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(timeout=5)


async def test_resolver_feeds_pool_provider_for_real(env: Any, license_server: str) -> None:
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None
    )

    async def resolver(tenant_id: int) -> str | None:
        return await svc.get_tenant_license_key(env.adal, env.dal, tenant_id)

    provider = LicenseServerPoolProvider(
        pools.PoolStore(env.adal, env.dal),
        resolver,
        lambda: False,
        base_url=license_server,
        max_attempts=1,
        allow_loopback_http=True,
    )
    info = await provider.get_pool(env.tenant, 2)
    assert (info.limit, info.source) == (4, "license_server")
    assert _Entitlements.seen[-1]["license_key"] == KEY_A

    calls = len(_Entitlements.seen)
    with pytest.raises(PoolUnavailable, match="no_tenant_license"):
        await provider.get_pool(env.other, 2)  # tenant without a key
    assert len(_Entitlements.seen) == calls  # never reached the server without a key


async def test_omitted_limit_does_not_wipe_an_existing_pool(env: Any) -> None:
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2"
    await env.client.put(url, headers=_ops(BYPASS_HOST), json={"limit": 5})
    r = await env.client.put(url, headers=_ops(BYPASS_HOST), json={})
    assert r.status_code == 400
    assert (await pools.PoolStore(env.adal, env.dal).get(env.tenant, 2)).limit_count == 5


async def test_operator_put_refused_when_license_server_and_tenant_key_are_set(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: a (spoofed-host) bypass cannot override a license-server pool."""
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2"
    monkeypatch.setenv("LICENSE_SERVER_URL", "https://license.example.test")
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None
    )
    r = await env.client.put(url, headers=_ops(BYPASS_HOST), json={"limit": 9})
    assert r.status_code == 409
    assert (await r.get_json())["error"]["code"] == "POOL_OPERATOR_SET_NOT_ALLOWED"
    assert await pools.PoolStore(env.adal, env.dal).get(env.tenant, 2) is None
    # no key set -> the operator path still works; key set but no license server -> works
    await svc.clear_tenant_license_key(env.adal, env.dal, tenant_id=env.tenant, actor=None)
    assert (
        await env.client.put(url, headers=_ops(BYPASS_HOST), json={"limit": 9})
    ).status_code == 200


@pytest.mark.parametrize(
    "spoof",
    [
        f"{BYPASS_HOST}@evil.example",
        f"evil.example@{BYPASS_HOST}",
        f"{BYPASS_HOST}.evil.example",
        f"{BYPASS_HOST}, evil.example",
    ],
)
async def test_spoofed_host_cannot_write_operator_pools(env: Any, spoof: str) -> None:
    r = await env.client.put(
        f"/api/v1/superadmin/tenants/{env.tenant}/license-pools/2",
        headers=_ops(spoof),
        json={"limit": 3},
    )
    assert r.status_code == 409
    assert await pools.PoolStore(env.adal, env.dal).get(env.tenant, 2) is None


async def test_key_change_evicts_license_server_pools_but_keeps_operator_rows(env: Any) -> None:
    store = pools.PoolStore(env.adal, env.dal)
    now = pools.now_utc()
    await store.put(env.tenant, 2, 4, pools.POOL_SOURCE_LICENSE_SERVER, now)
    await store.put(env.other, 2, 6, pools.POOL_SOURCE_OPERATOR_SET, now)
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None
    )
    assert await store.get(env.tenant, 2) is None  # cached pool gone on key set
    assert (await store.get(env.other, 2)).limit_count == 6
    await store.put(env.tenant, 2, 4, pools.POOL_SOURCE_LICENSE_SERVER, now)
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_B, actor=None
    )
    assert await store.get(env.tenant, 2) is None  # ...and on replace
    await store.put(env.tenant, 2, 4, pools.POOL_SOURCE_LICENSE_SERVER, now)
    await svc.clear_tenant_license_key(env.adal, env.dal, tenant_id=env.tenant, actor=None)
    assert await store.get(env.tenant, 2) is None  # ...and on clear
    assert (await store.get(env.other, 2)).limit_count == 6


async def test_seal_version_below_one_is_rejected_on_read(
    env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await svc.set_tenant_license_key(
        env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None
    )
    t = env.dal.tenant_license_keys
    env.dal(t.tenant_id == env.tenant).update(seal_version=0)
    env.dal.commit()
    with caplog.at_level(logging.ERROR):
        assert await svc.get_tenant_license_key(env.adal, env.dal, env.tenant) is None
    assert "InvalidSealVersion" in caplog.text and KEY_A not in caplog.text


async def test_database_errors_propagate_instead_of_reading_as_no_key(env: Any) -> None:
    class Boom:
        executor = env.adal.executor

    class BadDal:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await svc.get_tenant_license_key(Boom, BadDal(), env.tenant)


async def test_key_audit_failure_is_loud_and_not_swallowed(
    env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    class Flaky:
        calls = 0

        async def record(self, event: Any) -> None:
            Flaky.calls += 1
            raise RuntimeError("audit down")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await svc.set_tenant_license_key(
            env.adal, env.dal, tenant_id=env.tenant, license_key=KEY_A, actor=None, audit=Flaky()
        )
    assert Flaky.calls == 2 and "audit_write_failed" in caplog.text
    assert KEY_A not in caplog.text


def test_license_key_dto_repr_hides_the_key() -> None:
    from blueprints.v1.tenant_license import SetLicenseKeyRequest

    assert KEY_A not in repr(SetLicenseKeyRequest(licenseKey=KEY_A))
