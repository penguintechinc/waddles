"""SOC2 CC7.2: sensitive inline-authz routes emit audit events (and never a secret value).

Inline-authz routes (a `tenant_middleware` + in-handler check, no `@require_scope`) publish no
`AuthzDecision`, so the generic admin-action rule in `services.audit_http` never saw them. They are
now listed in `services.audit_events.INLINE_AUTHZ_ROUTES`. These tests pin, against the real
`after_request` hook and a real (sqlite) audit store, that

* every live mutating route lacking `require_scope` is audited or explicitly exempt (the
  denominator is asserted, so a scanner pointed at the wrong place fails instead of passing);
* each audited route emits the expected category/action/outcome for the caller's UUID + tenant;
* no request or response secret (body, query, header, echoed response) reaches the event.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from flask_core.tenancy import tenant_middleware
from quart import Quart, request
from sqlalchemy import select

from app import create_app
from services import audit_http
from services.audit_events import (
    INLINE_AUTHZ_ROUTES,
    INLINE_AUTHZ_UNAUDITED,
    SEMANTIC_ROUTES,
    UNAUDITED_RULE_PREFIXES,
    AuditAction,
    AuditCategory,
    AuditOutcome,
    RequestFacts,
    RouteAudit,
    classify_request,
    is_auditable_token,
)
from services.audit_http import install_audit_hooks
from services.audit_service import AuditService
from tests.audit_support import USER_UUID
from tests.conftest import make_token
from tests.test_app_factory import _test_config

#: A fixture value asserted to NEVER appear in an audit event.
SECRET = "sk-LIVE-4f9a1c0de5-SUPERSECRET"  # noqa: S105  # nosec B105
ADMIN_TOKEN = {"scope": "community:admin tenant:admin", "user_id": "7"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
#: Inline routes that run no `tenant_middleware` in production: the audit hook must still
#: attribute them to the caller's tenant chain from the verified JWT claim.
_NO_TENANT_MIDDLEWARE_PREFIX = "/api/v1/admin/<community_id>/calendar/"
_INLINE_PARAMS = sorted(INLINE_AUTHZ_ROUTES.items())


def _concrete(rule: str) -> str:
    """Turn a URL rule into a concrete path (every converter becomes ``1``)."""
    return re.sub(r"<[^>]+>", "1", rule)


def _auth(**kwargs: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(**kwargs)}"}


async def _events(dal: Any) -> list[Any]:
    table = dal.metadata.tables["audit_events"]
    stmt = select(table).order_by(table.c.chain_id, table.c.seq)
    async with dal.engine.connect() as conn:
        return list((await conn.execute(stmt)).all())


class TestInlineAuthzCoverage:
    """Every sensitive inline-authz route is classified (audited, or exempt with a reason)."""

    def test_every_mutating_route_without_require_scope_is_classified(self) -> None:
        application = create_app(_test_config())
        unclassified: list[tuple[str, str]] = []
        examined = 0
        for rule in application.url_map.iter_rules():
            for method in (rule.methods or set()) & _MUTATING:
                view = application.view_functions[rule.endpoint]
                while hasattr(view, "__wrapped__"):
                    view = view.__wrapped__
                try:
                    source = inspect.getsource(view)
                except (OSError, TypeError):
                    source = ""
                if "require_scope" in source:
                    continue  # the generic admin-action rule already covers it
                examined += 1
                key = (method, rule.rule)
                if (
                    key in SEMANTIC_ROUTES
                    or key in INLINE_AUTHZ_ROUTES
                    or key in INLINE_AUTHZ_UNAUDITED
                    or rule.rule.startswith(UNAUDITED_RULE_PREFIXES)
                ):
                    continue
                unclassified.append(key)
        assert examined > 100, "scanner examined too few routes; it is pointed at the wrong place"
        assert not unclassified, (
            "mutating routes with inline authz must emit an audit event: add them to "
            f"INLINE_AUTHZ_ROUTES (or INLINE_AUTHZ_UNAUDITED with a reason): {unclassified}"
        )

    def test_inline_maps_name_live_routes_and_are_disjoint(self) -> None:
        application = create_app(_test_config())
        live = {
            (method, rule.rule)
            for rule in application.url_map.iter_rules()
            for method in rule.methods or ()
        }
        assert len(INLINE_AUTHZ_ROUTES) >= 70  # the finding named ~70 routes
        assert not [k for k in INLINE_AUTHZ_ROUTES if k not in live]
        assert not [k for k in INLINE_AUTHZ_UNAUDITED if k not in live]
        assert not set(INLINE_AUTHZ_ROUTES) & set(INLINE_AUTHZ_UNAUDITED)
        assert not set(INLINE_AUTHZ_ROUTES) & set(SEMANTIC_ROUTES)
        assert all(reason for reason in INLINE_AUTHZ_UNAUDITED.values())

    def test_named_sensitive_routes_are_audited(self) -> None:
        """The finding's examples: BYOK keys, stream keys, PATs, payout profile."""
        for key in (
            ("PUT", "/api/v1/admin/<int:community_id>/ai/byok-keys"),
            ("DELETE", "/api/v1/admin/<int:community_id>/ai/byok-keys/<provider>"),
            ("POST", "/api/v1/admin/<int:community_id>/streams/key/regenerate"),
            ("POST", "/api/v1/user/tokens/pat"),
            ("DELETE", "/api/v1/user/tokens/pat"),
            ("POST", "/api/v1/marketplace/vendor/profile"),
            ("PUT", "/api/v1/marketplace/vendor/profile"),
        ):
            assert key in INLINE_AUTHZ_ROUTES, key


class TestClassifyInlineRoutes:
    """Pure classifier behaviour for inline routes (no HTTP, no store)."""

    @staticmethod
    def _facts(**overrides: Any) -> RequestFacts:
        base: dict[str, Any] = {
            "method": "POST",
            "rule": "/api/v1/user/tokens/pat",
            "status_code": 201,
            "scope_checked": False,
            "denied": False,
            "authenticated": True,
        }
        base.update(overrides)
        return RequestFacts(**base)

    def test_authenticated_inline_mutation_is_a_credential_event(self) -> None:
        result = classify_request(self._facts())
        assert result is not None
        assert (result.category, result.action, result.outcome) == (
            AuditCategory.CREDENTIAL,
            AuditAction.CREDENTIAL_PAT_CREATED,
            AuditOutcome.SUCCESS,
        )

    def test_error_status_is_a_failure(self) -> None:
        result = classify_request(self._facts(status_code=500))
        assert result is not None and result.outcome is AuditOutcome.FAILURE

    def test_unauthenticated_is_not_audited(self) -> None:
        assert classify_request(self._facts(authenticated=False, status_code=401)) is None

    def test_every_entry_is_well_formed(self) -> None:
        assert INLINE_AUTHZ_ROUTES  # an empty map would prove nothing
        for (method, rule), meaning in INLINE_AUTHZ_ROUTES.items():
            assert method in _MUTATING
            assert rule.startswith("/api/")
            assert is_auditable_token(rule), f"rule {rule!r} would be refused as a detail value"
            assert meaning.action in set(AuditAction)


@pytest.fixture
async def inline_app(
    bundle_install_db: Any,
    audit_dal: Any,
    audit_service: AuditService,
) -> Quart:
    """One stand-in handler per inline-authz route, behind the real audit hook."""
    quart_app = Quart(__name__)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = audit_dal
    quart_app.config[audit_http.AUDIT_SERVICE_CONFIG_KEY] = audit_service
    install_audit_hooks(quart_app)

    async def handler(**_kwargs: Any) -> tuple[dict[str, Any], int]:
        status = int(request.headers.get("X-Force-Status", "200"))
        # Echo the secret like a real key-issuing endpoint would.
        return {"issued": SECRET}, status

    for index, ((method, rule), _meaning) in enumerate(_INLINE_PARAMS):
        view: Any = handler
        if not rule.startswith(_NO_TENANT_MIDDLEWARE_PREFIX):
            view = tenant_middleware(handler)
        quart_app.add_url_rule(rule, endpoint=f"inline_{index}", view_func=view, methods=[method])
    return quart_app


async def _call(app: Quart, method: str, rule: str, **kw: Any) -> Any:
    return await app.test_client().open(
        _concrete(rule) + f"?token={SECRET}",
        method=method,
        json={"api_key": SECRET, "password": SECRET, "name": "alice@example.com"},
        **kw,
    )


class TestInlineAuthzRoutesEmitAuditEvents:
    """Each audited route emits exactly one event, UUID-attributed, with no secret in it."""

    @pytest.mark.parametrize(
        ("key", "meaning"), _INLINE_PARAMS, ids=[f"{m} {r}" for (m, r), _ in _INLINE_PARAMS]
    )
    async def test_route_emits_expected_event_without_the_secret(
        self,
        inline_app: Quart,
        audit_dal: Any,
        key: tuple[str, str],
        meaning: RouteAudit,
    ) -> None:
        method, rule = key
        headers = {**_auth(**ADMIN_TOKEN), "X-Api-Key": SECRET}
        response = await _call(inline_app, method, rule, headers=headers)
        assert response.status_code == 200
        (event,) = await _events(audit_dal)
        assert (event.category, event.action, event.outcome) == (
            meaning.category.value,
            meaning.action.value,
            "success",
        )
        assert event.chain_id == "tenant:1"  # tenant from the verified token, with or without TM
        assert event.actor_uuid == str(USER_UUID)  # UUID, never the raw user id/e-mail/username
        assert event.details == {"method": method, "rule": rule, "status": 200}
        flat = repr(tuple(event))
        assert SECRET not in flat
        assert "alice@example.com" not in flat
        assert "token=" not in flat

    @pytest.mark.parametrize(("key", "meaning"), _INLINE_PARAMS[:3])
    async def test_failed_action_is_recorded_as_failure(
        self, inline_app: Quart, audit_dal: Any, key: tuple[str, str], meaning: RouteAudit
    ) -> None:
        method, rule = key
        headers = {**_auth(**ADMIN_TOKEN), "X-Force-Status": "500"}
        await _call(inline_app, method, rule, headers=headers)
        (event,) = await _events(audit_dal)
        assert event.action == meaning.action.value
        assert event.outcome == "failure" and event.details["status"] == 500
        assert SECRET not in repr(tuple(event))

    async def test_unauthenticated_attempt_is_not_recorded(
        self, inline_app: Quart, audit_dal: Any
    ) -> None:
        """Pre-identity noise must not be able to bloat the chain (and no secret is stored)."""
        for method, rule in INLINE_AUTHZ_ROUTES:
            response = await _call(inline_app, method, rule)
            assert response.status_code in {200, 401}
        assert await _events(audit_dal) == []

    async def test_inline_refusal_is_an_authz_denial_not_a_success(
        self, inline_app: Quart, audit_dal: Any
    ) -> None:
        method, rule = ("PUT", "/api/v1/admin/<int:community_id>/ai/byok-keys")
        headers = {**_auth(**ADMIN_TOKEN), "X-Force-Status": "403"}
        await _call(inline_app, method, rule, headers=headers)
        (event,) = await _events(audit_dal)
        assert (event.category, event.action, event.outcome) == ("authz", "authz.denied", "denied")
        assert SECRET not in repr(tuple(event))
