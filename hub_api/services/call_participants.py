"""Participant identity for call rooms: the UUID hub-api sends downstream, and display names.

svc-streaming addresses every participant by UUID only. hub-api owns `hub_users` -- the PII
boundary -- so it alone maps a participant id back to a human-readable name, and only for
people who are members of the community being asked about. Names are never sent to
svc-streaming and never logged.

**Identity derivation.** A participant's UUID is the user's `hub_users.uuid`: exactly what
the data plane's identity resolver (`resolve_identity_uuid`, alembic 0048: an explicit hub
user link resolves to `hub_users.uuid`) stores in `community_members.user_uuid` and exposes as
`community_member_identities.user_uuid` -- the host-verified actor of the `rooms` app bundle.
Using the same UUID on the REST path (hub-api join/kick/mute) and the chat path (`!room ...`)
means a moderation action taken from either side targets the same participant. Deriving a
separate `uuid5(tenant:community:user)` id here would give one human two identities (plan risk
R5), so it is deliberately not done. Members with no hub account (platform-only chat users)
carry an ephemeral-pseudonym `user_uuid`; they appear in a room only through the chat path and
resolve by that `community_members.user_uuid` to the member row's own display name.

Resolution is tenant-safe because the caller has already been authorised for the community
(`services.community_access`, which also verifies the community belongs to the caller's
tenant) and a name is only returned for ids that are members of that community; any other id
renders as `GUEST_LABEL`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from services.errors import ApiError

#: Rendered for an id that is not a known member of the community (guest, left, or erased).
GUEST_LABEL = "guest"

#: Max ids per `belongs()` query -- keeps the IN list well under every backend's parameter cap.
_CHUNK = 200


async def _select(async_dal: Any, query: Any, *fields: Any) -> Any:
    """`select_async` returning an empty list instead of `None` for no rows."""
    rows = await async_dal.select_async(query, *fields)
    return list(rows or [])


async def identity_uuid(async_dal: Any, dal: Any, *, user_id: int) -> str:
    """The `hub_users.uuid` of `user_id` -- the participant/actor UUID sent to svc-streaming.

    Raises `ApiError(401, UNKNOWN_USER)` when no such user exists (a valid token for a deleted
    user): loud, never a made-up id.
    """
    users = dal.hub_users
    rows = await async_dal.select_async(dal(users.id == user_id), users.uuid)
    row = rows[0] if rows else None
    if row is None or not row.uuid:
        raise ApiError(
            message="Authenticated user has no identity record",
            status_code=401,
            code="UNKNOWN_USER",
        )
    return str(uuid.UUID(str(row.uuid)))


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
    async_dal: Any, dal: Any, *, community_id: int, participant_ids: Iterable[str]
) -> dict[str, str]:
    """Map every id in `participant_ids` to a display name, `GUEST_LABEL` when unknown.

    Only members of `community_id` resolve: an id is looked up over that community's
    `community_members` rows (by `user_uuid`, and by the `hub_users.uuid` of hub-linked
    members). The returned dict always has exactly one entry per distinct input id (keyed as
    given) so callers never need a fallback of their own.
    """
    ids = list(dict.fromkeys(participant_ids))
    names = {pid: GUEST_LABEL for pid in ids}
    canonical = {pid: _canonical(pid) for pid in ids}
    wanted = sorted({c for c in canonical.values() if c})
    if not wanted:
        return names

    users = dal.hub_users
    members = dal.community_members
    by_uuid: dict[str, str] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        # Members addressed by their data-plane identity (also the only path for platform-only
        # members, whose uuid is a pseudonym with no hub_users row).
        member_rows = await _select(
            async_dal,
            dal((members.community_id == community_id) & (members.user_uuid.belongs(chunk))),
            members.user_id,
            members.user_uuid,
            members.display_name,
        )
        for m in member_rows:
            if m.user_uuid and m.display_name:
                by_uuid[str(uuid.UUID(str(m.user_uuid)))] = str(m.display_name)
        # Hub-linked members addressed by hub_users.uuid: hub_users' own name wins.
        user_rows = await _select(
            async_dal,
            dal(users.uuid.belongs(chunk)),
            users.id,
            users.uuid,
            users.display_name,
            users.username,
        )
        if not user_rows:
            continue
        in_community = await _select(
            async_dal,
            dal(
                (members.community_id == community_id)
                & (members.user_id.belongs([str(r.id) for r in user_rows]))
            ),
            members.user_id,
        )
        member_ids = {str(m.user_id) for m in in_community}
        for r in user_rows:
            name = r.display_name or r.username
            if str(r.id) in member_ids and name:
                by_uuid[str(uuid.UUID(str(r.uuid)))] = str(name)

    for pid, canon in canonical.items():
        if canon is not None and canon in by_uuid:
            names[pid] = by_uuid[canon]
    return names
