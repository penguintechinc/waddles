"""
Interactive Productivity Service - Combined Quart Application

Merges three productivity modules into a single service on port 8030:
1. Calendar Interaction (port 8030) - /api/v1/calendar, /api/v1/context, /api/v1/tournament
2. Memories Interaction - /api/v1/memories
3. Translate Interaction - /api/v1/translate (REST + gRPC on 50033)

Architecture: One Quart app + gRPC server for translate module
"""
import asyncio
import os
import sys
import logging
import logging.handlers
from collections import OrderedDict
from concurrent import futures
import time

import grpc

_base_dir = os.path.dirname(__file__)


_COLLIDING_MODULES = frozenset({
    'services', 'controllers', 'config', 'validation_models', 'models',
})


def _setup_module_imports(module_name: str) -> None:
    """Flush cached packages that collide across modules and prioritize module dir."""
    for key in list(sys.modules):
        top = key.split('.', 1)[0]
        if top in _COLLIDING_MODULES:
            del sys.modules[key]
    sys.path.insert(0, os.path.join(_base_dir, module_name))


# Initialize first module path for imports
_setup_module_imports('calendar_interaction_module')

from quart import Blueprint, Quart, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from flask_core import create_health_blueprint

# Load config from the current service
from config import Config

# Quart app setup
app = Quart(__name__)

# Logging setup
os.makedirs(Config.LOG_DIR if hasattr(Config, 'LOG_DIR') else '/tmp', exist_ok=True)
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL if hasattr(Config, 'LOG_LEVEL') else 'INFO'),
    format="[%(asctime)s] %(levelname)s %(name)s:%(lineno)d - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            f"{Config.LOG_DIR if hasattr(Config, 'LOG_DIR') else '/tmp'}/interactive-productivity.log",
            maxBytes=10485760,
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger(__name__)

# Global service instances
dal = None
calendar_service = None
calendar_bp = None
context_bp = None
ticket_bp = None
tournament_bp = None

quote_service = None
bookmark_service = None
reminder_service = None
memories_bp = None

translation_service = None
translate_bp = None
cache_manager = None


# ============================================================================
# HEALTH CHECKS
# ============================================================================

health_bp = create_health_blueprint('interactive-productivity', '2.0.0')
app.register_blueprint(health_bp)


# ============================================================================
# CALENDAR MODULE INITIALIZATION
# ============================================================================

def init_calendar_services():
    """Initialize calendar module services."""
    global dal, calendar_service, calendar_bp, context_bp, ticket_bp, tournament_bp

    if calendar_service is not None:
        return

    try:
        from calendar_interaction_module.config import Config as CalConfig
        from flask_core import (
            init_database, setup_aaa_logging
        )
        from calendar_interaction_module.services.calendar_service import CalendarService
        from calendar_interaction_module.services.permission_service import PermissionService
        from calendar_interaction_module.services.context_service import ContextService
        from calendar_interaction_module.services.rsvp_service import RSVPService
        from calendar_interaction_module.services.ticket_service import TicketService
        from calendar_interaction_module.services.event_admin_service import EventAdminService
        from calendar_interaction_module.services.calendar_oauth_service import CalendarOAuthService
        from calendar_interaction_module.services.availability_service import AvailabilityService
        from calendar_interaction_module.services.booking_service import BookingService
        from calendar_interaction_module.services.group_availability_service import GroupAvailabilityService
        from calendar_interaction_module.services.tournament_service import TournamentService

        calendar_logger = setup_aaa_logging('calendar_interaction_module', CalConfig.MODULE_VERSION)
        calendar_logger.system("Initializing calendar module", action="startup")

        dal = init_database(CalConfig.DATABASE_URL)
        app.config['dal'] = dal

        # Initialize services
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
        tournament_service = TournamentService(dal, CalConfig)

        # Blueprint setup - import endpoints after services initialized
        from calendar_interaction_module.app import (
            calendar_bp as cal_bp, context_bp as ctx_bp,
            ticket_bp as tick_bp, tournament_bp as tour_bp
        )
        calendar_bp = cal_bp
        context_bp = ctx_bp
        ticket_bp = tick_bp
        tournament_bp = tour_bp

        app.register_blueprint(calendar_bp)
        app.register_blueprint(context_bp)
        app.register_blueprint(ticket_bp)
        app.register_blueprint(tournament_bp)

        calendar_logger.system("Calendar module initialized", result="SUCCESS")

    except Exception as e:
        logger.error(f"Failed to initialize calendar module: {e}", exc_info=True)
        raise


# ============================================================================
# MEMORIES MODULE INITIALIZATION
# ============================================================================

def init_memories_services():
    """Initialize memories module services."""
    global dal, quote_service, bookmark_service, reminder_service, memories_bp

    if quote_service is not None:
        return

    try:
        _setup_module_imports('memories_interaction_module')
        from memories_interaction_module.config import Config as MemConfig
        from flask_core import (
            init_database, setup_aaa_logging
        )
        from memories_interaction_module.services.quote_service import QuoteService
        from memories_interaction_module.services.bookmark_service import BookmarkService
        from memories_interaction_module.services.reminder_service import ReminderService

        mem_logger = setup_aaa_logging('memories_interaction_module', MemConfig.MODULE_VERSION)
        mem_logger.system("Initializing memories module", action="startup")

        if dal is None:
            dal = init_database(MemConfig.DATABASE_URL)
            app.config['dal'] = dal

        quote_service = QuoteService(dal)
        bookmark_service = BookmarkService(dal)
        reminder_service = ReminderService(dal)

        # Create and register blueprint
        from flask_core import async_endpoint, success_response, error_response
        from flask_core.validation import validate_json, validate_query
        from memories_interaction_module.validation_models import (
            QuoteCreateRequest, QuoteSearchParams, QuoteVoteRequest, QuoteDeleteRequest,
            BookmarkCreateRequest, BookmarkSearchParams, BookmarkDeleteRequest,
            PopularBookmarksParams, ReminderCreateRequest, ReminderSearchParams,
            ReminderMarkSentRequest, ReminderDeleteRequest, UserRemindersParams
        )
        from datetime import datetime

        memories_bp = Blueprint('memories', __name__, url_prefix='/api/v1/memories')

        @memories_bp.route('/status')
        @async_endpoint
        async def status():
            return success_response({
                "status": "operational",
                "module": "memories_interaction_module",
                "version": MemConfig.MODULE_VERSION
            })

        # Quotes endpoints
        @memories_bp.route('/quotes', methods=['POST'])
        @validate_json(QuoteCreateRequest)
        @async_endpoint
        async def add_quote(validated_data: QuoteCreateRequest):
            try:
                quote = await quote_service.add_quote(
                    community_id=validated_data.community_id,
                    quote_text=validated_data.quote_text,
                    created_by_username=validated_data.created_by_username,
                    created_by_user_id=validated_data.created_by_user_id,
                    author_username=validated_data.author_username,
                    author_user_id=validated_data.author_user_id,
                    category=validated_data.category
                )
                mem_logger.audit(action="add_quote", community=validated_data.community_id,
                                user=validated_data.created_by_username, result="SUCCESS")
                return success_response(quote)
            except Exception as e:
                mem_logger.error(f"Failed to add quote: {e}")
                return error_response(str(e), status_code=500)

        @memories_bp.route('/quotes/<int:community_id>', methods=['GET'])
        @validate_query(QuoteSearchParams)
        @async_endpoint
        async def search_quotes(query_params: QuoteSearchParams, community_id: int):
            try:
                quotes = await quote_service.search_quotes(
                    community_id=community_id,
                    search_query=query_params.search_query,
                    category=query_params.category,
                    author=query_params.author,
                    limit=query_params.limit,
                    offset=query_params.offset
                )
                return success_response({'quotes': quotes, 'count': len(quotes)})
            except Exception as e:
                mem_logger.error(f"Failed to search quotes: {e}")
                return error_response(str(e), status_code=500)

        @memories_bp.route('/quotes/<int:community_id>/random', methods=['GET'])
        @async_endpoint
        async def get_random_quote(community_id: int):
            try:
                quote = await quote_service.get_quote(community_id)
                if quote:
                    return success_response(quote)
                return error_response("No quotes found", status_code=404)
            except Exception as e:
                mem_logger.error(f"Failed to get random quote: {e}")
                return error_response(str(e), status_code=500)

        @memories_bp.route('/quotes/<int:community_id>/<int:quote_id>', methods=['GET'])
        @async_endpoint
        async def get_quote(community_id: int, quote_id: int):
            try:
                quote = await quote_service.get_quote(community_id, quote_id)
                if quote:
                    return success_response(quote)
                return error_response("Quote not found", status_code=404)
            except Exception as e:
                mem_logger.error(f"Failed to get quote: {e}")
                return error_response(str(e), status_code=500)

        @memories_bp.route('/bookmarks', methods=['POST'])
        @validate_json(BookmarkCreateRequest)
        @async_endpoint
        async def add_bookmark(validated_data: BookmarkCreateRequest):
            try:
                bookmark = await bookmark_service.add_bookmark(
                    community_id=validated_data.community_id,
                    url=validated_data.url,
                    created_by_username=validated_data.created_by_username,
                    created_by_user_id=validated_data.created_by_user_id,
                    title=validated_data.title,
                    description=validated_data.description,
                    tags=validated_data.tags,
                    auto_fetch_metadata=validated_data.auto_fetch_metadata
                )
                mem_logger.audit(action="add_bookmark", community=validated_data.community_id,
                                user=validated_data.created_by_username, result="SUCCESS")
                return success_response(bookmark)
            except Exception as e:
                mem_logger.error(f"Failed to add bookmark: {e}")
                return error_response(str(e), status_code=500)

        @memories_bp.route('/reminders', methods=['POST'])
        @validate_json(ReminderCreateRequest)
        @async_endpoint
        async def create_reminder(validated_data: ReminderCreateRequest):
            try:
                try:
                    remind_at = datetime.fromisoformat(validated_data.remind_in.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    remind_at = await reminder_service.parse_relative_time(validated_data.remind_in)
                reminder = await reminder_service.create_reminder(
                    community_id=validated_data.community_id,
                    user_id=validated_data.user_id,
                    username=validated_data.username,
                    reminder_text=validated_data.reminder_text,
                    remind_at=remind_at,
                    channel=validated_data.channel,
                    platform_channel_id=validated_data.platform_channel_id,
                    recurring_rule=validated_data.recurring_rule
                )
                mem_logger.audit(action="create_reminder", community=validated_data.community_id,
                                user=validated_data.username, result="SUCCESS")
                return success_response(reminder)
            except Exception as e:
                mem_logger.error(f"Failed to create reminder: {e}")
                return error_response(str(e), status_code=500)

        app.register_blueprint(memories_bp)
        mem_logger.system("Memories module initialized", result="SUCCESS")

    except Exception as e:
        logger.error(f"Failed to initialize memories module: {e}", exc_info=True)
        raise


# ============================================================================
# TRANSLATE MODULE INITIALIZATION & CACHE
# ============================================================================

class _CacheManager:
    """Simple in-memory LRU + Redis cache manager."""

    def __init__(self):
        self._memory = OrderedDict()
        self._max = 1000
        self._redis = None

    async def _get_redis(self):
        if self._redis is None and hasattr(Config, 'REDIS_URL') and Config.REDIS_URL:
            try:
                import aioredis
                self._redis = await aioredis.from_url(Config.REDIS_URL)
            except Exception:
                pass
        return self._redis

    async def get(self, key: str):
        if key in self._memory:
            val, expires = self._memory[key]
            if expires > time.time():
                self._memory.move_to_end(key)
                return val
            del self._memory[key]
        r = await self._get_redis()
        if r:
            try:
                val = await r.get(key)
                if val:
                    return val.decode() if isinstance(val, bytes) else val
            except Exception:
                pass
        return None

    async def set(self, key: str, value, ttl: int = 3600):
        if len(self._memory) >= self._max:
            self._memory.popitem(last=False)
        self._memory[key] = (value, time.time() + ttl)
        r = await self._get_redis()
        if r:
            try:
                await r.set(key, value, ex=ttl)
            except Exception:
                pass

    async def delete(self, key: str):
        self._memory.pop(key, None)
        r = await self._get_redis()
        if r:
            try:
                await r.delete(key)
            except Exception:
                pass


def init_translate_services():
    """Initialize translate module services (REST + gRPC)."""
    global dal, translation_service, translate_bp, cache_manager

    if translation_service is not None:
        return

    try:
        _setup_module_imports('translate_interaction_module')
        from translate_interaction_module.config import Config as TransConfig
        from translate_interaction_module.services.translation_service import TranslationService
        from translate_interaction_module.proto import translate_interaction_pb2_grpc
        from translate_interaction_module.services.grpc_handler import TranslateInteractionServicer

        trans_logger = logging.getLogger('translate_interaction_module')
        trans_logger.info(f"Initializing translate module REST on {Config.MODULE_PORT} gRPC on 50033")

        if dal is None:
            from penguin_dal import DAL
            dal = DAL(TransConfig.DATABASE_URL, folder=None, pool_size=10)
            app.config['dal'] = dal

        cache_manager = _CacheManager()
        translation_service = TranslationService(dal=dal, cache_manager=cache_manager)

        # REST endpoints
        translate_bp = Blueprint('translate', __name__, url_prefix='/api/v1/translate')

        @translate_bp.route('', methods=['POST'])
        async def translate():
            """REST translation endpoint."""
            data = await request.get_json()
            if not data:
                return jsonify({"error": "Request body required"}), 400
            text = data.get('text', '').strip()
            target_lang = data.get('target_lang', 'en')
            community_id = data.get('community_id')
            platform = data.get('platform', 'unknown')
            channel_id = data.get('channel_id', '')
            if not text:
                return jsonify({"error": "text is required"}), 400
            if not community_id:
                return jsonify({"error": "community_id is required"}), 400
            try:
                handler = TranslateInteractionServicer(translation_service, dal)
                config = await handler._load_config(community_id)
                result = await translation_service.translate(
                    text=text, target_lang=target_lang,
                    community_id=community_id, config=config,
                    platform=platform, channel_id=channel_id,
                )
                if result is None:
                    return jsonify({"skipped": True, "reason": "translation not needed"})
                return jsonify({"status": "success", "data": result})
            except Exception as e:
                trans_logger.error(f"REST translate error: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        @translate_bp.route('/detect', methods=['POST'])
        async def detect():
            data = await request.get_json()
            if not data or not data.get('text', '').strip():
                return jsonify({"error": "text is required"}), 400
            try:
                lang, conf = await translation_service._detect_language(data['text'])
                return jsonify({"detected_lang": lang, "confidence": conf})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @translate_bp.route('/cache/stats', methods=['GET'])
        async def cache_stats():
            try:
                stats = await translation_service.get_cache_stats()
                return jsonify(stats)
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @translate_bp.route('/cache/cleanup', methods=['POST'])
        async def cache_cleanup():
            try:
                await translation_service.cleanup_cache()
                return jsonify({"message": "Cache cleanup complete"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        app.register_blueprint(translate_bp)
        trans_logger.info("Translate module initialized")

    except Exception as e:
        logger.error(f"Failed to initialize translate module: {e}", exc_info=True)
        # Don't raise - translate is optional if proto files not available


# ============================================================================
# APP LIFECYCLE
# ============================================================================

@app.before_serving
async def startup():
    """Initialize all modules on app startup."""
    logger.info("Starting interactive-productivity service on port 8030")
    init_calendar_services()
    init_memories_services()
    try:
        init_translate_services()
    except Exception as e:
        logger.warning(f"Translate module optional - skipping: {e}")
    logger.info("All modules initialized successfully")


# ============================================================================
# GRPC SERVER (for translate module)
# ============================================================================

async def start_grpc_server():
    """Run gRPC server (translate module) on port 50033."""
    try:
        if translation_service is None:
            logger.info("Translate service not initialized - skipping gRPC")
            return

        _setup_module_imports('translate_interaction_module')
        from translate_interaction_module.proto import translate_interaction_pb2_grpc
        from translate_interaction_module.services.grpc_handler import TranslateInteractionServicer
        from flask_core.grpc_tls import bind_secure_port, default_server_options

        server = grpc.aio.server(
            futures.ThreadPoolExecutor(max_workers=10),
            options=default_server_options(),
        )
        translate_interaction_pb2_grpc.add_TranslateInteractionServicer_to_server(
            TranslateInteractionServicer(translation_service, dal),
            server,
        )
        bind_secure_port(server, "0.0.0.0:50033")
        await server.start()
        logger.info("gRPC server (TLS) listening on port 50033")
        await server.wait_for_termination()
    except Exception as e:
        logger.warning(f"gRPC server startup failed (translate optional): {e}")


# ============================================================================
# ENTRY POINT
# ============================================================================

async def main():
    hconfig = HypercornConfig()
    hconfig.bind = ["0.0.0.0:8030"]

    grpc_task = asyncio.create_task(start_grpc_server())
    rest_task = asyncio.create_task(serve(app, hconfig))

    await asyncio.gather(grpc_task, rest_task)


if __name__ == '__main__':
    asyncio.run(main())
