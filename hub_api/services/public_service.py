"""Pre-auth public surface -- ported from `publicController.js`.

Every route in `blueprints/v1/public.py` is PRE-AUTH (no JWT, no
`tenant_middleware`/`require_scope` -- see `hub_api/PORTING.md`'s Auth
pattern table). That makes the `communities`-backed endpoints
(`getCommunities`/`getCommunity`/`getSpotlightedCommunities`) a genuine
cross-tenant data-leak risk that Node's own queries do NOT guard against
today: `communities.tenant_id` is `NOT NULL` (`058_tenants_and_claims.
sql`), but `publicController.js`'s SQL never filters on it, so an
anonymous caller against a real multi-tenant deployment sees every
tenant's communities, not just the operator's own. This is the one place
this port deliberately does NOT faithfully reproduce Node -- security.md
Tenant Isolation is a hard boundary regardless of auth state, and the
task's own SECURITY note calls this out explicitly ("public endpoints
... don't leak private/cross-tenant data").

Fix: resolve a `TenantContext` for `HubAPIConfig.default_tenant_slug`
(env `DEFAULT_TENANT_SLUG`, default `"default"` -- the same fallback
`login()`/OAuth already use for pre-auth flows, see `auth_service.py`/
`oauth_service.py`'s own `DEFAULT_TENANT_SLUG` notes) and run every
`communities` query through `flask_core.tenancy.tenant_scoped()` -- the
SAME ORM-layer helper `tenant_middleware`-gated routes use elsewhere in
this codebase, just built from a resolved default tenant instead of a
JWT claim (there is no JWT here). This is additive scoping, not new
behavior invented from nothing: `communities.tenant_id` already exists
in the real schema, `tenant_scoped()` already exists in `flask_core`, and
`DEFAULT_TENANT_SLUG` already exists in this app's config.

`hub_modules`/`coordination`/`hub_settings` (marketplace catalog, live
streams, hub-wide settings) have NO `tenant_id` column in any migration
-- genuinely global, cross-tenant-by-design tables (a shared module
catalog, a shared live-stream index, a shared settings store), not an
oversight. Scoping those would mean inventing a column the schema
doesn't have, which `hub_api/PORTING.md`'s Gotcha #4 precedent
explicitly rules out ("don't silently invent a column") -- ported
faithfully, unscoped, matching Node.

pydal query builder only, never raw SQL -- see `hub_api/PORTING.md`
Gotcha #1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from flask_core.tenancy import TenantContext, tenant_scoped

from config import HubAPIConfig
from services.auth_service import get_hub_settings_map
from services.errors import not_found


async def resolve_default_tenant(async_dal: Any, dal: Any, cfg: HubAPIConfig) -> TenantContext:
    """Resolve `HubAPIConfig.default_tenant_slug` into a `TenantContext` for pre-auth queries.

    Raises `LookupError` if the default tenant row is missing/inactive --
    a deployment-configuration error (the seed migration always creates
    it), not a per-request 4xx; callers let this propagate to a 500
    rather than inventing a fake tenant_id.
    """
    rows = await async_dal.select_async(dal(dal.tenants.slug == cfg.default_tenant_slug))
    row = rows.first() if rows else None
    if row is None or not row.is_active:
        raise LookupError(f"default tenant '{cfg.default_tenant_slug}' missing or inactive")
    return TenantContext(
        tenant_id=row.id, tenant_slug=row.slug, is_default=(row.slug == cfg.default_tenant_slug)
    )


@dataclass(slots=True)
class _PlatformBucket:
    """Mutable per-platform accumulator for `get_stats()`'s coordination breakdown."""

    server_ids: set[str] = field(default_factory=set)
    channels: int = 0
    live: int = 0
    viewers: int = 0


async def get_stats(async_dal: Any, dal: Any, ctx: TenantContext) -> dict[str, Any]:
    """Platform stats for the public landing page: community count + per-platform live counts."""
    community_count = await async_dal.count_async(
        tenant_scoped(dal.communities.is_active == True, ctx)  # noqa: E712
    )

    stats: dict[str, Any] = {
        "communities": community_count,
        "discord": {"servers": 0, "channels": 0},
        "twitch": {"channels": 0, "live": 0, "viewers": 0},
        "slack": {"workspaces": 0, "channels": 0},
    }

    # `coordination` has no tenant_id -- global, faithful to Node (see
    # module docstring). Node wraps this in its own try/except so a
    # missing/malformed table degrades to zeros rather than a 500;
    # mirrored here.
    try:
        rows = await async_dal.select_async(
            dal(dal.coordination.platform != None),  # noqa: E711
            dal.coordination.platform,
            dal.coordination.server_id,
            dal.coordination.is_live,
            dal.coordination.viewer_count,
        )
    except Exception:  # noqa: BLE001 - degrade to zeros, matching Node's own catch
        rows = []

    per_platform: dict[str, _PlatformBucket] = {}
    for row in rows:
        bucket = per_platform.setdefault(row.platform, _PlatformBucket())
        if row.server_id:
            bucket.server_ids.add(row.server_id)
        bucket.channels += 1
        if row.is_live:
            bucket.live += 1
            bucket.viewers += row.viewer_count or 0

    if "discord" in per_platform:
        b = per_platform["discord"]
        stats["discord"] = {"servers": len(b.server_ids), "channels": b.channels}
    if "twitch" in per_platform:
        b = per_platform["twitch"]
        stats["twitch"] = {"channels": b.channels, "live": b.live, "viewers": b.viewers}
    if "slack" in per_platform:
        b = per_platform["slack"]
        stats["slack"] = {"workspaces": len(b.server_ids), "channels": b.channels}
    return stats


async def get_spotlighted_communities(async_dal: Any, dal: Any, ctx: TenantContext) -> list[Any]:
    """Top 5 active, public, non-support communities."""
    not_support = (dal.communities.community_type == None) | (  # noqa: E711
        dal.communities.community_type != "support"
    )
    query = tenant_scoped(
        (dal.communities.is_active == True)  # noqa: E712
        & (dal.communities.is_public == True)  # noqa: E712
        & not_support,
        ctx,
    )
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.communities.member_count | ~dal.communities.created_at,
        limitby=(0, 5),
    )
    return list(rows)


async def list_communities(
    async_dal: Any, dal: Any, ctx: TenantContext, *, page: int, limit: int
) -> tuple[list[Any], int, int]:
    """Public, paginated community directory."""
    page = max(1, page)
    limit = min(50, max(1, limit))
    offset = (page - 1) * limit

    query = tenant_scoped(
        (dal.communities.is_active == True) & (dal.communities.is_public == True),  # noqa: E712
        ctx,
    )
    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.communities.member_count | dal.communities.name,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_community(async_dal: Any, dal: Any, ctx: TenantContext, *, community_id: int) -> Any:
    """Public single-community lookup -- active + public only."""
    query = tenant_scoped(
        (dal.communities.id == community_id)
        & (dal.communities.is_active == True)  # noqa: E712
        & (dal.communities.is_public == True),  # noqa: E712
        ctx,
    )
    rows = await async_dal.select_async(dal(query))
    if not rows:
        raise not_found("Community not found")
    return rows.first()


async def get_live_streams(async_dal: Any, dal: Any, *, limit: int) -> list[Any]:
    """Live Twitch streams, highest viewer count first. Global -- see module docstring."""
    limit = min(50, max(1, limit))
    rows = await async_dal.select_async(
        dal((dal.coordination.is_live == True) & (dal.coordination.platform == "twitch")),  # noqa: E712
        orderby=~dal.coordination.viewer_count,
        limitby=(0, limit),
    )
    return list(rows)


async def get_stream_details(async_dal: Any, dal: Any, *, entity_id: str) -> Any:
    """Single stream's detail by `coordination.entity_id`.

    Matches Node's real route (`GET /api/v1/public/live/:entityId`,
    `routes/public.js`), NOT `frontend/src/services/api.js`'s
    `getStreamDetails()`, which calls `/api/v1/public/streams/${entityId}`
    -- a path Node's own router never registers (a pre-existing frontend/
    backend contract drift, same class as `hub_api/PORTING.md` Gotcha #4;
    confirmed by reading `routes/public.js` directly, not inferred).
    Faithfully porting Node's real, working route rather than the
    frontend's broken call target.
    """
    rows = await async_dal.select_async(dal(dal.coordination.entity_id == entity_id))
    if not rows:
        raise not_found("Stream not found")
    return rows.first()


async def get_signup_settings(async_dal: Any, dal: Any) -> tuple[bool, list[str]]:
    """`(signup_enabled, allowed_domains)` derived from `hub_settings`."""
    settings = await get_hub_settings_map(async_dal, dal)
    signup_enabled = settings.get("signup_enabled", "true") == "true"
    email_configured = settings.get("email_configured", "false") == "true"
    allowed_domains_raw = settings.get("signup_allowed_domains", "")
    allowed_domains = (
        [d.strip() for d in allowed_domains_raw.split(",") if d.strip()]
        if allowed_domains_raw
        else []
    )
    return (signup_enabled and email_configured), allowed_domains


async def get_banner(async_dal: Any, dal: Any) -> dict[str, Any]:
    """Global announcement banner settings."""
    settings = await get_hub_settings_map(async_dal, dal)
    return {
        "enabled": settings.get("banner_enabled") == "true",
        "text": settings.get("banner_text", ""),
        "bg_color": settings.get("banner_bg_color", "#F5C518"),
        "text_color": settings.get("banner_text_color", "#000000"),
    }
