"""`compliance.bulk_dsar` (Enterprise) + `tenancy.whitelabel` (Professional) contracts.

Proves the two newest Core/platform Feature contracts resolve to the right
entitlement through the REAL two-gate (:class:`flask_core.entitlement.
EntitlementClient`: PostHog flag AND license tier) once `tier_requirements`
is built from the contracts' own `flag`/`min_tier` -- fake gates, no live
PostHog/license server -- and that an outage fails closed.

hub-api cannot import this package (`libs/` is not on its path), so it
duplicates the two flag strings as literals
(`hub_api/services/admin_data_privacy_service.py::FEATURE_BULK_DSAR`,
`hub_api/services/branding_service.py::FEATURE_WHITELABEL`); the literals
asserted below are the other half of that drift check (hub-api's own tests
pin the same strings on their side).

Statutory self-service DSAR is deliberately NOT a contract: it must stay
available in every tier (critical-rules.md), and only the admin/bulk
convenience layer is Enterprise.

Fail-on-purpose proof: temporarily flipping `tenancy.whitelabel`'s
`min_tier` to `"free"` in `features.py` made
`test_tier_resolution[tenancy.whitelabel-free-False]` go red, then reverted.
"""

from __future__ import annotations

from typing import Mapping, Optional

import pytest

from core_platform_module.features import build_contracts, build_default_apps
from flask_core.entitlement import EntitlementClient

#: Literals duplicated in hub-api -- see module docstring.
BULK_DSAR_FLAG = "waddles.compliance.bulk_dsar"
WHITELABEL_FLAG = "waddles.tenancy.whitelabel"


class _FlagGate:
    def __init__(self, result: Optional[bool], raises: Optional[Exception] = None) -> None:
        self.result = result
        self.raises = raises

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Optional[Mapping[str, str]] = None
    ) -> Optional[bool]:
        if self.raises is not None:
            raise self.raises
        return self.result


class _LicenseGate:
    def __init__(self, tier: str, raises: Optional[Exception] = None) -> None:
        self.tier = tier
        self.raises = raises

    def resolve_tier(self) -> str:
        if self.raises is not None:
            raise self.raises
        return self.tier


def _contracts_by_id() -> dict:
    return {c.id: c for c in build_contracts()}


def _client(
    *, tier: str = "free", flag: Optional[bool] = True, down: bool = False
) -> EntitlementClient:
    """A real `EntitlementClient` whose tier requirements come from the real contracts."""
    boom = ConnectionError("down") if down else None
    return EntitlementClient(
        flag_gate=_FlagGate(flag, raises=boom),
        license_gate=_LicenseGate(tier, raises=boom),
        tier_requirements={c.flag: c.min_tier for c in build_contracts()},
    )


class TestContractShape:
    def test_bulk_dsar_is_enterprise_and_namespaced_under_compliance(self) -> None:
        contract = _contracts_by_id()["compliance.bulk_dsar"]
        assert contract.flag == BULK_DSAR_FLAG
        assert contract.min_tier == "enterprise"
        assert contract.module == "compliance"
        assert contract.requires_scopes == frozenset({"compliance.dsar:admin"})

    def test_whitelabel_is_professional_and_namespaced_under_tenancy(self) -> None:
        contract = _contracts_by_id()["tenancy.whitelabel"]
        assert contract.flag == WHITELABEL_FLAG
        assert contract.min_tier == "professional"
        assert contract.module == "tenancy"
        assert contract.requires_scopes == frozenset({"tenancy.whitelabel:admin"})

    def test_self_service_dsar_is_not_a_gated_contract(self) -> None:
        """Statutory rights are never tier-gated -- only the admin/bulk layer is a Feature."""
        compliance_ids = {c.id for c in build_contracts() if c.module == "compliance"}
        assert compliance_ids == {
            "compliance.audit_logs",
            "compliance.external_kms",
            "compliance.bulk_dsar",
        }

    def test_each_has_exactly_one_default_app_inside_its_scopes(self) -> None:
        contracts = _contracts_by_id()
        for feature_id in ("compliance.bulk_dsar", "tenancy.whitelabel"):
            contract = contracts[feature_id]
            apps = [a for a in build_default_apps() if a.feature == contract.flag]
            assert len(apps) == 1
            assert apps[0].is_default is True
            assert set(apps[0].permissions) <= contract.requires_scopes


class TestTierResolution:
    @pytest.mark.parametrize(
        ("feature_id", "tier", "expected"),
        [
            ("tenancy.whitelabel", "free", False),
            ("tenancy.whitelabel", "community", False),
            ("tenancy.whitelabel", "professional", True),
            ("tenancy.whitelabel", "enterprise", True),
            ("compliance.bulk_dsar", "free", False),
            ("compliance.bulk_dsar", "community", False),
            ("compliance.bulk_dsar", "professional", False),
            ("compliance.bulk_dsar", "enterprise", True),
        ],
    )
    async def test_tier_resolution(self, feature_id: str, tier: str, expected: bool) -> None:
        flag = _contracts_by_id()[feature_id].flag

        assert await _client(tier=tier).evaluate(flag, tenant="acme") is expected

    @pytest.mark.parametrize("feature_id", ["tenancy.whitelabel", "compliance.bulk_dsar"])
    async def test_entitled_license_with_flag_off_or_unresolvable_is_denied(
        self, feature_id: str
    ) -> None:
        flag = _contracts_by_id()[feature_id].flag

        assert await _client(tier="enterprise", flag=False).evaluate(flag, tenant="acme") is False
        assert await _client(tier="enterprise", flag=None).evaluate(flag, tenant="acme") is False

    @pytest.mark.parametrize("feature_id", ["tenancy.whitelabel", "compliance.bulk_dsar"])
    async def test_outage_with_nothing_cached_fails_closed(self, feature_id: str) -> None:
        flag = _contracts_by_id()[feature_id].flag

        assert await _client(tier="enterprise", down=True).evaluate(flag, tenant="acme") is False
