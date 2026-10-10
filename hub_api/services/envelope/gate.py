"""Fail-closed Enterprise entitlement gate for external KMS (``compliance.external_kms``).

The decision itself is `flask_core.entitlement.EntitlementClient.evaluate`: the
PostHog flag ``waddles.compliance.external_kms`` AND the tenant's licence tier
meeting the flag's required tier. Since #781 that required tier comes from the
static `flask_core.tier_catalog` (Enterprise for this flag), so a flag alone can
never unlock it. This module adds exactly two things on top:

1. **A defence-in-depth tier assertion.** If the client ever reports a required
   tier below Enterprise for this flag (a catalog regression, a mis-registered
   contract), the gate denies instead of trusting it -- the "BYOK is the
   Enterprise upsell" invariant must not depend on one table staying correct.
2. **A tenant-less / error-safe surface.** An empty tenant slug is denied; the
   gate never raises into a request path (the client already degrades to the
   last-known cached value, else ``default=False``, during a PostHog or
   licence-server outage -- a tenant is never *newly* entitled by an outage).

The gate governs *using external KMS for new key material* (configuring,
activating, wrapping a new/rotated DEK under the customer key, writing new
objects under a KMS key). It is deliberately **not** consulted when unwrapping
existing DEKs and never blocks the exit ramp back to the platform baseline --
a lapsed licence must never strand a tenant's already-encrypted data.
"""

from __future__ import annotations

import logging
from typing import Protocol

from flask_core.entitlement import EntitlementClient, get_entitlement_client
from flask_core.tier_catalog import TIER_ENTERPRISE
from quart import has_request_context, request

logger = logging.getLogger(__name__)

#: The PostHog flag + Feature-contract flag for this capability
#: (`libs/core_platform_module/features.py`, id ``compliance.external_kms``).
FEATURE_EXTERNAL_KMS = "waddles.compliance.external_kms"
#: Minimum licence tier, mirrored from the Feature contract's ``min_tier``.
REQUIRED_TIER = TIER_ENTERPRISE
#: Scope the Feature contract requires for managing a tenant's KMS config.
REQUIRED_SCOPE = "compliance.kms:admin"


def current_request_host() -> str | None:
    """`Host` of the active Quart request, or None in jobs/CLI/anywhere without one.

    Feeds the entitlement client's hard-coded licence-bypass-domain check; absence only
    means that check cannot fire -- it never raises.
    """
    if not has_request_context():
        return None
    return str(request.host)


class ExternalKmsEntitlement(Protocol):
    """Anything that can answer "may this tenant use external KMS right now?"."""

    async def is_entitled(self, tenant_slug: str, *, request_host: str | None = None) -> bool:
        """Return True only when the tenant is positively entitled (fail closed otherwise)."""
        ...


class ExternalKmsGate:
    """Evaluates the flag AND the Enterprise tier for ``compliance.external_kms``."""

    def __init__(self, client: EntitlementClient | None = None) -> None:
        """Use `client` if given (tests), else the process-wide entitlement client."""
        self._client = client

    async def is_entitled(self, tenant_slug: str, *, request_host: str | None = None) -> bool:
        """True iff the PostHog flag is on AND the licence tier is Enterprise (else False)."""
        if not tenant_slug:
            logger.warning("envelope.gate.denied_no_tenant")
            return False
        client = self._client or get_entitlement_client()
        required = client.required_tier(FEATURE_EXTERNAL_KMS)
        if required != REQUIRED_TIER:
            logger.error(
                "envelope.gate.tier_requirement_below_enterprise",
                extra={"feature": FEATURE_EXTERNAL_KMS, "required": required},
            )
            return False
        entitled = await client.evaluate(
            FEATURE_EXTERNAL_KMS,
            tenant=tenant_slug,
            default=False,
            request_host=request_host,
        )
        logger.debug(
            "envelope.gate.evaluated",
            extra={"feature": FEATURE_EXTERNAL_KMS, "entitled": bool(entitled)},
        )
        return bool(entitled)
