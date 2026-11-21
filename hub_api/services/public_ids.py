"""Generic entity public-id support: `<puuid:...>` converter + scoped per-entity resolver.

Entity-identity rule: the numeric PK is internal; the row's `public_id` UUID is the only id that
crosses an API boundary. `community_public_id` (`cuuid`) and the user variant (`uuuid`, PR #1510)
solved this for two entities; this module generalises it to every other entity table (alembic
`xxxx_entity_public_ids` adds the columns) without forking the pattern:

1. `PublicUuidConverter` -- rules declare `<puuid:announcement_id>`; matches ONLY a dashed UUID
   (a numeric id no longer routes: uniform 404) and yields a lowercase-normalised `PublicId`.
   It subclasses the community converter so `CommunityOpenAPIProvider` already documents it as
   `string`/`format: uuid`.
2. `resolve_public_id(dal, table, uuid, *, scope)` -- resolves ONE entity UUID to its numeric id
   WITHIN the caller's tenant/community scope. A malformed UUID, an unknown UUID and a UUID owned
   by another tenant/community all return `None`: indistinguishable, never an existence oracle.
   `scope` is a required keyword -- a forgotten scope is a bug, not global access (use
   `Scope.unscoped()` for genuinely global tables such as the module catalogue).
3. `build_view_arg_translator` -- a `before_request` factory (the `uuuid` pattern) that swaps
   `PublicId` view args for the numeric id so handlers keep taking `int`. An unknown UUID becomes
   `UNKNOWN_ID` and flows into the handler's existing not-found path.
4. `lookup_public_ids` -- ONE-query numeric -> UUID mapping for response DTOs (fails loud).

`uuuid` (`services/user_public_id.py`, hub users) stays a separate converter on purpose: users
resolve through `hub_users.uuid` (the identity UUID, not a `public_id` column) and its
`before_request` hook dispatches on the `UserPublicId` marker for the fixed `user_id` param,
whereas `puuid` carries per-param table/scope targets. Both share the strict UUID shape.

Internal gRPC, DB FKs, data-plane keys and logs stay numeric.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from quart import current_app, request
from quart.routing import QuartMap
from werkzeug.datastructures import ImmutableDict

from services.community_public_id import CommunityUuidConverter

#: Converter name used in route rules: `<puuid:announcement_id>`.
CONVERTER_NAME = "puuid"

#: Numeric id substituted for an unknown UUID; real ids are positive identities.
UNKNOWN_ID = -1

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_FULL = re.compile(_UUID_RE)


class PublicId(str):
    """Marker type: a normalised entity public UUID taken from a `puuid` path param."""

    __slots__ = ()


class PublicUuidConverter(CommunityUuidConverter):
    """Matches only a dashed UUID; normalises to lowercase and tags it `PublicId`."""

    def to_python(self, value: str) -> PublicId:  # type: ignore[override]
        """Lowercase canonical UUID text (the regex already guarantees the shape)."""
        return PublicId(str(uuid.UUID(value)))


@dataclass(slots=True, frozen=True)
class Scope:
    """Which rows a resolution may see: a tenant, a community, both, or explicitly everything.

    Construction fails loud when nothing is chosen, so an omitted scope can never silently widen
    to every tenant. `tenant_id`/`community_id` are the caller's already-authorised numeric ids.
    """

    tenant_id: int | None = None
    community_id: int | None = None
    all_rows: bool = False

    def __post_init__(self) -> None:
        """Reject an empty scope and a contradictory unscoped+scoped one."""
        scoped = self.tenant_id is not None or self.community_id is not None
        if self.all_rows and scoped:
            raise ValueError("Scope: all_rows cannot be combined with tenant_id/community_id")
        if not self.all_rows and not scoped:
            raise ValueError("Scope: pass tenant_id and/or community_id, or Scope.unscoped()")

    @classmethod
    def unscoped(cls) -> Scope:
        """Explicit opt-out for globally-visible tables (the caller's authz still applies)."""
        return cls(all_rows=True)


def parse_public_id(raw: object) -> str | None:
    """Canonical lowercase UUID text for a client-supplied id, else `None` (strict dashed form)."""
    if not isinstance(raw, str) or not _UUID_FULL.fullmatch(raw.strip()):
        return None
    return str(uuid.UUID(raw.strip()))


def _table(dal: Any, table: str) -> Any:
    """The bound pydal table; fails loud when it is not bound or carries no `public_id`."""
    if table not in dal.tables:
        raise LookupError(f"table {table!r} is not bound on this DAL")
    tbl = dal[table]
    if "public_id" not in tbl.fields:
        raise LookupError(f"table {table!r} declares no public_id field")
    return tbl


def _scoped_query(tbl: Any, table: str, scope: Scope, extra: Any) -> Any:
    """AND the scope columns onto `extra`; a scope column the table lacks is a programming error."""
    query = extra
    for field, value in (("tenant_id", scope.tenant_id), ("community_id", scope.community_id)):
        if value is None:
            continue
        if field not in tbl.fields:
            raise ValueError(f"table {table!r} has no {field}; it cannot be scoped by it")
        query = query & (tbl[field] == value)
    return query


def resolve_public_id_sync(dal: Any, table: str, public_id: object, *, scope: Scope) -> int | None:
    """Numeric id of the `table` row owning `public_id` inside `scope`, else `None` (sync DAL)."""
    canonical = parse_public_id(public_id)
    if canonical is None:
        return None
    tbl = _table(dal, table)
    query = _scoped_query(tbl, table, scope, tbl.public_id == canonical)
    row = dal(query).select(tbl.id, limitby=(0, 1)).first()
    return int(row.id) if row is not None else None


async def resolve_public_id(
    dal: Any, table: str, public_id: object, *, scope: Scope, async_dal: Any = None
) -> int | None:
    """Async twin of `resolve_public_id_sync`; the identical not-found result for every miss.

    With an `async_dal` the query runs natively (`select_async`, the `lookup_user_id` pattern);
    otherwise the pydal `dal` runs in a worker thread, as the sync services already do.
    """
    canonical = parse_public_id(public_id)
    if canonical is None:
        return None
    if async_dal is None:
        return await asyncio.to_thread(resolve_public_id_sync, dal, table, canonical, scope=scope)
    tbl = _table(dal, table)
    query = _scoped_query(tbl, table, scope, tbl.public_id == canonical)
    rows = await async_dal.select_async(dal(query), tbl.id)
    return int(rows[0].id) if rows else None


async def resolve_public_id_or_unknown(
    dal: Any, table: str, public_id: object, *, scope: Scope, async_dal: Any = None
) -> int:
    """`resolve_public_id`, with `UNKNOWN_ID` standing in for a miss (view-arg translation)."""
    resolved = await resolve_public_id(dal, table, public_id, scope=scope, async_dal=async_dal)
    return UNKNOWN_ID if resolved is None else resolved


def lookup_public_ids(dal: Any, table: str, ids: Iterable[object]) -> dict[int, str]:
    """Public UUIDs for many numeric ids in ONE query (no N+1 on list responses).

    Every requested id must resolve: a missing row/UUID raises `RuntimeError` (fail loud, PII-free)
    rather than letting a response fall back to the numeric id.
    """
    wanted = sorted({int(str(i)) for i in ids})
    if not wanted:
        return {}
    tbl = _table(dal, table)
    rows = dal(tbl.id.belongs(wanted)).select(tbl.id, tbl.public_id)
    found = {int(r.id): str(r.public_id) for r in rows if r.public_id}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise RuntimeError(f"{len(missing)} {table} row(s) missing public_id: {missing[:5]}")
    return found


@dataclass(slots=True, frozen=True)
class PublicIdTarget:
    """Which table backs a view arg, and which view arg (if any) carries its community scope.

    `community_param` names an already-translated numeric community view arg (the `cuuid` hook
    runs first); `None` means the table is globally visible and resolution is unscoped.
    """

    table: str
    community_param: str | None = "community_id"


def build_view_arg_translator(
    targets: Mapping[str, PublicIdTarget],
) -> Callable[[], Awaitable[None]]:
    """`before_request` hook factory: swap each `puuid` view arg for its numeric id in place.

    Only args holding a `PublicId` (i.e. declared `<puuid:...>`) are touched. A community-scoped
    target whose community arg did not resolve to a real id (unknown UUID -> `UNKNOWN_COMMUNITY_ID`)
    resolves to `UNKNOWN_ID` without a query.
    """

    async def translate_public_view_args() -> None:
        view_args = request.view_args
        if not view_args:
            return None
        dal = current_app.config["dal"]
        async_dal = current_app.config.get("async_dal")
        for param, target in targets.items():
            raw = view_args.get(param)
            if not isinstance(raw, PublicId):
                continue
            if target.community_param is None:
                scope = Scope.unscoped()
            else:
                community_id = view_args.get(target.community_param)
                if not isinstance(community_id, int) or community_id <= 0:
                    view_args[param] = UNKNOWN_ID
                    continue
                scope = Scope(community_id=community_id)
            view_args[param] = await resolve_public_id_or_unknown(
                dal, target.table, str(raw), scope=scope, async_dal=async_dal
            )
        return None

    return translate_public_view_args


def register_default_converter() -> None:
    """Make `puuid` known to every Quart app in the process (idempotent, import-time)."""
    QuartMap.default_converters = ImmutableDict(
        {**QuartMap.default_converters, CONVERTER_NAME: PublicUuidConverter}
    )
