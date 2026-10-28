"""v1 `support` group -- port of Node's `supportController.js` (support-ticket system).

Two route surfaces, matching `admin/hub_module/frontend/src/services/
api.js`'s pinned contract and `routes/support.js`'s own admin/member split:

- Admin (`AdminSupportDashboard.jsx`/`AdminSupportTicketDetail.jsx`):
  category CRUD, ticket triage (list/get/status/assign/priority/comment),
  stats -- `tenant_middleware` + `require_scope("community.support:admin")`,
  matching Node's `requireCommunityAdmin` and the established
  `community.<group>:admin` scope convention (`blueprints/v1/
  community_activity.py`).
- Member (`SupportSubmitTicket.jsx`/`SupportMyTickets.jsx`): category read
  (view-only, needed for the ticket-submission form), submit, own-tickets
  list/get/comment -- `tenant_middleware` ONLY, no `require_scope`. Matches
  Node's plain `requireAuth` (no `requireCommunityAdmin`) on these routes
  1:1, and `hub_api/PORTING.md`'s Auth-pattern table's self-service row
  (caller acting on their own resource).

**SECURITY FIX** (see `services/support_service.py`'s module docstring):
the member-facing `get_ticket`/`add_comment` calls pass `require_reporter_id`
so a caller can only read/comment on tickets THEY reported -- Node's
`getTicket()`/`addComment()` never verified this for the `/my-tickets/*`
routes.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from services import support_service as svc
from services.community_common import api_error, community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError

support_admin_bp = Blueprint("v1_support_admin", __name__, url_prefix="/api/v1/admin")
support_member_bp = Blueprint("v1_support_member", __name__, url_prefix="/api/v1/admin")


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into Node's flat `{"error": "..."}` shape.

    See `blueprints/v1/access_token.py::_err`'s docstring -- same rationale,
    shared JSON-error contract across this whole port group.
    """
    return {"error": exc.message}, exc.status_code


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, str(default)))
    except ValueError:
        return default


def _reporter_identity(dal: Any, user_id: int) -> tuple[str | None, str | None]:
    """`(display_name, email)` for the caller.

    Matches Node's `req.user?.display_name`/`req.user?.email` fallback.
    """
    row = dal.hub_users[user_id]
    if row is None:
        return None, None
    return getattr(row, "display_name", None), getattr(row, "email", None)


# ---------------------------------------------------------------------------
# DTOs -- snake_case: wire contract mirrors Node's raw Postgres row columns
# verbatim (see `blueprints/v1/access_token.py`'s module docstring for the
# same DTO-casing note).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CategoryDTO:
    """One `support_ticket_categories` row."""

    id: int
    community_id: int
    name: str
    description: str | None
    sort_order: int
    is_active: bool
    form_fields: list[Any]
    created_at: str | None


@dataclass(slots=True, frozen=True)
class TicketDTO:
    """One `support_tickets` row, plus its category's name (Node's `LEFT JOIN` column)."""

    id: int
    community_id: int
    category_id: int | None
    ticket_number: str
    subject: str
    description: str | None
    status: str
    priority: str
    reporter_user_id: int | None
    reporter_name: str | None
    reporter_email: str | None
    assignee_user_id: int | None
    custom_fields: dict[str, Any]
    resolved_at: str | None
    created_at: str | None
    updated_at: str | None
    category_name: str | None = None


@dataclass(slots=True, frozen=True)
class CommentDTO:
    """One `support_ticket_comments` row."""

    id: int
    ticket_id: int
    author_user_id: int | None
    author_name: str | None
    content: str
    is_internal: bool
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CreateCategoryRequest:
    """Request DTO for `POST /support/categories`."""

    name: str
    description: str | None = None
    sort_order: int | None = None
    form_fields: list[Any] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class UpdateCategoryRequest:
    """Request DTO for `PUT /support/categories/<id>`."""

    name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    form_fields: list[Any] | None = None


@dataclass(slots=True, frozen=True)
class CreateTicketRequest:
    """Request DTO for `POST /support/submit`."""

    subject: str
    category_id: int | None = None
    description: str | None = None
    priority: str | None = None
    reporter_name: str | None = None
    reporter_email: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class UpdateStatusRequest:
    """Request DTO for `PUT /support/tickets/<id>/status`."""

    status: str


@dataclass(slots=True, frozen=True)
class AssignRequest:
    """Request DTO for `PUT /support/tickets/<id>/assign`."""

    assignee_user_id: int | None = None


@dataclass(slots=True, frozen=True)
class UpdatePriorityRequest:
    """Request DTO for `PUT /support/tickets/<id>/priority`."""

    priority: str


@dataclass(slots=True, frozen=True)
class AddCommentRequest:
    """Request DTO for the admin `POST /support/tickets/<id>/comments` -- can set `is_internal`."""

    content: str
    is_internal: bool = False


@dataclass(slots=True, frozen=True)
class AddOwnCommentRequest:
    """Request DTO for the member `POST /support/my-tickets/<id>/comments`.

    Deliberately has NO `is_internal` field -- Node forces `is_internal =
    false` server-side for this route (`req.body.is_internal = false`
    before calling `addComment`) so a member can never post an internal-
    only comment; the DTO shape itself enforces that here (a member
    payload has nowhere to smuggle `is_internal: true` into).
    """

    content: str


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _category_dto(row: Any) -> CategoryDTO:
    return CategoryDTO(
        id=row.id,
        community_id=row.community_id,
        name=row.name,
        description=row.description,
        sort_order=row.sort_order,
        is_active=bool(row.is_active),
        form_fields=list(row.form_fields) if row.form_fields else [],
        created_at=_iso(row.created_at),
    )


def _ticket_dto(row: Any, category_name: str | None) -> TicketDTO:
    return TicketDTO(
        id=row.id,
        community_id=row.community_id,
        category_id=row.category_id,
        ticket_number=row.ticket_number,
        subject=row.subject,
        description=row.description,
        status=row.status,
        priority=row.priority,
        reporter_user_id=row.reporter_user_id,
        reporter_name=row.reporter_name,
        reporter_email=row.reporter_email,
        assignee_user_id=row.assignee_user_id,
        custom_fields=dict(row.custom_fields) if row.custom_fields else {},
        resolved_at=_iso(row.resolved_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        category_name=category_name,
    )


def _comment_dto(row: Any) -> CommentDTO:
    return CommentDTO(
        id=row.id,
        ticket_id=row.ticket_id,
        author_user_id=row.author_user_id,
        author_name=row.author_name,
        content=row.content,
        is_internal=bool(row.is_internal),
        created_at=_iso(row.created_at),
    )


# ---------------------------------------------------------------------------
# Categories (view: any authenticated tenant member; CUD: community.support:admin)
# ---------------------------------------------------------------------------


@support_admin_bp.route("/<int:community_id>/support/categories", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def list_categories(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/categories` -- open to any authenticated tenant member."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    rows = svc.list_categories(dal, community_id)
    return {"categories": [asdict(_category_dto(r)) for r in rows]}, 200


@support_admin_bp.route("/<int:community_id>/support/categories", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(CreateCategoryRequest)
async def create_category(
    data: CreateCategoryRequest, community_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/support/categories`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        row = svc.create_category(
            dal,
            community_id,
            name=data.name,
            description=data.description,
            sort_order=data.sort_order,
            form_fields=data.form_fields,
        )
    except ApiError as exc:
        return _err(exc)
    return {"category": asdict(_category_dto(row))}, 201


@support_admin_bp.route("/<int:community_id>/support/categories/<int:category_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(UpdateCategoryRequest)
async def update_category(
    data: UpdateCategoryRequest, community_id: int, category_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/support/categories/<categoryId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        row = svc.update_category(
            dal,
            community_id,
            category_id,
            name=data.name,
            description=data.description,
            sort_order=data.sort_order,
            is_active=data.is_active,
            form_fields=data.form_fields,
        )
    except ApiError as exc:
        return _err(exc)
    return {"category": asdict(_category_dto(row))}, 200


@support_admin_bp.route(
    "/<int:community_id>/support/categories/<int:category_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
async def delete_category(community_id: int, category_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/support/categories/<categoryId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        svc.delete_category(dal, community_id, category_id)
    except ApiError as exc:
        return _err(exc)
    return {"message": "Category deleted"}, 200


# ---------------------------------------------------------------------------
# Admin ticket triage -- community.support:admin
# ---------------------------------------------------------------------------


@support_admin_bp.route("/<int:community_id>/support/tickets", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
async def list_tickets(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/tickets` -- filters + pagination."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    limit = min(100, max(1, _int_arg("limit", 25)))
    offset = max(0, _int_arg("offset", 0))
    rows, names, total = svc.list_tickets(
        dal,
        community_id,
        status=request.args.get("status"),
        priority=request.args.get("priority"),
        category_id=request.args.get("category_id", type=int),
        assignee_user_id=request.args.get("assignee_user_id", type=int),
        search=request.args.get("search"),
        limit=limit,
        offset=offset,
    )
    tickets = [asdict(_ticket_dto(r, names.get(r.category_id))) for r in rows]
    return {"tickets": tickets, "total": total, "limit": limit, "offset": offset}, 200


@support_admin_bp.route("/<int:community_id>/support/tickets/<int:ticket_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
async def get_ticket(community_id: int, ticket_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/tickets/<ticketId>` -- includes internal comments."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        ticket, category_name, comments = svc.get_ticket(
            dal, community_id, ticket_id, include_internal=True
        )
    except ApiError as exc:
        return _err(exc)
    return {
        "ticket": asdict(_ticket_dto(ticket, category_name)),
        "comments": [asdict(_comment_dto(c)) for c in comments],
    }, 200


@support_admin_bp.route(
    "/<int:community_id>/support/tickets/<int:ticket_id>/status", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(UpdateStatusRequest)
async def update_ticket_status(
    data: UpdateStatusRequest, community_id: int, ticket_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/support/tickets/<ticketId>/status`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        row = svc.update_ticket_status(dal, community_id, ticket_id, data.status)
    except ApiError as exc:
        return _err(exc)
    return {"ticket": asdict(_ticket_dto(row, None))}, 200


@support_admin_bp.route(
    "/<int:community_id>/support/tickets/<int:ticket_id>/assign", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(AssignRequest)
async def assign_ticket(
    data: AssignRequest, community_id: int, ticket_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/support/tickets/<ticketId>/assign`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        row = svc.assign_ticket(dal, community_id, ticket_id, data.assignee_user_id)
    except ApiError as exc:
        return _err(exc)
    return {"ticket": asdict(_ticket_dto(row, None))}, 200


@support_admin_bp.route(
    "/<int:community_id>/support/tickets/<int:ticket_id>/priority", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(UpdatePriorityRequest)
async def update_ticket_priority(
    data: UpdatePriorityRequest, community_id: int, ticket_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/support/tickets/<ticketId>/priority`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    try:
        row = svc.update_ticket_priority(dal, community_id, ticket_id, data.priority)
    except ApiError as exc:
        return _err(exc)
    return {"ticket": asdict(_ticket_dto(row, None))}, 200


@support_admin_bp.route(
    "/<int:community_id>/support/tickets/<int:ticket_id>/comments", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
@validate_request(AddCommentRequest)
async def add_comment(
    data: AddCommentRequest, community_id: int, ticket_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/support/tickets/<ticketId>/comments` -- admin, may be internal."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    author_name, _ = _reporter_identity(dal, user_id)
    try:
        row = svc.add_comment(
            dal,
            community_id,
            ticket_id,
            content=data.content,
            is_internal=data.is_internal,
            author_user_id=user_id,
            author_name=author_name,
        )
    except ApiError as exc:
        return _err(exc)
    return {"comment": asdict(_comment_dto(row))}, 201


@support_admin_bp.route("/<int:community_id>/support/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.support:admin")  # type: ignore[misc]
async def get_ticket_stats(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/stats`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    stats = svc.get_ticket_stats(dal, community_id)
    return {
        "stats": {
            "total": stats.total,
            "open": stats.open,
            "in_progress": stats.in_progress,
            "waiting": stats.waiting,
            "resolved": stats.resolved,
            "closed": stats.closed,
            "avg_resolution_seconds": stats.avg_resolution_seconds,
        }
    }, 200


# ---------------------------------------------------------------------------
# Member self-service -- tenant_middleware only, no require_scope (matches
# Node's plain requireAuth + PORTING.md's self-service Auth-pattern row).
# ---------------------------------------------------------------------------


@support_member_bp.route("/<int:community_id>/support/submit", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(CreateTicketRequest)
async def submit_ticket(
    data: CreateTicketRequest, community_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/support/submit`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    fallback_name, fallback_email = _reporter_identity(dal, user_id)
    try:
        row = svc.create_ticket(
            dal,
            community_id,
            category_id=data.category_id,
            subject=data.subject,
            description=data.description,
            priority=data.priority,
            reporter_user_id=user_id,
            reporter_name=data.reporter_name or fallback_name,
            reporter_email=data.reporter_email or fallback_email,
            custom_fields=data.custom_fields,
        )
    except ApiError as exc:
        return _err(exc)
    return {"ticket": asdict(_ticket_dto(row, None))}, 201


@support_member_bp.route("/<int:community_id>/support/my-tickets", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_my_tickets(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/my-tickets`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    pairs = svc.get_my_tickets(dal, community_id, user_id)
    return {"tickets": [asdict(_ticket_dto(row, name)) for row, name in pairs]}, 200


@support_member_bp.route("/<int:community_id>/support/my-tickets/<int:ticket_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_my_ticket(community_id: int, ticket_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/support/my-tickets/<ticketId>` -- reporter-only (SECURITY FIX)."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    try:
        ticket, category_name, comments = svc.get_ticket(
            dal,
            community_id,
            ticket_id,
            include_internal=False,
            require_reporter_id=user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return {
        "ticket": asdict(_ticket_dto(ticket, category_name)),
        "comments": [asdict(_comment_dto(c)) for c in comments],
    }, 200


@support_member_bp.route(
    "/<int:community_id>/support/my-tickets/<int:ticket_id>/comments", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@validate_request(AddOwnCommentRequest)
async def add_own_comment(
    data: AddOwnCommentRequest, community_id: int, ticket_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/support/my-tickets/<ticketId>/comments`.

    Reporter-only (SECURITY FIX -- see this module's docstring).
    """
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = get_current_user_id(request)
    author_name, _ = _reporter_identity(dal, user_id)
    try:
        row = svc.add_comment(
            dal,
            community_id,
            ticket_id,
            content=data.content,
            is_internal=False,
            author_user_id=user_id,
            author_name=author_name,
            require_reporter_id=user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return {"comment": asdict(_comment_dto(row))}, 201


BLUEPRINTS: list[Blueprint] = [support_admin_bp, support_member_bp]
