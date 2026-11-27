"""SECURITY hotfix regressions: CAT (`wdl_c_`) auth fails closed + desktop OAuth PKCE.

A Community Access Token is a community-scoped machine credential, never a user session:
- `get_current_user_id` must 401 for a CAT (it used to resolve to the CAT's creator);
- only routes opted in via `flask_core.cat_auth.allow_cat` accept CATs, each with explicit
  scopes and a declared community source (parity walker below);
- `/communities/my` returns only the CAT's own community.
The desktop (`waddles://`) OAuth flow must prove PKCE S256 possession on `/auth/exchange`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core.cat_auth import CAT_SELF_ONLY, get_cat_policy, reject_community
from quart import Quart, request
from quart_schema import QuartSchema

from blueprints import register_blueprints
from blueprints.v1.access_token import community_tokens_bp
from services import access_token_service, oauth_service
from services.community_public_id import install_community_public_ids, lookup_public_id
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.schema import bind_support_token_tables
from tests.test_v1_penguind_parity import _make_app, _seed_community, _seed_user

#: EVERY route allowed to accept a CAT, with its exact declared scopes. Adding a route here is a
#: security review decision: it must carry `@allow_cat(scopes=..., community_from=...)`.
CAT_ROUTE_ALLOWLIST: dict[tuple[str, str], object] = {
    ("GET", "/api/v1/communities/my"): CAT_SELF_ONLY,
}


def _mint_cat(auth_db: Any, *, community_id: int, user_id: int, scopes: list[str]) -> str:
    dal = auth_db.dal
    bind_support_token_tables(dal, migrate=True)
    access_token_service._bind(dal)
    for key in scopes:
        if dal(dal.permission_scopes.scope_key == key).isempty():
            dal.permission_scopes.insert(scope_key=key, category="test", display_name=key)
    dal.commit()
    return access_token_service.create_cat(
        dal,
        community_id,
        created_by_user_id=user_id,
        name="penguind",
        scopes=scopes,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class TestCatNeverActsAsUser:
    async def test_get_current_user_id_with_cat_is_401(self, auth_db: Any) -> None:
        uid = _seed_user(auth_db)
        cid = _seed_community(auth_db, name="Streamers")
        cat = _mint_cat(auth_db, community_id=cid, user_id=uid, scopes=["community.music:read"])
        app = _make_app(auth_db)
        async with app.test_request_context(
            "/anything", headers={"Authorization": f"Bearer {cat}"}
        ):
            with pytest.raises(ApiError) as exc:
                get_current_user_id(request)
        assert exc.value.status_code == 401

    async def test_cat_cannot_mint_cats_as_its_creator(self, auth_db: Any) -> None:
        """The concrete CRITICAL: a CAT reaching an admin route as `created_by_user_id`."""
        uid = _seed_user(auth_db)
        cid = _seed_community(auth_db, name="Streamers")
        cat = _mint_cat(auth_db, community_id=cid, user_id=uid, scopes=["community.tokens:admin"])
        app = _make_app(auth_db)
        app.register_blueprint(community_tokens_bp)
        async with app.app_context():
            cuuid = await lookup_public_id(cid)
        client = app.test_client()
        resp = await client.post(
            f"/api/v1/admin/{cuuid}/tokens/cats",
            json={"name": "escalate", "scopes": ["community.tokens:admin"]},
            headers={"Authorization": f"Bearer {cat}"},
        )
        assert resp.status_code in (401, 403)
        listed = await client.get(
            f"/api/v1/admin/{cuuid}/tokens/cats", headers={"Authorization": f"Bearer {cat}"}
        )
        assert listed.status_code in (401, 403)

    async def test_my_communities_cat_sees_only_its_own(self, auth_db: Any) -> None:
        uid = _seed_user(auth_db)
        cid = _seed_community(auth_db, name="Bound")
        other = _seed_community(auth_db, name="Creator Also Member")
        cat = _mint_cat(auth_db, community_id=cid, user_id=uid, scopes=["community.music:read"])
        dal = auth_db.dal
        dal.community_members.insert(community_id=other, user_id=str(uid), is_active=True)
        dal.community_members.insert(community_id=cid, user_id=str(uid), is_active=True)
        dal.commit()
        app = _make_app(auth_db)
        async with app.app_context():
            bound_uuid = await lookup_public_id(cid)
        resp = await app.test_client().get(
            "/api/v1/communities/my", headers={"Authorization": f"Bearer {cat}"}
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert [c["id"] for c in data["communities"]] == [bound_uuid]
        assert data["communities"][0]["role"] == "token"


class TestCatRouteParity:
    """Walk every registered hub-api route: only the allowlist accepts CATs, all explicitly."""

    def _cat_routes(self) -> dict[tuple[str, str], Any]:
        app = Quart(__name__)
        QuartSchema(app)
        install_community_public_ids(app)
        register_blueprints(app)
        found: dict[tuple[str, str], Any] = {}
        examined = 0
        for rule in app.url_map.iter_rules():
            view = app.view_functions.get(rule.endpoint)
            examined += 1
            policy = get_cat_policy(view)
            if policy is None:
                continue
            for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
                found[(method, rule.rule)] = policy
        assert examined > 100, f"route walker examined only {examined} routes"
        return found

    def test_only_allowlisted_routes_accept_cats(self) -> None:
        found = self._cat_routes()
        assert set(found) == set(CAT_ROUTE_ALLOWLIST)

    def test_every_cat_route_declares_scopes_and_community_source(self) -> None:
        for key, policy in self._cat_routes().items():
            assert policy.scopes is CAT_SELF_ONLY or (
                isinstance(policy.scopes, tuple) and policy.scopes
            ), key
            assert policy.scopes == CAT_ROUTE_ALLOWLIST[key], key
            assert policy.community_from is not reject_community, key


class TestDesktopOAuthPkce:
    async def _desktop_code(
        self, auth_db: Any, monkeypatch: Any, challenge: str | None
    ) -> tuple[Any, Any, str | None, int]:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")

        async def _fake_oauth_callback(*_args: Any, **_kwargs: Any) -> str:
            return "mock.jwt.token"

        monkeypatch.setattr(oauth_service, "oauth_callback", _fake_oauth_callback)
        app = _make_app(auth_db)
        client = app.test_client()
        url = "/api/v1/auth/oauth/discord?redirect_uri=waddles://oauth/callback"
        if challenge is not None:
            url += f"&code_challenge={challenge}&code_challenge_method=S256"
        start = await client.get(url)
        if start.status_code != 200:
            return app, client, None, start.status_code
        state = (await start.get_json())["state"]
        cb = await client.get(f"/api/v1/auth/oauth/discord/callback?code=pc&state={state}")
        assert cb.status_code == 302
        location = cb.headers["Location"]
        assert location.startswith("waddles://oauth/callback?code=")
        return app, client, location.split("code=")[1].split("&")[0], 200

    async def test_start_without_challenge_is_400(self, auth_db: Any, monkeypatch: Any) -> None:
        _, _, code, status = await self._desktop_code(auth_db, monkeypatch, None)
        assert code is None and status == 400

    async def test_start_with_plain_method_is_400(self, auth_db: Any, monkeypatch: Any) -> None:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
        app = _make_app(auth_db)
        _, challenge = _pkce_pair()
        resp = await app.test_client().get(
            "/api/v1/auth/oauth/discord?redirect_uri=waddles://oauth/callback"
            f"&code_challenge={challenge}&code_challenge_method=plain"
        )
        assert resp.status_code == 400

    async def test_missing_verifier_is_400(self, auth_db: Any, monkeypatch: Any) -> None:
        _, challenge = _pkce_pair()
        _, client, code, _ = await self._desktop_code(auth_db, monkeypatch, challenge)
        resp = await client.post("/api/v1/auth/exchange", json={"code": code})
        assert resp.status_code == 400

    async def test_wrong_verifier_is_400_and_does_not_burn_code(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        verifier, challenge = _pkce_pair()
        wrong, _ = _pkce_pair()
        _, client, code, _ = await self._desktop_code(auth_db, monkeypatch, challenge)
        bad = await client.post(
            "/api/v1/auth/exchange", json={"code": code, "code_verifier": wrong}
        )
        assert bad.status_code == 400
        good = await client.post(
            "/api/v1/auth/exchange", json={"code": code, "code_verifier": verifier}
        )
        assert good.status_code == 200

    async def test_correct_verifier_ok_then_reuse_refused(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        verifier, challenge = _pkce_pair()
        _, client, code, _ = await self._desktop_code(auth_db, monkeypatch, challenge)
        ok = await client.post(
            "/api/v1/auth/exchange", json={"code": code, "code_verifier": verifier}
        )
        assert ok.status_code == 200
        assert (await ok.get_json())["token"] == "mock.jwt.token"
        replay = await client.post(
            "/api/v1/auth/exchange", json={"code": code, "code_verifier": verifier}
        )
        assert replay.status_code == 400

    async def test_expired_code_refused(self, auth_db: Any, monkeypatch: Any) -> None:
        verifier, challenge = _pkce_pair()
        _, client, code, _ = await self._desktop_code(auth_db, monkeypatch, challenge)
        dal = auth_db.dal
        dal(dal.hub_oauth_exchange_codes.code == code).update(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        dal.commit()
        resp = await client.post(
            "/api/v1/auth/exchange", json={"code": code, "code_verifier": verifier}
        )
        assert resp.status_code == 400

    def test_exchange_code_ttl_is_short(self) -> None:
        assert oauth_service.EXCHANGE_CODE_TTL_SECONDS <= 60
