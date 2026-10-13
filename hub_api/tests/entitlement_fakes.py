"""Fake PostHog/license gates + a process-wide `EntitlementClient` installer for tier tests.

`flask_core.feature_flags.feature_enabled` resolves through
`flask_core.entitlement.get_entitlement_client()`'s module-level singleton.
Tests that want the REAL two-gate logic (not a mocked `feature_enabled`) --
flag AND license tier, tier requirements keyed on the contract's flag,
outage fail-closed -- swap that singleton for a client built over these
fakes via `install_entitlement_client()`. No live PostHog/license server.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from flask_core import entitlement as entitlement_module
from flask_core.entitlement import EntitlementClient


class FakeFlagGate:
    """PostHog stand-in: fixed result (`None` = unresolvable) or a raised error; counts calls."""

    def __init__(self, result: bool | None = True, raises: Exception | None = None) -> None:
        """Configure the fixed `result` (or the exception to raise) for every evaluation."""
        self.result = result
        self.raises = raises
        self.calls: list[str] = []

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Mapping[str, str] | None = None
    ) -> bool | None:
        self.calls.append(flag_key)
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeLicenseGate:
    """License-server stand-in: fixed tier or a raised error; counts calls."""

    def __init__(self, tier: str = "free", raises: Exception | None = None) -> None:
        """Configure the fixed license `tier` (or the exception to raise) for every lookup."""
        self.tier = tier
        self.raises = raises
        self.calls = 0

    def resolve_tier(self) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.tier


def install_entitlement_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag_gate: FakeFlagGate,
    license_gate: FakeLicenseGate,
    tier_requirements: Mapping[str, str] | None = None,
) -> EntitlementClient:
    """Make `feature_enabled()` evaluate against fakes for the duration of the test."""
    client = EntitlementClient(
        flag_gate=flag_gate,
        license_gate=license_gate,
        tier_requirements=dict(tier_requirements or {}),
    )
    monkeypatch.setattr(entitlement_module, "_default_client", client)
    return client
