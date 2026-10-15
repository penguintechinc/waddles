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


async def resolve_scope(async_dal: Any, dal: Any, platform: str, source_id: str) -> ResolvedScope:
    """Resolve one platform source to its numeric ``(tenant_id, community_id)``.

    Raises :class:`InvalidSourceError`, :class:`ScopeNotFoundError` or
    :class:`AmbiguousScopeError`; never returns a guess.
    Inactive communities and communities with no tenant never resolve.
    """
    plat, src = normalize(platform, source_id)
    bind_admin_tables(dal)  # idempotent lazy bind of community_servers (see schema.py)

    primary_flag: dict[int, bool] = {}
    links = await async_dal.select_async(
        dal(
            (dal.community_servers.platform == plat)
            & (dal.community_servers.platform_server_id == src)
            & (dal.community_servers.status == "approved")
        )
    )
    for link in links:
        cid = int(link.community_id)
        primary_flag[cid] = primary_flag.get(cid, False) or bool(link.is_primary)

    legacy = await async_dal.select_async(
        dal((dal.communities.platform == plat) & (dal.communities.platform_server_id == src))
    )
    for row in legacy:
        # The community's own primary server is, by definition, its primary link.
        primary_flag[int(row.id)] = True

    if not primary_flag:
        raise ScopeNotFoundError("no community is linked to this source")

    communities = {}
    for row in await async_dal.select_async(dal(dal.communities.id.belongs(list(primary_flag)))):
        if row.tenant_id is not None and _truthy_active(row):
            communities[int(row.id)] = row
    candidates = [cid for cid in primary_flag if cid in communities]
    if not candidates:
        raise ScopeNotFoundError("no active community is linked to this source")

    if len(candidates) > 1:
        primaries = [cid for cid in candidates if primary_flag[cid]]
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
    return ResolvedScope(
        tenant_id=int(community.tenant_id),
        community_id=int(community.id),
        tenant_slug=str(tenant_rows[0].slug),
        community_name=str(community.name),
    )
