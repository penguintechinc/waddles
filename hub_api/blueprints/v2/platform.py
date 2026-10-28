"""v2 `core.platform` group -- the copy-me exemplar for every future port group.

Proves, end-to-end, the per-controller port pattern every other
controller in docs/plans/2026-08-31-hubapi-node-to-quart-migration.md's
checklist (§4) copies when porting a Node controller into hub-api:

    route (Quart Blueprint, path IDENTICAL to contract for v1 --
      here it's v2, additive)
    -> tenant_middleware (outermost auth decorator, tenant before scope --
      security.md Authentication & Authorization)
    -> require_scope (HTTP-layer OIDC scope check, never role names)
    -> quart-schema @validate_request (Pydantic/dataclass request DTO)
    -> quart-schema @validate_response (explicit response DTO --
      security.md Output Validation: never a raw model or **dict)

Matches the discovery contract every v2 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v2.py`'s
auto-discovery -- no edit to `routers/v2.py` needed. `url_prefix` is
already the FULL `/api/v2/...` path (discovery registers each blueprint
as-is, with no additional prefix wrapping).

Mounted at `/api/v2/core/platform/default/*` -- module=core,
surface=platform, app_bundle=default (the first-party default binding;
see flask_core/app_manifest.py), matching the v2
`/api/v2/{module}/{surface}/{app_bundle}/{target}` shape from the
migration plan §8 D2. This blueprint carries no real hub-api business
logic -- it exists to be copied, not extended.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, request
from quart_schema import validate_request, validate_response

platform_bp = Blueprint("core_platform_v2", __name__, url_prefix="/api/v2/core/platform/default")


@dataclass(slots=True, frozen=True)
class PlatformStatusResponse:
    """Response DTO for `GET .../status` -- the only fields the caller ever sees."""

    module: str
    status: str
    tenant: str


@dataclass(slots=True, frozen=True)
class PlatformEchoRequest:
    """Request DTO for `POST .../echo`, validated before the handler runs."""

    message: str


@dataclass(slots=True, frozen=True)
class PlatformEchoResponse:
    """Response DTO for `POST .../echo`."""

    echoed: str
    tenant: str


# flask_core ships no py.typed marker and tenant_middleware/require_scope's
# inner wrappers have no return-type annotations (libs/flask_core/flask_core/
# tenancy.py, authz.py) -- mypy --strict flags every use as "untyped
# decorator" across every consumer, not a hub-api-specific gap. Tracked as
# a known upstream issue (mem0); ignored at each call site in this scaffold
# -- the pattern every other ported controller will need to repeat until
# flask_core ships a py.typed marker -- rather than silencing --strict
# project-wide.
@platform_bp.route("/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:read")  # type: ignore[misc]
@validate_response(PlatformStatusResponse)
async def platform_status() -> PlatformStatusResponse:
    """Prove the tenant + scope + response-DTO chain with a trivial read."""
    ctx = get_tenant_context(request)
    # tenant_middleware already returned 401/403 and short-circuited before
    # this line if ctx were ever None -- a mypy type-narrowing aid, not a
    # runtime security control (worst case if stripped under -O: an
    # AttributeError -> 500, never an auth bypass).
    assert ctx is not None  # nosec B101
    return PlatformStatusResponse(module="hub-api", status="ok", tenant=ctx.tenant_slug)


@platform_bp.route("/echo", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:write")  # type: ignore[misc]
@validate_request(PlatformEchoRequest)
@validate_response(PlatformEchoResponse)
async def platform_echo(data: PlatformEchoRequest) -> PlatformEchoResponse:
    """Prove the request-DTO leg of the chain with a trivial write-shaped echo."""
    ctx = get_tenant_context(request)
    # Same postcondition/rationale as platform_status above.
    assert ctx is not None  # nosec B101
    return PlatformEchoResponse(echoed=data.message, tenant=ctx.tenant_slug)


BLUEPRINTS: list[Blueprint] = [platform_bp]
