"""v1 `community.interaction` group -- port of Node's `interactionController.js` (M6 Community).

Three route surfaces, same split as Node's `routes/interaction.js`:

- Admin (`/api/v1/admin/<id>/interaction/*`) -- channel/role/permission-override CRUD.
- Member (`/api/v1/community/<id>/interact/*`) -- channel list/self-create, forum read/post/reply.
- Internal relay (`/api/v1/internal/activity`-sibling `/api/v1/internal/relay/incoming`) --
  `X-Service-Key` only, matching Node's `internalRelayRouter`.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from flask_core.api_utils import auth_required
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services.community_common import (
    api_error,
    community_in_tenant,
    get_current_user,
    is_valid_service_key,
)
from services.community_interaction import (
    can_create_channel,
    create_community_role,
    create_forum_post,
    create_forum_reply,
    create_hub_channel,
    delete_community_role,
    delete_forum_reply,
    delete_hub_channel,
    get_channel_permission_overrides,
    get_community_roles,
    get_forum_post,
    get_forum_posts,
    get_hub_channels,
    internal_relay_incoming,
    moderate_forum_post,
    update_channel_permission_overrides,
    update_community_role,
    update_hub_channel,
)

interaction_admin_bp = Blueprint(
    "v1_community_interaction_admin", __name__, url_prefix="/api/v1/admin"
)
interaction_member_bp = Blueprint(
    "v1_community_interaction_member", __name__, url_prefix="/api/v1/community"
)
interaction_internal_bp = Blueprint(
    "v1_community_interaction_internal", __name__, url_prefix="/api/v1/internal"
)

#: Two-gate Feature flags -- `libs/community_module/features.py`. Channel/
#: role/permission-override CRUD (this file's "Admin: hub channels"/"Admin:
#: roles"/"Admin: permission overrides" sections) is `community.interactions`;
#: forum read/post/reply (this file's "Member: channels + forum" section)
#: is the separate `community.forums` capability -- both free tier, both
#: served by this one blueprint per the migration doc's own note that
#: `interactionController.js` covers "channel CRUD... forum read/post/
#: reply... roles" as one Node controller.
FEATURE_COMMUNITY_INTERACTIONS = "waddles.community.interactions"
FEATURE_COMMUNITY_FORUMS = "waddles.community.forums"


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


# ── Admin: hub channels ────────────────────────────────────────────────


@interaction_admin_bp.route("/<int:community_id>/interaction/channels", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def admin_list_channels(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/interaction/channels`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_INTERACTIONS, tenant=ctx.tenant_slug):
        return api_error("Community interactions are not enabled for this plan", 402)
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    channels = get_hub_channels(current_app.config["dal"], community_id)
    return {"success": True, "channels": channels}, 200


@interaction_admin_bp.route("/<int:community_id>/interaction/channels", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def admin_create_channel(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/interaction/channels`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = create_hub_channel(
        current_app.config["dal"], community_id, payload, int(get_current_user(request)["user_id"])
    )
    if err:
        status = 409 if "already exists" in err else 400
        return {"success": False, "error": err}, status
    return {"success": True, "channel": dto}, 201


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/channels/<int:channel_id>", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
async def admin_update_channel(community_id: int, channel_id: int) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/interaction/channels/<id>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    dto = update_hub_channel(current_app.config["dal"], community_id, channel_id, payload)
    if dto is None:
        return {"success": False, "error": "Channel not found"}, 404
    return {"success": True, "channel": dto}, 200


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/channels/<int:channel_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
async def admin_delete_channel(community_id: int, channel_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/interaction/channels/<id>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    if not delete_hub_channel(current_app.config["dal"], community_id, channel_id):
        return {"success": False, "error": "Channel not found"}, 404
    return {"success": True, "message": "Channel deleted"}, 200


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/forum/posts/<int:post_id>", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
async def admin_moderate_post(community_id: int, post_id: int) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/interaction/forum/posts/<id>` -- pin/lock/delete."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    err = moderate_forum_post(current_app.config["dal"], community_id, post_id, payload)
    if err:
        return {"success": False, "error": err}, 400
    return {"success": True, "message": "Post moderated"}, 200


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/forum/replies/<int:reply_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
async def admin_delete_reply(community_id: int, reply_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/interaction/forum/replies/<id>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    if not delete_forum_reply(current_app.config["dal"], community_id, reply_id):
        return {"success": False, "error": "Reply not found"}, 404
    return {"success": True, "message": "Reply deleted"}, 200


# ── Admin: community roles ────────────────────────────────────────────


@interaction_admin_bp.route("/<int:community_id>/interaction/roles", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def admin_list_roles(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/interaction/roles`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    return {
        "success": True,
        "roles": get_community_roles(current_app.config["dal"], community_id),
    }, 200


@interaction_admin_bp.route("/<int:community_id>/interaction/roles", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:manage_roles")  # type: ignore[misc]
async def admin_create_role(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/interaction/roles`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = create_community_role(current_app.config["dal"], community_id, payload)
    if err:
        status = 409 if "already exists" in err else 400
        return {"success": False, "error": err}, status
    return {"success": True, "role": dto}, 201


@interaction_admin_bp.route("/<int:community_id>/interaction/roles/<int:role_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:manage_roles")  # type: ignore[misc]
async def admin_update_role(community_id: int, role_id: int) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/interaction/roles/<roleId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    err = update_community_role(current_app.config["dal"], community_id, role_id, payload)
    if err == "__not_found__":
        return {"success": False, "error": "Role not found"}, 404
    return {"success": True, "message": "Role updated"}, 200


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/roles/<int:role_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:manage_roles")  # type: ignore[misc]
async def admin_delete_role(community_id: int, role_id: int) -> tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/interaction/roles/<roleId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    err = delete_community_role(current_app.config["dal"], community_id, role_id)
    if err == "__not_found__":
        return {"success": False, "error": "Role not found"}, 404
    if err:
        return {"success": False, "error": err}, 403
    return {"success": True, "message": "Role deleted, members reassigned to member role"}, 200


# ── Admin: channel permission overrides ───────────────────────────────


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/channels/<int:channel_id>/permissions", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def admin_get_overrides(community_id: int, channel_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/interaction/channels/<id>/permissions`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    overrides = get_channel_permission_overrides(
        current_app.config["dal"], community_id, channel_id
    )
    if overrides is None:
        return {"success": False, "error": "Channel not found"}, 404
    return {"success": True, "overrides": overrides}, 200


@interaction_admin_bp.route(
    "/<int:community_id>/interaction/channels/<int:channel_id>/permissions", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:manage_channels")  # type: ignore[misc]
async def admin_update_overrides(
    community_id: int, channel_id: int
) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/interaction/channels/<id>/permissions`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    overrides = payload.get("overrides")
    if not isinstance(overrides, list):
        return {"success": False, "error": "overrides must be an array"}, 400
    ok = update_channel_permission_overrides(
        current_app.config["dal"], community_id, channel_id, overrides
    )
    if not ok:
        return {"success": False, "error": "Channel not found"}, 404
    return {"success": True, "message": "Permission overrides updated"}, 200


# ── Member: channels + forum ──────────────────────────────────────────


@interaction_member_bp.route("/<int:community_id>/interact/channels", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def member_list_channels(community_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/interact/channels`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    channels = get_hub_channels(dal, community_id)
    user = getattr(request, "current_user", None)
    can_create = (
        can_create_channel(dal, community_id, int(user["user_id"]), False) if user else False
    )
    return {"success": True, "channels": channels, "canCreateChannel": can_create}, 200


@interaction_member_bp.route("/<int:community_id>/interact/channels", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def member_create_channel(community_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/community/<id>/interact/channels` -- gated by `channel_creation_policy`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    user_id = int(get_current_user(request)["user_id"])
    if not can_create_channel(dal, community_id, user_id, False):
        return api_error("You do not have permission to create channels", status_code=403)
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = create_hub_channel(dal, community_id, payload, user_id)
    if err:
        status = 409 if "already exists" in err else 400
        return {"success": False, "error": err}, status
    return {"success": True, "channel": dto}, 201


@interaction_member_bp.route(
    "/<int:community_id>/interact/forum/<int:channel_id>/posts", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def member_forum_posts(community_id: int, channel_id: int) -> tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/interact/forum/<channelId>/posts`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_FORUMS, tenant=ctx.tenant_slug):
        return api_error("Community forums are not enabled for this plan", 402)
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    page = max(1, int(request.args.get("page", "1") or 1))
    limit = min(50, max(1, int(request.args.get("limit", "20") or 20)))
    posts, total = get_forum_posts(
        current_app.config["dal"], community_id, channel_id, page=page, limit=limit
    )
    return {
        "success": True,
        "posts": posts,
        "pagination": {"page": page, "limit": limit, "total": total},
    }, 200


@interaction_member_bp.route(
    "/<int:community_id>/interact/forum/<int:channel_id>/posts/<int:post_id>", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:read")  # type: ignore[misc]
async def member_forum_post(
    community_id: int, channel_id: int, post_id: int
) -> tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/interact/forum/<channelId>/posts/<postId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    post = get_forum_post(current_app.config["dal"], community_id, post_id)
    if post is None:
        return {"success": False, "error": "Post not found"}, 404
    return {"success": True, "post": post}, 200


@interaction_member_bp.route(
    "/<int:community_id>/interact/forum/<int:channel_id>/posts", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def member_create_forum_post(
    community_id: int, channel_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/community/<id>/interact/forum/<channelId>/posts`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = await create_forum_post(
        current_app.config["dal"], community_id, channel_id, payload, get_current_user(request)
    )
    if err:
        return {"success": False, "error": err}, 400
    return {"success": True, "post": dto}, 201


@interaction_member_bp.route(
    "/<int:community_id>/interact/forum/posts/<int:post_id>/replies", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.interaction:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def member_create_forum_reply(
    community_id: int, post_id: int
) -> tuple[dict[str, object], int]:
    """`POST /api/v1/community/<id>/interact/forum/posts/<postId>/replies`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    dto, err = await create_forum_reply(
        current_app.config["dal"], community_id, post_id, payload, get_current_user(request)
    )
    if err == "__not_found__":
        return {"success": False, "error": "Post not found"}, 404
    if err:
        return {"success": False, "error": err}, 400
    return {"success": True, "reply": dto}, 201


# ── Internal relay ─────────────────────────────────────────────────────


@interaction_internal_bp.route("/relay/incoming", methods=["POST"])
async def relay_incoming() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/relay/incoming` -- service-to-service only."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401
    payload = await request.get_json(force=True, silent=True) or {}
    err = await internal_relay_incoming(current_app.config["dal"], payload)
    if err:
        return {"success": False, "error": err}, 404
    return {"success": True}, 200


BLUEPRINTS: list[Blueprint] = [interaction_admin_bp, interaction_member_bp, interaction_internal_bp]
