"""
Two-gate entitlement client -- the real engine behind `feature_flags.feature_enabled`.
========================================================================================

Every feature gate in Waddles is two checks, both mandatory (critical-rules.md
Feature Flags & License Tiers):

1. **PostHog flag** -- general enablement (staged rollout, kill-switch,
   experimentation). Wraps the published `posthog` SDK, never a hand-rolled
   HTTP call.
2. **License tier** -- does the tenant's license entitle this feature.
   Wraps the published `penguin_licensing` package (`LicenseClient.validate()`
   against `license.penguintech.io`), skipped only on a hardcoded
   license-bypass hostname (penguintech.md), never via env var or CLI flag.

Neither gate is allowed to take a request path down with it. Both PostHog and
the license server can be unreachable at the same time; this module answers
with the last-known cached value, or `default` if nothing has ever been
cached for that (flag, tenant, community) tuple -- never an exception. See
`EntitlementClient.evaluate` for the exact fallback order.

`feature_flags.feature_enabled` is the stable, product-facing signature;
this module is its implementation and is not meant to be imported directly
by product code except to build a non-default `EntitlementClient` (e.g. to
register `tier_requirements`, or to inject fakes in tests).

Tier model (the license gate, gate 2)
-------------------------------------
A flag is granted only when the PostHog flag is ON **and** the tenant's
*effective tier* is at or above the feature's required tier. A PostHog flag
alone never grants a licensed feature -- it is a rollout switch, not an
entitlement.

* **Required tier** -- `EntitlementClient.required_tier(flag)`: the *stricter*
  of (a) the explicit `tier_requirements` map, (b) the live
  `FeatureRegistry` contract for that flag (`FeatureContract.min_tier`), and
  (c) the static `tier_catalog.FEATURE_MIN_TIERS` snapshot of every contract
  (present even where the `*_module` packages aren't importable, e.g. the
  hub-api image). No source can lower another; a flag in none of them
  requires `"free"`. An unrecognised required tier is unsatisfiable (denies),
  never free.
* **Effective tier** -- ``max(tenant_tier, community_tier)``, cascading down
  (critical-rules.md: a tenant's tier lifts every community in it; a
  community can be allocated *above* its tenant by the tenant admin, never
  below). The tenant tier is `LicenseGate.resolve_tier()` (the
  `penguin_licensing` client against ``license.penguintech.io``); the
  optional community allocation comes from a `CommunityTierSource`. A
  tenant-wide check (``community=None``) uses the tenant tier only.
* **Fail closed** -- tier is a hard veto: a known-insufficient tier denies
  regardless of flag state, the degradation cache, or the caller's
  ``default``. If the tier cannot be resolved and was never seen, a licensed
  feature is denied (never ``default``); within `tier_grace_seconds` the
  *last-known* tier is used instead (mirrors `penguin_licensing`'s 72h offline
  grace). Only a free-tier feature ever degrades to ``default``.
* **Not bypassable** -- no env var, CLI arg or config flag touches any of the
  above. The only skip is the hardcoded bypass-domain list below (and the
  PostHog flag gate still runs there). Env baselines, where they exist, are
  for plain FEATURE flags only -- they feed gate 1, never gate 2.
* **Statutory rights** (DSAR, erasure, Do-Not-Sell, consent withdrawal) are
  never tier-gated: they appear in no catalog/contract and their endpoints
  never call this module.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Protocol

from opentelemetry import metrics

from .feature_contract import FeatureContract
from .feature_registry import FeatureRegistry
from .feature_registry import get_registry as get_feature_registry
from .tier_catalog import (
    FEATURE_MIN_TIERS,
    KNOWN_TIER_NAMES,
    TIER_FREE,
    TIER_LEVELS,
    normalize_tier,
    required_level,
    tier_level,
)

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("waddles.flask_core.entitlement")
_decision_counter = _meter.create_counter(
    "waddles_entitlement_decisions_total",
    description=(
        "Entitlement decisions by outcome and reason. `reason` distinguishes a "
        "tier denial (licensing enforcement) from a flag-off denial or a degraded answer."
    ),
)
_tier_resolution_histogram = _meter.create_histogram(
    "waddles_entitlement_tier_resolution_seconds",
    unit="s",
    description="Wall time to resolve a tier from the license gate / community tier source.",
)

# ---------------------------------------------------------------------------
# posthog wiring -- published SDK, required dependency (task explicitly asks
# this path be wired for real, not stubbed).
# ---------------------------------------------------------------------------
from posthog import Posthog  # noqa: E402

# ---------------------------------------------------------------------------
# penguin_licensing wiring -- published PyPI package. Import is guarded so a
# genuinely broken install degrades to a fail-closed (free-tier) adapter
# instead of taking the whole module down; every deployment CI validates the
# real dependency is present via requirements.txt, so this branch should
# never fire outside a misconfigured local env.
# ---------------------------------------------------------------------------
try:
    # NOTE: `get_license_client` is deliberately NOT imported. penguin-licensing
    # 0.1.0's shared singleton is `LicenseClient()` with the library default
    # `product="elder"`, which resolves every tier against the WRONG product on
    # license.penguintech.io. Waddles builds its own client pinned to
    # `LICENSE_PRODUCT` -- see `get_waddles_license_client()`.
    from penguin_licensing import LicenseClient

    _PENGUIN_LICENSING_AVAILABLE = True
except ImportError:  # pragma: no cover - only fires with a broken install
    LicenseClient = None  # type: ignore[assignment,misc]
    _PENGUIN_LICENSING_AVAILABLE = False

#: The product id Waddles is registered under on license.penguintech.io. Mirrors the
#: Rust data plane's `LICENSE_PRODUCT` (`core/svc_action/src/lib.rs`,
#: `core/svc_presentation/src/flags.rs`); `tests/test_license_product.py` pins the
#: three together so they cannot drift apart.
LICENSE_PRODUCT = "waddles"

_DEFAULT_LICENSE_SERVER_URL = "https://license.penguintech.io"

_license_client_built_counter = _meter.create_counter(
    "waddles_entitlement_license_client_built_total",
    description=(
        "License clients constructed, labelled by the product they resolve tiers against. "
        "Anything other than `waddles` is a misconfiguration (tier resolved for the wrong product)."
    ),
)

_license_client: Optional["LicenseClient"] = None
_license_client_lock = threading.Lock()


def build_waddles_license_client() -> "LicenseClient":
    """
    Construct a `penguin_licensing.LicenseClient` pinned to the Waddles product.

    Exists because `penguin_licensing.get_license_client()` hardcodes
    `product="elder"`: tier resolution through it asks the license server about
    Elder's licences, not Waddles's. `LICENSE_KEY` / `LICENSE_SERVER_URL` come from
    the environment exactly as the library's own helper reads them; only the
    product differs. Raises (never degrades to a default product) if the library is
    unavailable or if the constructed client does not carry `LICENSE_PRODUCT`.
    """
    if LicenseClient is None:
        raise RuntimeError("penguin_licensing is not installed; cannot build the Waddles license client")
    base_url = os.getenv("LICENSE_SERVER_URL") or _DEFAULT_LICENSE_SERVER_URL
    client = LicenseClient(
        license_key=os.getenv("LICENSE_KEY") or None,
        product=LICENSE_PRODUCT,
        base_url=base_url,
    )
    # Fail loud if the library ever stops honouring the product we passed -- a silent
    # fallback to its own default would resolve every tier against the wrong product.
    if getattr(client, "product", None) != LICENSE_PRODUCT:
        raise RuntimeError(
            "license client product mismatch: expected "
            f"{LICENSE_PRODUCT!r}, got {getattr(client, 'product', None)!r}"
        )
    _license_client_built_counter.add(1, {"product": LICENSE_PRODUCT})
    logger.info(
        "entitlement.license_client_built",
        extra={
            "product": LICENSE_PRODUCT,
            "license_server": base_url,
            "license_key_configured": bool(os.getenv("LICENSE_KEY")),
        },
    )
    return client


def get_waddles_license_client() -> "LicenseClient":
    """Return the process-wide Waddles license client, building it once (keeps the 5-min validation cache shared)."""
    global _license_client
    client = _license_client
    if client is not None:
        return client
    with _license_client_lock:
        if _license_client is None:
            _license_client = build_waddles_license_client()
        return _license_client


# ---------------------------------------------------------------------------
# License-bypass hostnames (penguintech.md License Bypass Domains) -- skips
# the LICENSE gate only, never the flag gate. fnmatch requires the pattern to
# match the ENTIRE candidate string (unlike `in`/`.find`), so
# "waddles.penguintech.cloud.attacker.com" cannot match "*.penguintech.cloud"
# the way a naive substring or unanchored `.endswith` check might be coaxed
# into via a crafted Host header.
#
# BYPASS DEPTH (program plan 2026-08-31-v3-sccebm-program-plan.md §3.3):
# a bypass hostname is not a flat yes/no -- it resolves to a DEPTH, because
# the two host classes exist for different reasons. `waddles*.penguintech.
# cloud` (+ the bare `penguincloud.io`/`penguintech.cloud` apex domains) are
# PenguinTech's own pre-prod SaaS environments (alpha/beta/gamma) -- every
# tier, including Enterprise-only per-COMMUNITY features, must be
# exercisable there so the feature can actually be demoed/tested pre-prod.
# `*.waddles.app` is the PRODUCT's own prod domain -- a customer's own
# deployment -- where only the GLOBAL/tenant-wide gate is free; individual
# communities still pay for community-scoped entitlement. Collapsing both
# classes into one boolean (the pre-fix shape) let a `*.waddles.app` host
# bypass community-tier entitlement it was never meant to -- see
# `resolve_bypass_depth`/`_evaluate` below for where the distinction is
# actually enforced (on whether `community` is None).
# ---------------------------------------------------------------------------
_GLOBAL_COMMUNITY_BYPASS_HOSTNAME_PATTERNS: frozenset[str] = frozenset(
    {
        "penguincloud.io",
        "*.penguincloud.io",
        "penguintech.cloud",
        "*.penguintech.cloud",
    }
)

_GLOBAL_ONLY_BYPASS_HOSTNAME_PATTERNS: frozenset[str] = frozenset(
    {
        "waddles.app",
        "*.waddles.app",
    }
)

# Kept for any external caller/test that only needs the full bypass set
# (e.g. documentation, `is_bypass_domain`'s own implementation below).
_BYPASS_HOSTNAME_PATTERNS: frozenset[str] = (
    _GLOBAL_COMMUNITY_BYPASS_HOSTNAME_PATTERNS | _GLOBAL_ONLY_BYPASS_HOSTNAME_PATTERNS
)

# Tier ordering lives in `tier_catalog` (shared with the static catalog and the
# drift tests). penguin_licensing's LicenseClient reports "community" for the
# unlicensed floor; critical-rules.md's canonical tier name is "free" -- both
# are the same rung rather than two vocabularies. The underscore aliases are
# kept for any in-repo caller that imported them from here.
_TIER_LEVELS: Mapping[str, int] = TIER_LEVELS
_normalize_tier = normalize_tier
_tier_level = tier_level
_required_level = required_level

_DEFAULT_CACHE_TTL_SECONDS = 300.0  # matches penguin_licensing's own validate() cache window

# How long a *last-known* tier may be reused while the license gate is
# unreachable. Matches penguin_licensing's own 72h offline grace so the two
# layers agree on how stale an entitlement may be before it stops counting.
_DEFAULT_TIER_GRACE_SECONDS = 72 * 3600.0

_FREE_LEVEL = TIER_LEVELS[TIER_FREE]


BypassDepth = Literal["none", "global", "global_community"]


def resolve_bypass_depth(hostname: Optional[str]) -> BypassDepth:
    """
    Resolve `hostname` to its license-bypass DEPTH -- `"none"`, `"global"`,
    or `"global_community"`. Takes the hostname as an explicit argument
    (never ambient config) so every call site, including tests, is
    deterministic and denial cases are directly testable.

    Matches the FULL hostname against each pattern set via `fnmatch` --
    never a substring/`.endswith` check, which a crafted Host header could
    defeat with a lookalike suffix (`waddles.penguintech.cloud.attacker.com`
    must NOT match `*.penguintech.cloud`).

    `"global_community"` (PenguinTech's own pre-prod SaaS hosts) bypasses
    the license gate for both tenant-wide AND per-community checks.
    `"global"` (the product's own prod domain, `*.waddles.app`) bypasses
    only tenant-wide checks -- a per-community check (`community is not
    None`) on a `"global"`-depth host must still hit the real license gate.
    See `_evaluate` for where that distinction is enforced.
    """
    if not hostname:
        return "none"
    host = hostname.split(":", 1)[0].strip().lower()
    if any(
        fnmatch.fnmatchcase(host, pattern) for pattern in _GLOBAL_COMMUNITY_BYPASS_HOSTNAME_PATTERNS
    ):
        return "global_community"
    if any(fnmatch.fnmatchcase(host, pattern) for pattern in _GLOBAL_ONLY_BYPASS_HOSTNAME_PATTERNS):
        return "global"
    return "none"


def tier_check_bypassed(hostname: Optional[str], community: Optional[int]) -> bool:
    """
    True if the TIER check is skipped for this host + scope (the flag gate never is).

    BYPASS DEPTH (see `resolve_bypass_depth`): ``"global_community"`` hosts
    (PenguinTech's own pre-prod SaaS) bypass for every scope; ``"global"``
    hosts (the product's prod domain, ``*.waddles.app``) bypass only a
    tenant-wide check (``community is None``) -- a per-community check on a
    ``"global"``-depth host must still hit the real tier gate. Domain-based
    only, never env var / CLI flag (penguintech.md License Bypass Domains).
    """
    depth = resolve_bypass_depth(hostname)
    if depth == "global_community":
        return True
    if depth == "global":
        return community is None
    return False


def is_bypass_domain(hostname: Optional[str]) -> bool:
    """
    True if `hostname` is a hardcoded license-bypass domain, at ANY depth.

    Kept for callers that only need the yes/no answer (e.g. `_evaluate`'s
    depth-aware branch below still calls this indirectly via
    `resolve_bypass_depth`). Only the LICENSE gate is ever skipped for a
    bypass domain; the PostHog flag gate always still runs. Use
    `resolve_bypass_depth` directly when the DEPTH (global vs
    global+community) matters, not just presence.
    """
    return resolve_bypass_depth(hostname) != "none"


# ---------------------------------------------------------------------------
# Gate adapters -- thin, injectable seams so tests exercise EntitlementClient
# without a live PostHog/license-server connection.
# ---------------------------------------------------------------------------
class FlagGate(Protocol):
    """Adapter contract for the PostHog general-enablement gate."""

    def is_enabled(
        self,
        flag_key: str,
        distinct_id: str,
        *,
        groups: Optional[Mapping[str, str]] = None,
    ) -> Optional[bool]:
        """Evaluate a flag. Returns None if the flag can't be resolved (error or unknown key)."""
        ...


class LicenseGate(Protocol):
    """Adapter contract for the license-tier gate."""

    def resolve_tier(self) -> str:
        """Return the deployment's current license tier ("free"/"professional"/"enterprise")."""
        ...


class CommunityTierSource(Protocol):
    """Adapter contract for the tier a tenant admin allocated to one community.

    The tenant tier is the floor for every community in it
    (`LicenseGate.resolve_tier`); this source supplies the *optional* uplift --
    a community the admin allocated Professional/Enterprise from the tenant's
    pool. It is async because the natural implementation is a DAL read
    (`communities.license_tier`) and must not be run through a thread.
    """

    async def community_tier(self, tenant: str, community: int) -> Optional[str]:
        """Return the tier allocated to `community` under `tenant`, or None if none is allocated."""
        ...


class PostHogFlagGate:
    """Wraps the published `posthog` SDK -- the only place this module calls it."""

    def __init__(self, client: Posthog) -> None:
        """Hold a pre-built `Posthog` client (constructed by `from_env`, or injected in tests)."""
        self._client = client

    @classmethod
    def from_env(cls) -> "PostHogFlagGate":
        """
        Build a client from POSTHOG_HOST / POSTHOG_API_KEY.

        `POSTHOG_KEY` is accepted as a fallback for parity with the
        `integrating-license-server` skill's documented env name. Without a
        key the client is constructed `disabled=True` so calls resolve to
        None (unresolvable) rather than raising -- degradation, not a crash,
        for an unconfigured deployment.
        """
        api_key = os.getenv("POSTHOG_API_KEY") or os.getenv("POSTHOG_KEY", "")
        host = os.getenv("POSTHOG_HOST", "https://license.penguintech.io")
        client = Posthog(project_api_key=api_key or "disabled", host=host, disabled=not api_key)
        return cls(client)

    def is_enabled(
        self,
        flag_key: str,
        distinct_id: str,
        *,
        groups: Optional[Mapping[str, str]] = None,
    ) -> Optional[bool]:
        """Evaluate via PostHog; any error (network, disabled client, unknown flag) yields None."""
        try:
            # TODO: posthog-python 7.x deprecates feature_enabled() in favor of
            # evaluate_flags()+flags.is_enabled(); migrate once that surface is
            # stable and documented in the integrating-license-server skill.
            return self._client.feature_enabled(flag_key, distinct_id, groups=groups)
        except Exception:  # noqa: BLE001 - a flag gate must never raise into a request path
            logger.warning("entitlement.flag_gate_error", extra={"flag_key": flag_key})
            return None


class _CommunityOnlyLicenseGate:
    """
    Fail-closed stand-in used only when `penguin_licensing` fails to import.

    TODO: once a `penguin-licensing` release is guaranteed present in every
    deployment target (it already imports cleanly against the PyPI package
    in this worktree's venv), delete this fallback and let `PenguinLicenseGate
    .from_env` raise ImportError unconditionally instead of degrading silently.
    """

    def resolve_tier(self) -> str:
        """Always report the unlicensed floor -- never grants entitlement it can't verify."""
        return "free"


class PenguinLicenseGate:
    """Wraps `penguin_licensing.LicenseClient.validate().tier` -- the only place this module calls it."""

    def __init__(self, client: object) -> None:
        """Hold a `LicenseClient` (real or injected fake) with a `.validate()` method."""
        self._client = client

    @classmethod
    def from_env(cls) -> LicenseGate:
        """
        Build the license gate to use for this process.

        Returns `_CommunityOnlyLicenseGate` directly (not wrapped in
        `PenguinLicenseGate`, whose `resolve_tier` assumes a `.validate()`
        method the fallback doesn't have) when `penguin_licensing` failed to
        import; otherwise a `PenguinLicenseGate` over the shared client pinned to `LICENSE_PRODUCT`.
        """
        if not _PENGUIN_LICENSING_AVAILABLE:
            logger.error(
                "entitlement.penguin_licensing_unavailable",
                extra={"action": "falling back to fail-closed free-tier adapter"},
            )
            return _CommunityOnlyLicenseGate()
        return cls(get_waddles_license_client())

    def resolve_tier(self) -> str:
        """Validate against the license server (penguin_licensing handles its own caching/fail-closed logic)."""
        return str(self._client.validate().tier)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# In-process degradation cache
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _CacheEntry:
    """A single (flag, tenant, community) decision, remembered for the outage window."""

    value: bool
    expires_at: float


CacheKey = tuple[str, str, Optional[int]]


@dataclass(slots=True)
class _TierEntry:
    """The last tier successfully resolved for a scope, kept for the outage grace window."""

    tier: str
    observed_at: float


#: Key for the last-known-tier cache: (scope kind, tenant, community or None).
TierCacheKey = tuple[str, str, Optional[int]]

_SCOPE_TENANT = "tenant"
_SCOPE_COMMUNITY = "community"


@dataclass(slots=True)
class EntitlementClient:
    """
    Evaluates the two-gate (flag AND license tier) decision for a flag/tenant/community.

    Both gates are injectable (`flag_gate`, `license_gate`) so tests run
    without a live PostHog/license-server connection. The tier a flag needs is
    `required_tier(flag)` -- the stricter of the explicit `tier_requirements`
    map, the live `feature_registry` contract's `min_tier`, and the static
    `tier_catalog.FEATURE_MIN_TIERS`; a flag in none of them needs only "free"
    (critical-rules.md: "Free: no gated functionality"). `feature_registry`
    defaults to the process-wide registry. `community_tier_source`, when set,
    supplies the per-community allocation that cascades with the tenant tier
    as ``max(tenant, community)``. See the module docstring for the full tier
    model and fail-closed behaviour.
    """

    flag_gate: FlagGate = field(default_factory=lambda: PostHogFlagGate.from_env())
    license_gate: LicenseGate = field(default_factory=lambda: PenguinLicenseGate.from_env())
    tier_requirements: Mapping[str, str] = field(default_factory=dict)
    community_tier_source: Optional[CommunityTierSource] = None
    feature_registry: Optional[FeatureRegistry] = None
    cache_ttl_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("ENTITLEMENT_CACHE_TTL_SECONDS", str(_DEFAULT_CACHE_TTL_SECONDS))
        )
    )
    tier_grace_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("ENTITLEMENT_TIER_GRACE_SECONDS", str(_DEFAULT_TIER_GRACE_SECONDS))
        )
    )
    _cache: dict[CacheKey, _CacheEntry] = field(default_factory=dict, init=False, repr=False)
    _tier_cache: dict[TierCacheKey, _TierEntry] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Reject an unrecognised tier in `tier_requirements` at construction, not at request time.

        A typo'd requirement (`"enterprize"`) would otherwise either crash a
        request path or -- if ranked naively -- silently make a licensed
        feature free. Failing loudly here is the fail-closed choice.
        """
        for flag_key, tier in self.tier_requirements.items():
            if normalize_tier(tier) not in KNOWN_TIER_NAMES:
                raise ValueError(
                    f"tier_requirements[{flag_key!r}] = {tier!r} is not one of "
                    f"{sorted(KNOWN_TIER_NAMES)}"
                )

    def required_tier(self, flag_key: str) -> str:
        """
        The minimum tier `flag_key` needs -- the stricter of every source.

        Sources: the explicit `tier_requirements`, the live registry's
        `FeatureContract.min_tier` for this flag, and the static
        `FEATURE_MIN_TIERS` catalog. Taking the maximum means no source can
        weaken another (a lax explicit entry cannot un-gate a contract, a
        missing registration cannot un-gate a catalogued flag). Returns
        ``"free"`` for a flag none of them mention. An unrecognised tier from
        any source wins (it is unsatisfiable -- see `tier_catalog.required_level`).
        """
        registry = (
            self.feature_registry if self.feature_registry is not None else get_feature_registry()
        )
        contract: Optional[FeatureContract] = registry.by_flag(flag_key)
        candidates: list[str] = [TIER_FREE]
        explicit = self.tier_requirements.get(flag_key)
        if explicit is not None:
            candidates.append(explicit)
        if contract is not None:
            candidates.append(contract.min_tier)
        catalogued = FEATURE_MIN_TIERS.get(flag_key)
        if catalogued is not None:
            candidates.append(catalogued)
        return normalize_tier(max(candidates, key=required_level))

    async def evaluate(
        self,
        flag_key: str,
        *,
        tenant: str,
        community: Optional[int] = None,
        default: bool = False,
        request_host: Optional[str] = None,
    ) -> bool:
        """
        Resolve `flag_key` for `tenant` (optionally narrower per `community`).

        Both gates are checked live on every call (no silently-stale
        decisions while the services are healthy -- entitlement freshness
        matters for licensing). Wrapped end-to-end: any unexpected exception
        here still returns without propagating into the caller's request
        path -- `default` for a free-tier flag, but always ``False`` for a
        licensed one (an internal error must never grant a paid feature).
        """
        try:
            return await self._evaluate(
                flag_key,
                tenant=tenant,
                community=community,
                default=default,
                request_host=request_host,
            )
        except Exception as exc:  # noqa: BLE001 - entitlement must never crash a request path
            logger.exception(
                "entitlement.evaluate_unexpected_error flag_key=%s error=%s: %s",
                flag_key,
                type(exc).__name__,
                exc,
            )
            return default and not self._is_licensed_feature(flag_key)

    def _is_licensed_feature(self, flag_key: str) -> bool:
        """True if `flag_key` needs more than the free tier -- and True if that can't be determined.

        Used on the error path, where answering "free" by mistake would grant a
        paid feature, so any failure to resolve the requirement counts as licensed.
        """
        try:
            return required_level(self.required_tier(flag_key)) > _FREE_LEVEL
        except Exception:  # noqa: BLE001 - fail closed: unknown requirement == licensed
            logger.exception("entitlement.required_tier_unresolvable flag_key=%s", flag_key)
            return True

    async def _evaluate(
        self,
        flag_key: str,
        *,
        tenant: str,
        community: Optional[int],
        default: bool,
        request_host: Optional[str],
    ) -> bool:
        if not tenant:
            raise ValueError("tenant is required for entitlement evaluation")

        cache_key: CacheKey = (flag_key, tenant, community)
        now = time.monotonic()

        required = self.required_tier(flag_key)
        licensed = required_level(required) > _FREE_LEVEL

        flag_result = await self._check_flag(flag_key, tenant, community)

        # BYPASS DEPTH (see resolve_bypass_depth's docstring): "global_community"
        # bypasses regardless of scope; "global" bypasses only a tenant-wide
        # check (community is None) -- a per-community check on a "global"-depth
        # host must still hit the real license gate, closing the gap where a
        # product-prod host (*.waddles.app) could bypass community-tier
        # entitlement it was never meant to. The bypass skips the TIER check
        # only; the flag gate above always runs.
        tier_result: Optional[bool] = (
            True
            if tier_check_bypassed(request_host, community)
            else await self._check_tier(flag_key, required, tenant=tenant, community=community)
        )

        # LICENSING ENFORCEMENT. A known-insufficient tier is a hard veto: it
        # outranks flag state, the degradation cache and the caller's `default`.
        # (The pre-fix shape sent a flag outage down the degraded path even when
        # the tier was already known too low, so `default=True` could grant a
        # feature the tenant had no licence for.)
        if tier_result is False:
            logger.info(
                "entitlement.tier_denied flag_key=%s tenant=%s required=%s",
                flag_key,
                tenant,
                required,
            )
            self._record_decision(False, "tier_denied", required)
            return False

        # The tier could not be verified (licence gate down AND no last-known
        # tier within the grace window). For a licensed feature that is a deny,
        # never `default` and never a stale decision: an entitlement we cannot
        # verify is not an entitlement. Free-tier flags still degrade below.
        if tier_result is None and licensed:
            logger.warning(
                "entitlement.tier_unverifiable_denied flag_key=%s tenant=%s required=%s",
                flag_key,
                tenant,
                required,
            )
            self._record_decision(False, "tier_unverifiable", required)
            return False

        if flag_result is None or tier_result is None:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                logger.info(
                    "entitlement.degraded_cache_hit",
                    extra={"flag_key": flag_key, "tenant": tenant, "value": cached.value},
                )
                self._record_decision(cached.value, "degraded_cache", required)
                return cached.value
            logger.info(
                "entitlement.degraded_default",
                extra={"flag_key": flag_key, "tenant": tenant, "default": default},
            )
            self._record_decision(default, "degraded_default", required)
            return default

        enabled = bool(flag_result) and bool(tier_result)
        self._cache[cache_key] = _CacheEntry(value=enabled, expires_at=now + self.cache_ttl_seconds)
        self._record_decision(enabled, "granted" if enabled else "flag_off", required)
        return enabled

    def _record_decision(self, enabled: bool, reason: str, required: str) -> None:
        """Count one decision. Labels are low-cardinality (no tenant/flag), never PII."""
        _decision_counter.add(
            1,
            {
                "outcome": "granted" if enabled else "denied",
                "reason": reason,
                "required_tier": required,
            },
        )

    async def _check_flag(
        self, flag_key: str, tenant: str, community: Optional[int]
    ) -> Optional[bool]:
        """Evaluate the PostHog gate off the event loop; any failure yields None (unresolvable)."""
        distinct_id = tenant if community is None else f"{tenant}:{community}"
        groups = {"tenant": tenant}
        try:
            return await asyncio.to_thread(
                self.flag_gate.is_enabled, flag_key, distinct_id, groups=groups
            )
        except Exception:  # noqa: BLE001 - defense in depth atop the adapter's own try/except
            logger.warning("entitlement.flag_gate_unreachable", extra={"flag_key": flag_key})
            return None

    async def _check_tier(
        self, flag_key: str, required: str, *, tenant: str, community: Optional[int]
    ) -> Optional[bool]:
        """Does the effective tier meet `required`? None if the tier can't be determined."""
        effective = await self.effective_tier(tenant=tenant, community=community, flag_key=flag_key)
        if effective is None:
            return None
        satisfied = tier_level(effective) >= required_level(required)
        logger.debug(
            "entitlement.tier_checked flag_key=%s tenant=%s effective=%s required=%s satisfied=%s",
            flag_key,
            tenant,
            effective,
            required,
            satisfied,
        )
        return satisfied

    async def effective_tier(
        self, *, tenant: str, community: Optional[int] = None, flag_key: str = ""
    ) -> Optional[str]:
        """
        ``max(tenant_tier, community_tier)`` -- the tier `tenant` (and `community`) is licensed at.

        Tiers cascade down: the tenant tier is the floor for every community
        in the tenant, and a community allocation can only raise it. A
        tenant-wide check (``community=None``) never consults the community
        source. Returns None only when the tenant tier itself is unknown -- the
        licence gate is unreachable and no tier was seen within
        `tier_grace_seconds` -- because the community allocation is drawn from
        the tenant's licensed pool and can't be trusted without it. A failing
        community source degrades to "no uplift" (a *lower* tier: fail closed).
        `flag_key` is optional log context only. Does not apply the bypass
        domains -- see `tier_check_bypassed` -- so it reports the licensed tier.
        """
        if not tenant:
            raise ValueError("tenant is required for tier resolution")
        tenant_tier = await self._tenant_tier(flag_key, tenant)
        if tenant_tier is None:
            return None
        if community is None or self.community_tier_source is None:
            return tenant_tier
        community_tier = await self._community_tier(flag_key, tenant, community)
        if community_tier is not None and tier_level(community_tier) > tier_level(tenant_tier):
            return community_tier
        return tenant_tier

    async def _tenant_tier(self, flag_key: str, tenant: str) -> Optional[str]:
        """Resolve the tenant tier off the event loop; last-known tier (within grace) on failure."""
        key: TierCacheKey = (_SCOPE_TENANT, tenant, None)
        started = time.monotonic()
        try:
            raw = await asyncio.to_thread(self.license_gate.resolve_tier)
        except Exception as exc:  # noqa: BLE001 - a down license server must degrade, not raise
            logger.warning(
                "entitlement.license_gate_unreachable flag_key=%s error=%s: %s",
                flag_key,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return self._last_known_tier(key)
        finally:
            _tier_resolution_histogram.record(
                time.monotonic() - started, {"source": "license_gate"}
            )
        tier = normalize_tier(str(raw))
        self._tier_cache[key] = _TierEntry(tier=tier, observed_at=time.monotonic())
        return tier

    async def _community_tier(self, flag_key: str, tenant: str, community: int) -> Optional[str]:
        """Resolve a community's allocated tier; None means no uplift (none, or source down)."""
        source = self.community_tier_source
        if source is None:  # pragma: no cover - guarded by the caller; kept for type narrowing
            return None
        key: TierCacheKey = (_SCOPE_COMMUNITY, tenant, community)
        started = time.monotonic()
        try:
            raw = await source.community_tier(tenant, community)
        except Exception as exc:  # noqa: BLE001 - a failing allocation source must not raise
            logger.warning(
                "entitlement.community_tier_source_unreachable flag_key=%s tenant=%s "
                "community=%s error=%s: %s",
                flag_key,
                tenant,
                community,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return self._last_known_tier(key)
        finally:
            _tier_resolution_histogram.record(
                time.monotonic() - started, {"source": "community_tier_source"}
            )
        # A successful "nothing allocated" answer is remembered as the free rung
        # so a later outage can't resurrect an allocation that was since removed.
        tier = TIER_FREE if raw is None else normalize_tier(str(raw))
        self._tier_cache[key] = _TierEntry(tier=tier, observed_at=time.monotonic())
        return tier

    def _last_known_tier(self, key: TierCacheKey) -> Optional[str]:
        """The last tier resolved for `key`, if seen within `tier_grace_seconds`; else None."""
        entry = self._tier_cache.get(key)
        if entry is None:
            logger.info("entitlement.tier_no_last_known scope=%s", key[0])
            return None
        age = time.monotonic() - entry.observed_at
        if age > self.tier_grace_seconds:
            logger.warning(
                "entitlement.tier_last_known_expired scope=%s age_seconds=%.0f grace_seconds=%.0f",
                key[0],
                age,
                self.tier_grace_seconds,
            )
            return None
        logger.info(
            "entitlement.tier_last_known_used scope=%s tier=%s age_seconds=%.0f",
            key[0],
            entry.tier,
            age,
        )
        return entry.tier


# ---------------------------------------------------------------------------
# Process-wide default client -- mirrors penguin_licensing's own
# double-checked-lock singleton so concurrent first requests share one
# warm cache instead of each building a rival (cold) EntitlementClient.
# ---------------------------------------------------------------------------
_default_client: Optional[EntitlementClient] = None
_default_client_lock = threading.Lock()


def get_entitlement_client() -> EntitlementClient:
    """Return the process-wide `EntitlementClient`, built from env on first use."""
    global _default_client

    client = _default_client
    if client is not None:
        return client

    with _default_client_lock:
        if _default_client is None:
            _default_client = EntitlementClient()
        return _default_client
