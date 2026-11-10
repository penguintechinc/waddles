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

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.rate_limiter import RateLimiter
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1 import media_byok
from services import media_byok_service as svc
from services.ai_routing.errors import invalid_byok_key
from services.schema import _define_audit_log
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
SCOPES = frozenset({media_byok.SCOPE_MEDIA_BYOK_RESOLVE, media_byok.SCOPE_MEDIA_TIER_RESOLVE})


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
    quart_app.register_blueprint(media_byok.media_byok_internal_bp)
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


async def _resolve(
    app: Quart, headers: dict[str, str], tenant_id: int, cid: int, provider: str = "gemini"
) -> Any:
    return await app.test_client().post(
        "/api/v1/internal/media/byok/resolve",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"tenant_id": tenant_id, "community_id": cid, "provider": provider}),
    )


async def _store(app: Quart, db: Any, headers: dict[str, str], cid: int) -> None:
    r = await app.test_client().put(
        f"/api/v1/admin/{cid}/media/byok-keys",
        headers=headers,
        json={"provider": "gemini", "api_key": PLAINTEXT},
    )
    assert r.status_code == 200


class TestResolver:
    async def test_resolve_ok_and_audited_without_key(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert r.status_code == 200
        assert await r.get_json() == {"status": "ok", "tier": "professional", "api_key": PLAINTEXT}
        assert r.headers["Cache-Control"] == "no-store"
        rows = _audit_rows(ai_routing_db)
        assert len(rows) == 1 and rows[0].created_at is not None
        dumped = json.dumps(rows[0].details) + str(rows[0].action) + str(rows[0].target_id)
        assert PLAINTEXT not in dumped and rows[0].details["caller"] == "svc-process"
        assert rows[0].details["outcome"] == "ok"

    async def test_not_configured(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        _, cid = _admin(ai_routing_db)
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert await r.get_json() == {
            "status": "not_configured",
            "tier": "professional",
            "api_key": None,
        }

    async def test_deleted_key_not_configured(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        await app.test_client().delete(f"/api/v1/admin/{cid}/media/byok-keys/gemini", headers=admin)
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert (await r.get_json())["status"] == "not_configured"

    async def test_resolve_free_tier_refused(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        _set_tier(ai_routing_db, cid, "free")  # downgrade after the key was stored
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        body = await r.get_json()
        assert body == {"status": "tier_required", "tier": "free", "api_key": None}
        assert PLAINTEXT not in await r.get_data(as_text=True)

    async def test_cross_tenant_resolve_is_404(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        other_tenant = _tenant_id(ai_routing_db, OTHER_TENANT_SLUG)
        r = await _resolve(app, _machine(issuer), other_tenant, cid)
        assert r.status_code == 404
        assert PLAINTEXT not in await r.get_data(as_text=True)

    async def test_resolve_non_allowlisted_caller_403(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        for sid in (ACTION, ROGUE):  # valid scope + system tenant, but not the media stage
            r = await _resolve(app, _machine(issuer, sid), tid, cid)
            assert r.status_code == 403
            assert PLAINTEXT not in await r.get_data(as_text=True)
        assert _audit_rows(ai_routing_db) == []

    async def test_resolve_auth_refusals(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        assert (await _resolve(app, {}, tid, cid)).status_code == 401
        assert (await _resolve(app, {"Authorization": "Bearer junk"}, tid, cid)).status_code == 401
        wrong_scope = _machine(issuer, scope=media_byok.SCOPE_MEDIA_TIER_RESOLVE)
        assert (await _resolve(app, wrong_scope, tid, cid)).status_code == 401
        user = {"Authorization": f"Bearer {make_user_token(user_id=801)}"}
        assert (await _resolve(app, user, tid, cid)).status_code == 401

    async def test_unconfigured_verifier_503(self, ai_routing_db: Any) -> None:
        bare = Quart(__name__)
        QuartSchema(bare)
        bare.register_blueprint(media_byok.media_byok_internal_bp)
        r = await bare.test_client().post(
            "/api/v1/internal/media/byok/resolve",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"tenant_id": 1, "community_id": 1, "provider": "gemini"}),
        )
        assert r.status_code == 503

    async def test_bad_provider_and_ids_400(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        assert (await _resolve(app, _machine(issuer), tid, cid, "openai")).status_code == 400
        assert (await _resolve(app, _machine(issuer), -1, cid)).status_code == 400

    async def test_rate_limited(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        await app.config[
            "rate_limiter"
        ].connect()  # no URL -> in-memory sliding window (as in prod startup)
        codes = [
            (await _resolve(app, _machine(issuer), tid, cid)).status_code
            for _ in range(media_byok.RESOLVE_LIMIT + 2)
        ]
        assert codes[: media_byok.RESOLVE_LIMIT] == [200] * media_byok.RESOLVE_LIMIT
        assert codes[-1] == 429

    async def test_no_limiter_fails_closed_503(
        self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        del app.config["rate_limiter"]
        _, cid = _admin(ai_routing_db)
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert r.status_code == 503

    async def test_audit_failure_withholds_key(
        self,
        app: Quart,
        ai_routing_db: Any,
        issuer: ServiceJwtIssuer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        admin, cid = _admin(ai_routing_db)
        await _store(app, ai_routing_db, admin, cid)

        async def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(media_byok, "_audit", _boom)
        r = await _resolve(app, _machine(issuer), _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert r.status_code == 500
        assert PLAINTEXT not in await r.get_data(as_text=True)


class TestEffectiveTier:
    async def _tier(self, app: Quart, headers: dict[str, str], tid: int, cid: int) -> Any:
        return await app.test_client().post(
            "/api/v1/internal/media/effective-tier",
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({"tenant_id": tid, "community_id": cid}),
        )

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
        app: Quart,
        ai_routing_db: Any,
        issuer: ServiceJwtIssuer,
        monkeypatch: pytest.MonkeyPatch,
        tenant_tier: str,
        community_tier: str | None,
        expected: str,
    ) -> None:
        async def _t(*, tenant: str, community: int | None = None) -> str:
            return tenant_tier

        monkeypatch.setattr(svc, "get_tier", _t)
        _, cid = _admin(ai_routing_db, tier=community_tier)
        headers = _machine(issuer, scope=media_byok.SCOPE_MEDIA_TIER_RESOLVE)
        r = await self._tier(app, headers, _tenant_id(ai_routing_db, TENANT_SLUG), cid)
        assert r.status_code == 200 and await r.get_json() == {"tier": expected}

    async def test_refusals(self, app: Quart, ai_routing_db: Any, issuer: ServiceJwtIssuer) -> None:
        _, cid = _admin(ai_routing_db)
        tid = _tenant_id(ai_routing_db, TENANT_SLUG)
        good = _machine(issuer, scope=media_byok.SCOPE_MEDIA_TIER_RESOLVE)
        assert (await self._tier(app, {}, tid, cid)).status_code == 401
        assert (await self._tier(app, _machine(issuer), tid, cid)).status_code == 401  # wrong scope
        rogue = _machine(issuer, ROGUE, media_byok.SCOPE_MEDIA_TIER_RESOLVE)
        assert (await self._tier(app, rogue, tid, cid)).status_code == 403
        assert (
            await self._tier(app, good, _tenant_id(ai_routing_db, OTHER_TENANT_SLUG), cid)
        ).status_code == 404
        assert (await self._tier(app, good, tid, -5)).status_code == 400


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
