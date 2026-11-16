"""
require_scope Decorator Tests
================================

Unit coverage for `authz.require_scope` -- the HTTP-layer scope check that
closes the tenant -> scope -> feature enforcement chain (security.md
Authentication & Authorization). Feature contracts have declared
``requires_scopes`` since ``feature_contract.py`` landed, and
``tenant_middleware`` already resolves the caller's tenant, but nothing
previously checked the JWT's `scope` claim against a handler's declared
requirement -- this is that missing check.

Exercised standalone via a minimal Quart app/test_client (mirrors
test_mcp_routes.py's pattern; no `tenant_middleware` in this app, matching
`require_scope`'s independent-verification design -- see authz.py's
module docstring), plus the wildcard-matching helper directly.

Fail-first proof: with `has_required_scopes(...)` in `require_scope`
temporarily replaced with a bare `True`, every "missing/wrong scope -> 403"
test below went red (200 instead of 403) -- `test_missing_scope_claim_is_403`,
`test_wrong_scope_is_403`, `test_wildcard_does_not_cross_actions`,
`test_multiple_required_one_missing_is_403`. Reverting the neuter restored
all four to green; see PR report for the exact before/after run.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from flask_core.auth import create_jwt_token
from flask_core.authz import has_required_scopes, require_scope

# require_scope() falls back to this exact default when SECRET_KEY isn't
# set in the environment (flask_core/authz.py, mirroring tenancy.py).
SECRET = "change-me-in-production"


def _token(scope: str = "", tenant: str = "acme-corp") -> str:
    return create_jwt_token(
        user_id="u1",
        username="alice",
        email="alice@example.com",
        roles=["viewer"],
        tenant=tenant,
        scope=scope,
    )


@pytest.fixture
def app() -> Quart:
    quart_app = Quart(__name__)

    @quart_app.route("/single", methods=["POST"])
    @require_scope("customer.account:write")
    async def single() -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    @quart_app.route("/multi", methods=["POST"])
    @require_scope("customer.account:write", "customer.account:read")
    async def multi() -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


class TestRequireScopeDecorator:
    async def test_no_authorization_header_is_403(self, client: Any) -> None:
        response = await client.post("/single")
        assert response.status_code == 403

    async def test_invalid_token_is_403(self, client: Any) -> None:
        response = await client.post(
            "/single", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 403

    async def test_missing_scope_claim_is_403(self, client: Any) -> None:
        """Token valid, minted with an explicit empty `scope` claim
        (`create_jwt_token`'s default) -- fail-closed, not a silent pass."""
        token = _token(scope="")
        response = await client.post("/single", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    async def test_wrong_scope_is_403(self, client: Any) -> None:
        token = _token(scope="social.quote:write")
        response = await client.post("/single", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    async def test_exact_scope_passes(self, client: Any) -> None:
        token = _token(scope="customer.account:write")
        response = await client.post("/single", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    async def test_wildcard_bundle_scope_passes(self, client: Any) -> None:
        """`*:write` -- the global:admin/global:maintainer SCOPE_BUNDLES
        convention (auth.py) -- covers any `resource:write` requirement."""
        token = _token(scope="*:write")
        response = await client.post("/single", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    async def test_wildcard_does_not_cross_actions(self, client: Any) -> None:
        """`*:read` must not satisfy a `:write` requirement -- the action
        half is never wildcarded, mirroring
        test_tenancy.py::TestScopeBundles::test_no_bundle_grants_unbounded_wildcard."""
        token = _token(scope="*:read")
        response = await client.post("/single", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    async def test_multiple_required_all_present_passes(self, client: Any) -> None:
        token = _token(scope="customer.account:write customer.account:read")
        response = await client.post("/multi", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    async def test_multiple_required_one_missing_is_403(self, client: Any) -> None:
        token = _token(scope="customer.account:write")
        response = await client.post("/multi", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403


class TestRequireScopeMisuse:
    def test_no_scopes_raises_at_decoration_time(self) -> None:
        """A bare `@require_scope()` would silently authorize everything --
        refused at decoration time, not left as a runtime footgun."""
        with pytest.raises(ValueError):
            require_scope()


class TestHasRequiredScopes:
    """Pure-function coverage of the wildcard-matching helper."""

    def test_exact_match(self) -> None:
        assert has_required_scopes(frozenset({"a:write"}), ("a:write",))

    def test_missing_scope_fails(self) -> None:
        assert not has_required_scopes(frozenset(), ("a:write",))

    def test_wildcard_resource_matches(self) -> None:
        assert has_required_scopes(frozenset({"*:write"}), ("a:write",))

    def test_wildcard_action_never_matches(self) -> None:
        assert not has_required_scopes(frozenset({"*:read"}), ("a:write",))

    def test_unrelated_scope_does_not_satisfy(self) -> None:
        assert not has_required_scopes(frozenset({"b:write"}), ("a:write",))

    def test_all_of_multiple_required_must_be_present(self) -> None:
        assert not has_required_scopes(frozenset({"a:write"}), ("a:write", "b:write"))
        assert has_required_scopes(
            frozenset({"a:write", "b:write"}), ("a:write", "b:write")
        )
