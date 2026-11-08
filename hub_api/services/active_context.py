"""Per-user active community context for a platform source (slash-command routing).

A Discord guild can be paired to several communities. Slash is guild-wide, so WHICH community
processes an invocation is the invoking user's *active context*: their explicit choice, else the
first-registered community of the source. svc-ingest (no database) asks hub-api through
``/api/v1/internal/dataplane/active-context`` and keys the data plane on the NUMERIC community id
returned here (data-plane numeric-id rule); communities are addressed to the caller by immutable
``public_id`` UUID, never by name.

Switchable set = the source's communities, resolved by the one shared rule
:func:`services.dataplane_scope.resolve_claimants` (an approved ``community_servers`` link beats a
legacy ``communities.platform_server_id``; an inactive verified claimant refuses with no legacy
fallback; a source claimed across tenants is refused outright, never pinned to a tenant). Inactive,
deleted, tenant-less or inactive-tenant communities are never members. A stored choice that has
since left the set silently yields to the default (``explicit=false``); it is never honoured.

Everything fails closed: nothing is guessed, and no id or name is ever logged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from services import dataplane_scope as scopes

logger = logging.getLogger(__name__)


class ActiveContextError(Exception):
    """Base class for refusals; every subclass maps to one HTTP status in the blueprint."""


class InvalidContextRequestError(ActiveContextError):
    """A parameter is not in its canonical form (400)."""


class NoCommunityError(ActiveContextError):
    """No usable community is linked to the source (404)."""


class AmbiguousSourceError(ActiveContextError):
    """The source is claimed across tenants; nothing is pinned or guessed (409)."""


class NotSwitchableError(ActiveContextError):
    """The requested community is not a switchable member of this source's set (404)."""


@dataclass(slots=True, frozen=True)
class CommunityChoice:
    """One switchable community; ``community_id`` is the numeric key, the rest is addressing."""

    community_id: int
    public_id: str
    display_name: str
    position: int


@dataclass(slots=True, frozen=True)
class ActiveContext:
    """A user's resolved context: the active choice plus every switchable community."""

    active: CommunityChoice
    explicit: bool
    communities: tuple[CommunityChoice, ...]


def canonical_uuid(value: object) -> str:
    """Return the lowercase canonical form of a UUID string or raise the 400 error."""
    if not isinstance(value, str) or len(value) != 36:
        raise InvalidContextRequestError("not a canonical uuid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise InvalidContextRequestError("not a canonical uuid") from exc
    return str(parsed)


def _source(platform: str, source_id: str) -> tuple[str, str]:
    """Normalize the source; the numeric-community pseudo platform is not a real source."""
    try:
        plat, src = scopes.normalize(platform, source_id)
    except scopes.InvalidSourceError as exc:
        raise InvalidContextRequestError("invalid source") from exc
    if plat == scopes.COMMUNITY_PLATFORM:
        raise InvalidContextRequestError("invalid source")
    return plat, src


async def switchable(async_dal: Any, platform: str, source_id: str) -> tuple[CommunityChoice, ...]:
    """The ordered switchable set for a source (position 0 = first-registered default).

    Candidate resolution is :func:`services.dataplane_scope.resolve_claimants` -- the SAME rule
    the scope resolver applies, so a cross-tenant source (409) or an inactive verified claimant
    (404, no legacy fallback) refuses here exactly as it does there.
    """
    plat, src = _source(platform, source_id)
    try:
        claimants = await scopes.resolve_claimants(async_dal, async_dal.dal, plat, src)
    except scopes.AmbiguousScopeError as exc:
        raise AmbiguousSourceError("source is claimed by more than one tenant") from exc
    except scopes.ScopeNotFoundError as exc:
        raise NoCommunityError("no usable community is linked to this source") from exc
    ordered = list(claimants.ordered)
    rows = await async_dal.executesql_async(
        "SELECT c.id, c.public_id::text, COALESCE(NULLIF(c.display_name, ''), c.name, '') "
        "FROM communities c JOIN tenants t ON t.id = c.tenant_id "
        "WHERE c.id = ANY(%s) AND c.deleted_at IS NULL AND t.is_active IS NOT FALSE",
        [ordered],
    )
    by_id = {int(r[0]): r for r in rows}
    usable = [cid for cid in ordered if cid in by_id]
    if not usable:
        raise NoCommunityError("no active community is linked to this source")
    return tuple(
        CommunityChoice(cid, str(by_id[cid][1]), str(by_id[cid][2]), position)
        for position, cid in enumerate(usable)
    )


async def get_context(async_dal: Any, platform: str, source_id: str, user: str) -> ActiveContext:
    """Resolve the user's active context: a still-valid stored choice, else the default."""
    user_uuid = canonical_uuid(user)
    plat, src = _source(platform, source_id)
    choices = await switchable(async_dal, plat, src)
    stored = await async_dal.executesql_async(
        "SELECT community_id FROM user_active_contexts "
        "WHERE platform = %s AND source_id = %s AND user_uuid = %s",
        [plat, src, user_uuid],
    )
    if stored:
        chosen = int(stored[0][0])
        for choice in choices:
            if choice.community_id == chosen:
                return ActiveContext(choice, True, choices)
    return ActiveContext(choices[0], False, choices)


async def set_context(
    async_dal: Any, platform: str, source_id: str, user: str, community_public_id: str
) -> ActiveContext:
    """Persist the user's choice (by community ``public_id``) and return the new context."""
    user_uuid = canonical_uuid(user)
    wanted = canonical_uuid(community_public_id)
    plat, src = _source(platform, source_id)
    choices = await switchable(async_dal, plat, src)
    target = next((c for c in choices if c.public_id == wanted), None)
    if target is None:
        raise NotSwitchableError("community is not switchable for this source")
    await async_dal.executesql_async(
        "INSERT INTO user_active_contexts (platform, source_id, user_uuid, community_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (platform, source_id, user_uuid) "
        "DO UPDATE SET community_id = EXCLUDED.community_id, updated_at = now()",
        [plat, src, user_uuid, target.community_id],
    )
    return ActiveContext(target, True, choices)
