"""
Public feature-flag entry point for Waddles modules (v3 flag plane).
======================================================================

Thin, stable-signature facade over `entitlement.EntitlementClient` -- the
real two-gate (PostHog flag AND license tier) evaluation and outage
degradation live there (see that module's docstring for the full contract).
Kept as its own module so product code imports a small surface
(`from flask_core.feature_flags import feature_enabled`) without needing to
know about the posthog/penguin_licensing wiring or gate adapters underneath.
"""

from __future__ import annotations

from typing import Optional

from .entitlement import get_entitlement_client, tier_check_bypassed
from .tier_catalog import TIER_ENTERPRISE, TIER_FREE, TIER_PROFESSIONAL

_CANONICAL_TIERS = frozenset({TIER_FREE, TIER_PROFESSIONAL, TIER_ENTERPRISE})


def _current_request_host() -> Optional[str]:
    """
    Best-effort request Host header, for the license bypass-domain check.

    Only meaningful inside an active Quart request context; a background
    job, CLI invocation, or scheduled task has none. Absence just means the
    bypass check can't fire (normal license gating still applies) -- it must
    never raise into the caller.
    """
    try:
        from quart import request  # type: ignore[import-not-found]

        return str(request.host)
    except Exception:  # noqa: BLE001 - no active request context, or quart unavailable
        return None


async def feature_enabled(
    flag_key: str,
    *,
    tenant: str,
    community: int | None = None,
    default: bool = False,
) -> bool:
    """
    Evaluate a namespaced (`waddles.<module>.<feature>`) flag for a tenant.

    Two gates, both must pass: the PostHog flag evaluates true AND the
    tenant's effective tier (``max(tenant, community)``) is at or above the
    flag's required tier -- the stricter of the Feature contract's
    ``min_tier``, the static tier catalog and any explicit requirement (the
    tier check is skipped on a hardcoded license-bypass domain, per
    penguintech.md -- never via env var or CLI flag; the flag check is never
    skipped). A PostHog flag alone never grants a licensed feature.
    Degrades to the last-known cached value, or `default` if nothing has
    ever been cached, on a PostHog outage; a licensed feature whose tier
    can't be verified is denied rather than defaulted -- never raises into
    the caller. See `entitlement.EntitlementClient.evaluate`.
    """
    client = get_entitlement_client()
    return await client.evaluate(
        flag_key,
        tenant=tenant,
        community=community,
        default=default,
        request_host=_current_request_host(),
    )


async def get_tier(*, tenant: str, community: int | None = None) -> str:
    """
    Resolve the effective license tier (``free``/``professional``/``enterprise``).

    ``max(tenant_tier, community_tier)``, cascading down -- the same
    resolution `feature_enabled` enforces, exposed for callers that need the
    tier itself (a `flags.tier` host capability, an admin UI badge). A
    hardcoded bypass domain reports ``enterprise`` exactly where it would skip
    the tier check. Fails closed: an unresolvable tier, an unknown tier
    string, or any error reports ``free`` -- never a higher tier by guess.
    Never raises into the caller.
    """
    try:
        if tier_check_bypassed(_current_request_host(), community):
            return TIER_ENTERPRISE
        resolved = await get_entitlement_client().effective_tier(
            tenant=tenant, community=community
        )
    except Exception:  # noqa: BLE001 - tier lookup must never crash a request path
        return TIER_FREE
    if resolved is None or resolved not in _CANONICAL_TIERS:
        return TIER_FREE
    return resolved
