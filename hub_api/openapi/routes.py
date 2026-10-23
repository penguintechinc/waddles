"""OpenAPI document routes -- the public one, and the full one, gated identically to the API.

Two routes, two trust levels (backend.md OpenAPI):

- `GET /openapi/v1-public.json` -- unauthenticated, exactly the one path
  `spec_builder.build_public_login_spec` describes.
- `GET /openapi/v1.json` -- the complete, generated document (every
  mounted route, `quart_schema`'s own introspection), behind
  `tenant_middleware` + `require_scope` -- the SAME auth chain as any
  other protected hub-api route, not a bespoke check. This is what the
  default (disabled here -- see `app.py`'s `QuartSchema(..., openapi_path=
  None, swagger_ui_path=None, ...)`) `/openapi.json`/`/docs` routes would
  have served unauthenticated; mounting it behind auth instead is the
  entire fix.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app

from .spec_builder import build_public_login_spec

openapi_bp = Blueprint("openapi_docs", __name__, url_prefix="/openapi")


@openapi_bp.route("/v1-public.json", methods=["GET"])
async def public_login_spec() -> dict[str, Any]:
    """The minimal, unauthenticated, login-only OpenAPI document."""
    cfg = current_app.config["HUB_API_CONFIG"]
    return build_public_login_spec(title=cfg.module_name, version=cfg.module_version)


@openapi_bp.route("/v1.json", methods=["GET"])
# flask_core ships no py.typed marker and tenant_middleware/require_scope's
# inner wrappers have no return-type annotations (libs/flask_core/flask_core/
# tenancy.py, authz.py) -- mypy --strict flags every use of these decorators
# as "untyped decorator" across every consumer, not a hub-api-specific gap.
# Tracked as a known upstream issue (mem0); ignored at each of the 2 call
# sites in this scaffold rather than silencing --strict project-wide.
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:read")  # type: ignore[misc]
async def full_spec() -> dict[str, Any]:
    """The complete, auto-generated OpenAPI document -- every mounted route.

    Behind the same tenant + scope chain as `blueprints/v2/platform.py`'s
    example endpoints -- `require_scope("platform:read")` is deliberately
    the same scope, so a caller entitled to read hub-api's platform
    surface is also entitled to see what surface exists.
    """
    provider = current_app.extensions["QUART_SCHEMA"].openapi_provider
    return cast(dict[str, Any], provider.schema())


def register_openapi_docs(app: Any) -> None:
    """Mount both OpenAPI document routes onto `app`."""
    app.register_blueprint(openapi_bp)
