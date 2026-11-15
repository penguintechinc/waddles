"""Data-plane scope resolution: ``(platform, source_id)`` -> numeric ``(tenant_id, community_id)``.

svc-ingest has no database; to key the data plane on numeric ids (never slugs or names -- a
validated integer has one canonical form and no parse step) it asks hub-api which community a
platform source (a Discord guild id, a Twitch channel login, ...) belongs to. This is Slice A of
the numeric-id migration
(``docs/superpowers/specs/2026-10-14-data-plane-numeric-ids-migration.md``).

Two tables carry the platform linkage and BOTH are consulted:

* ``community_servers`` -- the real N-servers-per-community link (every Discord guild AND every
  Twitch channel a community connects, ``status='approved'`` only).
  ``communities.platform_server_id`` is only the community's single *primary* server, so on its
  own it cannot map a Twitch channel.
* ``communities.platform`` / ``platform_server_id`` -- the legacy primary server; kept so a
  community that predates ``community_servers`` (or never got a link row) still resolves.

Resolution is fail-closed and never guesses: no match -> :class:`ScopeNotFoundError` (the caller
falls back to the tenant-wide ``community_id 0`` sentinel itself); several distinct communities
and no single primary -> :class:`AmbiguousScopeError` (a guild linked into more than one community
is a configuration the caller must not silently pick a side of).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from services.schema import bind_admin_tables

logger = logging.getLogger(__name__)

#: Platform ids are lowercase identifiers (``discord``, ``twitch``, ``spectrum`` ...).
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
#: A platform source id: Discord snowflake, Twitch login/id, Slack workspace id... Deliberately a
#: closed alphabet -- one canonical form, no whitespace/Unicode/control characters to confuse a key.
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")

#: Upper bound for the batch form -- a lookup is two indexed selects, but the cap keeps one request
#: bounded.
MAX_BATCH = 100


class ScopeError(Exception):
    """Base class for resolver refusals; every subclass maps to one HTTP status in the blueprint."""


class InvalidSourceError(ScopeError):
    """The platform or source id is not in the accepted canonical form (400)."""


class ScopeNotFoundError(ScopeError):
    """No approved community is linked to this platform source (404 -- caller fails closed)."""


class AmbiguousScopeError(ScopeError):
    """More than one community claims this source and none is the single primary (409)."""


@dataclass(slots=True, frozen=True)
class ResolvedScope:
    """The numeric keys, plus display-only names. Only the ids are ever a key."""

    tenant_id: int
    community_id: int
    tenant_slug: str
    community_name: str


def normalize(platform: str, source_id: str) -> tuple[str, str]:
    """Return the canonical ``(platform, source_id)`` or raise :class:`InvalidSourceError`.

    Platform is case-folded; Twitch logins are case-insensitive so the source is folded for
    ``twitch`` only (Discord/Slack ids are exact-match strings and are left untouched).
    """
    plat = platform.strip().lower() if isinstance(platform, str) else ""
    if not _PLATFORM_RE.fullmatch(plat):
        raise InvalidSourceError("platform is not a valid platform id")
    src = source_id if isinstance(source_id, str) else ""
    if not _SOURCE_RE.fullmatch(src):
        raise InvalidSourceError("source_id is not a valid platform source id")
    if plat == "twitch":
        src = src.lower()
    return plat, src


def _truthy_active(row: Any) -> bool:
    """``is_active`` defaults true; only an explicit false disables a community."""
    return getattr(row, "is_active", True) is not False


#: Reserved pseudo-platform: ``source_id`` is a NUMERIC community id, resolved to that community's
#: own tenant. For connections whose upstream resource has no ``community_servers`` link (a YouTube
#: live chat) the connector runtime already knows the community from the server-derived binding
#: feed and needs only the authoritative tenant plus the display slugs. Same active/tenant checks
#: as a source lookup; it never reads editable metadata.
COMMUNITY_PLATFORM = "community"


async def _resolve_community(async_dal: Any, dal: Any, source_id: str) -> ResolvedScope:
    """Resolve a numeric community id to its (active) tenant scope, fail-closed."""
    if not source_id.isascii() or not source_id.isdigit() or not 0 < int(source_id) < 2**31:
        raise InvalidSourceError("community id is not a positive integer")
    bind_admin_tables(dal)
    rows = await async_dal.select_async(dal(dal.communities.id == int(source_id)))
    if not rows or rows[0].tenant_id is None or not _truthy_active(rows[0]):
        raise ScopeNotFoundError("no active community with a tenant")
    community = rows[0]
    tenant_rows = await async_dal.select_async(dal(dal.tenants.id == int(community.tenant_id)))
    if not tenant_rows or not _truthy_active(tenant_rows[0]):
        raise ScopeNotFoundError("community has no valid active tenant")
    return ResolvedScope(
        tenant_id=int(community.tenant_id),
        community_id=int(community.id),
        tenant_slug=str(tenant_rows[0].slug),
        community_name=str(community.name),
    )


@dataclass(slots=True, frozen=True)
class Claimants:
    """Usable communities claiming one source, first-registered first, all in ONE tenant."""

    ordered: tuple[int, ...]
    communities: dict[int, Any]
    primary_flag: dict[int, bool]


async def resolve_claimants(async_dal: Any, dal: Any, plat: str, src: str) -> Claimants:
    """The single candidate-resolution rule shared by scope resolution and active context.

    ``plat``/``src`` must already be normalized. Fail-closed throughout: a verified claim beats a
    self-asserted one and is decided on the PRE-filter set (an inactive/orphaned verified owner
    refuses, never falls through to a legacy claimant); a source whose claimants span more than one
    tenant (judged before the activity filter) is :class:`AmbiguousScopeError`, never pinned to a
    side. Raises :class:`ScopeNotFoundError` / :class:`AmbiguousScopeError`.
    """
    bind_admin_tables(dal)  # idempotent lazy bind of community_servers (see schema.py)

    # ``verified`` = communities with an APPROVED community_servers link (a reviewed claim);
    # ``legacy`` = communities that merely self-assert the source in communities.platform_server_id
    # (an unreviewed column). Security review of 5937b709: a legacy row must never out-rank a
    # verified link nor mint cross-tenant "primary" authority.
    primary_flag: dict[int, bool] = {}
    verified: set[int] = set()
    links = await async_dal.select_async(
        dal(
            (dal.community_servers.platform == plat)
            & (dal.community_servers.platform_server_id == src)
            & (dal.community_servers.status == "approved")
        ),
        dal.community_servers.id,
        dal.community_servers.community_id,
        dal.community_servers.is_primary,
        orderby=dal.community_servers.id,
    )
    for link in links:
        cid = int(link.community_id)
        verified.add(cid)
        primary_flag[cid] = primary_flag.get(cid, False) or bool(link.is_primary)

    legacy = await async_dal.select_async(
        dal((dal.communities.platform == plat) & (dal.communities.platform_server_id == src)),
        dal.communities.id,
        orderby=dal.communities.id,
    )
    legacy_ids = [int(row.id) for row in legacy]
    for cid in legacy_ids:
        primary_flag.setdefault(cid, False)
        if cid in verified:
            # Backed by an approved link: its own primary server is its primary link. Only ever
            # used to break a tie *within one tenant* (see below).
            primary_flag[cid] = True

    if not primary_flag:
        raise ScopeNotFoundError("no community is linked to this source")

    # A verified claim beats a self-asserted one: legacy-only communities cannot contest (or
    # hijack) a source another community has had approved. Decided on the PRE-filter set.
    pool = [cid for cid in primary_flag if cid in verified] if verified else list(primary_flag)
    rows = {
        int(row.id): row
        for row in await async_dal.select_async(
            dal(dal.communities.id.belongs(pool)),
            dal.communities.id,
            dal.communities.tenant_id,
            dal.communities.is_active,
            dal.communities.name,
        )
    }

    # A source claimed across tenant boundaries is a misconfiguration (or an attack): refuse on the
    # PRE-filter claimants, never adjudicate by primary flag or pin to one tenant.
    tenants = {int(r.tenant_id) for r in rows.values() if r.tenant_id is not None}
    if len(tenants) > 1:
        logger.warning(
            "dataplane_scope.cross_tenant_contention platform=%s candidates=%d", plat, len(pool)
        )
        raise AmbiguousScopeError("source is claimed by more than one tenant")

    usable = {cid: r for cid, r in rows.items() if r.tenant_id is not None and _truthy_active(r)}
    ordered = tuple(cid for cid in pool if cid in usable)
    if not ordered:
        if verified:
            logger.warning(
                "dataplane_scope.verified_claimant_unusable platform=%s verified=%d",
                plat,
                len(verified),
            )
            raise ScopeNotFoundError("verified claimant is inactive; refusing legacy fallback")
        raise ScopeNotFoundError("no active community is linked to this source")
    return Claimants(ordered, usable, primary_flag)


async def resolve_scope(async_dal: Any, dal: Any, platform: str, source_id: str) -> ResolvedScope:
    """Resolve one platform source to its numeric ``(tenant_id, community_id)``.

    Raises :class:`InvalidSourceError`, :class:`ScopeNotFoundError` or
    :class:`AmbiguousScopeError`; never returns a guess.
    Inactive communities/tenants and communities with no tenant never resolve; a source claimed
    across tenants is ambiguous (fail closed).
    """
    plat, src = normalize(platform, source_id)
    if plat == COMMUNITY_PLATFORM:
        return await _resolve_community(async_dal, dal, src)
    claimants = await resolve_claimants(async_dal, dal, plat, src)
    communities = claimants.communities
    candidates = list(claimants.ordered)

    if len(candidates) > 1:
        primaries = [cid for cid in candidates if claimants.primary_flag[cid]]
        if len(primaries) != 1:
            logger.warning(
                "dataplane_scope.ambiguous platform=%s candidates=%d", plat, len(candidates)
            )
            raise AmbiguousScopeError("source is linked to more than one community")
        candidates = primaries

    community = communities[candidates[0]]
    tenant_rows = await async_dal.select_async(dal(dal.tenants.id == int(community.tenant_id)))
    if not tenant_rows:
        # A community pointing at a tenant that does not exist is corrupt data: refuse, never
        # emit a tenant id nothing can authorise.
        raise ScopeNotFoundError("community has no valid tenant")
    if not _truthy_active(tenant_rows[0]):
        # tenants.is_active (058_tenants_and_claims.sql): a suspended tenant must not keep flowing
        # through the data plane. Caller fails to the community_id 0 sentinel.
        logger.warning(
            "dataplane_scope.tenant_inactive tenant_id=%d community_id=%d",
            int(community.tenant_id),
            int(community.id),
        )
        raise ScopeNotFoundError("tenant is not active")
    return ResolvedScope(
        tenant_id=int(community.tenant_id),
        community_id=int(community.id),
        tenant_slug=str(tenant_rows[0].slug),
        community_name=str(community.name),
    )
