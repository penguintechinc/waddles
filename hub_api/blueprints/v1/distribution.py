"""v1 `distribution` group -- serves the active bundle set to stage-runners.

`GET /api/v1/distribution/bundles?stage={ingest|process|action}[&community_id=]`
is what `core/svc_ingest`/`core/svc_process`'s poll loop
(`flask_core.stage_runner.BundlePoller`) calls to learn which App Bundles
are installed+activated for its stage, at a (tenant, community). Real query
against `app_catalog`/`app_activations`/`app_tenant_availability`
(migrations 069/071) via `services.distribution_service`, from the read
replica when one is configured (backend.md Database Tier Architecture --
this is the exact high-frequency routing-read case
`flask_core.app_installations_db.DBInstallationLookup`'s own docstring
calls out).

Auth: `@tenant_middleware` (tenant strictly from the caller's own JWT
`tenant` claim, never a query/body param -- security.md Tenant Isolation)
+ `@require_scope("distribution:read")`. `community_id` IS accepted as a
query param -- unlike tenant, community is not carried on the JWT `teams`
claim in a form this endpoint can resolve, and per-community activations
are the whole point of the 3-tier model (migration 069); omitting it
resolves the tenant-wide `app_tenant_availability` fallback only. A
stage-runner's own service JWT is minted with the `tenant` claim it's
scoped to (its `RUNNER_TENANT_SLUG` config) -- this endpoint never lets a
caller widen scope beyond its own token's tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import distribution_service as svc
from services.errors import ApiError
from services.schema import bind_app_bundle_tables

distribution_bp = Blueprint("v1_distribution", __name__, url_prefix="/api/v1/distribution")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, read_dal)` -- the read-replica connection when configured, else primary.

    Matches `flask_core.app_installations_db.DBInstallationLookup`'s own
    read-replica-is-the-caller's-job contract: `AsyncDAL.read_dal` is a
    second, independent `pydal.DAL` connection (see
    `flask_core.database.AsyncDAL.__init__`) -- `bind_app_bundle_tables`
    must run against whichever connection this function returns, or pydal
    has no `Table` object to build a query against on that connection.
    """
    async_dal = current_app.config["async_dal"]
    read_dal = async_dal.read_dal if async_dal.read_dal is not None else async_dal.dal
    return async_dal, read_dal


def _ensure_tables(dal: Any) -> None:
    """Idempotently bind `app_catalog`/`app_tenant_availability`/`app_activations` on `dal`.

    `app.py` is frozen for this port (`hub_api/PORTING.md`'s auto-discovery
    contract) -- same lazy-bind pattern as `data_privacy.py`/
    `cookie_consent.py`'s own `before_request` hooks. Runs on BOTH the
    primary (`async_dal.dal`, always) and the read-replica connection
    (`async_dal.read_dal`, when configured) so `_dal()`'s read_dal branch
    always has bound tables to query against.
    """
    bind_app_bundle_tables(dal)


@distribution_bp.before_request
async def _bind_tables_before_request() -> None:
    """Bind on both connections before any route body runs -- see `_ensure_tables`."""
    async_dal = current_app.config["async_dal"]
    _ensure_tables(async_dal.dal)
    if async_dal.read_dal is not None:
        _ensure_tables(async_dal.read_dal)


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _parse_community_id(raw: str | None) -> int | None:
    """Parse the optional `community_id` query param; raises `ApiError` on a non-int value."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(
            f"community_id {raw!r} must be an integer", 400, "INVALID_COMMUNITY_ID"
        ) from exc


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DistributionBundleDTO:
    """One bundle's `{entrypoint, config, spec}` for the requested stage."""

    appId: str
    communityId: int | None
    entrypoint: str | None
    spec: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DistributionMetaDTO:
    """Response metadata -- backend.md API response format's `meta` block."""

    version: int
    timestamp: str


@dataclass(slots=True, frozen=True)
class DistributionBundlesResponse:
    """Response DTO for `GET /api/v1/distribution/bundles`."""

    success: bool
    stage: str
    bundles: list[DistributionBundleDTO]
    meta: DistributionMetaDTO


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@distribution_bp.route("/bundles", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("distribution:read")  # type: ignore[misc]
@validate_response(DistributionBundlesResponse)
async def list_distribution_bundles() -> (
    DistributionBundlesResponse | tuple[dict[str, object], int]
):
    """List every enabled, activated bundle implementing `stage` for the caller's tenant."""
    stage = request.args.get("stage", "")
    if stage not in svc.BUNDLE_STAGES:
        return _err(
            ApiError(
                f"stage must be one of {svc.BUNDLE_STAGES}, got {stage!r}",
                400,
                "INVALID_STAGE",
            )
        )

    try:
        community_id = _parse_community_id(request.args.get("community_id"))
    except ApiError as exc:
        return _err(exc)

    ctx = get_tenant_context(request)
    assert ctx is not None  # tenant_middleware always publishes this on the success path

    async_dal, read_dal = _dal()
    rows = await svc.list_bundles_for_stage(
        async_dal, read_dal, tenant_id=ctx.tenant_id, community_id=community_id, stage=stage
    )

    return DistributionBundlesResponse(
        success=True,
        stage=stage,
        bundles=[
            DistributionBundleDTO(
                appId=row.app_id,
                communityId=row.community_id,
                entrypoint=row.entrypoint,
                spec=row.spec,
                config=row.config,
            )
            for row in rows
        ],
        meta=DistributionMetaDTO(version=1, timestamp=datetime.now(UTC).isoformat()),
    )


BLUEPRINTS: list[Blueprint] = [distribution_bp]
