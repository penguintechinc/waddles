"""
Static license-tier catalog -- the runtime floor for `EntitlementClient`.
==========================================================================

Every Feature contract (``<module>_module.features``) declares a ``min_tier``
and a PostHog ``flag``. Entitlement (`flask_core.entitlement`) is keyed on the
``flag``. Before this module, nothing carried ``min_tier`` across that gap:
``EntitlementClient.tier_requirements`` defaulted to ``{}``, so a flag absent
from it required only ``"free"`` and an Enterprise feature was gated by its
PostHog flag *alone* -- a Free tenant with the flag on got Enterprise. That is
a licensing-enforcement bypass (critical-rules.md Feature Flags & License
Tiers: "Licensed features additionally gate on license.penguintech.io
entitlement").

Why a static catalog instead of reading only the live ``FeatureRegistry``:
production processes do not reliably populate it. ``hub_api``'s image
installs ``flask_core`` alone -- the sibling ``*_module`` packages that hold
the contracts are not importable there -- and no startup path calls each
module's ``register_all()``. An enforcement table that is only as complete as
"whatever happened to register" fails open exactly where it matters, so the
non-free ``flag -> min_tier`` pairs are pinned here, in the package every
service already imports.

Single source of truth stays the contracts: the catalog is a *snapshot*, and
``tests/test_tier_enforcement.py::TestCatalogMatchesContracts`` fails if any
contract's ``min_tier`` drifts from it (in either direction -- a new
non-free contract with no catalog entry, or a catalog entry whose contract
changed or vanished). ``EntitlementClient.required_tier`` additionally
consults the live registry and takes the *stricter* of every source, so a
contract registered at runtime is enforced even before the catalog catches up
-- and no source can ever *weaken* another's requirement.

Only ``"professional"`` and ``"enterprise"`` entries appear: ``"free"`` is the
default for any flag not listed, matching "Free: no license-gated
functionality". Statutory rights (DSAR/erasure/Do-Not-Sell/consent
withdrawal) are deliberately absent and must stay absent -- they ship in
every tier (``tests/test_tier_enforcement.py::TestStatutoryRightsUngated``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .feature_contract import FeatureContract

#: Canonical tier names, lowest to highest. ``"community"`` is
#: ``penguin_licensing``'s name for the unlicensed floor and is normalised to
#: ``"free"`` (``normalize_tier``).
TIER_FREE = "free"
TIER_PROFESSIONAL = "professional"
TIER_ENTERPRISE = "enterprise"

#: Tier ranking. ``"community"`` and ``"free"`` are the same rung rather than
#: two vocabularies tracked in parallel.
TIER_LEVELS: Mapping[str, int] = MappingProxyType(
    {
        "community": 1,
        TIER_FREE: 1,
        TIER_PROFESSIONAL: 2,
        TIER_ENTERPRISE: 3,
    }
)

#: Rank assigned to a *required* tier nobody recognises. Higher than every real
#: tier, so no tenant can ever satisfy it -- a typo'd or corrupted requirement
#: denies the feature instead of silently making it free (the same
#: unknown-requirement-is-99 stance ``penguin_licensing.check_tier`` takes).
UNKNOWN_REQUIRED_LEVEL = 99

#: Tier names a requirement may legitimately be written as.
KNOWN_TIER_NAMES: frozenset[str] = frozenset(TIER_LEVELS)


def normalize_tier(tier: str) -> str:
    """Map penguin_licensing's ``"community"`` onto the canonical ``"free"`` rung."""
    normalized = tier.strip().lower()
    return TIER_FREE if normalized == "community" else normalized


def tier_level(tier: str) -> int:
    """Numeric rung for a *held* tier; an unknown tier ranks 0, below ``"free"`` (fail closed)."""
    return TIER_LEVELS.get(normalize_tier(tier), 0)


def required_level(tier: str) -> int:
    """Numeric rung for a *required* tier; unknown ranks above ``"enterprise"`` (fail closed).

    The held-tier and required-tier rankings are deliberately asymmetric: an
    unrecognised tier a tenant *holds* must grant nothing, and an
    unrecognised tier a feature *requires* must be unsatisfiable. Using one
    helper for both would make a typo'd requirement rank 0 and pass for everyone.
    """
    return TIER_LEVELS.get(normalize_tier(tier), UNKNOWN_REQUIRED_LEVEL)

#: ``flag -> min_tier`` for every non-free Feature contract (22 as of the
#: eight-module catalog). Keep alphabetical by flag within each tier so a
#: drift-test failure diff is easy to read.
_FEATURE_MIN_TIERS: dict[str, str] = {
    # -- professional ------------------------------------------------------
    "waddles.analytics.bad_actor_detection": TIER_PROFESSIONAL,
    "waddles.analytics.community_health": TIER_PROFESSIONAL,
    "waddles.analytics.engagement_funnels": TIER_PROFESSIONAL,
    "waddles.analytics.retention_cohorts": TIER_PROFESSIONAL,
    "waddles.analytics.user_journey": TIER_PROFESSIONAL,
    "waddles.auth.sso_google": TIER_PROFESSIONAL,
    "waddles.community.inventory": TIER_PROFESSIONAL,
    "waddles.community.loyalty": TIER_PROFESSIONAL,
    "waddles.community.raffles": TIER_PROFESSIONAL,
    "waddles.community.virtual_stages": TIER_PROFESSIONAL,
    "waddles.event.ticketing": TIER_PROFESSIONAL,
    "waddles.marketing.publishing": TIER_PROFESSIONAL,
    "waddles.marketing.scheduling": TIER_PROFESSIONAL,
    "waddles.streaming.broadcast": TIER_PROFESSIONAL,
    "waddles.video_proxy.premium_limits": TIER_PROFESSIONAL,
    # -- enterprise --------------------------------------------------------
    "waddles.analytics.advanced": TIER_ENTERPRISE,
    "waddles.auth.sso_saml": TIER_ENTERPRISE,
    "waddles.compliance.audit_logs": TIER_ENTERPRISE,
    "waddles.compliance.external_kms": TIER_ENTERPRISE,
    "waddles.integrations.waddleai": TIER_ENTERPRISE,
    "waddles.social.welcome_ai": TIER_ENTERPRISE,
    "waddles.tenancy.multi_tenant": TIER_ENTERPRISE,
}

#: Read-only view -- the catalog is process-wide and must not be mutated by a
#: caller (a mutation would silently weaken enforcement for every client).
FEATURE_MIN_TIERS: Mapping[str, str] = MappingProxyType(_FEATURE_MIN_TIERS)


def tier_requirements_from_contracts(contracts: Iterable[FeatureContract]) -> dict[str, str]:
    """Derive a ``flag -> min_tier`` map from validated Feature contracts.

    Includes every contract (``"free"`` ones too) so the result is a complete
    picture of what the contracts declare -- used by the drift test, and by
    callers that want to hand an explicit ``tier_requirements`` to a bespoke
    `EntitlementClient`. Later contracts for the same flag do not weaken an
    earlier one: the stricter tier is kept.
    """
    derived: dict[str, str] = {}
    for contract in contracts:
        current = derived.get(contract.flag)
        if current is None or required_level(contract.min_tier) > required_level(current):
            derived[contract.flag] = contract.min_tier
    return derived
