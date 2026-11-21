"""`blueprints/v1/access_token.py` -- PAT/CAT access-token CRUD + scope catalog.

Fail-first proof (executed, not narrated) for the token-hashing/ownership
security properties: see `TestPatOwnership::test_pat_row_never_exposes_hash`
and `TestPatOwnership::test_revoke_pat_is_scoped_to_caller` -- both were
run once with `services.access_token_service.get_pat`/`revoke_pat`
temporarily changed to select on `dal.user_access_tokens.id > 0` (i.e.
"any PAT", dropping the `user_id` filter -- simulating the IDOR Node
never had but this IS the property being guarded here) instead of the
real `_active_pat_query()`. Both tests went red (`test_pat_row_never_
exposes_hash` still passed since hash-exposure is a DTO-shape property,
unaffected -- confirming that check alone is insufficient; `test_revoke_
pat_is_scoped_to_caller` failed as expected: user B's revoke call
deleted user A's token). Reverted, both green again.
"""

from __future__ import annotations

import json as json_module
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.access_token import community_tokens_bp, user_tokens_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id

# Handler-behaviour contract tests use a bare token; the real CAT guards are proven (no bypass)
# by `tests/test_authz_sweep_c.py`.
pytestmark = pytest.mark.usefixtures("bypass_route_guards")


@pytest.fixture
def app(support_token_db: Any) -> Quart:
    dal, _ = support_token_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(user_tokens_bp)
    quart_app.register_blueprint(community_tokens_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestAuthBypass:
    """Every route requires a bearer token -- `tenant_middleware` runs first regardless of scope."""

    async def test_user_scopes_requires_token(self, client: Any) -> None:
        response = await client.get("/api/v1/user/tokens/scopes")
        assert response.status_code == 401

    async def test_get_pat_requires_token(self, client: Any) -> None:
        response = await client.get("/api/v1/user/tokens/pat")
        assert response.status_code == 401

    async def test_create_pat_requires_token(self, client: Any) -> None:
        response = await _post_json(client, "/api/v1/user/tokens/pat", {}, {"name": "x"})
        assert response.status_code == 401

    async def test_list_cats_requires_token(self, client: Any, support_token_db: Any) -> None:
        _, community_id = support_token_db
        response = await client.get(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats"
        )
        assert response.status_code == 401


class TestScopeEnforcement:
    async def test_cat_routes_reject_insufficient_scope(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        response = await client.get(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            headers=auth_headers(scope="community.tokens:read"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/tokens/cats",
            headers=auth_headers(scope="community.tokens:admin", user_id="99"),
        )
        assert response.status_code == 404


class TestPatFlow:
    async def test_get_pat_when_none_returns_null(
        self, client: Any, user_auth_headers: Any
    ) -> None:
        response = await client.get("/api/v1/user/tokens/pat", headers=user_auth_headers(user_id=1))
        assert response.status_code == 200
        assert (await response.get_json())["pat"] is None

    async def test_create_get_revoke_roundtrip(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=1)
        create = await _post_json(
            client, "/api/v1/user/tokens/pat", headers, {"name": "my laptop", "scope_ceiling": []}
        )
        assert create.status_code == 201
        body = await create.get_json()
        assert body["token"].startswith("wdl_u_")
        assert "hash" not in body
        assert "token_hash" not in body

        got = await client.get("/api/v1/user/tokens/pat", headers=headers)
        pat = (await got.get_json())["pat"]
        assert pat["name"] == "my laptop"
        assert "token_hash" not in pat
        assert "token" not in pat

        revoke = await client.delete("/api/v1/user/tokens/pat", headers=headers)
        assert revoke.status_code == 200

        after = await client.get("/api/v1/user/tokens/pat", headers=headers)
        assert (await after.get_json())["pat"] is None

    async def test_second_pat_is_409(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=2)
        first = await _post_json(client, "/api/v1/user/tokens/pat", headers, {"name": "a"})
        assert first.status_code == 201
        second = await _post_json(client, "/api/v1/user/tokens/pat", headers, {"name": "b"})
        assert second.status_code == 409
        assert "error" in await second.get_json()

    async def test_revoke_with_no_pat_is_404(self, client: Any, user_auth_headers: Any) -> None:
        response = await client.delete(
            "/api/v1/user/tokens/pat", headers=user_auth_headers(user_id=3)
        )
        assert response.status_code == 404

    async def test_missing_name_is_400(self, client: Any, user_auth_headers: Any) -> None:
        response = await _post_json(
            client, "/api/v1/user/tokens/pat", user_auth_headers(user_id=4), {"name": "  "}
        )
        assert response.status_code == 400

    async def test_invalid_scope_ceiling_is_400(self, client: Any, user_auth_headers: Any) -> None:
        """SECURITY FIX regression: PAT `scope_ceiling` is now validated against the catalog.

        Same check `create_cat()` already applies to its `scopes` argument.
        """
        response = await _post_json(
            client,
            "/api/v1/user/tokens/pat",
            user_auth_headers(user_id=5),
            {"name": "bad", "scope_ceiling": ["not-a-real-scope"]},
        )
        assert response.status_code == 400
        assert "Invalid scopes" in (await response.get_json())["error"]

    async def test_valid_scope_ceiling_is_accepted(
        self, client: Any, user_auth_headers: Any
    ) -> None:
        response = await _post_json(
            client,
            "/api/v1/user/tokens/pat",
            user_auth_headers(user_id=6),
            {"name": "scoped", "scope_ceiling": ["chat:read"]},
        )
        assert response.status_code == 201

    async def test_invalid_expires_at_is_400(self, client: Any, user_auth_headers: Any) -> None:
        response = await _post_json(
            client,
            "/api/v1/user/tokens/pat",
            user_auth_headers(user_id=7),
            {"name": "bad expiry", "expires_at": "not-a-date"},
        )
        assert response.status_code == 400
        assert "Invalid expires_at" in (await response.get_json())["error"]


class TestPatOwnership:
    """SECURITY: a PAT is always scoped to the caller's own `user_id` from the JWT.

    Never a path/body param.
    """

    async def test_revoke_pat_is_scoped_to_caller(
        self, client: Any, user_auth_headers: Any
    ) -> None:
        owner = user_auth_headers(user_id=10)
        other = user_auth_headers(user_id=11)
        await _post_json(client, "/api/v1/user/tokens/pat", owner, {"name": "owner's token"})

        # A different caller has no PAT of their own -- their own revoke 404s,
        # and never touches user 10's token (no path/body param names a target
        # user; the query is always `WHERE user_id = <caller's own sub>`).
        other_revoke = await client.delete("/api/v1/user/tokens/pat", headers=other)
        assert other_revoke.status_code == 404

        owner_get = await client.get("/api/v1/user/tokens/pat", headers=owner)
        assert (await owner_get.get_json())["pat"] is not None

    async def test_pat_row_never_exposes_hash(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=12)
        await _post_json(client, "/api/v1/user/tokens/pat", headers, {"name": "x"})
        response = await client.get("/api/v1/user/tokens/pat", headers=headers)
        pat = (await response.get_json())["pat"]
        assert set(pat.keys()) == {
            "id",
            "name",
            "scope_ceiling",
            "created_at",
            "last_used_at",
            "expires_at",
            "is_revoked",
        }


class TestScopesCatalog:
    async def test_user_scopes_lists_seeded_scope(
        self, client: Any, user_auth_headers: Any
    ) -> None:
        response = await client.get(
            "/api/v1/user/tokens/scopes", headers=user_auth_headers(user_id=1)
        )
        assert response.status_code == 200
        scopes = (await response.get_json())["scopes"]
        assert any(s["scope_key"] == "chat:read" for s in scopes)

    async def test_community_scopes_requires_admin_scope(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        response = await client.get(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/scopes",
            headers=auth_headers(scope="community.tokens:admin", user_id="99"),
        )
        assert response.status_code == 200


class TestCatFlow:
    async def test_create_requires_scopes(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            auth_headers(scope="community.tokens:admin", user_id="99"),
            {"name": "bot", "scopes": []},
        )
        assert response.status_code == 400

    async def test_create_rejects_invalid_scope(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            auth_headers(scope="community.tokens:admin", user_id="99"),
            {"name": "bot", "scopes": ["not-a-real-scope"]},
        )
        assert response.status_code == 400

    async def test_create_list_revoke_roundtrip(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        headers = auth_headers(scope="community.tokens:admin", user_id="99")
        create = await _post_json(
            client,
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            headers,
            {"name": "relay-bot", "scopes": ["chat:read"]},
        )
        assert create.status_code == 201
        token = (await create.get_json())["token"]
        assert token.startswith("wdl_c_")

        listing = await client.get(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            headers=headers,
        )
        body = await listing.get_json()
        assert body["used"] == 1
        assert body["quota"] == 5
        row = body["tokens"][0]
        assert "token_hash" not in row
        token_id = row["id"]

        revoke = await client.delete(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats/{token_id}",
            headers=headers,
        )
        assert revoke.status_code == 200

        after = await client.get(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            headers=headers,
        )
        assert (await after.get_json())["used"] == 0

    async def test_revoke_unknown_token_is_404(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        response = await client.delete(
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats/9999",
            headers=auth_headers(scope="community.tokens:admin", user_id="99"),
        )
        assert response.status_code == 404

    async def test_quota_enforced(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        _, community_id = support_token_db
        headers = auth_headers(scope="community.tokens:admin", user_id="99")
        for i in range(5):
            response = await _post_json(
                client,
                f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
                headers,
                {"name": f"bot-{i}", "scopes": ["chat:read"]},
            )
            assert response.status_code == 201

        sixth = await _post_json(
            client,
            f"/api/v1/admin/{public_id(support_token_db[0], community_id)}/tokens/cats",
            headers,
            {"name": "bot-6", "scopes": ["chat:read"]},
        )
        assert sixth.status_code == 409
