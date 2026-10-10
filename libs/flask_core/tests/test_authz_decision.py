"""`require_scope` publishes its verdict as `request.authz_decision` (read by hub-api's audit hook).

GRC audit finding #3 needs authorization *decisions* -- denials above all -- to reach the
tamper-evident audit log without every route having to remember to log them. `require_scope`
already knows the verdict; this pins that it publishes it (and never publishes the granted
scopes anywhere loggable), plus the scope-bundle fact the audit endpoints' scope choice rests on:
every session carries `*:read`, which must NOT satisfy `compliance.audit:admin`.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart, request

from flask_core.auth import SCOPE_BUNDLES, create_jwt_token
from flask_core.authz import (
    AuthzDecision,
    get_authz_decision,
    has_required_scopes,
    require_scope,
)

SECRET = "change-me-in-production"


def _token(scope: str = "", user_id: str = "7") -> str:
    return create_jwt_token(
        user_id=user_id,
        username="alice",
        email="alice@example.com",
        roles=["viewer"],
        secret_key=SECRET,
        tenant="acme-corp",
        scope=scope,
    )


@pytest.fixture
def seen() -> list[AuthzDecision | None]:
    return []


@pytest.fixture
def app(seen: list[AuthzDecision | None]) -> Quart:
    quart_app = Quart(__name__)

    @quart_app.route("/guarded", methods=["GET"])
    @require_scope("compliance.audit:admin")
    async def guarded() -> tuple[dict[str, Any], int]:
        seen.append(get_authz_decision(request))
        return {"ok": True}, 200

    @quart_app.route("/open", methods=["GET"])
    async def open_route() -> tuple[dict[str, Any], int]:
        seen.append(get_authz_decision(request))
        return {"ok": True}, 200

    return quart_app


class TestDecisionIsPublished:
    async def test_allowed_decision_carries_subject_and_required_scopes(
        self, app: Quart, seen: list[AuthzDecision | None]
    ) -> None:
        response = await app.test_client().get(
            "/guarded",
            headers={"Authorization": f"Bearer {_token('compliance.audit:admin')}"},
        )
        assert response.status_code == 200
        (decision,) = seen
        assert decision is not None
        assert (decision.allowed, decision.reason, decision.subject) == (
            True,
            "ok",
            "7",
        )
        assert decision.required_scopes == ("compliance.audit:admin",)
        assert decision.granted == frozenset({"compliance.audit:admin"})

    async def test_insufficient_scope_is_published_after_the_403(
        self, app: Quart
    ) -> None:
        captured: list[AuthzDecision | None] = []

        @app.after_request
        async def grab(response: Any) -> Any:
            captured.append(get_authz_decision(request))
            return response

        response = await app.test_client().get(
            "/guarded", headers={"Authorization": f"Bearer {_token('community:read')}"}
        )
        assert response.status_code == 403
        (decision,) = captured
        assert decision is not None
        assert (decision.allowed, decision.reason, decision.subject) == (
            False,
            "insufficient_scope",
            "7",
        )

    @pytest.mark.parametrize(
        ("headers", "reason"),
        [
            ({}, "no_bearer"),
            ({"Authorization": "Bearer not-a-jwt"}, "invalid_token"),
        ],
    )
    async def test_unauthenticated_denials_have_no_subject(
        self, app: Quart, headers: dict[str, str], reason: str
    ) -> None:
        captured: list[AuthzDecision | None] = []

        @app.after_request
        async def grab(response: Any) -> Any:
            captured.append(get_authz_decision(request))
            return response

        assert (
            await app.test_client().get("/guarded", headers=headers)
        ).status_code == 403
        (decision,) = captured
        assert decision is not None
        assert (decision.allowed, decision.reason, decision.subject) == (
            False,
            reason,
            None,
        )

    async def test_routes_without_require_scope_publish_nothing(
        self, app: Quart, seen: list[AuthzDecision | None]
    ) -> None:
        await app.test_client().get("/open")
        assert seen == [None]

    def test_granted_scopes_are_excluded_from_repr_so_they_cannot_be_logged_by_accident(
        self,
    ) -> None:
        decision = AuthzDecision(
            ("a:b",),
            False,
            "insufficient_scope",
            "7",
            frozenset({"secret.scope:admin"}),
        )
        assert "secret.scope" not in repr(decision)

    def test_get_authz_decision_ignores_foreign_values(self) -> None:
        class Fake:
            authz_decision = "not a decision"

        assert get_authz_decision(Fake()) is None
        assert get_authz_decision(object()) is None


class TestAuditScopeIsNotReachableViaStarRead:
    """The reason the audit endpoints require `:admin`: every session already has `*:read`."""

    def test_star_read_does_not_satisfy_the_audit_scope(self) -> None:
        every_session = frozenset(SCOPE_BUNDLES["global"]["viewer"])
        assert "*:read" in every_session
        assert not has_required_scopes(every_session, ("compliance.audit:admin",))
        assert has_required_scopes(
            every_session, ("compliance.audit:read",)
        )  # the old, unsafe name

    def test_global_admin_and_tenant_owner_bundles_do_satisfy_it(self) -> None:
        assert has_required_scopes(
            frozenset(SCOPE_BUNDLES["global"]["admin"]), ("compliance.audit:admin",)
        )
        assert has_required_scopes(
            frozenset(SCOPE_BUNDLES["tenant"]["admin"]), ("compliance.audit:admin",)
        )

    @pytest.mark.parametrize(
        ("level", "bundle"),
        [
            ("tenant", "maintainer"),
            ("tenant", "viewer"),
            ("community", "admin"),
            ("community", "maintainer"),
            ("community", "viewer"),
            ("global", "maintainer"),
            ("global", "viewer"),
        ],
    )
    def test_no_other_bundle_can_read_the_audit_log(
        self, level: str, bundle: str
    ) -> None:
        granted = frozenset(SCOPE_BUNDLES[level][bundle])
        assert not has_required_scopes(granted, ("compliance.audit:admin",))
