"""v1 super-admin bundle management -- `/api/v1/superadmin/bundles` (GLOBAL tier, `platform:admin`).

`GET ?kind=app|service` lists installed bundles (global current installs) for the two
super-admin pages; `POST /<app_id>/disable|enable` toggles a SERVICE bundle through the
existing lifecycle (`services/bundle_global_state_service.py`). Disabling a bundle that other
installed bundles request tools from is refused with a typed 409 unless `?force=true`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import bundle_global_state_service as svc
from services.bundle_telemetry import bundle_span
from services.current_user import get_current_user_id
from services.errors import ApiError

bundle_superadmin_bp = Blueprint(
    "v1_bundle_superadmin", __name__, url_prefix="/api/v1/superadmin/bundles"
)

_KINDS = ("app", "service")


@dataclass(slots=True, frozen=True)
class InstalledBundleDTO:
    """One installed bundle row."""

    appId: str  # noqa: N815 - camelCase is the wire contract
    name: str
    kind: str
    version: str
    digestShort: str | None  # noqa: N815
    installedAt: str | None  # noqa: N815
    installSource: str  # noqa: N815
    status: str


@dataclass(slots=True, frozen=True)
class InstalledBundlesResponse:
    """Response DTO for `GET /api/v1/superadmin/bundles`."""

    success: bool
    bundles: list[InstalledBundleDTO]


@dataclass(slots=True, frozen=True)
class BundleStateResponse:
    """Response DTO for disable/enable; `changed` is false on an idempotent repeat."""

    success: bool
    appId: str  # noqa: N815
    status: str
    changed: bool
    tenantsHidden: int  # noqa: N815


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, Any], int]:
    return cast(
        tuple[dict[str, Any], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else cast("str | None", value)


@bundle_superadmin_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(InstalledBundlesResponse)
async def list_bundles() -> InstalledBundlesResponse | tuple[dict[str, Any], int]:
    """Installed bundles, optionally filtered by `?kind=app|service`."""
    kind = request.args.get("kind")
    if kind is not None and kind not in _KINDS:
        return cast(
            tuple[dict[str, Any], int],
            error_response("kind must be 'app' or 'service'", 400, "invalid_kind"),
        )
    rows = await svc.list_installed(_install_dal(), kind=kind)
    return InstalledBundlesResponse(
        success=True,
        bundles=[
            InstalledBundleDTO(
                appId=r.app_id,
                name=r.name,
                kind=r.kind,
                version=r.version,
                digestShort=svc.short_digest(r.digest),
                installedAt=_iso(r.installed_at),
                installSource=r.install_source,
                status=r.status,
            )
            for r in rows
        ],
    )


@bundle_superadmin_bp.route("/<app_id>/disable", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(BundleStateResponse)
async def post_disable(app_id: str) -> BundleStateResponse | tuple[dict[str, Any], int]:
    """Disable a service bundle globally; 409 `has_dependents` unless `?force=true`."""
    force = request.args.get("force", "").lower() == "true"
    try:
        async with bundle_span("bundle_superadmin.disable", app_id=app_id, force=force):
            changed, hidden = await svc.disable_service(
                _install_dal(), app_id=app_id, actor_id=get_current_user_id(request), force=force
            )
    except ApiError as exc:
        return _err(exc)
    return BundleStateResponse(
        success=True,
        appId=app_id,
        status=svc.STATUS_DISABLED,
        changed=changed,
        tenantsHidden=hidden,
    )


@bundle_superadmin_bp.route("/<app_id>/enable", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(BundleStateResponse)
async def post_enable(app_id: str) -> BundleStateResponse | tuple[dict[str, Any], int]:
    """Re-enable a disabled service bundle globally (idempotent)."""
    try:
        async with bundle_span("bundle_superadmin.enable", app_id=app_id):
            changed = await svc.enable_service(
                _install_dal(), app_id=app_id, actor_id=get_current_user_id(request)
            )
    except ApiError as exc:
        return _err(exc)
    return BundleStateResponse(
        success=True, appId=app_id, status=svc.STATUS_ACTIVE, changed=changed, tenantsHidden=0
    )


BLUEPRINTS: list[Blueprint] = [bundle_superadmin_bp]
