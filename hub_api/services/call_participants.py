"""Participant identity for call rooms: the UUID hub-api sends downstream, and display names.

svc-streaming addresses every participant by UUID only. hub-api owns `hub_users` -- the PII
boundary -- so it alone maps a participant id back to a human-readable name, and only for
ACTIVE members of the community being asked about. Names are never sent to svc-streaming and
never logged.

**Identity derivation.** A participant's UUID is a PER-COMMUNITY pseudonym:
`uuid5(NS, "<tenant>:<community_id>:<user_id>")`. The same person therefore has a different id
in every community, so neither svc-streaming nor any REST response lets two communities (or a
room and a platform identity) be linked. The global `hub_users.uuid` is never sent or returned.
`community_members.user_uuid` is NOT used: for a hub-linked member it IS the global
`hub_users.uuid` (alembic 0048), which would be linkable across communities.

Names are resolved by recomputing that pseudonym over the community's ACTIVE members
(`community_members.is_active`); a former or deactivated member, a non-member and an unknown id
all render as `GUEST_LABEL`. Platform-only chat members (no hub account) have no hub user id to
derive from; they resolve by their (per-tenant) pseudonym `community_members.user_uuid`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

#: Namespace of the per-community participant pseudonym (uuid5 of tenant + community + user).
_PARTICIPANT_NS = uuid.UUID("6f3c1c1e-5b8e-4a67-9d3b-2f0c8f3f6a11")

#: Rendered for an id that is not a known member of the community (guest, left, or erased).
GUEST_LABEL = "guest"

#: Max ids per `belongs()` query -- keeps the IN list well under every backend's parameter cap.
_CHUNK = 200


async def _select(async_dal: Any, query: Any, *fields: Any) -> Any:
    """`select_async` returning an empty list instead of `None` for no rows."""
    rows = await async_dal.select_async(query, *fields)
    return list(rows or [])


def identity_uuid(*, tenant_slug: str, community_id: int, user_id: int) -> str:
    """The caller's per-community participant/actor UUID (pure; the global user uuid is unused)."""
    return str(uuid.uuid5(_PARTICIPANT_NS, f"{tenant_slug}:{community_id}:{user_id}"))


async def is_community_staff(async_dal: Any, dal: Any, *, community_id: int, user_id: int) -> bool:
    """True when `user_id` is a community owner/admin (or a global admin): room-access staff.

    Staff are never refused by a room lock or schedule (`services/call_access.py`). Reuses the
    same two definitions of "admin" the admin routes authorise with.
    """
    from services.community_access import _ADMIN_ROLES
    from services.community_authz import is_super_admin

    if await is_super_admin(async_dal, dal, user_id=user_id):
        return True
    members = dal.community_members
    rows = await _select(
        async_dal,
        dal(
            (members.community_id == community_id)
            & (members.user_id == str(user_id))
            & (members.is_active == True)  # noqa: E712 - pydal query operator
            & (members.role.belongs(_ADMIN_ROLES))
        ),
        members.id,
    )
    return bool(rows)


def _canonical(participant_id: str) -> str | None:
    """Canonical lowercase UUID text, or None when `participant_id` is not a UUID."""
    try:
        return str(uuid.UUID(participant_id))
    except (ValueError, AttributeError, TypeError):
        return None


async def resolve_display_names(
    async_dal: Any,
    dal: Any,
    *,
    tenant_slug: str,
    community_id: int,
    participant_ids: Iterable[str],
) -> dict[str, str]:
    """Map every id in `participant_ids` to a display name, `GUEST_LABEL` when unknown.

    Only ACTIVE members of `community_id` resolve. The returned dict always has exactly one
    entry per distinct input id (keyed as given) so callers never need a fallback of their own.
    """
    ids = list(dict.fromkeys(participant_ids))
    names = {pid: GUEST_LABEL for pid in ids}
    canonical = {pid: _canonical(pid) for pid in ids}
    wanted = {c for c in canonical.values() if c}
    if not wanted:
        return names

    users = dal.hub_users
    members = dal.community_members
    by_uuid: dict[str, str] = {}

    # Hub-linked members: recompute each ACTIVE member's pseudonym and keep the wanted ones.
    active = await _select(
        async_dal,
        dal((members.community_id == community_id) & (members.is_active == True)),  # noqa: E712
        members.user_id,
    )
    matched: dict[str, int] = {}
    for m in active:
        try:
            uid = int(str(m.user_id))
        except (TypeError, ValueError):
            continue  # platform-only member: no hub user id to derive from
        pseudonym = identity_uuid(tenant_slug=tenant_slug, community_id=community_id, user_id=uid)
        if pseudonym in wanted:
            matched[pseudonym] = uid
    ordered = sorted(matched.items())
    for start in range(0, len(ordered), _CHUNK):
        chunk = ordered[start : start + _CHUNK]
        rows = await _select(
            async_dal,
            dal(users.id.belongs([uid for _, uid in chunk])),
            users.id,
            users.display_name,
            users.username,
        )
        by_id = {int(r.id): (r.display_name or r.username) for r in rows}
        for pseudonym, uid in chunk:
            if by_id.get(uid):
                by_uuid[pseudonym] = str(by_id[uid])

    # Platform-only chat members: addressed by their per-tenant pseudonym, ACTIVE rows only.
    pseudo = sorted(wanted)
    for start in range(0, len(pseudo), _CHUNK):
        chunk_ids = pseudo[start : start + _CHUNK]
        rows = await _select(
            async_dal,
            dal(
                (members.community_id == community_id)
                & (members.is_active == True)  # noqa: E712 - pydal query operator
                & (members.user_uuid.belongs(chunk_ids))
            ),
            members.user_id,
            members.user_uuid,
            members.display_name,
        )
        for m in rows:
            if m.user_id is None and m.user_uuid and m.display_name:
                by_uuid.setdefault(str(uuid.UUID(str(m.user_uuid))), str(m.display_name))

    for pid, canon in canonical.items():
        if canon is not None and canon in by_uuid:
            names[pid] = by_uuid[canon]
    return names
