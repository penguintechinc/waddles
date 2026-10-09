"""
Calendar Interaction Module - Complete event management with approval workflow
Supports event CRUD, RSVP, recurring events, platform sync, and multi-community context
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'libs'))

from quart import Blueprint, Quart, request

from config import Config
from flask_core import (
    async_endpoint, create_health_blueprint, init_database,
    setup_aaa_logging, success_response, error_response,
    verify_service_key)
from flask_core.validation import validate_json, validate_query

from validation_models import (
    EventCreateRequest, EventSearchParams, EventUpdateRequest,
    EventApprovalRequest, RSVPRequest, AttendeeSearchParams,
    EventFullTextSearchParams, UpcomingEventsParams,
    PermissionsConfigRequest, CategoryCreateRequest,
    ContextSwitchRequest,
    # Ticketing models
    TicketTypeCreateRequest, TicketTypeUpdateRequest,
    TicketCreateRequest, TicketVerifyRequest, TicketCheckInRequest,
    TicketUndoCheckInRequest, TicketTransferRequest,
    TicketSearchParams, CheckInLogParams, TicketingConfigRequest,
    # Event admin models
    EventAdminAssignRequest, EventAdminUpdateRequest,
    # Calendar OAuth & Availability models
    AvailabilitySettingsUpdateRequest, WeeklyAvailabilityUpdateRequest,
    AvailableSlotsParams,
    # Booking models
    BookingPageCreateRequest, BookingPageUpdateRequest,
    BookingCreateRequest, AvailableSlotsQueryParams,
    BookingListParams, GroupBookingPageCreateRequest,
    GroupMemberAddRequest, BestSlotsParams
)


app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

calendar_bp = Blueprint('calendar', __name__, url_prefix='/api/v1/calendar')
context_bp = Blueprint('context', __name__, url_prefix='/api/v1/context')
ticket_bp = Blueprint('ticket', __name__, url_prefix='/api/v1/calendar')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
calendar_service = None
permission_service = None
context_service = None
rsvp_service = None
ticket_service = None
event_admin_service = None
calendar_oauth_service = None
availability_service = None
booking_service = None
group_availability_service = None
tournament_service = None


def check_service_api_key():
    """Reject any request lacking a valid `X-API-Key` before anything trusts it.

    SECURITY (live authz bypass fix): this module's Gateway route used to
    point directly at this service, bypassing hub-api's scope-gated
    `event_calendar_bp` + `EventCalendarProxyClient`
    (hub_api/services/event_calendar_proxy.py) -- the only legitimate
    setter of `X-User-Context`, which it builds post-JWT-validation and
    always pairs with this `X-API-Key`. `get_user_context()` below
    `json.loads()`s `X-User-Context` with no verification of its own, so
    without this gate any direct caller could set an arbitrary role
    (including 'admin') and be trusted outright. Routing now goes through
    hub-api too (k8s/httproute-modules.yaml), but this check is the
    defense-in-depth layer: it must hold even if routing is ever
    misconfigured again. Fails closed -- registered as a `before_request`
    on every blueprint in this module (see bottom of file), so it runs
    before any route body, including `get_user_context()`.
    """
    provided = request.headers.get('X-API-Key', '')
    if not verify_service_key(provided, Config.SERVICE_API_KEY):
        logger.warning(
            "Unauthorized calendar request: missing/invalid X-API-Key",
            extra={
                'event_type': 'AUTH',
                'action': 'check_service_api_key',
                'result': 'FAILURE',
                'path': request.path,
            },
        )
        return error_response("Unauthorized: invalid service key", 401)
    return None


def get_user_context():
    """Extract the caller's user context from the (now service-key-verified) request.

    Only reached after `check_service_api_key()` has confirmed the request
    carries a valid `X-API-Key` -- i.e. it came from hub-api's
    `EventCalendarProxyClient`, the sole legitimate setter of
    `X-User-Context`. A malformed/non-JSON value (e.g. a hub-api bug)
    still only degrades to the lowest-privilege anonymous/member context
    below rather than raising an unhandled 500 -- it can never be used to
    escalate, only to under-privilege, and the warning log surfaces the
    upstream bug.
    """
    auth_header = request.headers.get('X-User-Context')
    if auth_header:
        import json
        try:
            return json.loads(auth_header)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Malformed X-User-Context header",
                extra={'event_type': 'AUTH', 'action': 'get_user_context',
                       'result': 'FAILURE', 'error': str(exc)},
            )
            return {
                'user_id': None,
                'username': 'anonymous',
                'platform': 'api',
                'platform_user_id': 'anonymous',
                'role': 'member',
            }

    # No X-User-Context from hub-api (e.g. an internal health/diagnostic
    # call) -- lowest-privilege anonymous context, never elevated.
    return {
        'user_id': None,
        'username': 'anonymous',
        'platform': 'api',
        'platform_user_id': 'anonymous',
        'role': 'member'
    }


def init_services():
    """Initialize database and services (can be called from startup or tests)."""
    global dal, calendar_service, permission_service, context_service, rsvp_service, ticket_service, event_admin_service, calendar_oauth_service, availability_service, booking_service, group_availability_service, tournament_service

    if calendar_service is not None:
        return  # Already initialized

    from services.calendar_service import CalendarService
    from services.permission_service import PermissionService
    from services.context_service import ContextService
    from services.rsvp_service import RSVPService
    from services.ticket_service import TicketService
    from services.event_admin_service import EventAdminService
    from services.calendar_oauth_service import CalendarOAuthService
    from services.availability_service import AvailabilityService
    from services.booking_service import BookingService
    from services.group_availability_service import GroupAvailabilityService
    from services.tournament_service import TournamentService

    logger.system("Starting calendar module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    # Initialize services
    # Note: Order matters - ticket_service must be created before rsvp_service
    # so RSVP can auto-generate tickets on confirmation
    permission_service = PermissionService(dal)
    context_service = ContextService(dal)
    calendar_service = CalendarService(dal, permission_service)
    event_admin_service = EventAdminService(dal, permission_service)
    ticket_service = TicketService(dal, permission_service)
    rsvp_service = RSVPService(dal, ticket_service=ticket_service)
    calendar_oauth_service = CalendarOAuthService(dal)
    availability_service = AvailabilityService(dal)
    booking_service = BookingService(dal)
    group_availability_service = GroupAvailabilityService(dal)
    tournament_service = TournamentService(dal, Config)

    app.config['calendar_service'] = calendar_service
    app.config['permission_service'] = permission_service
    app.config['context_service'] = context_service
    app.config['rsvp_service'] = rsvp_service
    app.config['ticket_service'] = ticket_service
    app.config['event_admin_service'] = event_admin_service
    app.config['calendar_oauth_service'] = calendar_oauth_service
    app.config['booking_service'] = booking_service
    app.config['group_availability_service'] = group_availability_service
    app.config['availability_service'] = availability_service

    logger.system("Calendar module started", result="SUCCESS")


@app.before_serving
async def startup():
    """Initialize database and services on app startup."""
    init_services()


# ============================================================================
# EVENT MANAGEMENT ENDPOINTS
# ============================================================================

@calendar_bp.route('/<int:community_id>/events', methods=['GET'])
@async_endpoint
@validate_query(EventSearchParams)
async def list_events(community_id, query_params: EventSearchParams):
    """
    List events with validated query parameters.

    CRITICAL FIX: Uses Pydantic validation to prevent 500 errors from
    unsafe int() conversions on lines 116-117 of original code.
    """
    # Build filters from validated query params
    filters = {}
    if query_params.status:
        filters['status'] = query_params.status
    if query_params.date_from:
        filters['date_from'] = query_params.date_from.isoformat()
    if query_params.date_to:
        filters['date_to'] = query_params.date_to.isoformat()
    if query_params.category_id:
        filters['category_id'] = query_params.category_id
    if query_params.entity_id:
        filters['entity_id'] = query_params.entity_id
    if query_params.tags:
        filters['tags'] = query_params.tags
    if query_params.platform:
        filters['platform'] = query_params.platform

    pagination = {
        'offset': query_params.offset,
        'limit': query_params.limit
    }

    event_list = await calendar_service.list_events(
        community_id, filters, pagination
    )
    return success_response({'events': event_list, 'count': len(event_list)})


@calendar_bp.route('/<int:community_id>/events', methods=['POST'])
@async_endpoint
@validate_json(EventCreateRequest)
async def create_event(validated_data: EventCreateRequest, community_id):
    """Create new event with validated data."""
    # Override community_id from path parameter
    event_data = validated_data.dict()
    event_data['community_id'] = community_id
    user_context = get_user_context()

    event = await calendar_service.create_event(event_data, user_context)
    if event:
        return success_response(event, status_code=201)
    else:
        return error_response("Failed to create event", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>', methods=['GET'])
@async_endpoint
async def get_event(community_id, event_id):
    """Get event details."""
    include_attendees = request.args.get('include_attendees', 'false').lower() == 'true'
    event = await calendar_service.get_event(event_id, include_attendees)
    if event:
        return success_response(event)
    else:
        return error_response("Event not found", status_code=404)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>', methods=['PUT'])
@async_endpoint
@validate_json(EventUpdateRequest)
async def update_event(validated_data: EventUpdateRequest, community_id, event_id):
    """Update event with validated data."""
    user_context = get_user_context()
    event_data = validated_data.dict(exclude_unset=True)  # Only include fields that were set

    event = await calendar_service.update_event(event_id, event_data, user_context)
    if event:
        return success_response(event)
    else:
        return error_response("Failed to update event", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>', methods=['DELETE'])
@async_endpoint
async def delete_event(community_id, event_id):
    """Delete event."""
    user_context = get_user_context()
    success = await calendar_service.delete_event(event_id, user_context)
    if success:
        return success_response({"message": "Event deleted successfully"})
    else:
        return error_response("Failed to delete event", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/approve', methods=['POST'])
@async_endpoint
@validate_json(EventApprovalRequest)
async def approve_event(validated_data: EventApprovalRequest, community_id, event_id):
    """
    Approve or reject pending event with validated data (admin only).

    Unified endpoint for both approval and rejection actions.
    """
    user_context = get_user_context()

    if validated_data.status == 'approved':
        event = await calendar_service.approve_event(event_id, user_context)
        if event:
            return success_response(event)
        else:
            return error_response("Failed to approve event", status_code=400)
    else:
        # rejected
        reason = validated_data.notes or validated_data.reason or 'No reason provided'
        success = await calendar_service.reject_event(event_id, reason, user_context)
        if success:
            return success_response({"message": "Event rejected successfully"})
        else:
            return error_response("Failed to reject event", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/reject', methods=['POST'])
@async_endpoint
@validate_json(EventApprovalRequest)
async def reject_event(validated_data: EventApprovalRequest, community_id, event_id):
    """
    Reject pending event with reason (admin only).

    Deprecated: Use /approve endpoint with status='rejected' instead.
    """
    user_context = get_user_context()
    reason = validated_data.notes or validated_data.reason or 'No reason provided'

    success = await calendar_service.reject_event(event_id, reason, user_context)
    if success:
        return success_response({"message": "Event rejected successfully"})
    else:
        return error_response("Failed to reject event", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/cancel', methods=['POST'])
@async_endpoint
async def cancel_event(community_id, event_id):
    """Cancel event (same as delete but explicit)."""
    user_context = get_user_context()
    success = await calendar_service.delete_event(event_id, user_context)
    if success:
        return success_response({"message": "Event cancelled successfully"})
    else:
        return error_response("Failed to cancel event", status_code=400)


# ============================================================================
# RSVP MANAGEMENT ENDPOINTS
# ============================================================================

@calendar_bp.route('/<int:community_id>/events/<int:event_id>/rsvp', methods=['POST', 'PUT'])
@async_endpoint
@validate_json(RSVPRequest)
async def create_or_update_rsvp(validated_data: RSVPRequest, community_id, event_id):
    """Create or update RSVP with validated data."""
    user_context = get_user_context()

    result = await rsvp_service.rsvp_event(
        event_id, user_context,
        validated_data.status,
        validated_data.guest_count,
        validated_data.note
    )
    if result:
        return success_response(result)
    else:
        return error_response("Failed to RSVP", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/rsvp', methods=['DELETE'])
@async_endpoint
async def cancel_rsvp(community_id, event_id):
    """Cancel RSVP."""
    user_context = get_user_context()
    success = await rsvp_service.cancel_rsvp(event_id, user_context)
    if success:
        return success_response({"message": "RSVP cancelled successfully"})
    else:
        return error_response("Failed to cancel RSVP", status_code=400)


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/attendees', methods=['GET'])
@async_endpoint
@validate_query(AttendeeSearchParams)
async def get_attendees(query_params: AttendeeSearchParams, community_id, event_id):
    """Get attendee list with optional filtering."""
    attendee_list = await rsvp_service.get_attendees(event_id, query_params.status)
    return success_response({'attendees': attendee_list, 'count': len(attendee_list)})


# ============================================================================
# SEARCH & DISCOVERY ENDPOINTS
# ============================================================================

@calendar_bp.route('/<int:community_id>/search', methods=['GET'])
@async_endpoint
@validate_query(EventFullTextSearchParams)
async def search_events(query_params: EventFullTextSearchParams, community_id):
    """Full-text search on events with validated parameters."""
    filters = {}
    if query_params.category_id:
        filters['category_id'] = query_params.category_id
    if query_params.date_from:
        filters['date_from'] = query_params.date_from.isoformat()
    if query_params.date_to:
        filters['date_to'] = query_params.date_to.isoformat()

    events = await calendar_service.search_events(community_id, query_params.q, filters)
    return success_response({'events': events, 'count': len(events), 'query': query_params.q})


@calendar_bp.route('/<int:community_id>/upcoming', methods=['GET'])
@async_endpoint
@validate_query(UpcomingEventsParams)
async def upcoming_events(query_params: UpcomingEventsParams, community_id):
    """Get upcoming approved events with validated parameters."""
    events = await calendar_service.get_upcoming_events(
        community_id, query_params.limit, query_params.entity_id
    )
    return success_response({'events': events, 'count': len(events)})


@calendar_bp.route('/<int:community_id>/trending', methods=['GET'])
@async_endpoint
@validate_query(UpcomingEventsParams)
async def trending_events(query_params: UpcomingEventsParams, community_id):
    """
    Get trending events (placeholder for Phase 8).

    CRITICAL FIX: Uses validated parameters to prevent int() conversion errors.
    """
    # TODO: Implement trending algorithm in Phase 8
    # For now, return upcoming events as trending
    events = await calendar_service.get_upcoming_events(community_id, query_params.limit)
    return success_response({'events': events, 'count': len(events)})


# ============================================================================
# PLATFORM SYNC ENDPOINTS
# ============================================================================

@calendar_bp.route('/<int:community_id>/sync/enable', methods=['POST'])
@async_endpoint
async def enable_sync(community_id):
    """
    Toggle Discord event-sync opt-in for every guild paired with this community.

    Body (optional): {"enabled": bool}, defaults to true. Calls hub-api's
    internal POST /api/v1/internal/calendar/guild-pairings/event-sync,
    which flips guild_tenant_pairings.event_sync_enabled for every pairing
    under this community -- the opt-in event_discord_sync_service.py's
    push engine fans out against.
    """
    body = await request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get('enabled', True))

    result = await calendar_service.sync_client.set_event_sync_enabled(community_id, enabled)
    if not result.ok:
        return error_response(
            f"Failed to update sync configuration: {result.error}", status_code=502
        )

    return success_response({
        "community_id": result.community_id,
        "event_sync_enabled": result.event_sync_enabled,
        "pairings_updated": result.pairings_updated
    })


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/sync', methods=['POST'])
@async_endpoint
async def manual_sync(community_id, event_id):
    """
    Manually (synchronously) trigger a Discord sync for one event.

    Unlike the lifecycle hooks in calendar_service.py (fire-and-forget),
    this is a direct user action: call hub-api's internal sync-discord
    endpoint and wait for its result, so the caller sees the real outcome
    (or a clear failure) immediately rather than polling /sync/status.
    """
    event = await calendar_service.get_event(event_id)
    if not event:
        return error_response("Event not found", status_code=404)

    if event['status'] == 'cancelled':
        action = 'cancel'
    elif event.get('sync', {}).get('discord_event_id'):
        action = 'update'
    else:
        action = 'create'

    result = await calendar_service.sync_client.sync_event(event_id, action)
    if not result.ok:
        return error_response(
            f"Discord sync failed: {result.sync_error}", status_code=502
        )

    return success_response({
        "event_id": result.event_id,
        "action": action,
        "discord_event_id": result.discord_event_id,
        "sync_status": result.sync_status,
        "sync_error": result.sync_error
    })


@calendar_bp.route('/<int:community_id>/events/<int:event_id>/sync/status', methods=['GET'])
@async_endpoint
async def sync_status(community_id, event_id):
    """Get sync status for event."""
    event = await calendar_service.get_event(event_id)
    if event:
        return success_response(event.get('sync', {}))
    else:
        return error_response("Event not found", status_code=404)


@calendar_bp.route('/webhooks/discord', methods=['POST'])
@async_endpoint
async def discord_webhook():
    """Handle Discord scheduled event webhooks (Phase 4 implementation)."""
    # TODO: Implement in Phase 4
    data = await request.get_json()
    logger.info(f"Discord webhook received: {data.get('type')}")
    return success_response({"message": "Webhook received (stub)"})


@calendar_bp.route('/webhooks/twitch', methods=['POST'])
@async_endpoint
async def twitch_webhook():
    """Handle Twitch schedule webhooks (Phase 4 implementation)."""
    # TODO: Implement in Phase 4
    data = await request.get_json()
    logger.info(f"Twitch webhook received: {data.get('type')}")
    return success_response({"message": "Webhook received (stub)"})


# ============================================================================
# CONFIGURATION ENDPOINTS
# ============================================================================

@calendar_bp.route('/<int:community_id>/config/permissions', methods=['GET'])
@async_endpoint
async def get_permissions_config(community_id):
    """Get permissions configuration."""
    permissions = await permission_service.get_permissions(community_id)
    if permissions:
        return success_response(permissions)
    else:
        return error_response("Permissions not found", status_code=404)


@calendar_bp.route('/<int:community_id>/config/permissions', methods=['PUT'])
@async_endpoint
@validate_json(PermissionsConfigRequest)
async def update_permissions_config(validated_data: PermissionsConfigRequest, community_id):
    """Update permissions configuration with validated data (admin only)."""
    user_context = get_user_context()
    permissions_data = validated_data.dict(exclude_unset=True)

    success = await permission_service.update_permissions(
        community_id, permissions_data, user_context
    )
    if success:
        return success_response({"message": "Permissions updated successfully"})
    else:
        return error_response("Failed to update permissions", status_code=400)


@calendar_bp.route('/<int:community_id>/config/reminders', methods=['GET', 'PUT'])
@async_endpoint
async def reminders_config(community_id):
    """Get or update reminder configuration (Phase 5 implementation)."""
    # TODO: Implement in Phase 5
    if request.method == 'GET':
        return success_response({
            "allow_15min": True,
            "allow_1hour": True,
            "allow_24hour": True,
            "allow_1week": True,
            "default_1hour": True,
            "default_24hour": True
        })
    else:
        return success_response({"message": "Reminder config updated (stub)"})


@calendar_bp.route('/<int:community_id>/categories', methods=['GET'])
@async_endpoint
async def list_categories(community_id):
    """List event categories."""
    # Query categories from database
    query = """
        SELECT id, name, description, color, icon, display_order, is_active
        FROM calendar_categories
        WHERE community_id = $1 AND is_active = TRUE
        ORDER BY display_order ASC
    """
    rows = await dal.execute(query, [community_id])

    category_list = []
    for row in rows:
        category_list.append({
            'id': row['id'],
            'name': row['name'],
            'description': row['description'],
            'color': row['color'],
            'icon': row['icon'],
            'display_order': row['display_order']
        })

    return success_response({'categories': category_list, 'count': len(category_list)})


@calendar_bp.route('/<int:community_id>/categories', methods=['POST'])
@async_endpoint
@validate_json(CategoryCreateRequest)
async def create_category(validated_data: CategoryCreateRequest, community_id):
    """Create new event category with validated data (admin only)."""
    user_context = get_user_context()

    # Check admin permission
    if user_context.get('role') not in ['admin', 'super_admin']:
        return error_response("Admin permission required", status_code=403)

    query = """
        INSERT INTO calendar_categories (
            community_id, name, description, color, icon, display_order
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, name
    """
    result = await dal.execute(query, [
        community_id,
        validated_data.name,
        validated_data.description,
        validated_data.color,
        validated_data.icon,
        validated_data.display_order
    ])

    if result:
        return success_response(
            {'id': result[0]['id'], 'name': result[0]['name']},
            status_code=201
        )
    else:
        return error_response("Failed to create category", status_code=400)


# ============================================================================
# CONTEXT MANAGEMENT ENDPOINTS
# ============================================================================

@context_bp.route('/<entity_id>', methods=['GET'])
@async_endpoint
async def get_context(entity_id):
    """Get current community context for entity."""
    user_id = request.args.get('user_id', 'anonymous')
    context = await context_service.get_current_context(user_id, entity_id)

    if context:
        return success_response({'current_community_id': context})
    else:
        return success_response({'current_community_id': None})


@context_bp.route('/<entity_id>/switch', methods=['POST'])
@async_endpoint
@validate_json(ContextSwitchRequest)
async def switch_context(validated_data: ContextSwitchRequest, entity_id):
    """Switch active community context with validated data."""
    success = await context_service.switch_context(
        validated_data.user_id, entity_id, validated_data.community_name
    )
    if success:
        return success_response({"message": f"Switched to community: {validated_data.community_name}"})
    else:
        return error_response("Failed to switch context", status_code=400)


@context_bp.route('/<entity_id>/available', methods=['GET'])
@async_endpoint
async def available_communities(entity_id):
    """Get list of available communities for entity."""
    communities = await context_service.get_available_communities(entity_id)
    return success_response({'communities': communities, 'count': len(communities)})


# ============================================================================
# TICKETING ENDPOINTS
# ============================================================================

@ticket_bp.route('/verify-ticket', methods=['POST'])
@async_endpoint
@validate_json(TicketVerifyRequest)
async def verify_ticket(validated_data: TicketVerifyRequest):
    """
    Verify and optionally check-in a ticket via QR code.
    This is the main endpoint called by QR scanners.
    """
    from services.ticket_service import CheckInMethod

    user_context = get_user_context()

    result = await ticket_service.verify_ticket(
        ticket_code=validated_data.ticket_code,
        perform_checkin=validated_data.perform_checkin,
        operator_context=user_context,
        check_in_method=CheckInMethod.QR_SCAN,
        location=validated_data.location,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )

    if result.success:
        return success_response({
            'valid': True,
            'result': result.result_code.value,
            'ticket': result.ticket,
            'event': result.event_info,
            'message': result.message
        })
    else:
        return success_response({
            'valid': False,
            'result': result.result_code.value,
            'ticket': result.ticket,
            'message': result.message
        })


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticket-types', methods=['GET'])
@async_endpoint
async def list_ticket_types(community_id, event_id):
    """List ticket types for an event."""
    types = await ticket_service.list_ticket_types(event_id)
    return success_response({'ticket_types': types, 'count': len(types)})


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticket-types', methods=['POST'])
@async_endpoint
@validate_json(TicketTypeCreateRequest)
async def create_ticket_type(validated_data: TicketTypeCreateRequest, community_id, event_id):
    """Create a new ticket type for an event."""
    user_context = get_user_context()

    # Check permission (event admin with manage_ticket_types or community admin)
    can_manage = await event_admin_service.has_permission(
        event_id, user_context,
        event_admin_service.EventAdminPermission.MANAGE_TICKET_TYPES
    ) if event_admin_service else True

    if not can_manage:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict()
    ticket_type = await ticket_service.create_ticket_type(
        event_id=event_id,
        name=data['name'],
        description=data.get('description'),
        max_quantity=data.get('max_quantity'),
        price_cents=data.get('price_cents', 0),
        currency=data.get('currency', 'USD'),
        sales_start=data.get('sales_start'),
        sales_end=data.get('sales_end'),
        display_order=data.get('display_order', 0)
    )

    if ticket_type:
        return success_response(ticket_type, status_code=201)
    else:
        return error_response("Failed to create ticket type", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticket-types/<int:type_id>', methods=['PUT'])
@async_endpoint
@validate_json(TicketTypeUpdateRequest)
async def update_ticket_type(validated_data: TicketTypeUpdateRequest, community_id, event_id, type_id):
    """Update a ticket type (admin/event admin)."""
    user_context = get_user_context()

    # Check permission
    can_manage = await event_admin_service.can_manage_ticket_types(
        event_id, user_context
    ) if event_admin_service else True

    if not can_manage:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict(exclude_unset=True)
    ticket_type = await ticket_service.update_ticket_type(
        ticket_type_id=type_id,
        user_context=user_context,
        **data
    )

    if ticket_type:
        return success_response(ticket_type)
    else:
        return error_response("Failed to update ticket type", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticket-types/<int:type_id>', methods=['DELETE'])
@async_endpoint
async def delete_ticket_type(community_id, event_id, type_id):
    """Delete a ticket type (admin/event admin)."""
    user_context = get_user_context()

    # Check permission
    can_manage = await event_admin_service.can_manage_ticket_types(
        event_id, user_context
    ) if event_admin_service else True

    if not can_manage:
        return error_response("Permission denied", status_code=403)

    success = await ticket_service.delete_ticket_type(
        ticket_type_id=type_id,
        user_context=user_context
    )

    if success:
        return success_response({"deleted": True})
    else:
        return error_response("Failed to delete ticket type or type has existing tickets", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticketing/enable', methods=['POST'])
@async_endpoint
@validate_json(TicketingConfigRequest)
async def enable_ticketing(validated_data: TicketingConfigRequest, community_id, event_id):
    """Enable ticketing for an event (admin/event admin)."""
    user_context = get_user_context()

    # Check permission
    can_configure = await event_admin_service.can_configure_ticketing(
        event_id, user_context
    ) if event_admin_service else True

    if not can_configure:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict(exclude_unset=True)
    config = await ticket_service.enable_ticketing(
        event_id=event_id,
        user_context=user_context,
        **data
    )

    if config:
        return success_response(config)
    else:
        return error_response("Failed to enable ticketing", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/ticketing/disable', methods=['POST'])
@async_endpoint
async def disable_ticketing(community_id, event_id):
    """Disable ticketing for an event (admin/event admin)."""
    user_context = get_user_context()

    # Check permission
    can_configure = await event_admin_service.can_configure_ticketing(
        event_id, user_context
    ) if event_admin_service else True

    if not can_configure:
        return error_response("Permission denied", status_code=403)

    success = await ticket_service.disable_ticketing(
        event_id=event_id,
        user_context=user_context
    )

    if success:
        return success_response({"ticketing_enabled": False})
    else:
        return error_response("Failed to disable ticketing", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/tickets', methods=['GET'])
@async_endpoint
@validate_query(TicketSearchParams)
async def list_tickets(community_id, event_id, query_params: TicketSearchParams):
    """List tickets for an event (admin/event admin only)."""
    user_context = get_user_context()

    # Check permission
    can_view = await event_admin_service.can_view_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_view:
        return error_response("Permission denied", status_code=403)

    result = await ticket_service.list_tickets(
        event_id=event_id,
        status=query_params.status,
        is_checked_in=query_params.is_checked_in,
        ticket_type_id=query_params.ticket_type_id,
        search=query_params.search,
        limit=query_params.limit,
        offset=query_params.offset
    )
    return success_response(result)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/tickets', methods=['POST'])
@async_endpoint
@validate_json(TicketCreateRequest)
async def create_ticket(validated_data: TicketCreateRequest, community_id, event_id):
    """Create a ticket manually (admin/event admin)."""
    user_context = get_user_context()

    # Check permission
    can_view = await event_admin_service.can_view_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_view:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict()
    ticket_user_context = {
        'user_id': data.get('hub_user_id'),
        'platform': data['platform'],
        'platform_user_id': data['platform_user_id'],
        'username': data['username']
    }

    ticket = await ticket_service.create_ticket(
        event_id=event_id,
        user_context=ticket_user_context,
        ticket_type_id=data.get('ticket_type_id'),
        holder_name=data.get('holder_name'),
        holder_email=data.get('holder_email')
    )

    if ticket:
        return success_response(ticket, status_code=201)
    else:
        return error_response("Failed to create ticket", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/check-in', methods=['POST'])
@async_endpoint
@validate_json(TicketCheckInRequest)
async def check_in_ticket(validated_data: TicketCheckInRequest, community_id, event_id):
    """Check in a ticket manually."""
    from services.ticket_service import CheckInMethod

    user_context = get_user_context()

    # Check permission
    can_check_in = await event_admin_service.can_check_in(
        event_id, user_context
    ) if event_admin_service else True

    if not can_check_in:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict()

    if data.get('ticket_code'):
        result = await ticket_service.verify_ticket(
            ticket_code=data['ticket_code'],
            perform_checkin=True,
            operator_context=user_context,
            check_in_method=CheckInMethod.MANUAL,
            location=data.get('location'),
            ip_address=request.remote_addr
        )
    else:
        return error_response("ticket_code required for check-in", status_code=400)

    if result.success:
        return success_response({
            'checked_in': True,
            'ticket': result.ticket,
            'message': result.message
        })
    else:
        return success_response({
            'checked_in': False,
            'result': result.result_code.value,
            'message': result.message
        })


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/check-in/undo', methods=['POST'])
@async_endpoint
@validate_json(TicketUndoCheckInRequest)
async def undo_check_in(validated_data: TicketUndoCheckInRequest, community_id, event_id):
    """Undo a ticket check-in."""
    user_context = get_user_context()

    # Check permission
    can_check_in = await event_admin_service.can_check_in(
        event_id, user_context
    ) if event_admin_service else True

    if not can_check_in:
        return error_response("Permission denied", status_code=403)

    success = await ticket_service.undo_check_in(
        ticket_id=validated_data.ticket_id,
        operator_context=user_context,
        reason=validated_data.reason
    )

    if success:
        return success_response({'message': 'Check-in undone successfully'})
    else:
        return error_response("Failed to undo check-in", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/attendance', methods=['GET'])
@async_endpoint
async def get_attendance_stats(community_id, event_id):
    """Get attendance statistics for an event."""
    user_context = get_user_context()

    # Check permission
    can_view = await event_admin_service.can_view_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_view:
        return error_response("Permission denied", status_code=403)

    stats = await ticket_service.get_attendance_stats(event_id)
    return success_response(stats)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/check-in-log', methods=['GET'])
@async_endpoint
@validate_query(CheckInLogParams)
async def get_check_in_log(community_id, event_id, query_params: CheckInLogParams):
    """Get check-in audit log for an event."""
    user_context = get_user_context()

    # Check permission
    can_view = await event_admin_service.can_view_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_view:
        return error_response("Permission denied", status_code=403)

    result = await ticket_service.get_check_in_log(
        event_id=event_id,
        limit=query_params.limit,
        offset=query_params.offset,
        success_only=query_params.success_only
    )
    return success_response(result)


@ticket_bp.route('/<int:community_id>/tickets/<int:ticket_id>/transfer', methods=['POST'])
@async_endpoint
@validate_json(TicketTransferRequest)
async def transfer_ticket(validated_data: TicketTransferRequest, community_id, ticket_id):
    """Transfer a ticket to a new holder (admin only)."""
    user_context = get_user_context()

    # Get ticket to check event_id
    ticket = await ticket_service.get_ticket(ticket_id)
    if not ticket:
        return error_response("Ticket not found", status_code=404)

    event_id = ticket['event_id']

    # Check permission
    can_transfer = await event_admin_service.can_transfer_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_transfer:
        return error_response("Permission denied", status_code=403)

    data = validated_data.dict()
    new_holder_context = {
        'user_id': data.get('new_holder_user_id'),
        'platform': data['new_holder_platform'],
        'platform_user_id': data['new_holder_platform_user_id'],
        'username': data['new_holder_username'],
        'holder_name': data['new_holder_name'],
        'holder_email': data.get('new_holder_email')
    }

    new_ticket = await ticket_service.transfer_ticket(
        ticket_id=ticket_id,
        new_holder_context=new_holder_context,
        operator_context=user_context,
        notes=data.get('notes')
    )

    if new_ticket:
        return success_response(new_ticket)
    else:
        return error_response("Failed to transfer ticket", status_code=400)


@ticket_bp.route('/<int:community_id>/tickets/<int:ticket_id>', methods=['DELETE'])
@async_endpoint
async def cancel_ticket(community_id, ticket_id):
    """Cancel a ticket."""
    user_context = get_user_context()

    # Get ticket to check event_id
    ticket = await ticket_service.get_ticket(ticket_id)
    if not ticket:
        return error_response("Ticket not found", status_code=404)

    event_id = ticket['event_id']

    # Check permission
    can_cancel = await event_admin_service.can_cancel_tickets(
        event_id, user_context
    ) if event_admin_service else True

    if not can_cancel:
        return error_response("Permission denied", status_code=403)

    # Get reason from query params
    reason = request.args.get('reason')

    success = await ticket_service.cancel_ticket(
        ticket_id=ticket_id,
        cancelled_by=user_context,
        reason=reason
    )

    if success:
        return success_response({'message': 'Ticket cancelled successfully'})
    else:
        return error_response("Failed to cancel ticket", status_code=400)


# ============================================================================
# EVENT ADMIN ENDPOINTS
# ============================================================================

@ticket_bp.route('/<int:community_id>/events/<int:event_id>/admins', methods=['GET'])
@async_endpoint
async def list_event_admins(community_id, event_id):
    """List event admins for an event."""
    user_context = get_user_context()

    # Check if user can view (event creator, community admin, or has assign permission)
    can_view = await event_admin_service.can_assign_event_admins(
        event_id, user_context
    ) if event_admin_service else True

    if not can_view:
        return error_response("Permission denied", status_code=403)

    admins = await event_admin_service.list_event_admins(event_id)
    return success_response({'admins': admins, 'count': len(admins)})


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/admins', methods=['POST'])
@async_endpoint
@validate_json(EventAdminAssignRequest)
async def assign_event_admin(validated_data: EventAdminAssignRequest, community_id, event_id):
    """Assign a user as an event admin."""
    user_context = get_user_context()

    result = await event_admin_service.assign_event_admin(
        event_id=event_id,
        assignee_data=validated_data.dict(),
        assigner_context=user_context
    )

    if result:
        return success_response(result, status_code=201)
    else:
        return error_response("Failed to assign event admin", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/admins/<int:admin_id>', methods=['PUT'])
@async_endpoint
@validate_json(EventAdminUpdateRequest)
async def update_event_admin(validated_data: EventAdminUpdateRequest, community_id, event_id, admin_id):
    """Update an event admin's permissions."""
    user_context = get_user_context()

    success = await event_admin_service.update_event_admin(
        event_admin_id=admin_id,
        updates=validated_data.dict(exclude_unset=True),
        operator_context=user_context
    )

    if success:
        return success_response({'message': 'Event admin updated successfully'})
    else:
        return error_response("Failed to update event admin", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/admins/<int:admin_id>', methods=['DELETE'])
@async_endpoint
async def revoke_event_admin(community_id, event_id, admin_id):
    """Revoke an event admin's access."""
    user_context = get_user_context()
    reason = request.args.get('reason')

    success = await event_admin_service.revoke_event_admin(
        event_admin_id=admin_id,
        operator_context=user_context,
        reason=reason
    )

    if success:
        return success_response({'message': 'Event admin revoked successfully'})
    else:
        return error_response("Failed to revoke event admin", status_code=400)


@ticket_bp.route('/<int:community_id>/events/<int:event_id>/my-permissions', methods=['GET'])
@async_endpoint
async def get_my_permissions(community_id, event_id):
    """Get current user's permissions for an event."""
    user_context = get_user_context()

    permissions = await event_admin_service.get_user_permissions(
        event_id, user_context
    ) if event_admin_service else {}

    return success_response({'permissions': permissions})


# ============================================================================
# CALENDAR OAUTH ENDPOINTS (Phase 4A)
# ============================================================================

@calendar_bp.route('/oauth/google/auth-url', methods=['GET'])
@async_endpoint
async def google_auth_url():
    """Get Google Calendar OAuth authorization URL."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    redirect_uri = request.args.get('redirect_uri')
    if not redirect_uri:
        return error_response("redirect_uri parameter required", status_code=400)

    auth_url = await calendar_oauth_service.get_google_auth_url(user_id, redirect_uri)
    return success_response({'auth_url': auth_url})


@calendar_bp.route('/oauth/google/callback', methods=['GET'])
@async_endpoint
async def google_callback():
    """Handle Google Calendar OAuth callback."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    code = request.args.get('code')
    redirect_uri = request.args.get('redirect_uri')

    if not code or not redirect_uri:
        return error_response("code and redirect_uri required", status_code=400)

    calendar = await calendar_oauth_service.handle_google_callback(
        user_id, code, redirect_uri
    )

    if calendar:
        return success_response(calendar, status_code=201)
    else:
        return error_response("Failed to connect Google Calendar", status_code=400)


@calendar_bp.route('/oauth/microsoft/auth-url', methods=['GET'])
@async_endpoint
async def microsoft_auth_url():
    """Get Microsoft Calendar OAuth authorization URL."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    redirect_uri = request.args.get('redirect_uri')
    if not redirect_uri:
        return error_response("redirect_uri parameter required", status_code=400)

    auth_url = await calendar_oauth_service.get_microsoft_auth_url(user_id, redirect_uri)
    return success_response({'auth_url': auth_url})


@calendar_bp.route('/oauth/microsoft/callback', methods=['GET'])
@async_endpoint
async def microsoft_callback():
    """Handle Microsoft Calendar OAuth callback."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    code = request.args.get('code')
    redirect_uri = request.args.get('redirect_uri')

    if not code or not redirect_uri:
        return error_response("code and redirect_uri required", status_code=400)

    calendar = await calendar_oauth_service.handle_microsoft_callback(
        user_id, code, redirect_uri
    )

    if calendar:
        return success_response(calendar, status_code=201)
    else:
        return error_response("Failed to connect Microsoft Calendar", status_code=400)


@calendar_bp.route('/oauth/calendars', methods=['GET'])
@async_endpoint
async def list_connected_calendars():
    """List connected calendars for current user."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    calendars = await calendar_oauth_service.list_connected_calendars(user_id)
    return success_response({'calendars': calendars, 'count': len(calendars)})


@calendar_bp.route('/oauth/calendars/<int:calendar_id>/sync', methods=['POST'])
@async_endpoint
async def sync_calendar(calendar_id):
    """Manually trigger free/busy sync for a connected calendar."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    # Get date range from request (default to next 30 days)
    from datetime import datetime, timedelta, timezone
    start_date = datetime.now(timezone.utc)
    end_date = start_date + timedelta(days=30)

    success = await calendar_oauth_service.sync_free_busy(
        user_id, calendar_id, start_date, end_date
    )

    if success:
        return success_response({'message': 'Calendar synced successfully'})
    else:
        return error_response("Failed to sync calendar", status_code=400)


@calendar_bp.route('/oauth/calendars/<int:calendar_id>', methods=['DELETE'])
@async_endpoint
async def disconnect_calendar(calendar_id):
    """Disconnect a calendar."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    success = await calendar_oauth_service.disconnect_calendar(user_id, calendar_id)

    if success:
        return success_response({'message': 'Calendar disconnected successfully'})
    else:
        return error_response("Failed to disconnect calendar", status_code=400)


# ============================================================================
# AVAILABILITY ENDPOINTS (Phase 4B)
# ============================================================================

@calendar_bp.route('/availability/settings', methods=['GET'])
@async_endpoint
async def get_availability_settings():
    """Get availability settings for current user."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    settings = await availability_service.get_settings(user_id)
    return success_response(settings)


@calendar_bp.route('/availability/settings', methods=['PUT'])
@async_endpoint
@validate_json(AvailabilitySettingsUpdateRequest)
async def update_availability_settings(validated_data: AvailabilitySettingsUpdateRequest):
    """Update availability settings for current user."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    settings_dict = validated_data.dict(exclude_unset=True)
    success = await availability_service.update_settings(user_id, settings_dict)

    if success:
        return success_response({'message': 'Settings updated successfully'})
    else:
        return error_response("Failed to update settings", status_code=400)


@calendar_bp.route('/availability/weekly', methods=['GET'])
@async_endpoint
async def get_weekly_availability():
    """Get weekly availability schedule for current user."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    availability = await availability_service.get_weekly_availability(user_id)
    return success_response({'weekly_availability': availability})


@calendar_bp.route('/availability/weekly', methods=['PUT'])
@async_endpoint
@validate_json(WeeklyAvailabilityUpdateRequest)
async def update_weekly_availability(validated_data: WeeklyAvailabilityUpdateRequest):
    """Update weekly availability schedule for current user."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    availability = validated_data.dict()
    success = await availability_service.update_weekly_availability(user_id, availability)

    if success:
        return success_response({'message': 'Weekly availability updated successfully'})
    else:
        return error_response("Failed to update weekly availability", status_code=400)


@calendar_bp.route('/availability/<int:target_user_id>/slots', methods=['GET'])
@async_endpoint
@validate_query(AvailableSlotsParams)
async def get_available_slots(query_params: AvailableSlotsParams, target_user_id):
    """Get available time slots for a user on a specific date."""
    from datetime import datetime

    # Parse date
    try:
        date = datetime.strptime(query_params.date, '%Y-%m-%d')
    except ValueError:
        return error_response("Invalid date format, use YYYY-MM-DD", status_code=400)

    slots = await availability_service.compute_available_slots(
        target_user_id, date, query_params.duration
    )

    return success_response({'slots': slots, 'count': len(slots), 'date': query_params.date})


# ============================================================================
# BOOKING PAGE ENDPOINTS (Phase 4C)
# ============================================================================

@calendar_bp.route('/booking-pages', methods=['POST'])
@async_endpoint
@validate_json(BookingPageCreateRequest)
async def create_booking_page(validated_data: BookingPageCreateRequest):
    """Create an individual booking page."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    data = validated_data.dict()
    page = await booking_service.create_booking_page(user_id, data)

    if page:
        return success_response(page, status_code=201)
    else:
        return error_response("Failed to create booking page", status_code=400)


@calendar_bp.route('/booking-pages', methods=['GET'])
@async_endpoint
async def list_booking_pages():
    """List current user's booking pages."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    pages = await booking_service.list_user_booking_pages(user_id)
    return success_response({'pages': pages, 'count': len(pages)})


@calendar_bp.route('/booking-pages/<slug_or_id>', methods=['GET'])
@async_endpoint
async def get_booking_page(slug_or_id):
    """Get booking page by slug or ID."""
    page = await booking_service.get_booking_page(slug_or_id)

    if page:
        return success_response(page)
    else:
        return error_response("Booking page not found", status_code=404)


@calendar_bp.route('/booking-pages/<int:page_id>', methods=['PUT'])
@async_endpoint
@validate_json(BookingPageUpdateRequest)
async def update_booking_page(validated_data: BookingPageUpdateRequest, page_id):
    """Update booking page (owner only)."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    data = validated_data.dict(exclude_unset=True)
    page = await booking_service.update_booking_page(page_id, user_id, data)

    if page:
        return success_response(page)
    else:
        return error_response("Failed to update booking page", status_code=400)


@calendar_bp.route('/booking-pages/<int:page_id>', methods=['DELETE'])
@async_endpoint
async def delete_booking_page(page_id):
    """Delete booking page (owner only)."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    success = await booking_service.delete_booking_page(page_id, user_id)

    if success:
        return success_response({'message': 'Booking page deleted successfully'})
    else:
        return error_response("Failed to delete booking page", status_code=400)


# ============================================================================
# BOOKING ENDPOINTS (Phase 4C)
# ============================================================================

@calendar_bp.route('/book/<slug>/slots', methods=['GET'])
@async_endpoint
@validate_query(AvailableSlotsQueryParams)
async def get_booking_slots(query_params: AvailableSlotsQueryParams, slug):
    """
    Get available slots for a booking page (public endpoint).
    Works without authentication for public booking pages.
    """
    from datetime import datetime

    # Get booking page
    page = await booking_service.get_booking_page(slug)
    if not page:
        return error_response("Booking page not found", status_code=404)

    # Check access scope (implement access control if needed)
    # For now, allow public access based on access_scope

    # Parse date
    try:
        date = datetime.strptime(query_params.date, '%Y-%m-%d')
    except ValueError:
        return error_response("Invalid date format, use YYYY-MM-DD", status_code=400)

    slots = await booking_service.get_available_slots(page['id'], date)
    return success_response({'slots': slots, 'count': len(slots), 'date': query_params.date})


@calendar_bp.route('/book/<slug>', methods=['POST'])
@async_endpoint
@validate_json(BookingCreateRequest)
async def create_booking(validated_data: BookingCreateRequest, slug):
    """
    Create a booking on a booking page (public endpoint).
    Works without authentication for public booking pages.
    """
    from datetime import datetime

    # Get booking page
    page = await booking_service.get_booking_page(slug)
    if not page:
        return error_response("Booking page not found", status_code=404)

    # Check access scope
    user_context = get_user_context()
    access_scope = page.get('access_scope', 'public')

    if access_scope == 'registered' and not user_context.get('user_id'):
        return error_response("Authentication required for this booking page", status_code=401)

    if access_scope == 'community':
        # Would need to check community membership here
        pass

    # Prepare guest data
    guest_data = {
        'guest_user_id': user_context.get('user_id'),
        'guest_name': validated_data.guest_name,
        'guest_email': validated_data.guest_email
    }

    booking = await booking_service.create_booking(
        booking_page_id=page['id'],
        guest_data=guest_data,
        slot_start=validated_data.slot_start,
        slot_end=validated_data.slot_end,
        form_responses=validated_data.form_responses
    )

    if booking:
        return success_response(booking, status_code=201)
    else:
        return error_response("Failed to create booking (slot may be taken)", status_code=400)


@calendar_bp.route('/bookings/<uuid>', methods=['GET'])
@async_endpoint
async def get_booking(uuid):
    """Get booking details by UUID."""
    booking = await booking_service.get_booking(uuid)

    if booking:
        return success_response(booking)
    else:
        return error_response("Booking not found", status_code=404)


@calendar_bp.route('/bookings/<uuid>', methods=['DELETE'])
@async_endpoint
async def cancel_booking(uuid):
    """Cancel a booking."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    # Get booking to determine if user is host or guest
    booking = await booking_service.get_booking(uuid)
    if not booking:
        return error_response("Booking not found", status_code=404)

    # Determine cancellation role
    if user_id == booking['host_user_id']:
        cancelled_by = 'host'
    elif user_id == booking.get('guest_user_id'):
        cancelled_by = 'guest'
    else:
        return error_response("Not authorized to cancel this booking", status_code=403)

    reason = request.args.get('reason')
    success = await booking_service.cancel_booking(uuid, cancelled_by, reason)

    if success:
        return success_response({'message': 'Booking cancelled successfully'})
    else:
        return error_response("Failed to cancel booking", status_code=400)


@calendar_bp.route('/my-bookings', methods=['GET'])
@async_endpoint
@validate_query(BookingListParams)
async def list_my_bookings(query_params: BookingListParams):
    """List bookings for current user (as host)."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    # Parse dates if provided
    from datetime import datetime
    start_date = None
    end_date = None

    if query_params.start:
        try:
            start_date = datetime.strptime(query_params.start, '%Y-%m-%d')
        except ValueError:
            return error_response("Invalid start date format", status_code=400)

    if query_params.end:
        try:
            end_date = datetime.strptime(query_params.end, '%Y-%m-%d')
        except ValueError:
            return error_response("Invalid end date format", status_code=400)

    bookings = await booking_service.list_host_bookings(
        user_id, query_params.status, start_date, end_date
    )

    return success_response({'bookings': bookings, 'count': len(bookings)})


# ============================================================================
# GROUP AVAILABILITY ENDPOINTS (Phase 4D)
# ============================================================================

@calendar_bp.route('/booking-pages/<int:page_id>/members', methods=['POST'])
@async_endpoint
@validate_json(GroupMemberAddRequest)
async def add_group_member(validated_data: GroupMemberAddRequest, page_id):
    """Add a member to a group booking page."""
    user_context = get_user_context()
    user_id = user_context.get('user_id')

    if not user_id:
        return error_response("User authentication required", status_code=401)

    # TODO: Check if user is community admin or page owner

    success = await group_availability_service.add_member(
        page_id, validated_data.user_id, validated_data.is_required
    )

    if success:
        return success_response({'message': 'Member added successfully'})
    else:
        return error_response("Failed to add member", status_code=400)


@calendar_bp.route('/booking-pages/<int:page_id>/members/<int:user_id>', methods=['DELETE'])
@async_endpoint
async def remove_group_member(page_id, user_id):
    """Remove a member from a group booking page."""
    user_context = get_user_context()
    current_user_id = user_context.get('user_id')

    if not current_user_id:
        return error_response("User authentication required", status_code=401)

    # TODO: Check if user is community admin or page owner

    success = await group_availability_service.remove_member(page_id, user_id)

    if success:
        return success_response({'message': 'Member removed successfully'})
    else:
        return error_response("Failed to remove member", status_code=400)


@calendar_bp.route('/booking-pages/<int:page_id>/members', methods=['GET'])
@async_endpoint
async def get_group_members(page_id):
    """Get member list for a group booking page."""
    members = await group_availability_service.get_group_members(page_id)
    return success_response({'members': members, 'count': len(members)})


@calendar_bp.route('/booking-pages/<int:page_id>/group-availability', methods=['GET'])
@async_endpoint
@validate_query(AvailableSlotsQueryParams)
async def get_group_availability(query_params: AvailableSlotsQueryParams, page_id):
    """Get aggregate availability for a group booking page."""
    from datetime import datetime

    # Parse date
    try:
        date = datetime.strptime(query_params.date, '%Y-%m-%d')
    except ValueError:
        return error_response("Invalid date format, use YYYY-MM-DD", status_code=400)

    slots = await group_availability_service.get_group_availability(page_id, date)
    return success_response({'slots': slots, 'count': len(slots), 'date': query_params.date})


@calendar_bp.route('/booking-pages/<int:page_id>/best-slots', methods=['GET'])
@async_endpoint
@validate_query(BestSlotsParams)
async def get_best_slots(query_params: BestSlotsParams, page_id):
    """Get the N most available slots across a date range for a group."""
    from datetime import datetime

    # Parse dates
    try:
        start_date = datetime.strptime(query_params.start, '%Y-%m-%d')
        end_date = datetime.strptime(query_params.end, '%Y-%m-%d')
    except ValueError:
        return error_response("Invalid date format, use YYYY-MM-DD", status_code=400)

    if end_date <= start_date:
        return error_response("end date must be after start date", status_code=400)

    slots = await group_availability_service.get_most_available_slots(
        page_id, start_date, end_date, query_params.limit
    )

    return success_response({
        'slots': slots,
        'count': len(slots),
        'start': query_params.start,
        'end': query_params.end
    })


# ============================================================================
# TOURNAMENT BRACKET ENDPOINTS
# ============================================================================

tournament_bp = Blueprint('tournament', __name__, url_prefix='/api/v1/tournament')


@tournament_bp.route('', methods=['POST'])
@async_endpoint
async def create_tournament():
    """Create a new tournament."""
    data = await request.get_json()
    community_id = data.get('community_id')
    name = data.get('name')

    if not community_id or not name:
        return error_response("community_id and name are required", status_code=400)

    result = await tournament_service.create_tournament(
        community_id=community_id,
        name=name,
        bracket_type=data.get('bracket_type', 'single_elim'),
        max_participants=data.get('max_participants', 64),
        description=data.get('description'),
        event_id=data.get('event_id'),
        prize_pool_points=data.get('prize_pool_points', 0),
        prize_giveaway_id=data.get('prize_giveaway_id'),
        seeding_method=data.get('seeding_method', 'random'),
        check_in_required=data.get('check_in_required', False),
        registration_closes_at=data.get('registration_closes_at'),
    )

    if result and 'error' in result:
        return error_response(result['error'], status_code=400)

    return success_response(result, 201)


@tournament_bp.route('/<int:tournament_id>', methods=['GET'])
@async_endpoint
async def get_tournament(tournament_id: int):
    """Get tournament details."""
    result = await tournament_service.get_tournament(tournament_id)
    if not result:
        return error_response("Tournament not found", status_code=404)
    return success_response(result)


@tournament_bp.route('/<int:tournament_id>/register', methods=['POST'])
@async_endpoint
async def register_participant(tournament_id: int):
    """Register a participant."""
    data = await request.get_json()
    user_id = str(data.get('user_id', ''))
    platform = data.get('platform', '')

    if not user_id or not platform:
        return error_response("user_id and platform are required", status_code=400)

    result = await tournament_service.register_participant(
        tournament_id=tournament_id,
        user_id=user_id,
        platform=platform,
        display_name=data.get('display_name'),
    )

    status = 200 if result.get('success') else 400
    return success_response(result) if result.get('success') else error_response(result['message'], status_code=status)


@tournament_bp.route('/<int:tournament_id>/seed', methods=['POST'])
@async_endpoint
async def seed_bracket(tournament_id: int):
    """Seed participants and generate bracket matches."""
    result = await tournament_service.seed_bracket(tournament_id)
    if 'error' in result:
        return error_response(result['error'], status_code=400)
    return success_response(result)


@tournament_bp.route('/<int:tournament_id>/start', methods=['POST'])
@async_endpoint
async def start_tournament(tournament_id: int):
    """Start the tournament (transition from seeding to active)."""
    result = await tournament_service.start_tournament(tournament_id)
    if 'error' in result:
        return error_response(result['error'], status_code=400)
    return success_response(result)


@tournament_bp.route('/<int:tournament_id>/bracket', methods=['GET'])
@async_endpoint
async def get_bracket(tournament_id: int):
    """Get full bracket state with all rounds and matches."""
    result = await tournament_service.get_bracket_state(tournament_id)
    if 'error' in result:
        return error_response(result['error'], status_code=400)
    return success_response(result)


@tournament_bp.route('/<int:tournament_id>/matches/<int:match_id>/result', methods=['POST'])
@async_endpoint
async def report_match(tournament_id: int, match_id: int):
    """Report a match result."""
    data = await request.get_json()
    winner_id = data.get('winner_id')

    if not winner_id:
        return error_response("winner_id is required", status_code=400)

    result = await tournament_service.report_match_result(
        tournament_id=tournament_id,
        match_id=match_id,
        winner_id=winner_id,
        score_a=data.get('score_a', 0),
        score_b=data.get('score_b', 0),
    )

    if 'error' in result:
        return error_response(result['error'], status_code=400)
    return success_response(result)


@tournament_bp.route('/<int:tournament_id>/standings', methods=['GET'])
@async_endpoint
async def get_standings(tournament_id: int):
    """Get tournament standings."""
    standings = await tournament_service.get_standings(tournament_id)
    return success_response({
        'standings': standings,
        'count': len(standings),
    })


@tournament_bp.route('/<int:tournament_id>/complete', methods=['POST'])
@async_endpoint
async def complete_tournament(tournament_id: int):
    """Complete tournament and award prizes."""
    result = await tournament_service.complete_tournament(tournament_id)
    if 'error' in result:
        return error_response(result['error'], status_code=400)
    return success_response(result)


@calendar_bp.route('/<int:community_id>/tournaments', methods=['GET'])
@async_endpoint
async def list_community_tournaments(community_id: int):
    """List tournaments for a community."""
    status_filter = request.args.get('status')
    limit = request.args.get('limit', 20, type=int)

    tournaments = await tournament_service.list_community_tournaments(
        community_id, status=status_filter, limit=limit,
    )
    return success_response({
        'tournaments': tournaments,
        'count': len(tournaments),
    })


# Service-key gate on every blueprint -- registered on the Blueprint
# (not the Quart `app`) so it is enforced identically whether this module
# runs standalone (this file's `app`) or merged in-process into
# services/interactive-productivity/app.py, which imports these same
# Blueprint objects and registers them on its own `app`.
for _calendar_blueprint in (calendar_bp, context_bp, ticket_bp, tournament_bp):
    _calendar_blueprint.before_request(check_service_api_key)

# Register blueprints
app.register_blueprint(calendar_bp)
app.register_blueprint(context_bp)
app.register_blueprint(ticket_bp)
app.register_blueprint(tournament_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
