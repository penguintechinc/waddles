"""HTTP-layer audit coverage: authz decisions, admin actions, privacy rights, SSO session issuance.

Drives the real hook (`services.audit_http.install_audit_hooks`) on a small Quart app whose routes
use the real `tenant_middleware` + `require_scope` + JWTs, against a real (sqlite) audit store.
Also pins the app-factory wiring (hook installed, service published at startup) and that every
entry of the semantic route map still names a real route.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Quart
from sqlalchemy import select

from app import create_app
from services import audit_http
from services.audit_events import SEMANTIC_ROUTES
from services.audit_http import install_audit_hooks, record_session_issued
from services.audit_service import AuditService, AuditWriteError
from tests.audit_support import (
    USER_UUID,
    FakeGate,
)
from tests.conftest import TENANT_SLUG, make_token
from tests.test_app_factory import _test_config

ADMIN_TOKEN = {"scope": "community:admin tenant:admin", "user_id": "7"}


@pytest.fixture
async def app(
    bundle_install_db: Any,
    audit_dal: Any,
    audit_service: AuditService,
) -> Quart:
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = audit_dal
    quart_app.config[audit_http.AUDIT_SERVICE_CONFIG_KEY] = audit_service
    install_audit_hooks(quart_app)

    @quart_app.route("/api/v1/admin/<int:community_id>/settings", methods=["PUT", "GET"])
    @tenant_middleware
    @require_scope("community:admin")
    async def community_settings(community_id: int) -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    @quart_app.route("/api/v1/admin/<int:community_id>/failing", methods=["POST"])
    @tenant_middleware
    @require_scope("community:admin")
    async def failing_mutation(community_id: int) -> tuple[dict[str, Any], int]:
        return {"ok": False}, 500

    @quart_app.route("/api/v1/tenant/<tenant_slug>/admins", methods=["POST"])
    @tenant_middleware
    @require_scope("tenant:admin")
    async def add_tenant_admin(tenant_slug: str) -> tuple[dict[str, Any], int]:
        return {"ok": True}, 201

    @quart_app.route("/api/v1/user/me/data", methods=["GET", "DELETE"])
    @tenant_middleware
    async def privacy(*_a: Any) -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    @quart_app.route("/api/v1/marketplace/webhooks/stripe", methods=["POST"])
    async def stripe_webhook() -> tuple[dict[str, Any], int]:
        return {"received": True}, 200

    @quart_app.route("/api/v1/internal/activity/batch", methods=["POST"])
    @tenant_middleware
    @require_scope("community:admin")
    async def internal_batch() -> tuple[dict[str, Any], int]:
        return {"ok": True}, 200

    @quart_app.route("/api/v1/community/<int:community_id>/inline", methods=["GET"])
    @tenant_middleware
    async def inline_authz(community_id: int) -> tuple[dict[str, Any], int]:
        return {"error": "not a member"}, 403

    @quart_app.route("/api/v1/public/hello", methods=["GET", "POST"])
    async def public_route() -> tuple[dict[str, Any], int]:
        return {"hello": "world"}, 200

    return quart_app


def _auth(**kwargs: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(**kwargs)}"}


async def _events(dal: Any, chain: str | None = None) -> list[Any]:
    table = dal.metadata.tables["audit_events"]
    stmt = select(table).order_by(table.c.chain_id, table.c.seq)
    if chain is not None:
        stmt = stmt.where(table.c.chain_id == chain)
    async with dal.engine.connect() as conn:
        return list((await conn.execute(stmt)).all())


class TestAdminActions:
    async def test_scope_protected_mutation_is_audited_with_rule_not_path(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        response = await app.test_client().put(
            "/api/v1/admin/424242/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 200
        (event,) = await _events(audit_dal)
        assert (event.chain_id, event.category, event.action) == (
            "tenant:1",
            "admin",
            "admin.action",
        )
        assert event.outcome == "success"
        assert event.actor_uuid == str(USER_UUID)
        assert event.details == {
            "method": "PUT",
            "rule": "/api/v1/admin/<int:community_id>/settings",
            "status": 200,
            "required_scopes": ["community:admin"],
        }
        assert "424242" not in str(event.details)  # the concrete path never reaches the log

    async def test_failed_mutation_is_recorded_as_a_failure(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        await app.test_client().post("/api/v1/admin/1/failing", headers=_auth(**ADMIN_TOKEN))
        (event,) = await _events(audit_dal)
        assert event.outcome == "failure" and event.details["status"] == 500

    async def test_semantic_route_gets_its_specific_action_and_category(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        response = await app.test_client().post(
            f"/api/v1/tenant/{TENANT_SLUG}/admins", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 201
        (event,) = await _events(audit_dal)
        assert (event.category, event.action) == ("role", "tenant.admin_added")
        assert TENANT_SLUG not in str(event.details)

    async def test_reads_are_not_audited(self, app: Quart, audit_dal: Any) -> None:
        response = await app.test_client().get(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 200
        assert await _events(audit_dal) == []

    async def test_internal_service_routes_are_never_audited_by_the_generic_rule(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        await app.test_client().post(
            "/api/v1/internal/activity/batch", headers=_auth(**ADMIN_TOKEN)
        )
        assert await _events(audit_dal) == []

    async def test_options_and_public_routes_are_ignored(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        client = app.test_client()
        await client.options("/api/v1/admin/1/settings")
        await client.post("/api/v1/public/hello")
        assert await _events(audit_dal) == []


class TestAuthzDecisions:
    async def test_authenticated_scope_denial_is_audited(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(scope="community:read", user_id="7")
        )
        assert response.status_code == 403
        (event,) = await _events(audit_dal)
        assert (event.category, event.action, event.outcome) == ("authz", "authz.denied", "denied")
        assert event.actor_uuid == str(USER_UUID)
        assert event.details["reason"] == "insufficient_scope"
        assert event.details["required_scopes"] == ["community:admin"]
        assert "community:read" not in str(event.details)  # granted scopes are never recorded

    async def test_denied_read_is_audited_too(self, app: Quart, audit_dal: Any) -> None:
        await app.test_client().get(
            "/api/v1/admin/1/settings", headers=_auth(scope="", user_id="7")
        )
        (event,) = await _events(audit_dal)
        assert event.action == "authz.denied"

    async def test_inline_authz_403_without_a_scope_check_is_audited(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        response = await app.test_client().get(
            "/api/v1/community/5/inline", headers=_auth(scope="", user_id="7")
        )
        assert response.status_code == 403
        (event,) = await _events(audit_dal)
        assert (event.action, event.details["rule"]) == (
            "authz.denied",
            "/api/v1/community/<int:community_id>/inline",
        )

    async def test_unauthenticated_requests_are_not_audited(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        """No identity -> no per-actor record (and no way to bloat the chain anonymously)."""
        client = app.test_client()
        assert (await client.put("/api/v1/admin/1/settings")).status_code in (401, 403)
        bad = await client.put(
            "/api/v1/admin/1/settings", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert bad.status_code in (401, 403)
        assert await _events(audit_dal) == []

    async def test_tenant_resolution_failure_for_an_authenticated_caller_is_audited_on_platform(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN, tenant="no-such-tenant")
        )
        assert response.status_code == 403
        (event,) = await _events(audit_dal)
        assert (event.chain_id, event.action) == ("platform", "authz.denied")


class TestPrivacyRights:
    async def test_dsar_export_and_erasure_are_audited(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        client = app.test_client()
        headers = _auth(scope="", user_id="7")
        assert (await client.get("/api/v1/user/me/data", headers=headers)).status_code == 200
        assert (await client.delete("/api/v1/user/me/data", headers=headers)).status_code == 200
        events = await _events(audit_dal)
        assert [(e.category, e.action) for e in events] == [
            ("privacy", "privacy.dsar_export"),
            ("privacy", "privacy.erasure_requested"),
        ]
        assert all(e.actor_uuid == str(USER_UUID) for e in events)

    async def test_statutory_rights_work_in_every_tier_even_when_audit_is_not_entitled(
        self,
        app: Quart,
        audit_dal: Any,
        gate: FakeGate,
    ) -> None:
        """Audit RECORDING is Enterprise; the DSAR/erasure RIGHTS are never gated."""
        gate.entitled = False
        client = app.test_client()
        headers = _auth(scope="", user_id="7")
        assert (await client.get("/api/v1/user/me/data", headers=headers)).status_code == 200
        assert (await client.delete("/api/v1/user/me/data", headers=headers)).status_code == 200
        assert await _events(audit_dal) == []


class TestLicenseProviderEvents:
    async def test_webhook_is_recorded_on_the_platform_chain_with_no_user(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        await app.test_client().post("/api/v1/marketplace/webhooks/stripe", json={"x": 1})
        (event,) = await _events(audit_dal)
        assert (event.chain_id, event.category, event.action) == (
            "platform",
            "license",
            "license.provider_event",
        )
        assert (event.actor_kind, event.actor_uuid) == ("external", None)


class TestEntitlementAndFailure:
    async def test_unentitled_tenant_records_nothing_and_the_response_is_untouched(
        self,
        app: Quart,
        audit_dal: Any,
        gate: FakeGate,
    ) -> None:
        gate.entitled = False
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 200
        assert await _events(audit_dal) == []

    async def test_audit_write_failure_fails_the_request_loudly_without_leaking(
        self,
        app: Quart,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "-".join(["internal", "driver", "detail"])

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError(secret)

        monkeypatch.setattr(audit_service, "_append", boom)
        caplog.set_level(logging.ERROR)
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 500
        body = await response.get_json()
        assert body["error"]["code"] == "AUDIT_UNAVAILABLE"
        assert secret not in str(body)
        assert any("audit write FAILED" in r.getMessage() for r in caplog.records)

    async def test_unbuildable_event_is_also_loud_not_silent(
        self,
        app: Quart,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def explode(**_kw: object) -> None:
            raise ValueError("cannot build")

        monkeypatch.setattr(audit_http, "_build_event", explode)
        caplog.set_level(logging.ERROR)
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 500
        assert any("audit write FAILED" in r.getMessage() for r in caplog.records)

    async def test_no_wired_service_passes_the_request_through(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        del app.config[audit_http.AUDIT_SERVICE_CONFIG_KEY]
        response = await app.test_client().put(
            "/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN)
        )
        assert response.status_code == 200
        assert await _events(audit_dal) == []

    async def test_chain_grows_and_stays_intact_across_requests(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        client = app.test_client()
        for _ in range(4):
            await client.put("/api/v1/admin/1/settings", headers=_auth(**ADMIN_TOKEN))
        await client.put("/api/v1/admin/1/settings", headers=_auth(scope="x:y", user_id="7"))
        report = await audit_service.verify("tenant:1")
        assert report.verification.ok and report.verification.examined == 5


class TestSessionIssuance:
    async def test_session_issued_event_records_the_auth_method_and_uuid_actor(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        async with app.app_context():
            await record_session_issued(
                user_id=7, tenant_id=None, tenant_slug=TENANT_SLUG, auth_method="oauth_discord"
            )
        (event,) = await _events(audit_dal)
        assert (event.category, event.action, event.outcome) == (
            "authn",
            "authn.session_issued",
            "success",
        )
        assert event.chain_id == "tenant:1"
        assert event.actor_uuid == str(USER_UUID)
        assert event.details == {
            "auth_method": "oauth_discord",
            "pending_link": False,
            "tenant_known": True,
        }

    async def test_unknown_login_tenant_goes_to_the_platform_chain_and_never_the_gate(
        self,
        app: Quart,
        audit_dal: Any,
        gate: FakeGate,
    ) -> None:
        async with app.app_context():
            await record_session_issued(
                user_id=7,
                tenant_id=None,
                tenant_slug="attacker chosen slug",
                auth_method="password",
            )
        (event,) = await _events(audit_dal)
        assert event.chain_id == "platform" and event.details["tenant_known"] is False
        assert "attacker chosen slug" not in gate.calls  # user input never reaches the gate

    async def test_pending_link_session_has_an_unresolved_actor(
        self,
        app: Quart,
        audit_dal: Any,
    ) -> None:
        async with app.app_context():
            await record_session_issued(
                user_id=None,
                tenant_id=1,
                tenant_slug=None,
                auth_method="temp_password",
                pending_link=True,
            )
        (event,) = await _events(audit_dal)
        assert (event.actor_kind, event.actor_uuid) == ("unresolved", None)
        assert event.details["pending_link"] is True

    async def test_unrecordable_session_raises_so_no_session_is_issued(
        self,
        app: Quart,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("down")

        monkeypatch.setattr(audit_service, "_append", boom)
        async with app.app_context():
            with pytest.raises(AuditWriteError):
                await record_session_issued(
                    user_id=7, tenant_id=1, tenant_slug=None, auth_method="password"
                )

    async def test_no_service_outside_an_app_is_a_noop(self) -> None:
        await record_session_issued(
            user_id=7, tenant_id=1, tenant_slug=None, auth_method="password"
        )

    async def test_create_session_token_audits_before_persisting_the_session(
        self,
        app: Quart,
        auth_db: Any,
        audit_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.auth_service import SessionUser, create_session_token

        cfg = _test_config()
        user = SessionUser(id=7, email="a@b.c", username="alice")
        async with app.app_context():
            token = await create_session_token(
                auth_db, auth_db.dal, cfg, user=user, tenant_slug=TENANT_SLUG, auth_method="passkey"
            )
        assert token
        (event,) = await _events(audit_dal)
        assert event.details["auth_method"] == "passkey"
        sessions = auth_db.dal(auth_db.dal.hub_sessions.id > 0).select()
        assert len(sessions) == 1

        # An unrecordable audit event must stop the session from being issued/persisted.
        async def boom(**_kw: object) -> None:
            raise AuditWriteError("audit down")

        monkeypatch.setattr("services.auth_service.record_session_issued", boom)
        async with app.app_context():
            with pytest.raises(AuditWriteError):
                await create_session_token(auth_db, auth_db.dal, cfg, user=user)
        assert len(auth_db.dal(auth_db.dal.hub_sessions.id > 0).select()) == 1


class TestAppFactoryWiring:
    """Regression: auditing cannot be silently disabled by forgetting to wire it."""

    async def test_startup_publishes_the_audit_service_and_the_hook_is_installed(self) -> None:
        application = create_app(_test_config())
        async with application.test_app():
            assert isinstance(
                application.config.get(audit_http.AUDIT_SERVICE_CONFIG_KEY), AuditService
            )
        assert audit_http._audit_response in [
            h for hooks in application.after_request_funcs.values() for h in hooks
        ]

    def test_every_semantic_route_names_a_real_route(self) -> None:
        """A renamed/removed route must fail here, not silently drop out of the audit trail."""
        application = create_app(_test_config())
        live = {
            (method, rule.rule)
            for rule in application.url_map.iter_rules()
            for method in rule.methods or ()
        }
        assert SEMANTIC_ROUTES
        missing = [key for key in SEMANTIC_ROUTES if key not in live]
        assert not missing, f"semantic audit routes with no live route: {missing}"

    def test_scope_protected_mutating_routes_are_discoverable_for_generic_coverage(self) -> None:
        """Denominator: the generic rule has real work to do (a zero count would prove nothing)."""
        application = create_app(_test_config())
        mutating = [
            rule
            for rule in application.url_map.iter_rules()
            if rule.methods and rule.methods & {"POST", "PUT", "PATCH", "DELETE"}
        ]
        assert len(mutating) > 200
