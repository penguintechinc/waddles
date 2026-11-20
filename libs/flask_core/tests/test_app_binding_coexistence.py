"""
App binding coexistence tests
================================

Covers Phase C2 of docs/plans/2026-08-31-app-bundle-sdk-design.md §5.2
(``resolve_app`` -> ``resolve_apps``) and §7.3 (symmetric conflict
detection). Deliberately its own file, not folded into
``test_app_framework.py``'s existing ``TestResolveAppBindingLadder`` class
(which covers ``resolve_app``'s narrowest-wins ladder, unchanged) or into
``conftest.py`` (no shared fixtures added there) -- C2 is an additive
capability, not a rewrite of C1's binding-ladder coverage.

Every behavioral claim here (union not override, dedupe, fallback-only-
when-empty, symmetric conflict, no false positive, disabled rows excluded)
was verified fail-first against a deliberately broken ``app_binding.py``
before landing; see the PR description for the mutation log.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from flask_core.app_binding import (
    AppInstallation,
    BindingError,
    detect_conflict,
    resolve_apps,
)
from flask_core.app_manifest import AppManifest, parse_manifest
from flask_core.app_registry import AppRegistry

FEATURE = "giveaway"

_BASE_MANIFEST: Dict[str, object] = {
    "app_id": f"penguintechinc.waddles.giveaway-classic",
    "name": "Giveaway (Classic)",
    "version": "1.0.0",
    "feature": FEATURE,
    "module": "bot",
    "provider": "builtin",
    "surfaces": ["ingest", "process", "action"],
    "permissions": ["bot.command:write"],
    "is_default": False,
}


def app(
    suffix: str,
    *,
    is_default: bool = False,
    compatible_with: Tuple[str, ...] = (),
    incompatible_with: Tuple[str, ...] = (),
) -> AppManifest:
    """Build a validated :class:`AppManifest` under ``FEATURE`` for ``suffix``."""
    data = dict(_BASE_MANIFEST)
    data["app_id"] = f"penguintechinc.waddles.{suffix}"
    data["is_default"] = is_default
    data["compatible_with"] = list(compatible_with)
    data["incompatible_with"] = list(incompatible_with)
    return parse_manifest(data)


def registry_with(*manifests: AppManifest) -> AppRegistry:
    """A fresh, isolated :class:`AppRegistry` holding exactly ``manifests``."""
    registry = AppRegistry()
    for manifest in manifests:
        registry.register(manifest)
    return registry


class FakeInstallations:
    """In-memory :class:`InstallationLookup` -- a local copy (this file's own
    test double, not imported from ``test_app_framework.py``) so C2's tests
    have no cross-file coupling to C1's."""

    def __init__(self, rows: List[AppInstallation]) -> None:
        self._rows = rows

    async def find(
        self, feature: str, *, tenant: str, community: Optional[int]
    ) -> Sequence[AppInstallation]:
        return [
            row
            for row in self._rows
            if row.feature == feature
            and row.tenant_id == tenant
            and (row.community_id is None or row.community_id == community)
        ]


# ---------------------------------------------------------------------------
# resolve_apps -- coexistence set (union, not override)
# ---------------------------------------------------------------------------
class TestResolveAppsUnion:
    async def test_union_of_community_and_tenant_wide_rows(self) -> None:
        classic = app("giveaway-classic", is_default=True)
        raffle = app("giveaway-raffle")
        milestone = app("giveaway-milestone")
        sub_only = app("giveaway-sub-only")
        registry = registry_with(classic, raffle, milestone, sub_only)
        installations = FakeInstallations(
            [
                AppInstallation(
                    tenant_id="acme", community_id=None, feature=FEATURE, app_id=classic.app_id
                ),
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=raffle.app_id
                ),
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=milestone.app_id
                ),
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=sub_only.app_id
                ),
            ]
        )
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert {m.app_id for m in result} == {
            classic.app_id,
            raffle.app_id,
            milestone.app_id,
            sub_only.app_id,
        }
        assert len(result) == 4

    async def test_tenant_wide_row_is_not_suppressed_by_a_community_row(self) -> None:
        """Under the old ladder, a community row would narrow out the tenant-wide
        one; resolve_apps keeps both -- proves union, not override."""
        classic = app("giveaway-classic", is_default=True)
        raffle = app("giveaway-raffle")
        registry = registry_with(classic, raffle)
        installations = FakeInstallations(
            [
                AppInstallation(
                    tenant_id="acme", community_id=None, feature=FEATURE, app_id=classic.app_id
                ),
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=raffle.app_id
                ),
            ]
        )
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert {m.app_id for m in result} == {classic.app_id, raffle.app_id}

    async def test_dedupes_an_app_bound_at_both_scopes(self) -> None:
        classic = app("giveaway-classic", is_default=True)
        registry = registry_with(classic)
        installations = FakeInstallations(
            [
                AppInstallation(
                    tenant_id="acme", community_id=None, feature=FEATURE, app_id=classic.app_id
                ),
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=classic.app_id
                ),
            ]
        )
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert len(result) == 1
        assert result[0].app_id == classic.app_id

    async def test_disabled_rows_are_excluded_from_the_union(self) -> None:
        classic = app("giveaway-classic", is_default=True)
        raffle = app("giveaway-raffle")
        registry = registry_with(classic, raffle)
        installations = FakeInstallations(
            [
                AppInstallation(
                    tenant_id="acme",
                    community_id=42,
                    feature=FEATURE,
                    app_id=raffle.app_id,
                    enabled=False,
                ),
            ]
        )
        # the only row is disabled -> union is empty -> falls back to default
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert len(result) == 1
        assert result[0].app_id == classic.app_id


class TestResolveAppsFallback:
    async def test_falls_back_to_default_when_union_is_empty(self) -> None:
        classic = app("giveaway-classic", is_default=True)
        registry = registry_with(classic)
        installations = FakeInstallations([])
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert len(result) == 1
        assert result[0].app_id == classic.app_id
        assert result[0].is_default is True

    async def test_raises_binding_error_when_union_empty_and_no_default(self) -> None:
        raffle = app("giveaway-raffle")  # not registered as default
        registry = registry_with(raffle)
        installations = FakeInstallations([])
        with pytest.raises(BindingError):
            await resolve_apps(
                FEATURE, tenant="acme", community=42, installations=installations, registry=registry
            )

    async def test_non_empty_union_is_never_topped_up_with_the_default(self) -> None:
        classic = app("giveaway-classic", is_default=True)
        raffle = app("giveaway-raffle")
        registry = registry_with(classic, raffle)
        installations = FakeInstallations(
            [
                AppInstallation(
                    tenant_id="acme", community_id=42, feature=FEATURE, app_id=raffle.app_id
                ),
            ]
        )
        result = await resolve_apps(
            FEATURE, tenant="acme", community=42, installations=installations, registry=registry
        )
        assert {m.app_id for m in result} == {raffle.app_id}


# ---------------------------------------------------------------------------
# detect_conflict -- symmetric coexistence check (spec §7.3)
# ---------------------------------------------------------------------------
class TestDetectConflict:
    def test_symmetric_conflict_declared_by_the_active_app(self) -> None:
        """official-eventsub (active) declares legacy-irc-bridge incompatible;
        the candidate (legacy-irc-bridge) declares nothing itself."""
        eventsub = app("official-eventsub", incompatible_with=(f"penguintechinc.waddles.legacy-irc-bridge",))
        irc = app("legacy-irc-bridge")
        conflict = detect_conflict(irc, [eventsub])
        assert conflict == eventsub.app_id

    def test_symmetric_conflict_declared_by_the_candidate(self) -> None:
        """legacy-irc-bridge (active) declares nothing; the candidate
        (official-eventsub) is the one declaring the incompatibility."""
        eventsub = app("official-eventsub", incompatible_with=(f"penguintechinc.waddles.legacy-irc-bridge",))
        irc = app("legacy-irc-bridge")
        conflict = detect_conflict(eventsub, [irc])
        assert conflict == irc.app_id

    def test_no_false_positive_on_independent_bundles(self) -> None:
        classic = app("giveaway-classic")
        raffle = app("giveaway-raffle")
        assert detect_conflict(classic, [raffle]) is None

    def test_no_false_positive_from_compatible_with_alone(self) -> None:
        """compatible_with is carried for future use and is not consulted by
        detect_conflict -- only incompatible_with drives a conflict."""
        classic = app("giveaway-classic", compatible_with=(f"penguintechinc.waddles.giveaway-raffle",))
        raffle = app("giveaway-raffle")
        assert detect_conflict(classic, [raffle]) is None

    def test_ignores_disabled_activations(self) -> None:
        """Mirrors the real call site: the caller (C3's activation write)
        filters `active` down to currently-enabled app_activations rows
        before calling detect_conflict. A disabled, otherwise-incompatible
        app excluded from that filtered set must not surface as a conflict."""
        eventsub = app("official-eventsub", incompatible_with=(f"penguintechinc.waddles.legacy-irc-bridge",))
        irc = app("legacy-irc-bridge")
        activation_rows: List[Tuple[AppManifest, bool]] = [(eventsub, False)]  # disabled
        active = [manifest for manifest, enabled in activation_rows if enabled]
        assert active == []
        assert detect_conflict(irc, active) is None

    def test_candidate_never_conflicts_with_its_own_active_row(self) -> None:
        eventsub = app("official-eventsub", incompatible_with=(f"penguintechinc.waddles.official-eventsub",))
        assert detect_conflict(eventsub, [eventsub]) is None
