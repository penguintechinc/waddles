"""Tenant `ingest_sources` registry -- read-write CRUD (spec Sec5.2, Sec10.3).

**Naming note -- NOT the same feature as `services/community_connections.py`.**
That module (gh-320) is a *different*, pre-existing, webui-integrated
feature: a per-COMMUNITY OAuth token store (`platform_integrations` table,
`integration_type='community_oauth'`) letting a community admin link the
platform's own shared OAuth app (YouTube/Spotify/Twitch/Discord/Kick/Slack)
so hub-api can post/read *as* that community -- it necessarily stores
encrypted access/refresh tokens, and its blueprint (`blueprints/v1/
community_connections.py`) is mounted at `/api/v1/communities/<id>/
connections/...`, consumed today by `hub_module/frontend/src/pages/admin/
CommunityConnections.jsx`. This module is a per-TENANT registry of inbound
event sources (spec Sec10.3) -- "which platform, which external
`source_id`, on/off" -- read by the Rust data plane's ingest workers,
never a credential store, and deliberately does NOT reuse
`community_connections.py`'s table, blueprint, or URL space: unifying the
two would either strip token storage from the live OAuth feature (breaking
`CommunityConnections.jsx`) or add credential columns to this registry
(violating this slice's own "no credential fields" requirement). The two
were kept as separate resources on separate paths precisely to avoid that
conflict -- see `blueprints/v1/ingest_sources.py`'s own docstring for the
same note from the route side.

**Design principle (this slice).** hub-api is the ONLY read-write path for
this registry; the Rust data plane reads it from a read-only replica later
(never queries this table directly, never writes to it). Every function
here is tenant-scoped by an already-validated `tenant_id` (the caller --
`blueprints/v1/ingest_sources.py` -- derives it exclusively from
`flask_core.tenancy.get_tenant_context`, never a path/body value) and reads/
writes through the penguin-dal `install_dal: AsyncDB`
(`services/bundle_install_dal.py`), same `ingest_sources` table `services/
ingest_source_service.py` owns.

**Relationship to `ingest_source_service.py`.** That module (M2b Task 26/39)
owns the generic-webhook-intake feature: every source it creates carries an
HMAC signing secret, shown once, encrypted at rest. This module is a
different, narrower feature over the SAME `ingest_sources` table -- a plain
registry entry (which platform, which external `source_id`, on/off) with NO
secret material at all. A client request is not permitted to carry any
credential field (`CreateIngestSourceRequest`/`UpdateIngestSourceRequest` in
the blueprint both set `__pydantic_config__ = ConfigDict(extra="forbid")`
so an extra `token`/`secret`/`apiKey` key is a 400, never silently dropped
or stored); every row this module creates has `secret_ciphertext`/
`secret_iv` NULL. Credentials/secret-refs for this surface are an
explicitly later design (not this slice).

Reuses `services/workstream_service.py`'s `create_workstream_for_source`
(its own docstring: "the standalone, idempotent helper for any caller other
than `create_source()` itself") and `disable_workstream_for_source` rather
than duplicating the 1:1 workstream lifecycle -- every entry this module
creates gets the same 1:1 workstream `ingest_source_service.create_source()`
creates, just without a secret.

**Community-scoped by product requirement, NOT by the current DB schema.**
The approved data model (per-tenant-admin decision, not yet migrated) is a
physical `sources` table with a `community_connections` M:N join -- one
Twitch channel/Discord server/webhook can legitimately be linked to
multiple communities of the same tenant (a streamer + their team + a
sponsor community, each with its own future per-link config). Every
create/list/update/delete in this module is scoped to a caller-supplied,
tenant-validated `community_id` in anticipation of that model. **However**
migration 0020's actual constraint is still tenant-wide -- `UNIQUE
(tenant_id, platform, source_id)`, no `community_id` column in the index
at all -- so today, registering the same `(platform, source_id)` under a
*second* community of the same tenant still fails at the DB layer.
`create_ingest_source` does NOT work around this (no source_id mangling,
no fake per-community suffix): it lets the `IntegrityError` surface as a
clear, documented 409 (`SOURCE_LINKED_TO_ANOTHER_COMMUNITY`) explaining
the limitation, rather than silently succeeding with a schema that can't
yet represent the requested state. This is a known, called-out gap that
closes when the `sources`/`community_connections` split migration lands
-- not a defect in this module.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB
from sqlalchemy.exc import IntegrityError

from services.bundle_telemetry import bundle_span
from services.errors import ApiError, bad_request, conflict, not_found, unprocessable
from services.workstream_service import (
    create_workstream_for_source,
    disable_workstream_for_source,
)

logger = logging.getLogger(__name__)

#: Platforms this registry accepts -- matches `services/community_activity.
#: py`'s `_VALID_PLATFORMS` (this repo's existing platform-name convention),
#: minus "hub" (an internal pseudo-platform for first-party bot events, not
#: an external ingest source a tenant admin registers here).
SUPPORTED_PLATFORMS = frozenset({"discord", "twitch", "kick", "youtube", "slack"})

#: VARCHAR(255) column ceiling (migration 0020) -- enforced here too so a
#: too-long value is a clean 422, not a truncated write or a DB-level error.
_SOURCE_ID_MAX_LEN = 255
_LABEL_MAX_LEN = 255

#: Conservative safe charset for an externally-sourced platform identifier
#: (channel id, guild id, workspace id, ...) -- alnum plus the handful of
#: separators every platform's own id format actually uses. Never used to
#: build a query string directly (penguin-dal parameterizes every value
#: below); this is a defense-in-depth bounds/shape check, not the injection
#: defense itself.
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


def _validate_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise unprocessable(f"Unsupported platform: {platform!r}")


def _validate_source_id(source_id: str) -> None:
    if not _SOURCE_ID_RE.match(source_id):
        raise unprocessable(
            "source_id must be 1-255 characters of letters, digits, '.', '_', ':' or '-'"
        )


def _validate_label(label: str) -> None:
    if not label or len(label) > _LABEL_MAX_LEN:
        raise unprocessable(f"label must be 1-{_LABEL_MAX_LEN} characters")


async def _validate_community_tenant(
    install_dal: AsyncDB, *, community_id: int, tenant_id: int
) -> None:
    """Refuse a `community_id` that does not exist or belongs to a different tenant.

    404 (not 403) deliberately masks whether the community exists at all
    outside the caller's tenant -- same IDOR-masking rationale as
    `bundle_approval_service._validate_community_tenant`, this module's own
    equivalent since that one is private to its own module.
    """
    rows = await install_dal(
        (install_dal.communities.id == community_id)
        & (install_dal.communities.tenant_id == tenant_id)
    ).select()
    if rows.first() is None:
        raise not_found("Community not found")


async def create_ingest_source(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    platform: str,
    source_id: str,
    label: str,
) -> Any:
    """Register a new ingest source for `community_id`. No secret material is ever stored.

    `community_id` is REQUIRED and validated against `tenant_id` -- every
    operation in this module is community-scoped by product requirement
    (module docstring). Duplicate detection is community-scoped
    `(tenant_id, community_id, platform, source_id)`, matching that intent
    -- but migration 0020's real constraint is still tenant-wide (`UNIQUE
    (tenant_id, platform, source_id)`, no `community_id`), so linking the
    same source to a *second* community of the same tenant still raises an
    `IntegrityError` at the DB layer; caught below and surfaced as a clear
    409 rather than worked around (module docstring).

    Creates the source's 1:1 `workstreams` row via `workstream_service.
    create_workstream_for_source` (idempotent) immediately after the insert.
    """
    async with bundle_span("hub.ingest_sources.create", tenant_id=tenant_id, platform=platform):
        _validate_platform(platform)
        _validate_source_id(source_id)
        _validate_label(label)
        await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )

        existing = await install_dal(
            (install_dal.ingest_sources.tenant_id == tenant_id)
            & (install_dal.ingest_sources.community_id == community_id)
            & (install_dal.ingest_sources.platform == platform)
            & (install_dal.ingest_sources.source_id == source_id)
        ).select()
        if existing.first() is not None:
            raise conflict(
                f"An ingest source for platform {platform!r} / source {source_id!r} "
                "already exists in this community"
            )

        now = datetime.now(UTC)
        try:
            new_id = await install_dal.ingest_sources.async_insert(
                tenant_id=tenant_id,
                community_id=community_id,
                platform=platform,
                source_id=source_id,
                label=label,
                secret_ciphertext=None,
                secret_iv=None,
                mapping=None,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        except IntegrityError as exc:
            # Real DB constraint is tenant-wide (see docstring) -- this source
            # is already linked to a DIFFERENT community of this tenant. Not
            # a bug to hack around here; closes with the sources/
            # community_connections schema split.
            raise ApiError(
                f"Platform {platform!r} / source {source_id!r} is already linked to another "
                "community in this tenant -- one physical source per multiple communities "
                "is not yet supported by the current schema",
                409,
                "SOURCE_LINKED_TO_ANOTHER_COMMUNITY",
            ) from exc
        await create_workstream_for_source(
            install_dal,
            ingest_source_id=new_id,
            tenant_id=tenant_id,
            community_id=community_id,
            platform=platform,
            source_id=source_id,
        )
        row = (await install_dal(install_dal.ingest_sources.id == new_id).select()).first()
        logger.info(
            "ingest_source.created", extra={"tenant_id": tenant_id, "ingest_source_id": new_id}
        )
        return row


async def get_ingest_source(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int, ingest_source_id: int
) -> Any | None:
    """The `ingest_sources` row for `ingest_source_id`, or `None` if absent/wrong tenant."""
    rows = await install_dal(
        (install_dal.ingest_sources.id == ingest_source_id)
        & (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.community_id == community_id)
    ).select()
    return rows.first()


async def list_ingest_sources(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    platform: str | None = None,
    enabled: bool | None = None,
    limit: int = _DEFAULT_LIST_LIMIT,
    cursor: int | None = None,
) -> tuple[list[Any], int | None]:
    """Tenant+community-scoped, filtered, cursor-paginated ingest source list.

    `community_id` is REQUIRED (module docstring: every operation here is
    community-scoped by product requirement). Cursor is the last-seen row
    `id` (ascending, opaque to the caller); returns `(rows, next_cursor)` --
    `next_cursor` is `None` once the last page has been reached. Scales to
    hundreds of tenants: always an indexed-equality tenant+community filter
    plus a keyset (`id > cursor`) predicate, never an OFFSET that grows
    linearly with page depth.
    """
    if limit < 1 or limit > _MAX_LIST_LIMIT:
        raise bad_request(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    await _validate_community_tenant(install_dal, community_id=community_id, tenant_id=tenant_id)

    query = (install_dal.ingest_sources.tenant_id == tenant_id) & (
        install_dal.ingest_sources.community_id == community_id
    )
    if platform is not None:
        query &= install_dal.ingest_sources.platform == platform
    if enabled is not None:
        query &= install_dal.ingest_sources.enabled == enabled
    if cursor is not None:
        query &= install_dal.ingest_sources.id > cursor

    rows = list(
        await install_dal(query).select(
            orderby=install_dal.ingest_sources.id,
            limitby=(0, limit + 1),
        )
    )
    next_cursor: int | None = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1].id
        rows = rows[:limit]
    return rows, next_cursor


async def update_ingest_source(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    ingest_source_id: int,
    label: str | None = None,
    enabled: bool | None = None,
) -> Any:
    """Update display metadata (`label`) and/or `enabled` on an ingest source. 404 if absent.

    `community_id` is REQUIRED -- 404 (not just tenant-scoped) if the source
    exists but belongs to a different community, same IDOR-masking
    rationale as `_validate_community_tenant`.

    Disabling also disables the source's workstream (never deletes it --
    same convention `ingest_source_service.delete_source` already
    established). Re-enabling a previously-disabled workstream is out of
    this slice's scope -- `workstream_service` has no re-enable primitive
    yet; the `ingest_sources.enabled` flag itself always reflects the
    caller's latest request.
    """
    span_attrs = {"tenant_id": tenant_id, "ingest_source_id": ingest_source_id}
    async with bundle_span("hub.ingest_sources.update", **span_attrs):
        await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )
        row = await get_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            ingest_source_id=ingest_source_id,
        )
        if row is None:
            raise not_found(f"Ingest source {ingest_source_id} not found")

        updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if label is not None:
            _validate_label(label)
            updates["label"] = label
        if enabled is not None:
            updates["enabled"] = enabled

        await install_dal(install_dal.ingest_sources.id == ingest_source_id).update(**updates)
        if enabled is False:
            await disable_workstream_for_source(install_dal, ingest_source_id=ingest_source_id)

        logger.info(
            "ingest_source.updated",
            extra={"tenant_id": tenant_id, "ingest_source_id": ingest_source_id},
        )
        return await get_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            ingest_source_id=ingest_source_id,
        )


async def delete_ingest_source(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int, ingest_source_id: int
) -> bool:
    """Remove an ingest source by id. `False` if absent (or owned by another tenant/community).

    `community_id` is REQUIRED -- same IDOR-masking rationale as
    `update_ingest_source`. Hard-deletes the `ingest_sources` row after
    disabling (never deleting) its workstream -- same order/rationale as
    `ingest_source_service.delete_source`: `workstream_usage_hourly` keeps
    its FK target for the life of the tenant's usage history.
    """
    span_attrs = {"tenant_id": tenant_id, "ingest_source_id": ingest_source_id}
    async with bundle_span("hub.ingest_sources.delete", **span_attrs):
        await _validate_community_tenant(
            install_dal, community_id=community_id, tenant_id=tenant_id
        )
        row = await get_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            ingest_source_id=ingest_source_id,
        )
        if row is None:
            return False
        await disable_workstream_for_source(install_dal, ingest_source_id=ingest_source_id)
        await install_dal(
            (install_dal.ingest_sources.id == ingest_source_id)
            & (install_dal.ingest_sources.tenant_id == tenant_id)
            & (install_dal.ingest_sources.community_id == community_id)
        ).delete()
        logger.info(
            "ingest_source.deleted",
            extra={"tenant_id": tenant_id, "ingest_source_id": ingest_source_id},
        )
        return True


__all__ = [
    "ApiError",
    "SUPPORTED_PLATFORMS",
    "create_ingest_source",
    "delete_ingest_source",
    "get_ingest_source",
    "list_ingest_sources",
    "update_ingest_source",
]
