"""Public-UUID plumbing shared by the marketplace / billing / module route group.

Every marketplace entity that used to cross the API as a numeric row id (discount code, module,
vendor module, submission, vendor submission) is now addressed by its `public_id` UUID
(`<puuid:...>` path params, UUID strings in request bodies and responses). This module is the thin
layer over `services.public_ids` those blueprints share:

* `marketplace_view_arg_hook` -- a `before_request` hook factory that binds the tables the group
  queries (idempotent) and then runs the generic `build_view_arg_translator`, so a handler keeps
  taking the numeric id while a malformed / unknown / foreign UUID becomes `UNKNOWN_ID` and flows
  into the handler's existing 404 path (a numeric id no longer routes at all: uniform 404).
* `body_id` -- resolve a client-supplied UUID from a request body (unknown/malformed -> `None`).
* `public_id_for` / `public_ids_for` -- numeric -> UUID for response DTOs (fail loud when missing).

The marketplace catalogue tables (`hub_modules`, `marketplace_modules`, submissions) are globally
visible/admin-owned, so resolution is `Scope.unscoped()`; ownership (vendor, community admin) is
still enforced by each handler exactly as before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from quart import current_app

from services.public_ids import (
    PublicIdTarget,
    Scope,
    build_view_arg_translator,
    lookup_public_ids,
    resolve_public_id_sync,
)


def global_target(table: str) -> PublicIdTarget:
    """`PublicIdTarget` for a table resolved without a tenant/community fence."""
    return PublicIdTarget(table, community_param=None)


def marketplace_view_arg_hook(
    targets: Mapping[str, PublicIdTarget],
    *binders: Callable[[Any], None],
) -> Callable[[], Awaitable[None]]:
    """`before_request` hook: run the idempotent `binders` on the DAL, then translate view args."""
    inner = build_view_arg_translator(targets)

    async def hook() -> None:
        dal = current_app.config["dal"]
        for binder in binders:
            binder(dal)
        await inner()

    return hook


def body_id(dal: Any, table: str, raw: object) -> int | None:
    """Numeric id of the `table` row whose `public_id` is `raw`, else `None` (unscoped, sync)."""
    return resolve_public_id_sync(dal, table, raw, scope=Scope.unscoped())


def public_ids_for(dal: Any, table: str, ids: Iterable[int | None]) -> dict[int, str]:
    """Public UUIDs for many numeric ids in one query (`None` ids are skipped)."""
    return lookup_public_ids(dal, table, [i for i in ids if i is not None])


def public_id_for(dal: Any, table: str, row_id: int) -> str:
    """Public UUID of one row (fail loud when it has none)."""
    return public_ids_for(dal, table, [row_id])[int(row_id)]
