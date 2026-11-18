"""Community public-id URL converter + request-time id translation.

Every community identifier that crosses the client-facing API boundary is the community's
`public_id` UUID (alembic 0084), never the enumerable numeric id. Routes declare
`<cuuid:community_id>`; this module then:

1. `CommunityUuidConverter` -- matches ONLY a dashed UUID (any case), so a numeric id no longer
   routes (uniform 404) and `to_python` hands back the lowercase-normalised value.
2. `install_community_public_ids(app)` -- registers the converter and a `before_request` hook
   that translates the UUID to the internal numeric id in `request.view_args`, so handlers and
   the authz helpers (`community_authz`, `community_access`) keep taking `community_id: int`.

Resolution is pure id translation: it does NOT filter by tenant. The existing helpers still run
exactly as before and enforce tenant/scope/membership. An UNKNOWN uuid resolves to
`UNKNOWN_COMMUNITY_ID` (a value no real row can hold) rather than raising here, so the response is
whatever the helpers already give a non-existent numeric id: identical to "not yours", never an
existence oracle (and an unauthenticated caller still gets the 401 before anything else).
Internal gRPC, DB FKs, data-plane keys and logs stay numeric.
"""

from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from typing import Any

from quart import Quart, current_app, request
from quart.routing import QuartMap
from quart_schema.openapi import OpenAPIProvider
from werkzeug.datastructures import ImmutableDict
from werkzeug.routing import BaseConverter

from services.schema import bind_auth_tables, bind_tenant_tables

#: Converter name used in route rules: `<cuuid:community_id>`.
CONVERTER_NAME = "cuuid"

#: Path param name the preprocessor translates.
COMMUNITY_PARAM = "community_id"

#: Numeric id substituted for an unknown UUID; real ids are positive BIGINT identities.
UNKNOWN_COMMUNITY_ID = -1

#: Cap for the immutable UUID -> numeric id cache (a mapping never changes once created).
CACHE_MAX_ENTRIES = 10_000

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_FULL = re.compile(_UUID_RE)


class CommunityPublicId(str):
    """Marker type: a normalised community public UUID taken from a `cuuid` path param."""

    __slots__ = ()


class CommunityUuidConverter(BaseConverter):
    """Matches only a dashed UUID; normalises to lowercase."""

    regex = _UUID_RE
    part_isolating = True

    def to_python(self, value: str) -> CommunityPublicId:
        """Lowercase canonical UUID text (the regex already guarantees the shape)."""
        return CommunityPublicId(str(uuid.UUID(value)))

    def to_url(self, value: Any) -> str:
        """Render a UUID for `url_for`; anything that is not a UUID is a programming error."""
        return str(uuid.UUID(str(value)))


class CommunityOpenAPIProvider(OpenAPIProvider):
    """quart-schema provider documenting `cuuid` path params as `string`/`format: uuid`."""

    def build_path_parameter(self, name: str, converter: BaseConverter) -> dict[str, Any]:
        """Path parameter object; `cuuid` is a UUID string, everything else is stock."""
        parameter = super().build_path_parameter(name, converter)
        if isinstance(converter, CommunityUuidConverter):
            parameter["schema"] = {"type": "string", "format": "uuid"}
        return parameter


class _IdCache:
    """Bounded LRU of public UUID -> numeric community id (positive results only)."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, int] = OrderedDict()

    def get(self, key: str) -> int | None:
        """Cached numeric id (refreshing recency) or `None`."""
        found = self._data.get(key)
        if found is not None:
            self._data.move_to_end(key)
        return found

    def put(self, key: str, value: int) -> None:
        """Store a mapping, evicting the least recently used entry beyond the cap."""
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """Drop everything (tests)."""
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


ID_CACHE = _IdCache()


async def lookup_community_id(public_id: str) -> int:
    """Numeric id for a canonical community UUID; `UNKNOWN_COMMUNITY_ID` when no row has it."""
    cached = ID_CACHE.get(public_id)
    if cached is not None:
        return cached
    async_dal = current_app.config["async_dal"]
    dal = current_app.config["dal"]
    if "communities" not in dal.tables:
        bind_auth_tables(dal)
    elif "public_id" not in dal.communities.fields:
        # `communities` was first bound without `public_id`; the tenant binder extends it
        # (idempotent, in-memory only) -- same guard as `data_privacy_service`.
        bind_tenant_tables(dal)
    rows = await async_dal.select_async(
        dal(dal.communities.public_id == public_id), dal.communities.id
    )
    if not rows:
        return UNKNOWN_COMMUNITY_ID
    resolved = int(rows[0].id)
    ID_CACHE.put(public_id, resolved)
    return resolved


async def translate_community_view_arg() -> None:
    """`before_request`: swap a `cuuid` view arg for the internal numeric id in place."""
    view_args = request.view_args
    if not view_args:
        return None
    raw = view_args.get(COMMUNITY_PARAM)
    if isinstance(raw, CommunityPublicId):
        view_args[COMMUNITY_PARAM] = await lookup_community_id(str(raw))
    return None


def register_default_converter() -> None:
    """Make `cuuid` known to every Quart app in the process (idempotent, import-time).

    `blueprints/__init__.py` calls this so any app that registers a v1 blueprint (the real app
    AND bare per-blueprint test apps) can parse `<cuuid:...>` rules. It only teaches the router
    the URL shape; the id-translation hook still needs `install_community_public_ids`.
    """
    QuartMap.default_converters = ImmutableDict(
        {**QuartMap.default_converters, CONVERTER_NAME: CommunityUuidConverter}
    )


def install_community_public_ids(app: Quart) -> None:
    """Register the `cuuid` converter and the id-translation hook (call before blueprints)."""
    app.url_map.converters[CONVERTER_NAME] = CommunityUuidConverter
    app.before_request(translate_community_view_arg)
