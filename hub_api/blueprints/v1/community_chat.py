"""v1 `community.chat` group -- port of Node's `chatController.js` (M6 Community).

REST-only port. Node's chat surface is split across a REST controller
(`chatController.js`: `getChatHistory`/`getChatChannels`, read-only) and a
Socket.io realtime layer (`websocket/index.js` + `websocket/chatHandler.js`:
`chat:join/leave/message/typing/history`, the actual send-path). Only the
REST half is ported here.

**Socket.io is deliberately stubbed, not ported** -- TODO(M6-followup):
mounting `python-socketio` in ASGI mode alongside Quart (migration plan
§5's chosen approach, to keep the `socket.io-client` wire protocol the
React frontend already speaks) requires wrapping/mounting a second ASGI
app in `hub_api/app.py`. That file is explicitly frozen for this porting
wave (`routers/_discovery.py`'s auto-discovery contract: port agents
never edit `app.py`/`routers/*.py`/`blueprints/__init__.py`, so parallel
M-phase agents don't collide on a shared file). Sending a chat message
therefore has **no live path in hub-api yet** -- history/channel-list
reads work, but nothing in this PR accepts a new message. Follow-up work
(tracked against migration plan §5) must land the socketio mount as an
`app.py` change reviewed on its own, then wire `chat:message` to reuse
`community_chat.get_chat_history`'s same `hub_chat_messages` table plus
the relay/activity side-effects `chatHandler.js` performs today.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.community_chat import (
    ChatChannelsResponse,
    ChatHistoryResponse,
    get_chat_channels,
    get_chat_history,
)
from services.community_common import api_error, community_in_tenant
from services.pagination import parse_limit

chat_bp = Blueprint("v1_community_chat", __name__, url_prefix="/api/v1/community")

#: Two-gate Feature flag (license tier AND PostHog) -- `libs/community_module/
#: features.py`'s `community.chat` Feature contract, free tier.
FEATURE_COMMUNITY_CHAT = "waddles.community.chat"


@chat_bp.route("/<int:community_id>/chat/history", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.chat:read")  # type: ignore[misc]
@validate_response(ChatHistoryResponse)
async def chat_history(community_id: int) -> ChatHistoryResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/chat/history` -- paginated message history."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
    if not await feature_enabled(FEATURE_COMMUNITY_CHAT, tenant=ctx.tenant_slug):
        return api_error("Community chat is not enabled for this plan", 402)
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    channel_name = request.args.get("channelName")
    before = request.args.get("before")
    limit = parse_limit(request.args.get("limit"), default=50)

    messages, has_more = get_chat_history(
        dal, community_id, channel_name=channel_name, limit=limit, before=before
    )
    return ChatHistoryResponse(success=True, messages=messages, has_more=has_more)


@chat_bp.route("/<int:community_id>/chat/channels", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.chat:read")  # type: ignore[misc]
@validate_response(ChatChannelsResponse)
async def chat_channels(community_id: int) -> ChatChannelsResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/chat/channels` -- distinct channels + activity."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    channels = get_chat_channels(dal, community_id)
    return ChatChannelsResponse(success=True, channels=channels)


BLUEPRINTS: list[Blueprint] = [chat_bp]
