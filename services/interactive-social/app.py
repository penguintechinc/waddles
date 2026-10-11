"""
Interactive Social Service - Combined Quart Application

Merges 4 interactive modules into one service:
1. alias_interaction_module - Alias/nickname management (port 8010)
2. shoutout_interaction_module - Shoutout announcements
3. presence_module - User presence tracking
4. quote_interaction_module - Quote management
"""
import asyncio
import os
import sys

# Add module directories to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'alias_interaction_module'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shoutout_interaction_module'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'presence_module'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'quote_interaction_module'))

# Also add shared libs path (two levels up from this file)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'libs'))

from quart import Quart, Blueprint, request  # noqa: E402
from flask_core import (  # noqa: E402
    setup_aaa_logging,
    init_database,
    async_endpoint,
    success_response,
    error_response,
    create_health_blueprint,
    auth_required,
)

# Import configs and services from each module
from alias_interaction_module.config import Config as AliasConfig  # noqa: E402
from shoutout_interaction_module.config import Config as ShoutoutConfig  # noqa: E402
from presence_module.config import Config as PresenceConfig  # noqa: E402
from quote_interaction_module.config import Config as QuoteConfig  # noqa: E402

# Use primary config (alias module default, 8010 port)
class Config:  # noqa: E302
    MODULE_NAME = "interactive-social"
    MODULE_VERSION = "1.0.0"
    MODULE_PORT = 8010
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:memory')
    DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '10'))
    READ_REPLICA_URL = os.getenv('READ_REPLICA_URL')
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
    TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
    TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
    YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
    IDENTITY_URL = os.getenv('IDENTITY_URL', 'http://identity-service:8000')
    AUTO_APPROVE_QUOTES = os.getenv('AUTO_APPROVE_QUOTES', 'false').lower() == 'true'
    DEFAULT_PAGE_SIZE = int(os.getenv('DEFAULT_PAGE_SIZE', '50'))
    MAX_PAGE_SIZE = int(os.getenv('MAX_PAGE_SIZE', '100'))
    MIN_SEARCH_QUERY_LENGTH = int(os.getenv('MIN_SEARCH_QUERY_LENGTH', '3'))

    @staticmethod
    def validate():
        if not Config.REDIS_URL:
            raise ValueError('REDIS_URL environment variable is required')

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# Create main API blueprint
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
db_pool = None
alias_service = None
shoutout_service = None
twitch_service = None
video_service = None
identity_service = None
video_shoutout_service = None
presence_service = None
quote_service = None


@app.before_serving
async def startup():  # noqa: E302
    """Initialize all services on startup"""
    global dal, db_pool, alias_service, shoutout_service, twitch_service
    global video_service, identity_service, video_shoutout_service
    global presence_service, quote_service

    logger.system("Starting interactive-social service", action="startup")

    # Initialize database
    dal = init_database(
        Config.DATABASE_URL,
        pool_size=Config.DB_POOL_SIZE,
        read_replica_uri=Config.READ_REPLICA_URL
    )
    app.config['dal'] = dal

    try:
        # Initialize alias service
        from alias_interaction_module.services.alias_service import AliasService
        alias_service = AliasService(dal)
        app.config['alias_service'] = alias_service
        logger.system("Alias service initialized", result="SUCCESS")
    except Exception as e:
        logger.error(f"Failed to initialize alias service: {e}")

    try:
        # Initialize shoutout services
        from shoutout_interaction_module.services.twitch_service import TwitchService
        from shoutout_interaction_module.services.shoutout_service import ShoutoutService
        from shoutout_interaction_module.services.video_service import VideoService
        from shoutout_interaction_module.services.identity_service import IdentityService
        from shoutout_interaction_module.services.video_shoutout_service import VideoShoutoutService
        import asyncpg

        db_pool = await asyncpg.create_pool(Config.DATABASE_URL)
        twitch_service = TwitchService(
            client_id=Config.TWITCH_CLIENT_ID,
            client_secret=Config.TWITCH_CLIENT_SECRET
        )
        shoutout_service = ShoutoutService(dal)
        video_service = VideoService(
            twitch_client_id=Config.TWITCH_CLIENT_ID,
            twitch_client_secret=Config.TWITCH_CLIENT_SECRET,
            youtube_api_key=Config.YOUTUBE_API_KEY
        )
        identity_service = IdentityService(Config.IDENTITY_URL)
        video_shoutout_service = VideoShoutoutService(
            db_pool=db_pool,
            video_service=video_service,
            identity_service=identity_service
        )
        app.config['twitch_service'] = twitch_service
        app.config['shoutout_service'] = shoutout_service
        app.config['video_service'] = video_service
        logger.system("Shoutout services initialized", result="SUCCESS")
    except Exception as e:
        logger.error(f"Failed to initialize shoutout services: {e}")

    try:
        # Initialize presence service
        Config.validate()
        import redis.asyncio as aioredis
        from presence_module.presence import PresenceStateStore, PresenceSyncEngine
        from presence_module.services.presence_service import PresenceService

        redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
        state_store = PresenceStateStore(redis_client)
        sync_engine = PresenceSyncEngine(state_store=state_store)
        presence_service = PresenceService(
            state_store=state_store,
            sync_engine=sync_engine,
            redis_client=redis_client,
        )
        app.config['presence_service'] = presence_service
        logger.system("Presence service initialized", result="SUCCESS")
    except Exception as e:
        logger.error(f"Failed to initialize presence service: {e}")

    try:
        # Initialize quote service
        from quote_interaction_module.services.quote_service import QuoteService
        quote_service = QuoteService(dal)
        app.config['quote_service'] = quote_service
        logger.system("Quote service initialized", result="SUCCESS")
    except Exception as e:
        logger.error(f"Failed to initialize quote service: {e}")

    logger.system("interactive-social service started", result="SUCCESS")


# =====================================================
# ALIAS ENDPOINTS
# =====================================================

@api_bp.route('/aliases', methods=['GET', 'POST'])
@async_endpoint
async def aliases():
    """List or create aliases"""
    if request.method == 'GET':
        community_id = request.args.get('community_id')
        aliases_list = await alias_service.list_aliases(community_id)
        return success_response(aliases_list)
    else:
        data = await request.get_json()
        alias = await alias_service.create_alias(
            data['community_id'], data['alias_name'],
            data['command'], data['created_by']
        )
        return success_response(alias, status_code=201)


@api_bp.route('/aliases/<alias_id>', methods=['DELETE'])
@async_endpoint
async def delete_alias(alias_id):
    """Delete alias"""
    await alias_service.delete_alias(alias_id)
    return success_response({"message": "Alias deleted"})


@api_bp.route('/aliases/execute', methods=['POST'])
@async_endpoint
async def execute_alias():
    """Execute alias with variable substitution"""
    data = await request.get_json()
    command = await alias_service.execute_alias(
        data['alias_name'], data['user'], data.get('args', [])
    )
    if command:
        return success_response({"command": command})
    return error_response("Alias not found", status_code=404)


# =====================================================
# SHOUTOUT ENDPOINTS
# =====================================================

@api_bp.route('/shoutout', methods=['POST'])
@async_endpoint
async def create_shoutout():
    """Generate shoutout for a Twitch user"""
    try:
        data = await request.get_json()
        username = data.get('username')
        community_id = data.get('community_id')
        platform = data.get('platform', 'twitch')

        if not username:
            return error_response("username is required", status_code=400)
        if not community_id:
            return error_response("community_id is required", status_code=400)

        logger.audit(
            action="create_shoutout",
            user="system",
            community=community_id,
            result="STARTED",
            target_user=username
        )

        twitch_data = await twitch_service.get_full_shoutout_data(username)
        if not twitch_data:
            return error_response(
                f"User '{username}' not found on Twitch",
                status_code=404
            )

        shoutout = await shoutout_service.generate_shoutout(
            twitch_data,
            community_id,
            platform
        )

        logger.audit(
            action="create_shoutout",
            user="system",
            community=community_id,
            result="SUCCESS",
            target_user=username
        )

        return success_response(shoutout)

    except Exception as e:
        logger.error(f"Failed to create shoutout: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/shoutout/history/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_shoutout_history(community_id: int):
    """Get shoutout history for community"""
    try:
        limit = int(request.args.get('limit', 50))
        history = await shoutout_service.get_shoutout_history(
            community_id,
            limit
        )
        return success_response({
            'history': history,
            'count': len(history)
        })
    except Exception as e:
        logger.error(f"Failed to get history: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/shoutout/stats/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_shoutout_stats(community_id: int):
    """Get shoutout statistics for community"""
    try:
        stats = await shoutout_service.get_stats(community_id)
        return success_response(stats)
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/shoutout/template', methods=['POST'])
@auth_required
@async_endpoint
async def save_shoutout_template():
    """Save custom shoutout template"""
    try:
        data = await request.get_json()
        community_id = data.get('community_id')
        platform = data.get('platform', 'twitch')
        is_live = data.get('is_live', True)
        template = data.get('template')

        if not all([community_id, template]):
            return error_response(
                "community_id and template are required",
                status_code=400
            )

        success = await shoutout_service.save_custom_template(
            community_id,
            platform,
            is_live,
            template
        )

        if success:
            logger.audit(
                action="save_template",
                user="system",
                community=community_id,
                result="SUCCESS"
            )
            return success_response({"message": "Template saved"})
        else:
            return error_response("Failed to save template", status_code=500)

    except Exception as e:
        logger.error(f"Failed to save template: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/shoutout/twitch/user/<username>', methods=['GET'])
@auth_required
@async_endpoint
async def get_twitch_user(username: str):
    """Get Twitch user information"""
    try:
        data = await twitch_service.get_full_shoutout_data(username)
        if not data:
            return error_response("User not found", status_code=404)
        return success_response(data)
    except Exception as e:
        logger.error(f"Failed to get Twitch user: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/shoutout/circuit-breaker/metrics', methods=['GET'])
@auth_required
@async_endpoint
async def circuit_breaker_metrics():
    """Get circuit breaker metrics for Twitch API"""
    try:
        metrics = twitch_service.get_circuit_breaker_metrics()
        return success_response(metrics)
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        return error_response(str(e), status_code=500)


# Video shoutout endpoints
@api_bp.route('/shoutout/video-shoutout', methods=['POST'])
@async_endpoint
async def execute_video_shoutout():
    """Execute a video shoutout (!vso command)"""
    try:
        from dataclasses import asdict
        data = await request.get_json()

        community_id = data.get('community_id')
        target_username = data.get('target_username')
        target_platform = data.get('target_platform', 'twitch')
        triggered_by_user_id = data.get('triggered_by_user_id')
        triggered_by_username = data.get('triggered_by_username')
        user_roles = data.get('user_roles', [])

        if not community_id or not target_username:
            return error_response(
                "community_id and target_username required",
                status_code=400
            )

        result = await video_shoutout_service.execute_video_shoutout(
            community_id=community_id,
            target_username=target_username,
            target_platform=target_platform,
            trigger_type='manual',
            triggered_by_user_id=triggered_by_user_id,
            triggered_by_username=triggered_by_username,
            user_roles=user_roles
        )

        if not result.success:
            logger.audit(
                action="video_shoutout",
                user="system",
                community=community_id,
                result="FAILED",
                target_user=target_username,
                error=result.error
            )
            return error_response(result.error, status_code=400)

        response_data = {
            'success': True,
            'video': asdict(result.video) if result.video else None,
            'channel': asdict(result.channel) if result.channel else None,
            'game_name': result.game_name,
            'is_live': result.is_live
        }

        return success_response(response_data)

    except Exception as e:
        logger.error(f"Video shoutout failed: {e}")
        return error_response(str(e), status_code=500)


# =====================================================
# PRESENCE ENDPOINTS
# =====================================================

@api_bp.route('/presence/update', methods=['POST'])
@async_endpoint
async def update_presence():
    """Process an incoming presence update from a platform"""
    try:
        data = await request.get_json()
        if not data:
            return error_response("Request body is required", status_code=400)

        user_id = data.get('user_id')
        source_platform = data.get('source_platform')
        canonical_status = data.get('canonical_status')
        platform_status = data.get('platform_status', canonical_status)
        metadata = data.get('metadata', {})

        if not user_id:
            return error_response("user_id is required", status_code=400)
        if not source_platform:
            return error_response("source_platform is required", status_code=400)
        if not canonical_status:
            return error_response("canonical_status is required", status_code=400)

        logger.audit(
            action="presence_update",
            user=user_id,
            community="",
            result="STARTED",
            platform=source_platform,
            status=canonical_status,
        )

        result = await presence_service.process_presence_update(
            user_id=user_id,
            source_platform=source_platform,
            canonical_status=canonical_status,
            platform_status=platform_status,
            metadata=metadata,
        )

        logger.audit(
            action="presence_update",
            user=user_id,
            community="",
            result="SUCCESS",
            platform=source_platform,
            status=canonical_status,
        )
        return success_response(result)

    except ValueError as exc:
        return error_response(str(exc), status_code=422)
    except Exception as exc:
        logger.error("Failed to process presence update: %s", exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>', methods=['GET'])
@async_endpoint
async def get_presence(user_id: str):
    """Get the aggregated presence for a user"""
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        platform = request.args.get('platform')
        all_platforms = request.args.get('all_platforms', '').lower() == 'true'

        presence_data = await presence_service.get_user_presence(
            user_id=user_id,
            platform=platform,
            all_platforms=all_platforms,
        )

        if presence_data is None:
            return error_response(
                f"No presence data found for user {user_id}",
                status_code=404,
            )

        return success_response(presence_data)

    except Exception as exc:
        logger.error("Failed to get presence for user %s: %s", user_id, exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>/settings', methods=['GET'])
@async_endpoint
async def get_user_settings(user_id: str):
    """Get presence sync settings for a user"""
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        settings = await presence_service.get_user_settings(user_id)
        return success_response(settings)

    except Exception as exc:
        logger.error(
            "Failed to get settings for user %s: %s", user_id, exc
        )
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>/settings', methods=['PUT'])
@async_endpoint
async def update_user_settings(user_id: str):
    """Update presence sync settings for a user"""
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        data = await request.get_json()
        if not data:
            return error_response("Request body is required", status_code=400)

        updated = await presence_service.update_user_settings(user_id, data)

        logger.audit(
            action="update_presence_settings",
            user=user_id,
            community="",
            result="SUCCESS",
        )
        return success_response(updated)

    except ValueError as exc:
        return error_response(str(exc), status_code=422)
    except Exception as exc:
        logger.error(
            "Failed to update settings for user %s: %s", user_id, exc
        )
        return error_response(str(exc), status_code=500)


# =====================================================
# QUOTE ENDPOINTS
# =====================================================

@api_bp.route('/quotes', methods=['POST'])
@async_endpoint
async def add_quote():
    """Add a new quote"""
    try:
        data = await request.get_json()

        if not data.get('community_id') or not data.get('text'):
            return error_response(
                "Missing required fields: community_id, text",
                status_code=400
            )

        quote = await quote_service.add_quote(
            community_id=data['community_id'],
            text=data['text'],
            author=data.get('author'),
            added_by_user_id=data.get('added_by_user_id'),
            quoted_user_id=data.get('quoted_user_id'),
            platform=data.get('platform'),
            context=data.get('context'),
            tags=data.get('tags'),
            is_approved=data.get('is_approved', Config.AUTO_APPROVE_QUOTES)
        )

        return success_response(quote, status_code=201)

    except Exception as e:
        logger.error(f"Failed to add quote: {e}")
        return error_response(f"Failed to add quote: {str(e)}", status_code=500)


@api_bp.route('/quotes/<int:quote_id>', methods=['GET'])
@async_endpoint
async def get_quote(quote_id):
    """Get a specific quote by ID"""
    try:
        quote = await quote_service.get_quote(quote_id)

        if not quote:
            return error_response("Quote not found", status_code=404)

        return success_response(quote)

    except Exception as e:
        logger.error(f"Failed to get quote: {e}")
        return error_response(f"Failed to get quote: {str(e)}", status_code=500)


@api_bp.route('/quotes/random/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_random_quote(community_id):
    """Get a random quote from community"""
    try:
        quote = await quote_service.get_random_quote(community_id)

        if not quote:
            return error_response(
                "No quotes available for this community",
                status_code=404
            )

        return success_response(quote)

    except Exception as e:
        logger.error(f"Failed to get random quote: {e}")
        return error_response(f"Failed to get random quote: {str(e)}", status_code=500)


@api_bp.route('/quotes/list/<int:community_id>', methods=['GET'])
@async_endpoint
async def list_quotes(community_id):
    """List quotes for a community with pagination"""
    try:
        limit = min(
            int(request.args.get('limit', Config.DEFAULT_PAGE_SIZE)),
            Config.MAX_PAGE_SIZE
        )
        offset = int(request.args.get('offset', 0))
        only_approved = request.args.get('approved', 'true').lower() == 'true'

        quotes, total_count = await quote_service.get_quotes(
            community_id=community_id,
            limit=limit,
            offset=offset,
            only_approved=only_approved
        )

        return success_response({
            'quotes': quotes,
            'pagination': {
                'limit': limit,
                'offset': offset,
                'total': total_count,
                'has_more': (offset + limit) < total_count
            }
        })

    except Exception as e:
        logger.error(f"Failed to list quotes: {e}")
        return error_response(f"Failed to list quotes: {str(e)}", status_code=500)


@api_bp.route('/quotes/search/<int:community_id>', methods=['GET'])
@async_endpoint
async def search_quotes(community_id):
    """Search quotes using full-text search"""
    try:
        query = request.args.get('q', '').strip()

        if not query or len(query) < Config.MIN_SEARCH_QUERY_LENGTH:
            return error_response(
                f"Search query must be at least {Config.MIN_SEARCH_QUERY_LENGTH} characters",
                status_code=400
            )

        limit = min(
            int(request.args.get('limit', Config.DEFAULT_PAGE_SIZE)),
            Config.MAX_PAGE_SIZE
        )
        offset = int(request.args.get('offset', 0))

        quotes, total_count = await quote_service.search_quotes(
            community_id=community_id,
            query=query,
            limit=limit,
            offset=offset
        )

        return success_response({
            'query': query,
            'quotes': quotes,
            'pagination': {
                'limit': limit,
                'offset': offset,
                'total': total_count,
                'has_more': (offset + limit) < total_count
            }
        })

    except Exception as e:
        logger.error(f"Failed to search quotes: {e}")
        return error_response(f"Failed to search quotes: {str(e)}", status_code=500)


@api_bp.route('/quotes/author/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_by_author(community_id):
    """Get quotes by a specific author"""
    try:
        author = request.args.get('author', '').strip()

        if not author:
            return error_response("Author name is required", status_code=400)

        limit = min(
            int(request.args.get('limit', Config.DEFAULT_PAGE_SIZE)),
            Config.MAX_PAGE_SIZE
        )
        offset = int(request.args.get('offset', 0))

        quotes, total_count = await quote_service.get_quotes_by_author(
            community_id=community_id,
            author=author,
            limit=limit,
            offset=offset
        )

        return success_response({
            'author': author,
            'quotes': quotes,
            'pagination': {
                'limit': limit,
                'offset': offset,
                'total': total_count,
                'has_more': (offset + limit) < total_count
            }
        })

    except Exception as e:
        logger.error(f"Failed to get quotes by author: {e}")
        return error_response(f"Failed to get quotes by author: {str(e)}", status_code=500)


@api_bp.route('/quotes/<int:quote_id>', methods=['PUT'])
@async_endpoint
async def update_quote(quote_id):
    """Update a quote"""
    try:
        data = await request.get_json()

        success = await quote_service.update_quote(
            quote_id=quote_id,
            text=data.get('text'),
            author=data.get('author'),
            context=data.get('context'),
            tags=data.get('tags'),
            is_approved=data.get('is_approved'),
            platform=data.get('platform')
        )

        if not success:
            return error_response("Quote not found", status_code=404)

        return success_response({
            'id': quote_id,
            'message': 'Quote updated successfully'
        })

    except Exception as e:
        logger.error(f"Failed to update quote: {e}")
        return error_response(f"Failed to update quote: {str(e)}", status_code=500)


@api_bp.route('/quotes/<int:quote_id>', methods=['DELETE'])
@async_endpoint
async def delete_quote(quote_id):
    """Delete a quote (soft-delete)"""
    try:
        success = await quote_service.delete_quote(quote_id)

        if not success:
            return error_response("Quote not found", status_code=404)

        return success_response({
            'id': quote_id,
            'message': 'Quote deleted successfully'
        })

    except Exception as e:
        logger.error(f"Failed to delete quote: {e}")
        return error_response(f"Failed to delete quote: {str(e)}", status_code=500)


@api_bp.route('/quotes/stats/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_quote_stats(community_id):
    """Get quote statistics for a community"""
    try:
        stats = await quote_service.get_quote_stats(community_id)
        return success_response(stats)

    except Exception as e:
        logger.error(f"Failed to get quote stats: {e}")
        return error_response(f"Failed to get quote stats: {str(e)}", status_code=500)


# =====================================================
# GENERAL STATUS
# =====================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Get service status"""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "services": {
            "aliases": alias_service is not None,
            "shoutouts": shoutout_service is not None,
            "presence": presence_service is not None,
            "quotes": quote_service is not None,
        }
    })


app.register_blueprint(api_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
