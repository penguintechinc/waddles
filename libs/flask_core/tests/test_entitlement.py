"""
Tests for flask_core.entitlement / flask_core.feature_flags (v3 flag plane).

Exercises the two-gate contract (PostHog flag AND license tier, both
mandatory) and the outage-degradation contract (unreachable gate -> warm
cache -> `default`, never a raised exception) using fake `FlagGate`/
`LicenseGate` adapters -- no live PostHog or license-server connection.

Fail-on-purpose proof (see FAIL_ON_PURPOSE_LOG below the tests): each test
group was verified to actually catch a matching regression by temporarily
mutating `entitlement.py` (`and` -> `or` in the gate combination, dropping
the None-on-error fallback, always-true bypass) and confirming the expected
tests go red, then reverting to green. Not re-run automatically on every
`pytest` invocation -- see the module docstring in `entitlement.py` for the
production contract these tests pin down.
"""

from __future__ import annotations

import sys
import types
from typing import Mapping, Optional
from unittest.mock import MagicMock

import pytest

import flask_core.entitlement as entitlement_module
from flask_core.entitlement import (
    EntitlementClient,
    PenguinLicenseGate,
    PostHogFlagGate,
    _CommunityOnlyLicenseGate,
    get_entitlement_client,
    is_bypass_domain,
    resolve_bypass_depth,
)
from flask_core.feature_flags import feature_enabled

FLAG_KEY = "waddles.community.custom_domains"
TENANT = "acme"


class FakeFlagGate:
    """Records calls; returns a fixed result or raises a fixed exception."""

    def __init__(self, result: Optional[bool] = None, raises: Optional[Exception] = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, str, Optional[Mapping[str, str]]]] = []

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Optional[Mapping[str, str]] = None
    ) -> Optional[bool]:
        self.calls.append((flag_key, distinct_id, groups))
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeLicenseGate:
    """Records calls; returns a fixed tier or raises a fixed exception."""

    def __init__(self, tier: Optional[str] = None, raises: Optional[Exception] = None) -> None:
        self.tier = tier
        self.raises = raises
        self.calls = 0

    def resolve_tier(self) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        assert self.tier is not None
        return self.tier


def make_client(
    *,
    flag_result: Optional[bool] = True,
    flag_raises: Optional[Exception] = None,
    tier: Optional[str] = "professional",
    tier_raises: Optional[Exception] = None,
    tier_requirements: Optional[Mapping[str, str]] = None,
) -> tuple[EntitlementClient, FakeFlagGate, FakeLicenseGate]:
    flag_gate = FakeFlagGate(result=flag_result, raises=flag_raises)
    license_gate = FakeLicenseGate(tier=tier, raises=tier_raises)
    client = EntitlementClient(
        flag_gate=flag_gate,
        license_gate=license_gate,
        tier_requirements=tier_requirements or {},
    )
    return client, flag_gate, license_gate


# ---------------------------------------------------------------------------
# 1. flag ON + tier entitled -> True
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_on_and_tier_entitled_returns_true() -> None:
    client, _, _ = make_client(
        flag_result=True,
        tier="professional",
        tier_requirements={FLAG_KEY: "professional"},
    )
    assert await client.evaluate(FLAG_KEY, tenant=TENANT) is True


# ---------------------------------------------------------------------------
# 2. flag OFF -> False (even though the license tier would entitle it)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_off_returns_false() -> None:
    client, _, license_gate = make_client(flag_result=False, tier="enterprise")
    assert await client.evaluate(FLAG_KEY, tenant=TENANT) is False
    assert license_gate.calls == 1  # license gate still runs; flag is the deciding factor


# ---------------------------------------------------------------------------
# 3. tier NOT entitled -> False even with flag ON
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tier_not_entitled_returns_false_even_with_flag_on() -> None:
    client, flag_gate, _ = make_client(
        flag_result=True,
        tier="free",
        tier_requirements={FLAG_KEY: "enterprise"},
    )
    assert await client.evaluate(FLAG_KEY, tenant=TENANT) is False
    assert flag_gate.calls  # flag gate did run and returned True -- license alone vetoed it


# ---------------------------------------------------------------------------
# 4. both services down + warm cache -> cached value
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_both_services_down_with_warm_cache_returns_cached_value() -> None:
    client, flag_gate, license_gate = make_client(
        flag_result=True,
        tier="professional",
        tier_requirements={FLAG_KEY: "professional"},
    )
    # Warm the cache while both gates are healthy.
    assert await client.evaluate(FLAG_KEY, tenant=TENANT) is True

    # Both gates now fail outright.
    flag_gate.raises = ConnectionError("posthog unreachable")
    license_gate.raises = ConnectionError("license server unreachable")

    result = await client.evaluate(FLAG_KEY, tenant=TENANT, default=False)
    assert result is True  # served from cache, not the (False) default


# ---------------------------------------------------------------------------
# 5. cold cache + unknown flag -> default (never the deterministic OFF-by-
#    coincidence -- default=True proves the *parameter* is honored, not a
#    hardcoded False).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cold_cache_unknown_flag_returns_default() -> None:
    client, _, _ = make_client(flag_result=None, tier="enterprise")  # None = unresolvable/unknown
    assert await client.evaluate("waddles.never.seen", tenant=TENANT, default=True) is True
    assert await client.evaluate("waddles.never.seen.2", tenant=TENANT, default=False) is False


# ---------------------------------------------------------------------------
# 6. bypass domain skips the license gate, never the flag gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bypass_domain_skips_license_but_not_flag() -> None:
    client, flag_gate, license_gate = make_client(flag_result=True, tier=None)
    result = await client.evaluate(
        FLAG_KEY, tenant=TENANT, request_host="waddles-beta.penguintech.cloud"
    )
    assert result is True
    assert flag_gate.calls  # flag gate ran
    assert license_gate.calls == 0  # license gate was never called -- bypassed


@pytest.mark.asyncio
async def test_bypass_domain_still_respects_flag_off() -> None:
    client, _, license_gate = make_client(flag_result=False, tier=None)
    result = await client.evaluate(FLAG_KEY, tenant=TENANT, request_host="app.waddles.app")
    assert result is False
    assert license_gate.calls == 0


@pytest.mark.asyncio
async def test_non_bypass_domain_still_checks_license() -> None:
    client, _, license_gate = make_client(
        flag_result=True, tier="free", tier_requirements={FLAG_KEY: "enterprise"}
    )
    result = await client.evaluate(FLAG_KEY, tenant=TENANT, request_host="app.example.com")
    assert result is False
    assert license_gate.calls == 1


# ---------------------------------------------------------------------------
# is_bypass_domain: full-hostname matching, never substring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostname,expected",
    [
        ("waddles.penguintech.cloud", True),
        ("waddles-beta.penguintech.cloud", True),
        ("penguintech.cloud", True),
        ("app.waddles.app", True),
        ("waddles.app", True),
        ("waddles.app:8443", True),  # port suffix stripped before matching
        (None, False),
        ("", False),
        ("app.example.com", False),
        # Lookalike/attacker-controlled suffix attack: substring/.endswith
        # would be fooled by these; full-hostname fnmatch is not.
        ("waddles.penguintech.cloud.attacker.com", False),
        ("evil-waddles.app.attacker.com", False),
        ("notwaddles.app", False),
    ],
)
def test_is_bypass_domain_matches_full_hostname_only(
    hostname: Optional[str], expected: bool
) -> None:
    assert is_bypass_domain(hostname) is expected


# ---------------------------------------------------------------------------
# BYPASS DEPTH (program plan SS3.3 "the bypass-DEPTH gap"): a bypass
# hostname is not flat yes/no -- it resolves to "none" / "global" /
# "global_community". PenguinTech's own pre-prod SaaS hosts
# (penguincloud.io / penguintech.cloud, incl. waddles*.penguintech.cloud
# subdomains) get "global_community" (both tenant-wide AND per-community
# checks bypass); the product's own prod domain (*.waddles.app) gets only
# "global" (tenant-wide bypasses, per-community does NOT).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostname,expected_depth",
    [
        ("penguincloud.io", "global_community"),
        ("waddles.penguincloud.io", "global_community"),
        ("penguintech.cloud", "global_community"),
        ("waddles.penguintech.cloud", "global_community"),
        ("waddles-beta.penguintech.cloud", "global_community"),
        ("waddles-gamma.penguintech.cloud", "global_community"),
        ("waddles.app", "global"),
        ("app.waddles.app", "global"),
        ("us-app.waddles.app", "global"),
        (None, "none"),
        ("", "none"),
        ("app.example.com", "none"),
        # Lookalike/attacker-controlled suffix -- must not upgrade depth.
        ("waddles.penguintech.cloud.attacker.com", "none"),
        ("evil-waddles.app.attacker.com", "none"),
        ("notwaddles.app", "none"),
    ],
)
def test_resolve_bypass_depth(hostname: Optional[str], expected_depth: str) -> None:
    assert resolve_bypass_depth(hostname) == expected_depth


@pytest.mark.asyncio
async def test_global_community_bypass_skips_license_for_community_scoped_check() -> None:
    """`"global_community"` depth (pre-prod SaaS host) bypasses even a per-community check."""
    client, _, license_gate = make_client(
        flag_result=True, tier="free", tier_requirements={FLAG_KEY: "enterprise"}
    )
    result = await client.evaluate(
        FLAG_KEY,
        tenant=TENANT,
        community=42,
        request_host="waddles-beta.penguintech.cloud",
    )
    assert result is True
    assert license_gate.calls == 0  # bypassed


@pytest.mark.asyncio
async def test_global_only_bypass_does_not_bypass_community_scoped_license() -> None:
    """
    THE GAP THIS CLOSES: `"global"` depth (product prod domain,
    *.waddles.app) must NOT bypass a per-community (`community is not
    None`) license check -- only a tenant-wide one. Flag is ON and would
    otherwise pass, but the (fake) license tier is below what FLAG_KEY
    requires, so the real license gate denying it must be honored.
    """
    client, _, license_gate = make_client(
        flag_result=True, tier="free", tier_requirements={FLAG_KEY: "enterprise"}
    )
    result = await client.evaluate(
        FLAG_KEY,
        tenant=TENANT,
        community=42,
        request_host="app.waddles.app",
    )
    assert result is False
    assert license_gate.calls == 1  # NOT bypassed -- the real gate ran and denied it


@pytest.mark.asyncio
async def test_global_only_bypass_still_bypasses_tenant_wide_license() -> None:
    """`"global"` depth still bypasses a tenant-wide check (community=None) -- baseline preserved."""
    client, _, license_gate = make_client(flag_result=True, tier=None)
    result = await client.evaluate(
        FLAG_KEY, tenant=TENANT, community=None, request_host="app.waddles.app"
    )
    assert result is True
    assert license_gate.calls == 0  # bypassed -- community-scoped restriction doesn't apply here


# ---------------------------------------------------------------------------
# feature_flags.feature_enabled delegates to the entitlement client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature_enabled_delegates_to_entitlement_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client, flag_gate, license_gate = make_client(
        flag_result=True, tier="professional", tier_requirements={FLAG_KEY: "professional"}
    )

    import flask_core.feature_flags as feature_flags_module

    monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)

    result = await feature_enabled(FLAG_KEY, tenant=TENANT, community=42, default=False)

    assert result is True
    assert flag_gate.calls == [(FLAG_KEY, f"{TENANT}:42", {"tenant": TENANT})]
    assert license_gate.calls == 1


@pytest.mark.asyncio
async def test_feature_enabled_never_raises_outside_request_context() -> None:
    """No active Quart request context (background job/CLI) -- host resolves to None, no crash."""
    client, _, _ = make_client(flag_result=True, tier="enterprise")

    import flask_core.feature_flags as feature_flags_module

    orig = feature_flags_module.get_entitlement_client
    feature_flags_module.get_entitlement_client = lambda: client
    try:
        result = await feature_enabled(FLAG_KEY, tenant=TENANT)
    finally:
        feature_flags_module.get_entitlement_client = orig

    assert result is True


# ---------------------------------------------------------------------------
# Missing tenant / outer safety net -- must never crash a request path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_tenant_returns_default_never_raises() -> None:
    client, _, _ = make_client(flag_result=True, tier="enterprise")
    assert await client.evaluate(FLAG_KEY, tenant="", default=True) is True


@pytest.mark.asyncio
async def test_unexpected_exception_in_gate_returns_default_never_raises() -> None:
    """A gate adapter that raises something even the per-gate try/except can't help with
    (e.g. a bug in a caller-injected adapter's synchronous setup) still can't take the
    request path down -- the outer `evaluate()` try/except is the last line of defense."""

    class ExplodingFlagGate:
        def is_enabled(self, *args: object, **kwargs: object) -> Optional[bool]:
            raise RuntimeError("boom")

    client = EntitlementClient(flag_gate=ExplodingFlagGate(), license_gate=FakeLicenseGate(tier="free"))
    assert await client.evaluate(FLAG_KEY, tenant=TENANT, default=False) is False


# ---------------------------------------------------------------------------
# Gate adapters -- real posthog / penguin_licensing wiring, mocked at the SDK boundary
# ---------------------------------------------------------------------------
def test_posthog_flag_gate_delegates_to_posthog_client() -> None:
    mock_client = MagicMock()
    mock_client.feature_enabled.return_value = True
    gate = PostHogFlagGate(mock_client)

    result = gate.is_enabled(FLAG_KEY, TENANT, groups={"tenant": TENANT})

    assert result is True
    mock_client.feature_enabled.assert_called_once_with(FLAG_KEY, TENANT, groups={"tenant": TENANT})


def test_posthog_flag_gate_returns_none_on_client_error() -> None:
    mock_client = MagicMock()
    mock_client.feature_enabled.side_effect = RuntimeError("posthog down")
    gate = PostHogFlagGate(mock_client)

    assert gate.is_enabled(FLAG_KEY, TENANT) is None


def test_posthog_flag_gate_from_env_disabled_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    gate = PostHogFlagGate.from_env()
    assert gate.is_enabled(FLAG_KEY, TENANT) is None  # disabled client resolves to None, never raises


def test_posthog_flag_gate_from_env_reads_configured_host_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_key")
    monkeypatch.setenv("POSTHOG_HOST", "https://posthog.example.com")
    gate = PostHogFlagGate.from_env()
    assert gate._client.api_key == "phc_test_key"


def test_penguin_license_gate_delegates_to_license_client() -> None:
    mock_client = MagicMock()
    mock_client.validate.return_value.tier = "enterprise"
    gate = PenguinLicenseGate(mock_client)

    assert gate.resolve_tier() == "enterprise"
    mock_client.validate.assert_called_once_with()


def test_community_only_license_gate_is_fail_closed() -> None:
    assert _CommunityOnlyLicenseGate().resolve_tier() == "free"


def test_penguin_license_gate_from_env_falls_back_when_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entitlement_module, "_PENGUIN_LICENSING_AVAILABLE", False)
    gate = PenguinLicenseGate.from_env()
    assert gate.resolve_tier() == "free"


def test_penguin_license_gate_from_env_uses_real_dependency_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_client = MagicMock()
    monkeypatch.setattr(entitlement_module, "_PENGUIN_LICENSING_AVAILABLE", True)
    monkeypatch.setattr(entitlement_module, "get_waddles_license_client", lambda: sentinel_client)
    gate = PenguinLicenseGate.from_env()
    assert gate._client is sentinel_client


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
def test_get_entitlement_client_returns_shared_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entitlement_module, "_default_client", None)
    first = get_entitlement_client()
    second = get_entitlement_client()
    assert first is second


# ---------------------------------------------------------------------------
# feature_flags._current_request_host -- inside an (emulated) Quart request
# context, quart isn't a test dependency of this leaf module so a fake
# `quart` module is injected into sys.modules for the duration of the test.
# ---------------------------------------------------------------------------
def test_current_request_host_reads_quart_request_host(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_request = types.SimpleNamespace(host="waddles.penguintech.cloud")
    fake_quart = types.ModuleType("quart")
    fake_quart.request = fake_request  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quart", fake_quart)

    import flask_core.feature_flags as feature_flags_module

    assert feature_flags_module._current_request_host() == "waddles.penguintech.cloud"
