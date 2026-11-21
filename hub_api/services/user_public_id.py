"""User public-id URL converter + request-time id translation.

Every user/member identifier that crosses the client-facing API boundary is the hub user's
`hub_users.uuid` (the identity UUID), never the enumerable numeric id. Routes declare
`<uuuid:user_id>`; this module then:

1. `UserUuidConverter` -- matches ONLY a dashed UUID (any case), so a numeric id no longer
   routes (uniform 404) and `to_python` hands back the lowercase-normalised value.
2. `translate_user_view_arg` -- a `before_request` hook that swaps the UUID for the internal
   numeric id in `request.view_args`, so handlers, the authz helpers and services keep taking
   `user_id: int` and every existing scope / tenant / membership check runs exactly as before.

Resolution is pure id translation: it does NOT filter by tenant or scope. An UNKNOWN uuid
resolves to `UNKNOWN_USER_ID` (a value no real row can hold), so the response is whatever the
existing handler already gives a non-existent numeric id -- never an existence oracle.
Internal gRPC, DB FKs, data-plane keys and logs stay numeric.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from quart import Blueprint, Quart, current_app, request
from quart.routing import QuartMap
from werkzeug.datastructures import ImmutableDict
from werkzeug.routing import BaseConverter

from services.schema import bind_auth_tables

#: Converter name used in route rules: `<uuuid:user_id>`.
CONVERTER_NAME = "uuuid"

#: Path param name the preprocessor translates.
USER_PARAM = "user_id"

#: Numeric id substituted for an unknown UUID; real ids are positive identities.
UNKNOWN_USER_ID = -1

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_FULL = re.compile(_UUID_RE)


class UserPublicId(str):
    """Marker type: a normalised hub-user public UUID taken from a `uuuid` path param."""

    __slots__ = ()


class UserUuidConverter(BaseConverter):
    """Matches only a dashed UUID; normalises to lowercase."""

    regex = _UUID_RE
    part_isolating = True
    public_uuid = True  # lets the OpenAPI provider document it as `string`/`format: uuid`

    def to_python(self, value: str) -> UserPublicId:
        """Lowercase canonical UUID text (the regex already guarantees the shape)."""
        return UserPublicId(str(uuid.UUID(value)))

    def to_url(self, value: Any) -> str:
        """Render a UUID for `url_for`; anything that is not a UUID is a programming error."""
        return str(uuid.UUID(str(value)))


def _hub_users(dal: Any) -> Any:
    """The `hub_users` table, binding the auth tables on first use."""
    if "hub_users" not in dal.tables:
        bind_auth_tables(dal)
    return dal.hub_users


async def lookup_user_id(public_id: str) -> int:
    """Numeric id for a canonical user UUID; `UNKNOWN_USER_ID` when no row has it."""
    async_dal = current_app.config["async_dal"]
    dal = current_app.config["dal"]
    table = _hub_users(dal)
    rows = await async_dal.select_async(dal(table.uuid == public_id), table.id)
    if not rows:
        return UNKNOWN_USER_ID
    return int(rows[0].id)


async def translate_user_view_arg() -> None:
    """`before_request`: swap a `uuuid` view arg for the internal numeric id in place."""
    view_args = request.view_args
    if not view_args:
        return None
    raw = view_args.get(USER_PARAM)
    if isinstance(raw, UserPublicId):
        view_args[USER_PARAM] = await lookup_user_id(str(raw))
    return None


async def lookup_user_public_ids(
    user_ids: Iterable[object], *, async_dal: Any = None, dal: Any = None
) -> dict[int, str]:
    """Public UUIDs for many internal user ids in ONE query (no N+1 on list responses).

    Ids that are not numeric (legacy platform-only members) or have no `hub_users` row are
    simply absent from the result; callers emit `None` for them -- never the numeric id.
    """
    wanted: set[int] = set()
    for raw in user_ids:
        try:
            wanted.add(int(str(raw)))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}
    async_dal = async_dal if async_dal is not None else current_app.config["async_dal"]
    dal = dal if dal is not None else current_app.config["dal"]
    table = _hub_users(dal)
    rows = await async_dal.select_async(dal(table.id.belongs(sorted(wanted))), table.id, table.uuid)
    return {int(r.id): str(r.uuid) for r in rows if r.uuid}


def public_id_for(mapping: dict[int, str], raw: object) -> str | None:
    """Public UUID for a raw (int/str) user id from a `lookup_user_public_ids` mapping."""
    try:
        return mapping.get(int(str(raw)))
    except (TypeError, ValueError):
        return None


def parse_public_user_id(raw: object) -> str | None:
    """Canonical lowercase UUID text for a client-supplied user id, else `None` (strict)."""
    if not isinstance(raw, str) or not _UUID_FULL.fullmatch(raw.strip()):
        return None
    return str(uuid.UUID(raw.strip()))


def register_default_converter() -> None:
    """Make `uuuid` known to every Quart app in the process (idempotent, import-time)."""
    QuartMap.default_converters = ImmutableDict(
        {**QuartMap.default_converters, CONVERTER_NAME: UserUuidConverter}
    )


def install_user_public_ids(app: Quart) -> None:
    """Register the `uuuid` converter on `app` (call before blueprints).

    The id-translation hook is attached per blueprint (`use_user_public_ids`), so any app that
    mounts a `<uuuid:user_id>` blueprint translates correctly without extra wiring.
    """
    app.url_map.converters[CONVERTER_NAME] = UserUuidConverter


def use_user_public_ids(blueprint: Blueprint) -> None:
    """Make `blueprint` translate its `<uuuid:user_id>` view arg to the internal numeric id."""
    blueprint.before_request(translate_user_view_arg)
