"""Audit event validation (PII-free by construction) and HTTP-request classification."""

from __future__ import annotations

import uuid

import pytest

from services.audit_events import (
    FORBIDDEN_DETAIL_KEYS,
    MAX_DETAIL_KEYS,
    MUTATING_METHODS,
    SEMANTIC_ROUTES,
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    AuditValidationError,
    RequestFacts,
    classify_request,
    is_auditable_token,
    is_valid_target_type,
    lenient_details,
    validate_details,
)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "category": AuditCategory.ADMIN,
        "action": "admin.action",
        "actor_uuid": uuid.uuid4(),
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


class TestEventValidation:
    def test_valid_event_normalises_details_to_a_copy(self) -> None:
        source = {"method": "PUT", "status": 200, "ok": True, "none": None}
        event = _event(details=source)
        assert dict(event.details) == source
        source["method"] = "mutated-after-the-fact"
        assert event.details["method"] == "PUT"

    @pytest.mark.parametrize(
        "bad_value",
        [
            "alice@example.com",  # e-mail
            "Alice Smith",  # free text / name
            "hello world",
            "it's",
            "",
            "x" * 129,
            "line\nbreak",
        ],
    )
    def test_free_text_and_email_values_are_refused(self, bad_value: str) -> None:
        with pytest.raises(AuditValidationError):
            validate_details({"reason": bad_value})

    @pytest.mark.parametrize("key", sorted(FORBIDDEN_DETAIL_KEYS))
    def test_pii_and_credential_key_names_are_refused_even_with_innocent_values(
        self, key: str
    ) -> None:
        with pytest.raises(AuditValidationError, match="reserved"):
            validate_details({key: "bob123"})

    @pytest.mark.parametrize("key", ["Method", "1st", "has space", "a-b", "x" * 41, ""])
    def test_detail_keys_must_be_snake_case(self, key: str) -> None:
        with pytest.raises(AuditValidationError):
            validate_details({key: 1})

    def test_floats_and_nested_structures_are_refused(self) -> None:
        with pytest.raises(AuditValidationError):
            validate_details({"ratio": 0.5})
        with pytest.raises(AuditValidationError):
            validate_details({"nested": {"a": 1}})
        with pytest.raises(AuditValidationError):
            validate_details({"items": [{"a": 1}]})

    def test_lists_of_identifiers_are_allowed_and_bounded(self) -> None:
        assert validate_details({"scopes": ["tenant:admin", "tenant:read"]}) == {
            "scopes": ["tenant:admin", "tenant:read"]
        }
        assert validate_details({"ids": {3, 1, 2}}) == {"ids": [1, 2, 3]}
        with pytest.raises(AuditValidationError, match="more than"):
            validate_details({"ids": list(range(65))})
        with pytest.raises(AuditValidationError):
            validate_details({"scopes": ["has space"]})

    def test_too_many_keys_refused(self) -> None:
        with pytest.raises(AuditValidationError, match="more than"):
            validate_details({f"k{i}": i for i in range(MAX_DETAIL_KEYS + 1)})

    def test_empty_details(self) -> None:
        assert validate_details(None) == {}
        assert validate_details({}) == {}

    @pytest.mark.parametrize(
        "action", ["", "A.b", "1abc", "has space", "x", "a" * 101, "tenant/created"]
    )
    def test_action_shape(self, action: str) -> None:
        with pytest.raises(AuditValidationError, match="action"):
            _event(action=action)

    @pytest.mark.parametrize("action", ["app_installed_globally", "authz.denied", "role.x_y"])
    def test_legacy_snake_case_and_dotted_actions_are_valid(self, action: str) -> None:
        assert _event(action=action).action == action

    def test_user_actor_requires_exactly_one_identifier(self) -> None:
        with pytest.raises(AuditValidationError, match="needs actor_uuid or actor_user_id"):
            AuditEvent(category=AuditCategory.ADMIN, action="admin.action")
        with pytest.raises(AuditValidationError, match="not both"):
            _event(actor_user_id=3)

    def test_non_user_actors_carry_no_uuid(self) -> None:
        with pytest.raises(AuditValidationError, match="only USER"):
            AuditEvent(
                category=AuditCategory.ADMIN,
                action="admin.action",
                actor_kind=ActorKind.SYSTEM,
                actor_user_id=1,
            )
        ok = AuditEvent(
            category=AuditCategory.LICENSE,
            action="license.provider_event",
            actor_kind=ActorKind.EXTERNAL,
        )
        assert ok.actor_uuid is None

    def test_actor_uuid_must_be_a_uuid_object_not_a_string(self) -> None:
        with pytest.raises(AuditValidationError, match="uuid.UUID"):
            _event(actor_uuid="11111111-1111-4111-8111-111111111111")

    def test_target_and_tenant_slug_shapes(self) -> None:
        with pytest.raises(AuditValidationError):
            _event(target_type="Not Snake")
        with pytest.raises(AuditValidationError):
            _event(target_id="bob@example.com")
        with pytest.raises(AuditValidationError):
            _event(tenant_slug="has space")
        assert _event(target_id="waddles.core.ping@1.2.3").target_id == "waddles.core.ping@1.2.3"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("tenant:3:app.id", True),
            ("waddles.core.ping@1.0.0", True),
            ("app@2.0.0-rc.1", True),
            ("bob@example.com", False),
            ("bob@1password.com", False),
            ("two words", False),
            ("", False),
        ],
    )
    def test_is_auditable_token(self, value: str, expected: bool) -> None:
        assert is_auditable_token(value) is expected

    def test_is_valid_target_type(self) -> None:
        assert is_valid_target_type("app_install_approvals")
        assert not is_valid_target_type("Bad Type")


class TestLenientDetails:
    def test_keeps_auditable_entries_and_counts_dropped_ones(self) -> None:
        cleaned = lenient_details(
            {
                "tenant_id": 4,
                "permission_ids": ["storage.kv", "net.http"],
                "reason": "target app 'x' is not installed",  # free text -> dropped
                "email": "a@b.c",  # forbidden key -> dropped
            }
        )
        assert cleaned["tenant_id"] == 4
        assert cleaned["permission_ids"] == ["storage.kv", "net.http"]
        assert "reason" not in cleaned
        assert "email" not in cleaned
        assert cleaned["dropped_detail_keys"] == 2

    def test_no_marker_when_nothing_dropped(self) -> None:
        assert lenient_details({"a": 1}) == {"a": 1}
        assert lenient_details(None) == {}

    def test_overlong_details_are_truncated_and_counted(self) -> None:
        cleaned = lenient_details({f"k{i}": i for i in range(MAX_DETAIL_KEYS + 5)})
        assert len(cleaned) <= MAX_DETAIL_KEYS
        assert cleaned["dropped_detail_keys"] >= 5

    def test_result_always_passes_strict_validation(self) -> None:
        raw = {f"k{i}": i for i in range(MAX_DETAIL_KEYS + 3)} | {"reason": "free text here"}
        assert validate_details(lenient_details(raw))


def _facts(**overrides: object) -> RequestFacts:
    base: dict[str, object] = {
        "method": "PUT",
        "rule": "/api/v1/admin/<int:community_id>/settings",
        "status_code": 200,
        "scope_checked": True,
        "denied": False,
        "authenticated": True,
    }
    base.update(overrides)
    return RequestFacts(**base)  # type: ignore[arg-type]


class TestClassifyRequest:
    def test_scope_protected_mutation_is_a_generic_admin_action(self) -> None:
        result = classify_request(_facts())
        assert result is not None
        assert (result.category, result.action, result.outcome) == (
            AuditCategory.ADMIN,
            AuditAction.ADMIN_ACTION,
            AuditOutcome.SUCCESS,
        )

    def test_failed_mutation_is_recorded_as_a_failure(self) -> None:
        result = classify_request(_facts(status_code=500))
        assert result is not None
        assert result.outcome is AuditOutcome.FAILURE

    def test_authenticated_scope_denial_is_an_authz_event(self) -> None:
        result = classify_request(_facts(status_code=403, denied=True))
        assert result is not None
        assert (result.category, result.action, result.outcome) == (
            AuditCategory.AUTHZ,
            AuditAction.AUTHZ_DENIED,
            AuditOutcome.DENIED,
        )

    def test_denied_read_is_audited_too(self) -> None:
        result = classify_request(_facts(method="GET", status_code=403, denied=True))
        assert result is not None
        assert result.action == AuditAction.AUTHZ_DENIED

    def test_authenticated_403_without_a_scope_check_is_still_a_denial(self) -> None:
        """Inline authz (tenant mismatch, community membership) answers 403 with no decision."""
        result = classify_request(_facts(method="GET", status_code=403, scope_checked=False))
        assert result is not None
        assert result.action == AuditAction.AUTHZ_DENIED

    def test_inline_403_after_a_passed_scope_check_is_a_denial_not_an_admin_action(self) -> None:
        """Scope check passed, then the handler refused (e.g. not a member): still authz."""
        result = classify_request(_facts(status_code=403, denied=False, scope_checked=True))
        assert result is not None
        assert (result.category, result.action) == (AuditCategory.AUTHZ, AuditAction.AUTHZ_DENIED)

    def test_unauthenticated_denial_is_not_audited(self) -> None:
        """Pre-identity noise must not be able to bloat the chain."""
        assert classify_request(_facts(status_code=403, denied=True, authenticated=False)) is None

    def test_reads_are_not_audited(self) -> None:
        assert classify_request(_facts(method="GET")) is None

    def test_unscoped_mutation_without_a_semantic_entry_is_not_audited(self) -> None:
        assert classify_request(_facts(scope_checked=False)) is None

    @pytest.mark.parametrize(
        "rule", ["/api/v1/internal/activity/batch", "/internal/service-token", "/mcp/v1"]
    )
    def test_service_plumbing_is_never_classified(self, rule: str) -> None:
        assert classify_request(_facts(method="POST", rule=rule)) is None

    def test_unmatched_route_is_not_audited(self) -> None:
        assert classify_request(_facts(rule=None)) is None

    def test_semantic_route_overrides_the_generic_action(self) -> None:
        result = classify_request(
            _facts(method="DELETE", rule="/api/v1/tenant/<tenant_slug>/admins/<int:user_id>")
        )
        assert result is not None
        assert (result.category, result.action) == (
            AuditCategory.ROLE,
            AuditAction.TENANT_ADMIN_REMOVED,
        )

    def test_dsar_export_get_is_audited_though_it_is_a_read(self) -> None:
        result = classify_request(
            _facts(method="GET", rule="/api/v1/user/me/data", scope_checked=False)
        )
        assert result is not None
        assert result.action == AuditAction.PRIVACY_DSAR_EXPORT

    def test_erasure_failure_is_recorded_as_a_failure(self) -> None:
        result = classify_request(
            _facts(
                method="DELETE", rule="/api/v1/user/me/data", scope_checked=False, status_code=401
            )
        )
        assert result is not None
        assert result.action == AuditAction.PRIVACY_ERASURE_REQUESTED
        assert result.outcome is AuditOutcome.FAILURE

    def test_webhook_events_have_no_user_but_are_license_events(self) -> None:
        result = classify_request(
            _facts(
                method="POST",
                rule="/api/v1/marketplace/webhooks/stripe",
                scope_checked=False,
                authenticated=False,
            )
        )
        assert result is not None
        assert (result.category, result.action) == (
            AuditCategory.LICENSE,
            AuditAction.LICENSE_PROVIDER_EVENT,
        )

    def test_method_case_is_normalised(self) -> None:
        assert classify_request(_facts(method="put")) is not None


class TestSemanticMapShape:
    def test_every_entry_is_well_formed(self) -> None:
        assert SEMANTIC_ROUTES  # a map that examined zero entries would prove nothing
        for (method, rule), meaning in SEMANTIC_ROUTES.items():
            assert method in MUTATING_METHODS | {"GET"}
            assert rule.startswith("/api/")
            assert is_auditable_token(rule), f"rule {rule!r} would be refused as a detail value"
            assert len(rule) <= 128
            assert meaning.action in set(AuditAction)

    def test_covers_each_required_security_domain(self) -> None:
        """GRC #3 coverage list: authz, tenant/role changes, erasure/DSAR, admin, license."""
        actions = {meaning.action for meaning in SEMANTIC_ROUTES.values()}
        for required in (
            AuditAction.TENANT_SETTINGS_CHANGED,
            AuditAction.TENANT_ADMIN_ADDED,
            AuditAction.ROLE_SUPER_ADMIN_CHANGED,
            AuditAction.PRIVACY_DSAR_EXPORT,
            AuditAction.PRIVACY_ERASURE_REQUESTED,
            AuditAction.LICENSE_SUBSCRIPTION_CHANGED,
            AuditAction.ADMIN_PLATFORM_CONFIG_CHANGED,
        ):
            assert required in actions
