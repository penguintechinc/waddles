"""Media (Gemini) BYOK admin API + internal resolver/tier endpoints -- real pydal DB, real JWTs.

Real: sqlite-backed `ai_routing_db` (tenants/communities/members/ai_byok_keys/audit_log), real
AES-256-GCM, real ES256 machine JWTs through `machine_authenticated`, real rate limiter
(in-memory fallback). Stubbed only: the outbound Gemini `models` validation call and the license
gate (`get_tier`), both external services.

Mutation checks (executed): (1) removing the `tier_meets_media_floor` gate in `resolve_media_key`
-> `test_resolve_free_tier_refused` red; (2) dropping `community_belongs_to_tenant` from
`effective_tier` -> `test_cross_tenant_resolve_is_404` red; (3) removing the caller allow-list ->
`test_resolve_non_allowlisted_caller_403` red; (4) echoing `api_key` from `set` ->
`test_key_never_returned_or_stored_plain` red. Each reverted, green again.
"""

from __future__ import annotations

import json
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.rate_limiter import RateLimiter
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from quart import Quart
from quart_schema import QuartSchema
from waddles.hub.internal.v1 import media_pb2

from blueprints.v1 import media_byok
from grpc_internal.dataplane_servicers import (
    RESOLVE_LIMIT,
    SCOPE_MEDIA_BYOK_RESOLVE,
    SCOPE_MEDIA_TIER_RESOLVE,
    MediaServicer,
)
from services import media_byok_service as svc
from services.ai_routing.errors import invalid_byok_key
from services.schema import _define_audit_log
from tests._dataplane_grpc import DataplaneClient, serving_dataplane
from tests.conftest import (
    OTHER_TENANT_SLUG,
    TENANT_SLUG,
    make_user_token,
    seed_community,
    seed_membership,
)

PLAINTEXT = "not-a-real-gemini-key-" + "x" * 20 + "-test"
PROCESS = spiffe_id("alpha", "svc-process")
ACTION = spiffe_id("alpha", "svc-action")
ROGUE = spiffe_id("alpha", "svc-rogue")
SCOPES = frozenset({SCOPE_MEDIA_BYOK_RESOLVE, SCOPE_MEDIA_TIER_RESOLVE})


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_BYOK_ENCRYPTION_KEY", "22" * 32)
    monkeypatch.delenv("MEDIA_BYOK_ALLOWED_SERVICES", raising=False)

    async def _valid(api_key: str) -> None:
        if api_key == "bad-key":
            raise invalid_byok_key("gemini rejected this API key")

    async def _free_tenant(*, tenant: str, community: int | None = None) -> str:
        return "free"

    monkeypatch.setattr(svc, "_validate_gemini_key", _valid)
    monkeypatch.setattr(svc, "get_tier", _free_tenant)


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    priv = ec.generate_private_key(ec.SECP256R1())
    ids = {
        sid: ServiceIdentity(sid, "waddlebot", name, SCOPES, "system")
        for sid, name in ((PROCESS, "svc-process"), (ACTION, "svc-action"), (ROGUE, "svc-rogue"))
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


@pytest.fixture
def app(ai_routing_db: Any, issuer: ServiceJwtIssuer) -> Quart:
    _define_audit_log(ai_routing_db.dal, migrate=True)
    ai_routing_db.dal.commit()
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(media_byok.media_byok_admin_bp)
    quart_app.config["dal"] = ai_routing_db.dal
    quart_app.config["async_dal"] = ai_routing_db
    quart_app.config["SERVICE_JWT_ISSUER"] = issuer
    quart_app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    quart_app.config["rate_limiter"] = RateLimiter(redis_url="", namespace="t")
    return quart_app


def _set_tier(db: Any, community_id: int, tier: str | None) -> None:
    db.dal(db.dal.communities.id == community_id).update(license_tier=tier)
    db.dal.commit()


def _tenant_id(db: Any, slug: str) -> int:
    return int(db.dal(db.dal.tenants.slug == slug).select().first().id)


def _admin(db: Any, *, tenant: str = TENANT_SLUG, tier: str | None = "professional") -> tuple:
    community_id = seed_community(db, tenant_slug=tenant)
    _set_tier(db, community_id, tier)
    seed_membership(db, community_id=community_id, user_id=801, role="community-owner")
    headers = {"Authorization": f"Bearer {make_user_token(user_id=801, tenant=tenant)}"}
    return headers, community_id


def _machine(issuer: ServiceJwtIssuer, sid: str = PROCESS, scope: str = "") -> dict[str, str]:
    tok, _ = issuer.issue_with_claims(sid, scope or media_byok.SCOPE_MEDIA_BYOK_RESOLVE)
    return {"Authorization": f"Bearer {tok}"}


def _audit_rows(db: Any) -> list[Any]:
    return list(db.dal(db.dal.audit_log.action == "media.byok.resolve").select())


class TestAdminApi:
    async def test_set_status_rotate_delete(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        client = app.test_client()
        url = f"/api/v1/admin/{cid}/media/byok-keys"
        r = await client.put(
            url, headers=headers, json={"provider": "gemini", "api_key": PLAINTEXT}
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert body["key_last4"] == PLAINTEXT[-4:] and body["rotated_at"] is None
        r = await client.put(
            url, headers=headers, json={"provider": "gemini", "api_key": PLAINTEXT[:-4] + "ZZZZ"}
        )
        assert (await r.get_json())["rotated_at"] is not None
        status = await (await client.get(url, headers=headers)).get_json()
        assert status["configured"] is True and status["keys"][0]["key_last4"] == "ZZZZ"
        assert (await client.delete(f"{url}/gemini", headers=headers)).status_code == 200
        status = await (await client.get(url, headers=headers)).get_json()
        assert status["configured"] is False

    async def test_key_never_returned_or_stored_plain(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        client = app.test_client()
        url = f"/api/v1/admin/{cid}/media/byok-keys"
        put = await client.put(
            url, headers=headers, json={"provider": "gemini", "api_key": PLAINTEXT}
        )
        get = await client.get(url, headers=headers)
        for resp in (put, get):
            assert PLAINTEXT not in (await resp.get_data(as_text=True))
        row = ai_routing_db.dal(ai_routing_db.dal.ai_byok_keys.community_id == cid).select().first()
        assert PLAINTEXT not in row.encrypted_key and row.provider == "gemini"

    async def test_free_tier_refused_402_nothing_stored(
        self, app: Quart, ai_routing_db: Any
    ) -> None:
        headers, cid = _admin(ai_routing_db, tier="free")
        r = await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "gemini", "api_key": PLAINTEXT},
        )
        assert r.status_code == 402
        assert ai_routing_db.dal(ai_routing_db.dal.ai_byok_keys).count() == 0

    async def test_tenant_tier_lifts_free_community(
        self, app: Quart, ai_routing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _pro(*, tenant: str, community: int | None = None) -> str:
            return "professional"

        monkeypatch.setattr(svc, "get_tier", _pro)
        headers, cid = _admin(ai_routing_db, tier=None)
        r = await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "gemini", "api_key": PLAINTEXT},
        )
        assert r.status_code == 200

    async def test_rejected_key_not_stored(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        r = await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "gemini", "api_key": "bad-key"},
        )
        assert r.status_code >= 400
        assert ai_routing_db.dal(ai_routing_db.dal.ai_byok_keys).count() == 0

    async def test_invalid_provider_400(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        r = await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "openai", "api_key": PLAINTEXT},
        )
        assert r.status_code == 400

    async def test_non_admin_member_403(self, app: Quart, ai_routing_db: Any) -> None:
        _, cid = _admin(ai_routing_db)
        seed_membership(ai_routing_db, community_id=cid, user_id=802, role="member")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=802)}"}
        r = await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "gemini", "api_key": PLAINTEXT},
        )
        assert r.status_code == 403

    async def test_cross_tenant_admin_refused(self, app: Quart, ai_routing_db: Any) -> None:
        _, other_cid = _admin(ai_routing_db, tenant=OTHER_TENANT_SLUG)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=801, tenant=TENANT_SLUG)}"}
        client = app.test_client()
        url = f"/api/v1/admin/{other_cid}/media/byok-keys"
        assert (await client.get(url, headers=headers)).status_code == 403
        r = await client.put(
            url, headers=headers, json={"provider": "gemini", "api_key": PLAINTEXT}
        )
        assert r.status_code == 403
        assert (await client.delete(f"{url}/gemini", headers=headers)).status_code == 403

    async def test_unauthenticated_401(self, app: Quart) -> None:
        assert (await app.test_client().get("/api/v1/admin/1/media/byok-keys")).status_code == 401

    async def test_ai_listing_excludes_gemini(self, app: Quart, ai_routing_db: Any) -> None:
        from services.ai_routing import config_service

        headers, cid = _admin(ai_routing_db)
        await app.test_client().put(
            f"/api/v1/admin/{cid}/media/byok-keys",
            headers=headers,
            json={"provider": "gemini", "api_key": PLAINTEXT},
        )
        listed = await config_service.list_byok_keys(
            ai_routing_db, ai_routing_db.dal, community_id=cid
        )
        assert listed == []


async def _store(app: Quart, db: Any, headers: dict[str, str], cid: int) -> None:
    r = await app.test_client().put(
        f"/api/v1/admin/{cid}/media/byok-keys",
        headers=headers,
        json={"provider": "gemini", "api_key": PLAINTEXT},
    )
    assert r.status_code == 200


_BYOK = "/waddles.hub.internal.v1.MediaService/ResolveByok"
_TIER = "/waddles.hub.internal.v1.MediaService/GetEffectiveTier"
_TIER_NAMES = {
    media_pb2.MEDIA_TIER_FREE: "free",
    media_pb2.MEDIA_TIER_PROFESSIONAL: "professional",
    media_pb2.MEDIA_TIER_ENTERPRISE: "enterprise",
}


@pytest.fixture
async def dp(app: Quart, issuer: ServiceJwtIssuer) -> Any:
    """A `DataplaneClient` over the real interceptor chain + `MediaServicer` (shared app config)."""
    async with serving_dataplane(app.config, issuer) as address:
        yield DataplaneClient(address)


def _token(
    issuer: ServiceJwtIssuer, sid: str = PROCESS, scope: str = SCOPE_MEDIA_BYOK_RESOLVE
) -> str:
    token, _ = issuer.issue_with_claims(sid, scope)
    return token


async def _resolve(
    dp: DataplaneClient, token: str | None, tenant_id: int, cid: int, provider: str = "gemini"
) -> Any:
    return await dp.call(
        _BYOK,
        media_pb2.ResolveByokRequest(tenant_id=tenant_id, community_id=cid, provider=provider),
        media_pb2.ResolveByokResponse,
        token=token,
    )


async def _tier_call(dp: DataplaneClient, token: str | None, tid: int, cid: int) -> Any:
    return await dp.call(
        _TIER,
        media_pb2.GetEffectiveTierRequest(tenant_id=tid, community_id=cid),
        media_pb2.GetEffectiveTierResponse,
        token=token,
    )


async def _refused(call: Any) -> grpc.aio.AioRpcError:
    """Await `call` and return the `AioRpcError` it must raise (a clean return fails the test)."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await call
    return caught.value


class TestResolver:
    async def test_resolve_ok_and_audited_without_key(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        r = await _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert (r.status, _TIER_NAMES[r.tier], r.api_key) == (
            media_pb2.BYOK_STATUS_OK,
            "professional",
            PLAINTEXT,
        )
        rows = _audit_rows(ai_routing_db)
        assert len(rows) == 1 and rows[0].created_at is not None
        dumped = json.dumps(rows[0].details) + str(rows[0].action) + str(rows[0].target_id)
        assert PLAINTEXT not in dumped and rows[0].details["caller"] == "svc-process"
        assert rows[0].details["outcome"] == "ok"

    async def test_not_configured(
        self, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        _, cid = _admin(ai_routing_db)
        r = await _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert (r.status, _TIER_NAMES[r.tier], r.HasField("api_key")) == (
            media_pb2.BYOK_STATUS_NOT_CONFIGURED,
            "professional",
            False,
        )

    async def test_deleted_key_not_configured(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        await app.test_client().delete(f"/api/v1/admin/{cid}/media/byok-keys/gemini", headers=admin)
        r = await _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert r.status == media_pb2.BYOK_STATUS_NOT_CONFIGURED

    async def test_resolve_free_tier_refused(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        _set_tier(ai_routing_db, cid, "free")  # downgrade after the key was stored
        r = await _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert (r.status, _TIER_NAMES[r.tier], r.HasField("api_key")) == (
            media_pb2.BYOK_STATUS_TIER_REQUIRED,
            "free",
            False,
        )
        assert PLAINTEXT not in str(r)

    async def test_cross_tenant_resolve_is_not_found(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        other_tenant = _tenant_id(ai_routing_db, OTHER_TENANT_SLUG)
        err = await _refused(_resolve(dp, _token(issuer), other_tenant, cid))
        assert err.code() == grpc.StatusCode.NOT_FOUND
        assert PLAINTEXT not in (err.details() or "")
        (row,) = _audit_rows(ai_routing_db)
        assert row.details["outcome"] == "scope_mismatch"

    async def test_resolve_non_allowlisted_caller_denied(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        for sid in (ACTION, ROGUE):  # valid scope + system tenant, but not the media stage
            err = await _refused(_resolve(dp, _token(issuer, sid), tid, cid))
            assert err.code() == grpc.StatusCode.PERMISSION_DENIED
            assert PLAINTEXT not in (err.details() or "")
        assert _audit_rows(ai_routing_db) == []

    async def test_non_system_tenant_denied(self, app: Quart, ai_routing_db: Any) -> None:
        """An allow-listed identity bound to a NON-system tenant may not call (cross-tenant RPC)."""
        priv = ec.generate_private_key(ec.SECP256R1())
        tenant_issuer = ServiceJwtIssuer(
            keys={"k1": SigningKey("k1", priv, priv.public_key())},
            active_kid="k1",
            identities={
                PROCESS: ServiceIdentity(PROCESS, "waddlebot", "svc-process", SCOPES, "acme-corp")
            },
        )
        _, cid = _admin(ai_routing_db)
        async with serving_dataplane(app.config, tenant_issuer) as address:
            err = await _refused(
                _resolve(
                    DataplaneClient(address),
                    _token(tenant_issuer),
                    _tenant_id(ai_routing_db, TENANT_SLUG),
                    cid,
                )
            )
        assert err.code() == grpc.StatusCode.PERMISSION_DENIED

    async def test_resolve_auth_refusals(
        self, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        wrong_scope = _token(issuer, scope=SCOPE_MEDIA_TIER_RESOLVE)
        user = make_user_token(user_id=801)
        for token in (None, "junk", wrong_scope, user):  # none / garbage / wrong scope / HS256 user
            err = await _refused(_resolve(dp, token, tid, cid))
            assert err.code() == grpc.StatusCode.UNAUTHENTICATED, token

    async def test_bad_provider_and_ids_invalid_argument(
        self, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        for args in ((tid, cid, "openai"), (-1, cid, "gemini"), (tid, -1, "gemini")):
            err = await _refused(_resolve(dp, _token(issuer), *args))
            assert err.code() == grpc.StatusCode.INVALID_ARGUMENT, args

    async def test_rate_limited(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        await app.config[
            "rate_limiter"
        ].connect()  # no URL -> in-memory sliding window (as in prod startup)
        for _ in range(RESOLVE_LIMIT):
            await _resolve(dp, _token(issuer), tid, cid)
        err = await _refused(_resolve(dp, _token(issuer), tid, cid))
        assert err.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    async def test_no_limiter_fails_closed_unavailable(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        del app.config["rate_limiter"]
        _, cid = _admin(ai_routing_db)
        err = await _refused(
            _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        )
        assert err.code() == grpc.StatusCode.UNAVAILABLE

    async def test_audit_failure_withholds_key(
        self,
        app: Quart,
        ai_routing_db: Any,
        issuer: ServiceJwtIssuer,
        dp: DataplaneClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)

        async def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(MediaServicer, "_audit", _boom)
        err = await _refused(
            _resolve(dp, _token(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        )
        assert err.code() == grpc.StatusCode.INTERNAL
        assert PLAINTEXT not in (err.details() or "")


class TestEffectiveTier:
    @pytest.mark.parametrize(
        ("tenant_tier", "community_tier", "expected"),
        [
            ("free", None, "free"),
            ("free", "professional", "professional"),
            ("professional", "free", "professional"),
            ("professional", "enterprise", "enterprise"),
            ("enterprise", "free", "enterprise"),
            ("free", "bogus", "free"),
            ("bogus", None, "free"),
        ],
    )
    async def test_max_of_tenant_and_community(
        self,
        ai_routing_db: Any,
        issuer: ServiceJwtIssuer,
        dp: DataplaneClient,
        monkeypatch: pytest.MonkeyPatch,
        tenant_tier: str,
        community_tier: str | None,
        expected: str,
    ) -> None:
        async def _t(*, tenant: str, community: int | None = None) -> str:
            return tenant_tier

        monkeypatch.setattr(svc, "get_tier", _t)
        _, cid = _admin(ai_routing_db, tier=community_tier)
        token = _token(issuer, scope=SCOPE_MEDIA_TIER_RESOLVE)
        r = await _tier_call(dp, token, _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert _TIER_NAMES[r.tier] == expected

    async def test_refusals(
        self, ai_routing_db: Any, issuer: ServiceJwtIssuer, dp: DataplaneClient
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        good = _token(issuer, scope=SCOPE_MEDIA_TIER_RESOLVE)
        rogue = _token(issuer, ROGUE, SCOPE_MEDIA_TIER_RESOLVE)
        other = _tenant_id(ai_routing_db, OTHER_TENANT_SLUG)
        cases = [
            (None, tid, cid, grpc.StatusCode.UNAUTHENTICATED),
            (_token(issuer), tid, cid, grpc.StatusCode.UNAUTHENTICATED),  # wrong scope
            (rogue, tid, cid, grpc.StatusCode.PERMISSION_DENIED),  # not allow-listed
            (good, other, cid, grpc.StatusCode.NOT_FOUND),  # community not in that tenant
            (good, tid, -5, grpc.StatusCode.INVALID_ARGUMENT),
        ]
        for token, t, c, code in cases:
            err = await _refused(_tier_call(dp, token, t, c))
            assert err.code() == code, (token, t, c)


_REAL_ASYNC_CLIENT = svc.httpx.AsyncClient


class TestGeminiValidation:
    """The real `_validate_gemini_key` against an httpx MockTransport (no network)."""

    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[Any]:
        import httpx

        seen: list[Any] = []
        real = _REAL_ASYNC_CLIENT

        def _wrap(request: Any) -> Any:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(
            svc.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(_wrap), **kw),
        )
        return seen

    @pytest.fixture(autouse=True)
    def _real_validator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.undo()  # drop the autouse stub so the real function runs
        monkeypatch.setenv("AI_BYOK_ENCRYPTION_KEY", "22" * 32)

    async def test_sends_key_in_header_only_to_pinned_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        seen = self._patch(monkeypatch, lambda r: httpx.Response(200, json={}))
        await svc._validate_gemini_key(PLAINTEXT)
        req = seen[0]
        assert req.url.host == "generativelanguage.googleapis.com"
        assert req.headers["x-goog-api-key"] == PLAINTEXT and PLAINTEXT not in str(req.url)

    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_rejected_key_raises(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        import httpx

        from services.errors import ApiError

        self._patch(monkeypatch, lambda r: httpx.Response(status))
        with pytest.raises(ApiError) as exc:
            await svc._validate_gemini_key(PLAINTEXT)
        assert PLAINTEXT not in exc.value.message

    async def test_provider_5xx_and_network_error_are_typed_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        from services.errors import ApiError

        self._patch(monkeypatch, lambda r: httpx.Response(503))
        with pytest.raises(ApiError):
            await svc._validate_gemini_key(PLAINTEXT)

        def _boom(request: Any) -> Any:
            raise httpx.ConnectError("down", request=request)

        self._patch(monkeypatch, _boom)
        with pytest.raises(ApiError) as exc:
            await svc._validate_gemini_key(PLAINTEXT)
        assert PLAINTEXT not in exc.value.message


class TestInputBounds:
    async def test_blank_and_oversized_key_400(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        url = f"/api/v1/admin/{cid}/media/byok-keys"
        for key in ("   ", "k" * (svc.MAX_KEY_CHARS + 1)):
            r = await app.test_client().put(
                url, headers=headers, json={"provider": "gemini", "api_key": key}
            )
            assert r.status_code == 400
        assert ai_routing_db.dal(ai_routing_db.dal.ai_byok_keys).count() == 0

    async def test_delete_without_key_404(self, app: Quart, ai_routing_db: Any) -> None:
        headers, cid = _admin(ai_routing_db)
        r = await app.test_client().delete(
            f"/api/v1/admin/{cid}/media/byok-keys/gemini", headers=headers
        )
        assert r.status_code == 404
