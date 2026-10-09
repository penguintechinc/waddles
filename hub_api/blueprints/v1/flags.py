"""v1 resolved feature flags -- server-side PostHog proxy for client UIs.

Clients (hub-webui's `useFeatureFlag`, and any other client -- mobile/CLI)
must never hold a PostHog API key: `client.md` Authentication & Tokens
requires third-party API calls to be proxied through the backend, never
made directly from a client. This endpoint is that proxy: it resolves a
fixed, curated set of `CLIENT_FLAG_KEYS` through the SAME `EntitlementClient`
every other `waddles.<module>.<feature>` gate in this service already uses
(`flask_core.entitlement`), and returns a plain `{key: bool}` map -- never
the PostHog key, never raw SDK/internal state.

`CLIENT_FLAG_KEYS` is deliberately a short, explicit allowlist, NOT "every
flag this service knows about" -- a server-internal kill-switch
(`{product}.disable-{mechanism}`, critical-rules.md "Core platform
mechanisms") must never reach a client, and resolving the many
community-scoped flags (`waddles.community.*`, `waddles.streaming.*` etc.,
see `blueprints/v1/community_polls.py` and siblings) here would need a
`community_id` this tenant-wide endpoint doesn't take -- those stay
resolved inline by their own blueprint, same as today. Add a new
client-visible flag by appending its key here; no other wiring needed.

Resolution calls `EntitlementClient.evaluate()` directly (not the
`feature_flags.feature_enabled()` facade) so every key's degraded default
is explicit per call, and so this module's tests can inject a fake
`EntitlementClient` via `get_entitlement_client` the same seam
`entitlement.py`'s own tests use -- no live PostHog/license server needed.

Graceful degradation (critical-rules.md Feature Flags & License Tiers):
PostHog/license unreachable -> `EntitlementClient`'s own last-known cache
-> `default=False` -- `evaluate()` is already wrapped end-to-end and never
raises, so this route can never 500 on a flag-server outage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.entitlement import get_entitlement_client
from flask_core.tenancy import get_tenant_context, tenant_middleware
from opentelemetry import metrics
from quart import Blueprint, request
from quart_schema import validate_response

logger = logging.getLogger(__name__)

flags_bp = Blueprint("v1_flags", __name__, url_prefix="/api/v1")

# Client-visible flag allowlist -- see module docstring. Extend by appending
# a new `waddles.<module>.<feature>` key; NEVER add a server-internal
# kill-switch here.
CLIENT_FLAG_KEYS: tuple[str, ...] = (
    "waddles.webui.modular_nav",
    "waddles.webui.modular_dashboard",
    "waddles.webui.bundle_marketplace_ui",
    "waddles.webui.community_bundles",
    "waddles.webui.tenant_bundle_catalog",
    "waddles.webui.super_communities",
    "waddles.webui.super_tenants",
    "waddles.webui.role_sync_mapping",
    "waddles.webui.discord_bot_install_link",
    "waddles.secret-messaging",
)

_meter = metrics.get_meter("waddles.hub_api.flags")
_resolution_counter = _meter.create_counter(
    "waddles_hub_api_flag_resolution_requests_total",
    description="Count of /api/v1/flags resolution calls, by tenant.",
)


@dataclass(slots=True, frozen=True)
class ResolvedFlagsResponse:
    """`{key: bool}` map for the authenticated tenant -- no raw PostHog/internal state."""

    flags: dict[str, bool] = field(default_factory=dict)


@flags_bp.route("/flags", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("flags:read")  # type: ignore[untyped-decorator]
@validate_response(ResolvedFlagsResponse)
async def get_resolved_flags() -> ResolvedFlagsResponse | tuple[dict[str, object], int]:
    """Resolve `CLIENT_FLAG_KEYS` for the token's tenant -- see module docstring.

    Never 500s on a PostHog/license outage -- `EntitlementClient.evaluate()`
    degrades to its own cache, then to `default=False`, for every key.
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        # tenant_middleware always publishes this before the handler runs;
        # defensive only, never actually reachable in production.
        return cast(
            tuple[dict[str, object], int],
            error_response("Tenant context missing", 403, "FORBIDDEN"),
        )

    client = get_entitlement_client()
    request_host = request.host
    resolved: dict[str, bool] = {}
    for flag_key in CLIENT_FLAG_KEYS:
        value = await client.evaluate(
            flag_key,
            tenant=ctx.tenant_slug,
            default=False,
            request_host=request_host,
        )
        logger.debug(
            "flags.resolved",
            extra={"flag_key": flag_key, "tenant": ctx.tenant_slug, "value": value},
        )
        resolved[flag_key] = value

    _resolution_counter.add(1, {"tenant": ctx.tenant_slug})
    return ResolvedFlagsResponse(flags=resolved)


BLUEPRINTS: list[Blueprint] = [flags_bp]
