"""Cross-cutting wiring: scopes, the licence contract, route discovery, audit coverage."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from flask_core.auth import SCOPE_BUNDLES
from flask_core.auth import SCOPE_CONNECTIONS_ADMIN as CORE_SCOPE
from flask_core.authz import _scope_covers
from flask_core.entitlement import get_entitlement_client
from flask_core.tier_catalog import FEATURE_MIN_TIERS

from app import create_app
from blueprints.v1 import provider_connections as bp
from services.audit_events import (
    UNAUDITED_RULE_PREFIXES,
    AuditAction,
    AuditCategory,
    RequestFacts,
    classify_request,
)
from tests.provider_connections.test_api import ADMIN_FIELDS
from tests.test_app_factory import _test_config


def test_the_scope_constants_agree_across_flask_core_and_hub_api() -> None:
    assert bp.SCOPE_CONNECTIONS_ADMIN == CORE_SCOPE == "connections:admin"


def test_tenant_owners_hold_the_scope_and_ordinary_roles_do_not() -> None:
    assert CORE_SCOPE in SCOPE_BUNDLES["tenant"]["admin"]
    for level, bundles in SCOPE_BUNDLES.items():
        for name, scopes in bundles.items():
            if (level, name) != ("tenant", "admin"):
                assert CORE_SCOPE not in scopes, (level, name)


def test_the_universal_read_wildcard_can_never_satisfy_the_scope() -> None:
    assert not _scope_covers("*:read", CORE_SCOPE)
    assert not _scope_covers("connections:read", CORE_SCOPE)
    assert _scope_covers("*:admin", CORE_SCOPE)  # global admins, as intended
    assert _scope_covers(CORE_SCOPE, CORE_SCOPE)


def test_the_feature_is_a_professional_tier_licensed_contract() -> None:
    """The licence catalog marks the feature Professional.

    hub-api sees only the static catalog (the ``*_module`` contract packages are not installed in
    its image); ``libs/flask_core`` ``TestCatalogMatchesContracts`` and
    ``libs/core_platform_module`` pin the catalog <-> contract agreement.
    """
    assert FEATURE_MIN_TIERS[bp.FEATURE_PROVIDER_CONNECTIONS] == "professional"
    client = get_entitlement_client()
    assert client.required_tier(bp.FEATURE_PROVIDER_CONNECTIONS) == "professional"


async def test_routes_are_discovered_by_the_real_app_factory() -> None:
    app = create_app(_test_config())
    rules = {
        (method, rule.rule) for rule in app.url_map.iter_rules() for method in rule.methods or ()
    }
    base = "/api/v1/communities/<int:community_id>/provider-connections"
    for expected in (
        ("GET", "/api/v1/connection-providers"),
        ("GET", base),
        ("POST", base),
        ("GET", f"{base}/<int:connection_id>"),
        ("PATCH", f"{base}/<int:connection_id>"),
        ("DELETE", f"{base}/<int:connection_id>"),
        ("GET", "/api/v1/internal/provider-connections"),
        ("POST", f"/api/v1/internal{base.removeprefix('/api/v1')}/<int:connection_id>/resolve"),
    ):
        assert expected in rules, expected


def test_admin_mutations_are_audited_as_admin_actions_and_internal_routes_are_not() -> None:
    facts = RequestFacts(
        method="POST",
        rule="/api/v1/communities/<int:community_id>/provider-connections",
        status_code=201,
        scope_checked=True,
        denied=False,
        authenticated=True,
    )
    result = classify_request(facts)
    assert result is not None
    assert (result.category, result.action) == (AuditCategory.ADMIN, AuditAction.ADMIN_ACTION)
    denied = classify_request(
        RequestFacts(
            method="DELETE",
            rule="/api/v1/communities/<int:community_id>/provider-connections/<int:connection_id>",
            status_code=403,
            scope_checked=True,
            denied=True,
            authenticated=True,
        )
    )
    assert denied is not None and denied.category is AuditCategory.AUTHZ
    internal = RequestFacts(
        method="POST",
        rule="/api/v1/internal/communities/<int:community_id>/provider-connections/<int:connection_id>/resolve",
        status_code=200,
        scope_checked=False,
        denied=False,
        authenticated=False,
    )
    assert "/api/v1/internal/" in UNAUDITED_RULE_PREFIXES
    assert classify_request(internal) is None


SPEC_PATH = Path(__file__).resolve().parents[3] / "openapi" / "v1.yaml"


class TestCommittedOpenApiSpec:
    """``openapi/v1.yaml`` is generated (``scripts/export-hubapi-openapi.py``); regenerate on drift."""

    def _spec(self) -> dict[str, Any]:
        spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
        assert len(spec["paths"]) > 100  # the assertions below must look at the real, full spec
        return spec

    def test_every_route_of_the_framework_is_published(self) -> None:
        paths = self._spec()["paths"]
        base = "/api/v1/communities/{community_id}/provider-connections"
        expected = {
            "/api/v1/connection-providers": {"get"},
            base: {"get", "post"},
            f"{base}/{{connection_id}}": {"get", "patch", "delete"},
            "/api/v1/internal/provider-connections": {"get"},
            f"/api/v1/internal{base.removeprefix('/api/v1')}/{{connection_id}}/resolve": {"post"},
        }
        for path, methods in expected.items():
            assert path in paths, path
            assert methods <= set(paths[path]), path

    def test_operation_ids_are_unique_so_no_other_operation_was_renamed(self) -> None:
        ids = [
            op["operationId"]
            for item in self._spec()["paths"].values()
            for op in item.values()
            if isinstance(op, dict) and "operationId" in op
        ]
        assert len(ids) == len(set(ids))
        assert "post_create_connection" in ids  # SSO's id is untouched by this feature

    def test_the_admin_response_schema_has_no_secret_bearing_property(self) -> None:
        props = self._spec()["components"]["schemas"]["ProviderConnectionDTO"]["properties"]
        assert set(props) == ADMIN_FIELDS
        suspicious = [
            name for name in props if re.search(r"token|secret|cipher|password", name, re.I)
        ]
        assert suspicious == ["hasRefreshToken"]  # a boolean flag, not a value
        assert props["hasRefreshToken"]["type"] == "boolean"

    def test_the_request_schemas_forbid_extra_fields(self) -> None:
        spec = self._spec()
        base = spec["paths"]["/api/v1/communities/{community_id}/provider-connections"]
        one = spec["paths"][
            "/api/v1/communities/{community_id}/provider-connections/{connection_id}"
        ]
        for operation in (base["post"], one["patch"]):
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            assert body["additionalProperties"] is False
        assert spec["components"]["schemas"]["CredentialsBody"]["additionalProperties"] is False
