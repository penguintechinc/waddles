"""The external-KMS entitlement gate over a REAL EntitlementClient (fake flag/licence seams)."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pytest
from flask_core.entitlement import EntitlementClient
from flask_core.feature_registry import FeatureRegistry

from services.envelope.gate import FEATURE_EXTERNAL_KMS, REQUIRED_SCOPE, ExternalKmsGate

PLAIN_HOST = "app.example.com"


class FakeFlagGate:
    """PostHog stand-in."""

    def __init__(self, result: bool | None = True, raises: Exception | None = None) -> None:
        """Fixed flag result, or an error to raise."""
        self.result, self.raises = result, raises

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Mapping[str, str] | None = None
    ) -> bool | None:
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeLicenseGate:
    """Licence-server stand-in."""

    def __init__(self, tier: str = "free", raises: Exception | None = None) -> None:
        """Fixed tier, or an error to raise."""
        self.tier, self.raises = tier, raises

    def resolve_tier(self) -> str:
        if self.raises is not None:
            raise self.raises
        return self.tier


def gate_for(
    *, tier: str = "enterprise", flag: bool | None = True, **errors: Exception | None
) -> ExternalKmsGate:
    """A gate over a real client whose registry is isolated (so only the static catalog applies)."""
    client = EntitlementClient(
        flag_gate=FakeFlagGate(flag, errors.get("flag_raises")),
        license_gate=FakeLicenseGate(tier, errors.get("tier_raises")),
        feature_registry=FeatureRegistry(),
    )
    return ExternalKmsGate(client)


def test_the_gate_targets_the_enterprise_contract() -> None:
    assert FEATURE_EXTERNAL_KMS == "waddles.compliance.external_kms"
    assert REQUIRED_SCOPE == "compliance.kms:admin"


@pytest.mark.parametrize(
    ("tier", "flag", "expected"),
    [
        ("enterprise", True, True),
        ("professional", True, False),  # paid, but not the Enterprise upsell
        ("free", True, False),  # the flag ALONE must never unlock it
        ("community", True, False),
        ("enterprise", False, False),  # flag off wins even for Enterprise
        ("enterprise", None, False),  # unresolvable flag -> default OFF
    ],
)
async def test_flag_and_enterprise_tier_must_both_hold(tier, flag, expected) -> None:
    gate = gate_for(tier=tier, flag=flag)
    assert await gate.is_entitled("acme", request_host=PLAIN_HOST) is expected


async def test_an_outage_never_newly_entitles_a_tenant() -> None:
    assert not await gate_for(tier_raises=RuntimeError("licence server down")).is_entitled(
        "acme", request_host=PLAIN_HOST
    )
    assert not await gate_for(flag_raises=RuntimeError("posthog down")).is_entitled(
        "acme", request_host=PLAIN_HOST
    )


async def test_an_empty_tenant_is_denied_without_asking_anyone(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="services.envelope.gate"):
        assert not await gate_for().is_entitled("", request_host=PLAIN_HOST)
    assert any(r.getMessage() == "envelope.gate.denied_no_tenant" for r in caplog.records)


async def test_a_catalog_regression_below_enterprise_fails_closed(caplog) -> None:
    """If the tier table ever stops pinning this flag to Enterprise, deny rather than trust it."""

    class WeakClient:
        def required_tier(self, flag_key: str) -> str:
            return "free"

        async def evaluate(self, *args: object, **kwargs: object) -> bool:
            raise AssertionError("must not be consulted when the tier requirement is weakened")

    with caplog.at_level(logging.ERROR, logger="services.envelope.gate"):
        gate = ExternalKmsGate(WeakClient())  # type: ignore[arg-type]
        assert not await gate.is_entitled("acme", request_host=PLAIN_HOST)
    assert any("below_enterprise" in r.getMessage() for r in caplog.records)


async def test_the_process_wide_client_is_used_when_none_is_injected(monkeypatch) -> None:
    import services.envelope.gate as gate_module

    client = EntitlementClient(
        flag_gate=FakeFlagGate(True),
        license_gate=FakeLicenseGate("enterprise"),
        feature_registry=FeatureRegistry(),
    )
    monkeypatch.setattr(gate_module, "get_entitlement_client", lambda: client)
    assert await ExternalKmsGate().is_entitled("acme", request_host=PLAIN_HOST)


async def test_a_hardcoded_bypass_domain_skips_only_the_tier_check() -> None:
    """*.penguintech.cloud unlocks licensed features by design -- never past an OFF flag."""
    on = gate_for(tier="free", flag=True)
    assert await on.is_entitled("acme", request_host="beta.penguintech.cloud")
    off = gate_for(tier="enterprise", flag=False)
    assert not await off.is_entitled("acme", request_host="beta.penguintech.cloud")


def test_current_request_host_is_none_outside_a_request() -> None:
    from services.envelope.gate import current_request_host

    assert current_request_host() is None


async def test_current_request_host_reads_the_active_request() -> None:
    from quart import Quart

    from services.envelope.gate import current_request_host

    app = Quart(__name__)
    async with app.test_request_context("/", headers={"Host": "acme.example.com"}):
        assert current_request_host() == "acme.example.com"
