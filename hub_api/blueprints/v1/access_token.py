"""v1 `tokens` group -- port of Node's `tokenController.js` (PAT/CAT management).

Named `access_token.py`, not `token.py` or `tokens.py` -- this repo's
"tokens" also means metered billing/consumable tokens elsewhere
(marketplace/billing groups); PAT/CAT are auth-token credentials
entirely, hence the disambiguated module name.

Two route surfaces, matching `admin/hub_module/frontend/src/services/
api.js`'s pinned contract exactly (Node's own `routes/tokens.js` double-
mounts at both `/` and `/admin` -- see `hub_api/PORTING.md`'s port notes;
only the paths `api.js` actually calls are ported):

- `/api/v1/user/tokens/*` -- self-service PAT (own resource only,
  `tenant_middleware` only, no `require_scope` -- matches Node's
  `requireAuth`-only routes and the PORTING.md Auth-pattern table's
  self-service row).
- `/api/v1/admin/<communityId>/tokens/*` -- community-admin CAT,
  `require_scope("community.tokens:admin")` (Node's `requireCommunityAdmin`
  -- see PORTING.md's Auth pattern + the established `community.<group>:
  admin` scope convention `blueprints/v1/community_activity.py` set).

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from services import access_token_service as svc
from services.community_common import api_error, community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError

user_tokens_bp = Blueprint("v1_user_tokens", __name__, url_prefix="/api/v1/user/tokens")
community_tokens_bp = Blueprint("v1_community_tokens", __name__, url_prefix="/api/v1/admin")


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into Node's flat `{"error": "..."}` shape.

    Not `flask_core.api_utils.error_response()`'s nested `{success, error:
    {message, code, timestamp}}` envelope -- `SupportSubmitTicket.jsx`'s
    `err.response?.data?.error` (and every other consumer of these two
    controllers' errors in `frontend/src/`) reads `.error` as a plain
    string, matching `supportController.js`/`tokenController.js`'s own
    `res.status(...).json({ error: '...' })` calls verbatim.
    """
    return {"error": exc.message}, exc.status_code


def _parse_expiry(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 `expires_at` string; `None`/empty -> no expiry.

    Node accepts `expires_at` unvalidated (`expires_at || null` straight
    into the INSERT) -- input validation is mandatory at every boundary
    (security.md), so a malformed value here is a 400, not a stored
    garbage column.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(f"Invalid expires_at: {value}", 400, "BAD_REQUEST") from exc


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


# ---------------------------------------------------------------------------
# DTOs -- snake_case field names: the wire contract mirrors Node's raw
# Postgres row columns verbatim (`supportController.js`/`tokenController.js`
# serialize `result.rows[0]` directly), unlike the M1 auth group's
# camelCase DTOs -- see `hub_api/PORTING.md`'s DTO-casing section.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PatDTO:
    """PAT metadata -- no hash, no plaintext."""

    id: int
    name: str
    scope_ceiling: list[str] | None
    created_at: str | None
    last_used_at: str | None
    expires_at: str | None
    is_revoked: bool


@dataclass(slots=True, frozen=True)
class CatDTO:
    """CAT metadata -- no hash, no plaintext."""

    id: int
    name: str
    scopes: list[str]
    created_at: str | None
    last_used_at: str | None
    expires_at: str | None
    is_revoked: bool
    created_by_name: str | None


@dataclass(slots=True, frozen=True)
class ScopeDTO:
    """One `permission_scopes` catalog row."""

    scope_key: str
    display_name: str | None
    description: str | None
    category: str | None


@dataclass(slots=True, frozen=True)
class CreatePatRequest:
    """Request DTO for `POST /user/tokens/pat`."""

    name: str
    scope_ceiling: list[str] = field(default_factory=list)
    expires_at: str | None = None


@dataclass(slots=True, frozen=True)
class CreateCatRequest:
    """Request DTO for `POST /admin/<id>/tokens/cats`."""

    name: str
    scopes: list[str] = field(default_factory=list)
    expires_at: str | None = None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _pat_dto(row: Any) -> PatDTO:
    return PatDTO(
        id=row.id,
        name=row.name,
        scope_ceiling=list(row.scope_ceiling) if row.scope_ceiling else None,
        created_at=_iso(row.created_at),
        last_used_at=_iso(row.last_used_at),
        expires_at=_iso(row.expires_at),
        is_revoked=bool(row.is_revoked),
    )


def _cat_dto(row: Any, created_by_name: str | None) -> CatDTO:
    return CatDTO(
        id=row.id,
        name=row.name,
        scopes=list(row.scopes) if row.scopes else [],
        created_at=_iso(row.created_at),
        last_used_at=_iso(row.last_used_at),
        expires_at=_iso(row.expires_at),
        is_revoked=bool(row.is_revoked),
        created_by_name=created_by_name,
    )


def _scope_dto(row: Any) -> ScopeDTO:
    return ScopeDTO(
        scope_key=row.scope_key,
        display_name=row.display_name,
        description=row.description,
        category=row.category,
    )


# ---------------------------------------------------------------------------
# Self-service PAT -- `/api/v1/user/tokens/*`
# ---------------------------------------------------------------------------


@user_tokens_bp.route("/scopes", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def user_list_scopes() -> tuple[dict[str, object], int]:
    """`GET /api/v1/user/tokens/scopes` -- catalog for the PAT scope-ceiling picker."""
    dal = current_app.config["dal"]
    scopes = svc.list_scopes(dal)
    return {"scopes": [asdict(_scope_dto(s)) for s in scopes]}, 200


@user_tokens_bp.route("/pat", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_pat() -> tuple[dict[str, object], int]:
    """`GET /api/v1/user/tokens/pat` -- the caller's own PAT, scoped by their own JWT `sub`."""
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    row = svc.get_pat(dal, user_id)
    return {"pat": asdict(_pat_dto(row)) if row is not None else None}, 200


@user_tokens_bp.route("/pat", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(CreatePatRequest)
async def create_pat(data: CreatePatRequest) -> tuple[dict[str, object], int]:
    """`POST /api/v1/user/tokens/pat`."""
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    try:
        token = svc.create_pat(
            dal,
            user_id,
            name=data.name,
            scope_ceiling=data.scope_ceiling,
            expires_at=_parse_expiry(data.expires_at),
        )
    except ApiError as exc:
        return _err(exc)
    return {
        "token": token,
        "message": "Store this token securely — it will not be shown again.",
    }, 201


@user_tokens_bp.route("/pat", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
async def revoke_pat() -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/user/tokens/pat`."""
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    try:
        svc.revoke_pat(dal, user_id)
    except ApiError as exc:
        return _err(exc)
    return {"message": "PAT revoked"}, 200


# ---------------------------------------------------------------------------
# Community-admin CAT -- `/api/v1/admin/<communityId>/tokens/*`
# ---------------------------------------------------------------------------


@community_tokens_bp.route("/<int:community_id>/tokens/scopes", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.tokens:admin")  # type: ignore[misc]
async def community_list_scopes(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/tokens/scopes` -- catalog for the CAT scope picker."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    scopes = svc.list_scopes(dal)
    return {"scopes": [asdict(_scope_dto(s)) for s in scopes]}, 200


@community_tokens_bp.route("/<int:community_id>/tokens/cats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.tokens:admin")  # type: ignore[misc]
async def list_cats(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/tokens/cats`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    rows, names, quota = svc.list_cats(dal, community_id)
    tokens = [asdict(_cat_dto(r, names.get(r.created_by_user_id))) for r in rows]
    return {"tokens": tokens, "quota": quota, "used": len(tokens)}, 200


@community_tokens_bp.route("/<int:community_id>/tokens/cats", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.tokens:admin")  # type: ignore[misc]
@validate_request(CreateCatRequest)
async def create_cat(data: CreateCatRequest, community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/tokens/cats`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    try:
        token = svc.create_cat(
            dal,
            community_id,
            created_by_user_id=user_id,
            name=data.name,
            scopes=data.scopes,
            expires_at=_parse_expiry(data.expires_at),
        )
    except ApiError as exc:
        return _err(exc)
    return {
        "token": token,
        "message": "Store this token securely — it will not be shown again.",
    }, 201


@community_tokens_bp.route("/<int:community_id>/tokens/cats/<int:token_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.tokens:admin")  # type: ignore[misc]
async def revoke_cat(community_id: int, token_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/tokens/cats/<tokenId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        svc.revoke_cat(dal, community_id, token_id)
    except ApiError as exc:
        return _err(exc)
    return {"message": "CAT revoked"}, 200


BLUEPRINTS: list[Blueprint] = [user_tokens_bp, community_tokens_bp]
