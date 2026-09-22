"""v1 `bundle_versions` group -- POST/GET /api/v1/apps/{app_id}/versions (spec Sec9.2).

Global tier (`platform:admin`), same authorization level as the
existing `marketplace_lifecycle.py::install_bundle`. GET is open to any
authenticated tenant member.

R52 (coordinator ruling): `platform_settings`/`app_version_uploads` are
this milestone's own new tables, queried through
`current_app.config["install_dal"]` (penguin-dal).

**Scope note.** `allow_wildcard_consumes` and per-tenant custom
platforms (spec Decision #8, Sec10.4) are not wired here -- every
upload is validated with `allow_wildcard_consumes=False` and an empty
`known_custom_platforms` set, matching this milestone's realistic
scope (see `services.bundle_version_service`'s own docstring for the
launch-a-compiler-Job scope cut this file inherits). Widening either
is a follow-on once `tenant_bundle_settings`/`custom_platform_service`
land.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import bundle_version_service as svc
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request

bundle_versions_bp = Blueprint("v1_bundle_versions", __name__, url_prefix="/api/v1/apps")


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


async def _allow_prebuilt(install_dal: AsyncDB) -> bool:
    """Query the global `bundles.allow_prebuilt` setting (spec Sec9.2, Sec12.3). Defaults true."""
    rows = await install_dal(install_dal.platform_settings.key == "bundles.allow_prebuilt").select()
    row = rows.first()
    return (row.value if row is not None else "true") == "true"


@dataclass(slots=True, frozen=True)
class CreateVersionResponse:
    """Response DTO for `POST /apps/{app_id}/versions`."""

    success: bool
    versionId: int
    status: str


@dataclass(slots=True, frozen=True)
class VersionDTO:
    """Response DTO for `GET /apps/{app_id}/versions/{version}`."""

    success: bool
    versionId: int
    appId: str
    version: str
    status: str
    rejectReason: str | None
    scanStatus: str | None
    artifactDigest: str | None


@bundle_versions_bp.route("/<app_id>/versions", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("platform:admin")  # type: ignore[untyped-decorator]
async def post_version(app_id: str) -> tuple[dict[str, object], int]:
    """Upload a new bundle version (multipart: `manifest` + `source` XOR `component`)."""
    install_dal = _install_dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    files = await request.files
    manifest_file = files.get("manifest")
    if manifest_file is None:
        return _err(bad_request("manifest part is required"))
    manifest_bytes = manifest_file.read()
    source_file = files.get("source")
    component_file = files.get("component")
    source_bytes = source_file.read() if source_file is not None else None
    component_bytes = component_file.read() if component_file is not None else None

    caller_id = get_current_user_id(request)
    try:
        row = await svc.create_version(
            install_dal,
            tenant_id=ctx.tenant_id,
            app_id=app_id,
            requested_by=caller_id,
            manifest_bytes=manifest_bytes,
            source_bytes=source_bytes,
            component_bytes=component_bytes,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=await _allow_prebuilt(install_dal),
        )
    except ApiError as exc:
        return _err(exc)
    return (
        {"success": True, "versionId": row.id, "status": row.status},
        202,
    )


@bundle_versions_bp.route("/<app_id>/versions", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def list_versions_route(app_id: str) -> dict[str, object]:
    """Every uploaded version of `app_id`, newest first."""
    install_dal = _install_dal()
    rows = await svc.list_versions(install_dal, app_id=app_id)
    return {
        "success": True,
        "versions": [{"versionId": r.id, "version": r.version, "status": r.status} for r in rows],
    }


@bundle_versions_bp.route("/<app_id>/versions/<version>", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(VersionDTO)
async def get_version(app_id: str, version: str) -> VersionDTO | tuple[dict[str, object], int]:
    """The state-machine state, reject reason (if any), scan status and digest for one version."""
    install_dal = _install_dal()
    try:
        row = await svc.get_version(install_dal, app_id=app_id, version=version)
    except ApiError as exc:
        return _err(exc)
    scan_status = None
    artifact_digest = None
    if row.app_version_id is not None:
        published_rows = await install_dal(
            install_dal.app_versions.id == row.app_version_id
        ).select()
        published = published_rows.first()
        if published is not None:
            scan_status = published.scan_status
            artifact_digest = published.artifact_digest
    return VersionDTO(
        success=True,
        versionId=row.id,
        appId=row.app_id,
        version=row.version,
        status=row.status,
        rejectReason=row.reject_reason,
        scanStatus=scan_status,
        artifactDigest=artifact_digest,
    )


BLUEPRINTS: list[Blueprint] = [bundle_versions_bp]
