"""v1 `ingest_sources` group -- per-community `ingest_sources` registry (spec Sec5.2, Sec10.3).

**Naming note -- NOT the same feature/path as `blueprints/v1/
community_connections.py`.** That blueprint (gh-320, mounted at
`/api/v1/communities/<id>/connections/...`) is the pre-existing, live,
webui-integrated per-community OAuth token feature (`hub_module/frontend/
src/pages/admin/CommunityConnections.jsx` calls it today) -- a fundamentally
different resource (`platform_integrations` table, stores encrypted OAuth
access/refresh tokens) solving a different problem (posting/reading *as* a
community's own linked account). This blueprint is a registry of inbound
event sources over the `ingest_sources` table, read by the Rust data
plane's ingest workers, and carries no credential of any kind. Mounted at
`/api/v1/communities/<id>/ingest-sources` -- a deliberately distinct word
("ingest-sources" not "connections") from that sibling blueprint's path
segment, so the two are never confused at the route level. See
`services/ingest_source_registry_service.py`'s own docstring for the full
reconciliation note.

**Community-scoped by product requirement.** A physical source (Twitch
channel, Discord server, webhook) can legitimately be linked to multiple
communities of the same tenant -- the approved data model is a future
`sources`/`community_connections` M:N split. Every route below is nested
under `/communities/<int:community_id>/...`; `community_id` is validated
against the caller's tenant by the SERVICE layer (`services.
ingest_source_registry_service._validate_community_tenant`, an `install_dal`
-based check -- NOT `community_common.community_in_tenant`, which assumes
the pydal `dal`'s own `communities` binding that this M2b/M2a install-dal
feature area doesn't share, per `services/bundle_install_dal.py`'s R52
convention) before touching any row -- never a bare tenant-wide listing.
See the service module's own docstring for the known, called-out gap this
creates against migration 0020's still-tenant-wide DB constraint.

Design principle (this slice): hub-api is the ONLY read-write path for
this registry -- the Rust data plane reads it from a read-only replica
later, never through this API and never writing to the table directly.
`tenant_id` is derived exclusively from the validated JWT via
`flask_core.tenancy.get_tenant_context` (`tenant_middleware` runs first on
every route below, security.md's ordering contract) -- NEVER from a path
segment or request body; `community_id` comes from the URL path and is
always validated against that same tenant before use.

Scopes follow this repo's existing `tenant` SCOPE_BUNDLES ladder
(`libs/flask_core/flask_core/auth.py`): `tenant:read` (present in the
viewer/maintainer/admin bundles) for the list route, `tenant:admin`
(admin bundle only) for every write route -- same split `workstream_usage.
py` uses for its own tenant-level admin surface.

`CreateIngestSourceRequest`/`UpdateIngestSourceRequest` both set
`__pydantic_config__ = ConfigDict(extra="forbid")` -- quart-schema's
`validate_request` routes a plain dataclass through `pydantic.TypeAdapter`,
which honors this class-level config on a stdlib dataclass, so a body
carrying a `token`/`secret`/`apiKey` (or any other unrecognized) field is a
400 before the service layer ever sees it. See `services/
ingest_source_registry_service.py`'s own module docstring for why this
feature never stores credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from pydantic import ConfigDict
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import ingest_source_registry_service as svc
from services.errors import ApiError

ingest_sources_bp = Blueprint(
    "v1_ingest_sources",
    __name__,
    url_prefix="/api/v1/communities/<int:community_id>/ingest-sources",
)

#: Query-string page size ceiling -- mirrors `usage_query_service.MAX_USAGE_PAGE_SIZE`'s
#: own bound-the-page-size rationale for a tenant-admin listing surface.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _tenant_id() -> int:
    """The caller's tenant id from the validated JWT -- never a path/body value."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware always runs first
    return cast(int, ctx.tenant_id)


@dataclass(slots=True, frozen=True)
class IngestSourceDTO:
    """A single ingest source on the wire. Never carries any secret/credential field."""

    id: int
    platform: str
    sourceId: str
    label: str
    enabled: bool
    communityId: int | None
    createdAt: str
    updatedAt: str


@dataclass(slots=True, frozen=True)
class IngestSourceMetaDTO:
    """Cursor-pagination metadata."""

    limit: int
    nextCursor: str | None


@dataclass(slots=True, frozen=True)
class IngestSourceListResponse:
    """Response DTO for `GET /api/v1/communities/<id>/ingest-sources`."""

    success: bool
    ingestSources: list[IngestSourceDTO] = field(default_factory=list)
    meta: IngestSourceMetaDTO = field(
        default_factory=lambda: IngestSourceMetaDTO(limit=0, nextCursor=None)
    )


@dataclass(slots=True, frozen=True)
class IngestSourceResponse:
    """Response DTO for a single-ingest-source result (create/update)."""

    success: bool
    ingestSource: IngestSourceDTO


@dataclass(slots=True, frozen=True)
class CreateIngestSourceRequest:
    """Request DTO for `POST /api/v1/communities/<id>/ingest-sources`.

    `communityId` is NOT a body field -- it comes from the URL path (module
    docstring: every operation is community-scoped) -- and there is
    deliberately NO token/secret/credential field: `extra="forbid"` below
    rejects one outright rather than silently ignoring it.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")

    platform: str
    sourceId: str
    label: str


@dataclass(slots=True, frozen=True)
class UpdateIngestSourceRequest:
    """Request DTO for `PATCH .../ingest-sources/<id>` -- display metadata + enable/disable."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    label: str | None = None
    enabled: bool | None = None


def _to_dto(row: Any) -> IngestSourceDTO:
    return IngestSourceDTO(
        id=row.id,
        platform=row.platform,
        sourceId=row.source_id,
        label=row.label,
        enabled=bool(row.enabled),
        communityId=row.community_id,
        createdAt=row.created_at.isoformat() if row.created_at else "",
        updatedAt=row.updated_at.isoformat() if row.updated_at else "",
    )


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ApiError("enabled must be a boolean", 422, "invalid_enabled")


def _parse_int(raw: str | None, *, field_name: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"{field_name} must be an integer", 422, f"invalid_{field_name}") from exc


@ingest_sources_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:read")  # type: ignore[untyped-decorator]
@validate_response(IngestSourceListResponse)
async def list_ingest_sources(
    community_id: int,
) -> IngestSourceListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/communities/<id>/ingest-sources` -- community-scoped, filterable, paginated."""
    tenant_id = _tenant_id()
    install_dal = _install_dal()
    try:
        limit = _parse_int(request.args.get("limit"), field_name="limit") or _DEFAULT_LIMIT
        cursor = _parse_int(request.args.get("cursor"), field_name="cursor")
        enabled = _parse_bool(request.args.get("enabled"))
        rows, next_cursor = await svc.list_ingest_sources(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            platform=request.args.get("platform"),
            enabled=enabled,
            limit=limit,
            cursor=cursor,
        )
    except ApiError as exc:
        return _err(exc)

    return IngestSourceListResponse(
        success=True,
        ingestSources=[_to_dto(row) for row in rows],
        meta=IngestSourceMetaDTO(
            limit=limit, nextCursor=str(next_cursor) if next_cursor is not None else None
        ),
    )


@ingest_sources_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(CreateIngestSourceRequest)
@validate_response(IngestSourceResponse, status_code=201)
async def create_ingest_source(
    data: CreateIngestSourceRequest, community_id: int
) -> tuple[IngestSourceResponse | dict[str, object], int]:
    """`POST /api/v1/communities/<id>/ingest-sources` -- register a source. No secret material."""
    tenant_id = _tenant_id()
    install_dal = _install_dal()
    try:
        row = await svc.create_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            platform=data.platform,
            source_id=data.sourceId,
            label=data.label,
        )
    except ApiError as exc:
        return _err(exc)
    return IngestSourceResponse(success=True, ingestSource=_to_dto(row)), 201


@ingest_sources_bp.route("/<int:ingest_source_id>", methods=["PATCH"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(UpdateIngestSourceRequest)
@validate_response(IngestSourceResponse)
async def update_ingest_source(
    data: UpdateIngestSourceRequest, community_id: int, ingest_source_id: int
) -> IngestSourceResponse | tuple[dict[str, object], int]:
    """`PATCH .../ingest-sources/<id>` -- update `label` and/or `enabled` only."""
    tenant_id = _tenant_id()
    install_dal = _install_dal()
    try:
        row = await svc.update_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            ingest_source_id=ingest_source_id,
            label=data.label,
            enabled=data.enabled,
        )
    except ApiError as exc:
        return _err(exc)
    return IngestSourceResponse(success=True, ingestSource=_to_dto(row))


@ingest_sources_bp.route("/<int:ingest_source_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
async def delete_ingest_source(community_id: int, ingest_source_id: int) -> tuple[Any, int]:
    """`DELETE .../ingest-sources/<id>` -- hard-deletes the source, disables its workstream."""
    tenant_id = _tenant_id()
    install_dal = _install_dal()
    try:
        deleted = await svc.delete_ingest_source(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            ingest_source_id=ingest_source_id,
        )
    except ApiError as exc:
        return _err(exc)
    if not deleted:
        return _err(ApiError("Ingest source not found", 404, "NOT_FOUND"))
    return "", 204


BLUEPRINTS: list[Blueprint] = [ingest_sources_bp]
