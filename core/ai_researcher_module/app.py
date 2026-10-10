"""
AI Researcher Module - Quart Application
Contextual AI assistant with mem0 integration, bot detection, and research capabilities
"""
import os
import sys
import asyncio
import json
from datetime import datetime, timedelta

from quart import Quart, Blueprint, request
from quart_cors import cors
import redis.asyncio as aioredis

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs'))
from flask_core import (  # noqa: E402
    setup_aaa_logging,
    init_database,
    install_security_headers,
    async_endpoint,
    auth_required,
    success_response,
    error_response,
    create_health_blueprint,
    install_rate_limiting,
    describe_db_error,
)
from config import Config  # noqa: E402

# Import services
from services.ai_provider import AIProviderService  # noqa: E402
from services.safety_layer import SafetyLayer  # noqa: E402
from services.rate_limiter import RateLimiter  # noqa: E402
from services.research_service import ResearchService  # noqa: E402
from services.summary_service import SummaryService  # noqa: E402
from services.bot_detection import BotDetectionService  # noqa: E402
from services.mem0_service import Mem0Service  # noqa: E402
from services.insights_service import InsightsService  # noqa: E402
from services.anomaly_detector import AnomalyDetector  # noqa: E402
from services.behavior_profiler import BehaviorProfiler  # noqa: E402
from services.sentiment_analyzer import SentimentAnalyzer  # noqa: E402
from services.searxng_service import SearXNGService  # noqa: E402
from blueprints.game_bp import game_bp, setup as setup_game_bp  # noqa: E402
from blueprints.patch_bp import patch_bp, setup as setup_patch_bp  # noqa: E402
from blueprints.build_bp import build_bp, setup as setup_build_bp  # noqa: E402
from blueprints.tech_bp import tech_bp, setup as setup_tech_bp  # noqa: E402
from blueprints.price_bp import price_bp, setup as setup_price_bp  # noqa: E402
from blueprints.clip_researcher_bp import clip_researcher_bp, setup as setup_clip_researcher_bp  # noqa: E402
from blueprints.event_lookup_bp import event_lookup_bp, setup as setup_event_lookup_bp  # noqa: E402

app = Quart(__name__)
# security.md A05/A01 fix -- was `allow_origin="*"` (any site's browser JS
# could call this service's endpoints, none of which required auth). Now a
# configured exact-origin allowlist (`Config.CORS_ALLOWED_ORIGINS`, env
# `CORS_ALLOWED_ORIGINS`) -- unset/empty denies every cross-origin caller,
# never falls back to a wildcard. Same-origin/server-to-server callers
# (no `Origin` header) are unaffected either way; CORS only governs
# browser-enforced cross-origin JS.
app = cors(app, allow_origin=Config.CORS_ALLOWED_ORIGINS)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): only the /research /ask /recall /summarize command
# endpoints had rate limiting (per-command, via services/rate_limiter.py
# below) -- every other route here (status, firehose ingest, admin config,
# insights, anomalies, sentiment, behavior profiles, bot detection) had
# none. This is the same global-hook fix hub_api applied for its own 449
# endpoints (gh security PR #255), shared via flask_core.
install_rate_limiting(app, namespace=Config.MODULE_NAME, redis_url=Config.REDIS_URL)

# API Blueprints
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
researcher_bp = Blueprint('researcher', __name__, url_prefix='/api/v1/researcher')
admin_bp = Blueprint('admin', __name__, url_prefix='/api/v1/admin')

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
redis_client = None
ai_provider = None
safety_layer = None
rate_limiter = None
research_service = None
summary_service = None
searxng_service = None
bot_detection_services = {}  # community_id -> BotDetectionService
mem0_services = {}  # community_id -> Mem0Service


def _verify_service_key():
    """Verify X-Service-Key header for internal endpoints."""
    import secrets
    if not Config.SERVICE_API_KEY:
        logger.error("SERVICE_API_KEY not configured - rejecting request", action="security")
        return False  # No key configured, reject all requests for security
    key = request.headers.get('X-Service-Key', '')
    # Use constant-time comparison to prevent timing attacks
    return secrets.compare_digest(key, Config.SERVICE_API_KEY)


#: Paths every caller (including an unauthenticated browser) must reach --
#: k8s liveness/readiness probes never carry `X-Service-Key`, and `/status`
#: is deliberately public (module/feature-flag info only, no PII) -- same
#: exemption `tests/test_auth_required.py::TestUnaffectedRoutesStillWork::
#: test_status_endpoint_is_still_public` asserts for the sibling
#: `@auth_required` fix below.
_AUTH_EXEMPT_PATHS = frozenset({'/health', '/healthz', '/api/v1/status'})


def _has_valid_bearer_jwt() -> bool:
    """True if `Authorization: Bearer <token>` carries a JWT this service's `SECRET_KEY` signed.

    Same verification `flask_core.api_utils.auth_required` itself performs
    -- deliberately duplicated (not calling `auth_required` directly, which
    is a route decorator, not a standalone predicate) rather than
    reimplemented differently, so this blanket gate and the 17 routes'
    own `@auth_required` decorator agree on what counts as valid.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    from flask_core.auth import verify_jwt_token
    from flask_core.secrets import require_secret_key
    token = auth_header[7:]
    return verify_jwt_token(token, require_secret_key()) is not None


@app.before_request
async def _require_service_key():
    """security.md A01 fix -- every non-exempt route now requires SOME verified caller identity.

    Previously only `/researcher/messages/firehose` and `/researcher/stream/
    end` called `_verify_service_key()` inline; everything else had no auth
    at all -- combined with the `allow_origin="*"` CORS config this
    replaced, any site's browser JS could call them directly. A single
    `before_request` hook closes every route, present and future, the same
    way `hub_api/services/rate_limiting.py`'s global hook covers all 449 of
    its routes rather than needing a per-route decorator that a new route
    could forget.

    Accepts EITHER a valid `X-Service-Key` (internal service-to-service
    calls) OR a valid bearer JWT (`_has_valid_bearer_jwt`, same check
    `@auth_required` performs) -- not X-Service-Key alone. The 17 user-
    facing routes below already carry their own `@auth_required` decorator
    (added independently -- see `tests/test_auth_required.py`); requiring
    ONLY a service key here as well would 401 every real, correctly-
    JWT-authenticated frontend caller of those routes before `@auth_required`
    ever got a chance to run. The game/patch/build/tech/price/clip-research/
    event-lookup sub-blueprints (registered below, never covered by
    `@auth_required` or an inline service-key check) still get real,
    closed-by-default protection from this same hook -- either credential
    is accepted, but at least one is now mandatory everywhere but the
    exempt paths.

    `OPTIONS` (CORS preflight) is exempt: browsers never attach custom
    headers to a preflight request, so gating it here would make every
    real cross-origin call from an allowed origin fail before `quart_cors`
    ever gets to answer the preflight.
    """
    if request.path in _AUTH_EXEMPT_PATHS or request.method == 'OPTIONS':
        return None
    if _verify_service_key() or _has_valid_bearer_jwt():
        return None
    return error_response("Unauthorized", 401)


def _get_mem0_service(community_id: int) -> Mem0Service:
    """Get or create Mem0Service for a community."""
    global mem0_services
    if community_id not in mem0_services:
        mem0_services[community_id] = Mem0Service(community_id=community_id)
    return mem0_services[community_id]


def _get_bot_detection_service(community_id: int, is_premium: bool = False) -> BotDetectionService:
    """Get or create BotDetectionService for a community."""
    global bot_detection_services
    key = f"{community_id}:{is_premium}"
    if key not in bot_detection_services:
        bot_detection_services[key] = BotDetectionService(dal, is_premium=is_premium)
    return bot_detection_services[key]


@app.before_serving
async def startup():
    global dal, redis_client, ai_provider, safety_layer, rate_limiter
    global research_service, summary_service, searxng_service
    logger.system("Starting ai_researcher_module", action="startup")

    # Initialize database
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    # Initialize Redis
    try:
        redis_client = aioredis.from_url(
            Config.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
        await redis_client.ping()
        logger.system("Redis connected", result="SUCCESS")
    except Exception as e:
        logger.warning(
            f"Redis connection failed: {describe_db_error(e)}. "
            "Rate limiting will use DB fallback."
        )
        redis_client = None

    # Initialize AI provider
    ai_provider = AIProviderService(Config)
    logger.system("AIProviderService initialized", result="SUCCESS")

    # Initialize safety layer
    safety_layer = SafetyLayer()
    logger.system("SafetyLayer initialized", result="SUCCESS")

    # Initialize rate limiter
    rate_limiter = RateLimiter(redis_client, dal) if redis_client else RateLimiter(None, dal)
    logger.system("RateLimiter initialized", result="SUCCESS")

    # Note: ResearchService and SummaryService are created per-request
    # since they need community-specific mem0 instances

    # Initialize SearXNG-based sub-modules (shared SearXNG client)
    _searxng_needed = any([
        Config.GAME_LOOKUP_ENABLED,
        Config.PATCH_NOTES_ENABLED,
        Config.BUILD_ADVISOR_ENABLED,
        Config.TECH_TROUBLESHOOTER_ENABLED,
        Config.PRICE_TRACKER_ENABLED,
        Config.CLIP_RESEARCHER_ENABLED,
        Config.EVENT_LOOKUP_ENABLED,
    ])

    if _searxng_needed and not searxng_service:
        searxng_service = SearXNGService(
            base_url=Config.SEARXNG_URL,
            timeout=Config.SEARXNG_TIMEOUT,
            max_concurrent=Config.SEARXNG_MAX_CONCURRENT,
        )
        logger.system("SearXNG client initialized", result="SUCCESS")

    _bp_kwargs = dict(
        dal=dal,
        redis_client=redis_client,
        ai_provider=ai_provider,
        safety_layer=safety_layer,
        rate_limiter=rate_limiter,
        searxng_service=searxng_service,
        get_mem0_fn=_get_mem0_service,
        config=Config,
    )

    if Config.GAME_LOOKUP_ENABLED:
        setup_game_bp(**_bp_kwargs)
        logger.system("Game Lookup sub-module initialized", result="SUCCESS")

    if Config.PATCH_NOTES_ENABLED:
        setup_patch_bp(**_bp_kwargs)
        logger.system("Patch Notes sub-module initialized", result="SUCCESS")

    if Config.BUILD_ADVISOR_ENABLED:
        setup_build_bp(**_bp_kwargs)
        logger.system("Build Advisor sub-module initialized", result="SUCCESS")

    if Config.TECH_TROUBLESHOOTER_ENABLED:
        setup_tech_bp(**_bp_kwargs)
        logger.system("Tech Troubleshooter sub-module initialized", result="SUCCESS")

    if Config.PRICE_TRACKER_ENABLED:
        setup_price_bp(**_bp_kwargs)
        logger.system("Price Tracker sub-module initialized", result="SUCCESS")

    if Config.CLIP_RESEARCHER_ENABLED:
        setup_clip_researcher_bp(**_bp_kwargs)
        logger.system("Clip Researcher sub-module initialized", result="SUCCESS")

    if Config.EVENT_LOOKUP_ENABLED:
        setup_event_lookup_bp(**_bp_kwargs)
        logger.system("Event Lookup sub-module initialized", result="SUCCESS")

    logger.system("ai_researcher_module started", result="SUCCESS")


@app.after_serving
async def shutdown():
    global redis_client, ai_provider, searxng_service
    logger.system("Shutting down ai_researcher_module", action="shutdown")

    # Cleanup services
    if searxng_service:
        await searxng_service.close()

    if ai_provider:
        await ai_provider.close()

    if redis_client:
        await redis_client.close()

    logger.system("ai_researcher_module shutdown complete", result="SUCCESS")


# =============================================================================
# Public API Endpoints (Authenticated)
# =============================================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Get module status and feature information."""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "features": {
            "ai_research": True,
            "bot_detection": Config.BOT_DETECTION_ENABLED,
            "mem0_integration": True,
            "context_tracking": Config.FIREHOSE_ENABLED,
            "stream_awareness": True,
            "game_lookup": Config.GAME_LOOKUP_ENABLED,
            "patch_notes": Config.PATCH_NOTES_ENABLED,
            "build_advisor": Config.BUILD_ADVISOR_ENABLED,
            "tech_troubleshooter": Config.TECH_TROUBLESHOOTER_ENABLED,
            "price_tracker": Config.PRICE_TRACKER_ENABLED,
            "clip_researcher": Config.CLIP_RESEARCHER_ENABLED,
            "event_lookup": Config.EVENT_LOOKUP_ENABLED,
        }
    })


# =============================================================================
# AI Researcher Endpoints (Phase 1 - Stubs)
# =============================================================================

@researcher_bp.route('/research', methods=['POST'])
@async_endpoint
@auth_required
async def research():
    """
    Process !or/research command - perform web research on a topic.

    Expected payload:
    {
        "community_id": int,
        "user_id": int,
        "platform": str,
        "query": str,
        "max_queries": int (optional)
    }
    """
    data = await request.get_json()

    community_id = data.get('community_id')
    user_id = str(data.get('user_id', ''))
    query = data.get('query', '')

    if not community_id or not query:
        return error_response("community_id and query are required", 400)

    # Get community-specific mem0 service
    mem0_service = _get_mem0_service(community_id)

    # Create research service for this request
    service = ResearchService(
        ai_provider=ai_provider,
        mem0_service=mem0_service,
        safety_layer=safety_layer,
        rate_limiter=rate_limiter,
        redis_client=redis_client
    )

    result = await service.research(
        community_id=community_id,
        user_id=user_id,
        topic=query
    )

    if not result.success:
        return error_response(result.content, 429 if result.blocked_reason == "rate_limit" else 400)

    return success_response(result.to_dict())


@researcher_bp.route('/ask', methods=['POST'])
@async_endpoint
@auth_required
async def ask():
    """
    Process !or/ask command - ask a question with context awareness.

    Expected payload:
    {
        "community_id": int,
        "user_id": int,
        "platform": str,
        "question": str,
        "include_context": bool (optional)
    }
    """
    data = await request.get_json()

    community_id = data.get('community_id')
    user_id = str(data.get('user_id', ''))
    question = data.get('question', '')

    if not community_id or not question:
        return error_response("community_id and question are required", 400)

    # Get community-specific mem0 service
    mem0_service = _get_mem0_service(community_id)

    # Create research service for this request
    service = ResearchService(
        ai_provider=ai_provider,
        mem0_service=mem0_service,
        safety_layer=safety_layer,
        rate_limiter=rate_limiter,
        redis_client=redis_client
    )

    result = await service.ask(
        community_id=community_id,
        user_id=user_id,
        question=question
    )

    if not result.success:
        return error_response(result.content, 429 if result.blocked_reason == "rate_limit" else 400)

    return success_response(result.to_dict())


@researcher_bp.route('/recall', methods=['POST'])
@async_endpoint
@auth_required
async def recall():
    """
    Process !or/recall command - recall memories from mem0.

    Expected payload:
    {
        "community_id": int,
        "user_id": int,
        "platform": str,
        "query": str,
        "limit": int (optional)
    }
    """
    data = await request.get_json()

    community_id = data.get('community_id')
    user_id = str(data.get('user_id', ''))
    query = data.get('query', '')

    if not community_id or not query:
        return error_response("community_id and query are required", 400)

    # Get community-specific mem0 service
    mem0_service = _get_mem0_service(community_id)

    # Create research service for this request
    service = ResearchService(
        ai_provider=ai_provider,
        mem0_service=mem0_service,
        safety_layer=safety_layer,
        rate_limiter=rate_limiter,
        redis_client=redis_client
    )

    result = await service.recall(
        community_id=community_id,
        user_id=user_id,
        topic=query
    )

    if not result.success:
        return error_response(result.content, 429 if result.blocked_reason == "rate_limit" else 400)

    return success_response(result.to_dict())


@researcher_bp.route('/summarize', methods=['POST'])
@async_endpoint
@auth_required
async def summarize():
    """
    Process !or/summarize command - summarize recent conversation or stream.

    Expected payload:
    {
        "community_id": int,
        "user_id": int,
        "platform": str,
        "duration_minutes": int (optional),
        "topic": str (optional)
    }
    """
    data = await request.get_json()

    community_id = data.get('community_id')
    user_id = str(data.get('user_id', ''))
    duration_minutes = data.get('duration_minutes', 60)

    if not community_id:
        return error_response("community_id is required", 400)

    # Get community-specific mem0 service
    mem0_service = _get_mem0_service(community_id)

    # Create research service for this request
    service = ResearchService(
        ai_provider=ai_provider,
        mem0_service=mem0_service,
        safety_layer=safety_layer,
        rate_limiter=rate_limiter,
        redis_client=redis_client
    )

    result = await service.summarize(
        community_id=community_id,
        user_id=user_id,
        duration_minutes=duration_minutes
    )

    if not result.success:
        return error_response(result.content, 429 if result.blocked_reason == "rate_limit" else 400)

    return success_response(result.to_dict())


@researcher_bp.route('/messages/firehose', methods=['POST'])
@async_endpoint
async def receive_messages():
    """
    Receive ALL messages for context tracking (firehose).

    Expected payload:
    {
        "community_id": int,
        "user_id": int,
        "platform": str,
        "platform_user_id": str,
        "message": str,
        "timestamp": str,
        "metadata": dict (optional)
    }
    OR batch:
    {
        "messages": [...]
    }
    """
    if not _verify_service_key():
        return error_response("Unauthorized", 401)

    data = await request.get_json()

    # Handle batch or single message
    messages = data.get('messages', [data])
    processed = 0

    for msg in messages:
        community_id = msg.get('community_id')
        if not community_id:
            continue

        # Store message in database for context tracking
        try:
            query = """
                INSERT INTO ai_context_messages (
                    community_id, platform, platform_user_id, platform_username,
                    message_content, message_type, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """
            await dal.execute(query, [
                community_id,
                msg.get('platform', 'unknown'),
                msg.get('platform_user_id', ''),
                msg.get('username', msg.get('platform_username', '')),
                msg.get('message', msg.get('content', '')),
                msg.get('message_type', 'chat'),
                json.dumps(msg.get('metadata', {}))
            ])
            processed += 1
        except Exception as e:
            logger.error(f"Failed to store message: {describe_db_error(e)}")

    return success_response({
        "success": True,
        "processed": processed,
        "total": len(messages)
    })


@researcher_bp.route('/stream/end', methods=['POST'])
@async_endpoint
async def stream_end():
    """
    Notify that a stream has ended for context management.

    Expected payload:
    {
        "community_id": int,
        "platform": str,
        "ended_at": str,
        "duration_minutes": int (optional)
    }
    """
    if not _verify_service_key():
        return error_response("Unauthorized", 401)

    data = await request.get_json()
    community_id = data.get('community_id')

    if not community_id:
        return error_response("community_id is required", 400)

    # Get stream duration
    duration_minutes = data.get('duration_minutes', 60)
    stream_end_time = datetime.utcnow()
    stream_start_time = stream_end_time - timedelta(minutes=duration_minutes)

    # Get community-specific mem0 service
    mem0_service = _get_mem0_service(community_id)

    # Create summary service
    service = SummaryService(ai_provider, mem0_service, dal)

    # Generate stream summary
    try:
        summary = await service.generate_stream_summary(
            community_id=community_id,
            stream_start=stream_start_time,
            stream_end=stream_end_time
        )

        return success_response({
            "success": True,
            "summary": summary
        })
    except Exception as e:
        logger.error(f"Stream summary generation failed: {describe_db_error(e)}")
        return error_response("Failed to generate stream summary", 500)


@researcher_bp.route('/context/<int:community_id>')
@async_endpoint
@auth_required
async def get_context(community_id: int):
    """
    Get current conversation context for a community.

    Query params:
    - limit: Number of messages to return (default: 100)
    - since: Timestamp to get messages since
    """
    limit = request.args.get('limit', 100, type=int)
    since = request.args.get('since', None)

    try:
        # Build query
        if since:
            query = """
                SELECT id, platform, platform_user_id, platform_username,
                       message_content, message_type, metadata, created_at
                FROM ai_context_messages
                WHERE community_id = $1 AND created_at >= $2
                ORDER BY created_at DESC
                LIMIT $3
            """
            rows = await dal.execute(query, [community_id, since, limit])
        else:
            query = """
                SELECT id, platform, platform_user_id, platform_username,
                       message_content, message_type, metadata, created_at
                FROM ai_context_messages
                WHERE community_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """
            rows = await dal.execute(query, [community_id, limit])

        messages = [dict(row) for row in rows] if rows else []

        return success_response({
            "success": True,
            "community_id": community_id,
            "messages": messages,
            "count": len(messages)
        })
    except Exception as e:
        logger.error(f"Failed to get context: {describe_db_error(e)}")
        return error_response("Failed to retrieve context", 500)


@researcher_bp.route('/memory/<int:community_id>')
@async_endpoint
@auth_required
async def get_memory(community_id: int):
    """
    Get mem0 memories for a community.

    Query params:
    - query: Search query for memories
    - limit: Number of memories to return (default: 10)
    """
    query = request.args.get('query', '')
    limit = request.args.get('limit', 10, type=int)

    try:
        mem0_service = _get_mem0_service(community_id)

        if query:
            # Search for specific memories
            memories = await mem0_service.search(query=query, limit=limit)
        else:
            # Get all memories
            memories = await mem0_service.get_all()
            memories = memories[:limit] if memories else []

        return success_response({
            "success": True,
            "community_id": community_id,
            "memories": memories,
            "count": len(memories)
        })
    except Exception as e:
        logger.error(f"Failed to get memories: {describe_db_error(e)}")
        return error_response("Failed to retrieve memories", 500)


# =============================================================================
# Admin API Endpoints (Community Admin)
# =============================================================================

@admin_bp.route('/<int:community_id>/ai-insights')
@async_endpoint
@auth_required
async def get_ai_insights(community_id: int):
    """
    Get AI-generated insights for the community.

    Query params:
    - limit: Number of insights to return (default: 20)
    - type: Filter by insight type (topic, sentiment, etc.)
    """
    limit = request.args.get('limit', 20, type=int)
    insight_type = request.args.get('type', None)

    try:
        if insight_type:
            query = """
                SELECT id, insight_type, title, content, content_html,
                       metadata, period_start, period_end, created_at
                FROM ai_insights
                WHERE community_id = $1 AND insight_type = $2
                ORDER BY created_at DESC
                LIMIT $3
            """
            rows = await dal.execute(query, [community_id, insight_type, limit])
        else:
            query = """
                SELECT id, insight_type, title, content, content_html,
                       metadata, period_start, period_end, created_at
                FROM ai_insights
                WHERE community_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """
            rows = await dal.execute(query, [community_id, limit])

        insights = []
        for row in (rows or []):
            insights.append({
                'id': row['id'],
                'insight_type': row['insight_type'],
                'title': row['title'],
                'content': row['content'],
                'content_html': row['content_html'],
                'metadata': row['metadata'] or {},
                'period_start': str(row['period_start']) if row['period_start'] else None,
                'period_end': str(row['period_end']) if row['period_end'] else None,
                'created_at': str(row['created_at'])
            })

        return success_response({
            "success": True,
            "community_id": community_id,
            "insights": insights,
            "count": len(insights)
        })
    except Exception as e:
        logger.error(f"Failed to get insights: {describe_db_error(e)}")
        return error_response("Failed to retrieve insights", 500)


@admin_bp.route('/<int:community_id>/ai-researcher/config', methods=['GET'])
@async_endpoint
@auth_required
async def get_researcher_config(community_id: int):
    """Get AI Researcher configuration for a community."""
    try:
        query = """
            SELECT firehose_enabled, bot_detection_enabled, bot_detection_threshold,
                   research_max_queries, summary_enabled, mem0_enabled, ai_provider,
                   is_premium, created_at, updated_at
            FROM ai_researcher_config
            WHERE community_id = $1
        """
        rows = await dal.execute(query, [community_id])

        if rows and len(rows) > 0:
            row = rows[0]
            config = {
                'community_id': community_id,
                'firehose_enabled': row['firehose_enabled'],
                'bot_detection_enabled': row['bot_detection_enabled'],
                'bot_detection_threshold': float(row['bot_detection_threshold']),
                'research_max_queries': row['research_max_queries'],
                'summary_enabled': row['summary_enabled'],
                'mem0_enabled': row['mem0_enabled'],
                'ai_provider': row['ai_provider'],
                'is_premium': row['is_premium'],
                'created_at': str(row['created_at']),
                'updated_at': str(row['updated_at'])
            }
        else:
            # Return default config
            config = {
                'community_id': community_id,
                'firehose_enabled': Config.FIREHOSE_ENABLED,
                'bot_detection_enabled': Config.BOT_DETECTION_ENABLED,
                'bot_detection_threshold': Config.BOT_DETECTION_THRESHOLD,
                'research_max_queries': Config.RESEARCH_MAX_QUERIES,
                'summary_enabled': True,
                'mem0_enabled': True,
                'ai_provider': Config.AI_PROVIDER,
                'is_premium': False,
                'created_at': None,
                'updated_at': None
            }

        return success_response({
            "success": True,
            "config": config
        })
    except Exception as e:
        logger.error(f"Failed to get researcher config: {describe_db_error(e)}")
        return error_response("Failed to retrieve config", 500)


@admin_bp.route('/<int:community_id>/ai-researcher/config', methods=['PUT'])
@async_endpoint
@auth_required
async def update_researcher_config(community_id: int):
    """
    Update AI Researcher configuration.

    Expected payload:
    {
        "admin_id": int,
        "firehose_enabled": bool (optional),
        "bot_detection_enabled": bool (optional),
        "bot_detection_threshold": float (optional),
        "research_max_queries": int (optional),
        "mem0_enabled": bool (optional)
    }
    """
    data = await request.get_json()
    admin_id = data.get('admin_id')

    if not admin_id:
        return error_response("admin_id is required", 400)

    try:
        # Build update fields dynamically
        updates = []
        values = []
        param_idx = 1

        fields = [
            ('firehose_enabled', 'firehose_enabled'),
            ('bot_detection_enabled', 'bot_detection_enabled'),
            ('bot_detection_threshold', 'bot_detection_threshold'),
            ('research_max_queries', 'research_max_queries'),
            ('summary_enabled', 'summary_enabled'),
            ('mem0_enabled', 'mem0_enabled'),
            ('ai_provider', 'ai_provider'),
        ]

        for json_field, db_field in fields:
            if json_field in data:
                updates.append(f"{db_field} = ${param_idx}")
                values.append(data[json_field])
                param_idx += 1

        if not updates:
            return error_response("No fields to update", 400)

        # Add updated_at
        updates.append(f"updated_at = NOW()")

        # Check if config exists
        check_query = "SELECT id FROM ai_researcher_config WHERE community_id = $1"
        existing = await dal.execute(check_query, [community_id])

        if existing and len(existing) > 0:
            # Update existing
            query = f"""
                UPDATE ai_researcher_config
                SET {', '.join(updates)}
                WHERE community_id = ${param_idx}
            """
            values.append(community_id)
        else:
            # Insert new config
            field_names = ['community_id'] + [f[1] for f in fields if f[0] in data]
            placeholders = ', '.join([f'${i}' for i in range(1, len(values) + 2)])
            query = f"""
                INSERT INTO ai_researcher_config ({', '.join(field_names)})
                VALUES ({placeholders})
            """
            values = [community_id] + values

        await dal.execute(query, values)

        logger.audit(
            action="ai_researcher_config_updated",
            user=str(admin_id),
            community=str(community_id),
            result="SUCCESS"
        )

        return success_response({
            "success": True,
            "message": "Configuration updated"
        })
    except Exception as e:
        logger.error(f"Failed to update researcher config: {describe_db_error(e)}")
        return error_response("Failed to update config", 500)


# =============================================================================
# Community Insights Endpoints
# =============================================================================

@researcher_bp.route('/<int:community_id>/insights', methods=['GET'])
@async_endpoint
@auth_required
async def get_insights(community_id: int):
    """
    Get previously generated insights for a community.

    Query params:
    - limit: Number of insights to return (default: 20)
    - type: Filter by insight type
    - days: How many days back to look (default: 90)
    """
    limit = request.args.get('limit', 20, type=int)
    insight_type = request.args.get('type', None)
    days = request.args.get('days', 90, type=int)

    try:
        period_start = datetime.utcnow() - timedelta(days=days)

        if insight_type:
            query = """
                SELECT id, insight_type, content, metadata,
                       period_start, period_end, created_at
                FROM ai_community_insights
                WHERE community_id = $1
                  AND insight_type = $2
                  AND created_at >= $3
                ORDER BY created_at DESC
                LIMIT $4
            """
            rows = await dal.execute(query, [community_id, insight_type, period_start, limit])
        else:
            query = """
                SELECT id, insight_type, content, metadata,
                       period_start, period_end, created_at
                FROM ai_community_insights
                WHERE community_id = $1
                  AND created_at >= $2
                ORDER BY created_at DESC
                LIMIT $3
            """
            rows = await dal.execute(query, [community_id, period_start, limit])

        insights = []
        for row in (rows or []):
            insights.append({
                'id': row['id'],
                'type': row['insight_type'],
                'content': row['content'],
                'metadata': row['metadata'] or {},
                'period_start': str(row['period_start']) if row['period_start'] else None,
                'period_end': str(row['period_end']) if row['period_end'] else None,
                'created_at': str(row['created_at'])
            })

        return success_response({
            "success": True,
            "community_id": community_id,
            "insights": insights,
            "count": len(insights)
        })
    except Exception as e:
        logger.error(f"Failed to get insights: {describe_db_error(e)}")
        return error_response("Failed to retrieve insights", 500)


@researcher_bp.route('/<int:community_id>/insights/generate', methods=['POST'])
@async_endpoint
@auth_required
async def generate_insights(community_id: int):
    """
    Generate new AI-powered community insights.

    Expected payload:
    {
        "timeframe": str (optional, default: '7d'),
        "insight_types": list (optional, default: ['activity', 'trending', 'sentiment'])
    }
    """
    data = await request.get_json()
    timeframe = data.get('timeframe', '7d')
    insight_types = data.get('insight_types', ['activity', 'trending', 'sentiment'])

    try:
        # Create insights service
        insights_service = InsightsService(
            ai_provider=ai_provider,
            dal=dal,
            mem0_service=_get_mem0_service(community_id)
        )

        result = await insights_service.generate_community_insights(
            community_id=community_id,
            timeframe=timeframe,
            insight_types=insight_types
        )

        if not result.success:
            return error_response(result.error or "Failed to generate insights", 400)

        return success_response({
            "success": True,
            "insight_id": result.insight_id,
            "content": result.content,
            "insight_type": result.insight_type,
            "tokens_used": result.tokens_used,
            "processing_time_ms": result.processing_time_ms
        })
    except Exception as e:
        logger.error(f"Insight generation error: {describe_db_error(e)}")
        return error_response("Failed to generate insights", 500)


# =============================================================================
# Anomaly Detection Endpoints
# =============================================================================

@researcher_bp.route('/<int:community_id>/anomalies', methods=['GET'])
@async_endpoint
@auth_required
async def get_anomalies(community_id: int):
    """
    Get detected anomalies for a community.

    Query params:
    - hours: How many hours back to look (default: 24)
    - acknowledged: Filter by acknowledged status (default: false)
    - limit: Number of results (default: 50)
    """
    hours = request.args.get('hours', 24, type=int)
    acknowledged = request.args.get('acknowledged', 'false').lower() == 'true'
    limit = request.args.get('limit', 50, type=int)

    try:
        anomaly_detector = AnomalyDetector(dal)
        anomalies = await anomaly_detector.get_recent_anomalies(
            community_id=community_id,
            hours=hours,
            acknowledged=acknowledged
        )

        return success_response({
            "success": True,
            "community_id": community_id,
            "anomalies": anomalies[:limit],
            "count": len(anomalies[:limit])
        })
    except Exception as e:
        logger.error(f"Failed to get anomalies: {describe_db_error(e)}")
        return error_response("Failed to retrieve anomalies", 500)


@researcher_bp.route('/<int:community_id>/anomalies/<int:anomaly_id>/acknowledge', methods=['POST'])
@async_endpoint
@auth_required
async def acknowledge_anomaly(community_id: int, anomaly_id: int):
    """
    Mark an anomaly as acknowledged.

    Expected payload:
    {
        "admin_id": int,
        "notes": str (optional)
    }
    """
    data = await request.get_json()
    admin_id = data.get('admin_id')
    notes = data.get('notes')

    if not admin_id:
        return error_response("admin_id is required", 400)

    try:
        anomaly_detector = AnomalyDetector(dal)
        success = await anomaly_detector.acknowledge_anomaly(
            community_id=community_id,
            anomaly_id=anomaly_id,
            admin_id=admin_id,
            notes=notes
        )

        if not success:
            return error_response("Failed to acknowledge anomaly", 400)

        logger.audit(
            action="anomaly_acknowledged",
            user=str(admin_id),
            community=str(community_id),
            result="SUCCESS"
        )

        return success_response({
            "success": True,
            "message": "Anomaly acknowledged"
        })
    except Exception as e:
        logger.error(f"Anomaly acknowledgment error: {describe_db_error(e)}")
        return error_response("Failed to acknowledge anomaly", 500)


# =============================================================================
# Sentiment Analysis Endpoints
# =============================================================================

@researcher_bp.route('/<int:community_id>/sentiment', methods=['GET'])
@async_endpoint
@auth_required
async def get_sentiment(community_id: int):
    """
    Get sentiment analysis for a community.

    Query params:
    - timeframe: Analysis period ('1d', '7d', '30d', '90d', default: '7d')
    """
    timeframe = request.args.get('timeframe', '7d')

    try:
        sentiment_analyzer = SentimentAnalyzer(dal, ai_provider)
        result = await sentiment_analyzer.analyze_sentiment(
            community_id=community_id,
            timeframe=timeframe
        )

        if not result.success:
            return error_response(result.error or "Failed to analyze sentiment", 400)

        return success_response({
            "success": True,
            "community_id": community_id,
            "overall_sentiment": result.overall_sentiment,
            "sentiment_score": result.sentiment_score,
            "message_count": result.message_count,
            "sentiment_distribution": result.sentiment_distribution,
            "trends": result.trends,
            "processing_time_ms": result.processing_time_ms
        })
    except Exception as e:
        logger.error(f"Sentiment analysis error: {describe_db_error(e)}")
        return error_response("Failed to analyze sentiment", 500)


# =============================================================================
# User Behavior Profile Endpoints
# =============================================================================

@researcher_bp.route('/<int:community_id>/user/<platform>/<user_id>/profile', methods=['GET'])
@async_endpoint
@auth_required
async def get_user_profile(community_id: int, platform: str, user_id: str):
    """
    Get behavior profile for a specific user.

    URL params:
    - community_id: Community identifier
    - platform: Platform name (twitch, discord, etc.)
    - user_id: User ID on the platform

    Query params:
    - days: Historical data period (default: 90)
    """
    days = request.args.get('days', 90, type=int)

    try:
        behavior_profiler = BehaviorProfiler(dal)

        # Try to get stored profile first
        stored_profile = await behavior_profiler.get_user_profile(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        if stored_profile:
            return success_response({
                "success": True,
                "profile_id": stored_profile['id'],
                "data": stored_profile['data'],
                "created_at": stored_profile['created_at'],
                "updated_at": stored_profile['updated_at']
            })

        # If not found, generate new profile
        profile = await behavior_profiler.profile_user_behavior(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id,
            days=days
        )

        if not profile.success:
            return error_response(profile.error or "Failed to profile user", 400)

        return success_response({
            "success": True,
            "profile_id": profile.profile_id,
            "user_id": profile.user_id,
            "activity_level": profile.activity_level,
            "communication_style": profile.communication_style,
            "preferred_hours": profile.preferred_hours,
            "average_message_length": profile.average_message_length,
            "total_messages": profile.total_messages,
            "community_role": profile.community_role,
            "processing_time_ms": profile.processing_time_ms
        })
    except Exception as e:
        logger.error(f"User profile error: {describe_db_error(e)}")
        return error_response("Failed to retrieve user profile", 500)


@researcher_bp.route('/<int:community_id>/users/profiles', methods=['GET'])
@async_endpoint
@auth_required
async def get_community_profiles(community_id: int):
    """
    Get behavior profiles for all users in a community.

    Query params:
    - role: Filter by community role (optional)
    - limit: Number of profiles (default: 100)
    """
    role = request.args.get('role', None)
    limit = request.args.get('limit', 100, type=int)

    try:
        behavior_profiler = BehaviorProfiler(dal)
        profiles = await behavior_profiler.get_community_profiles(
            community_id=community_id,
            role=role
        )

        return success_response({
            "success": True,
            "community_id": community_id,
            "profiles": profiles[:limit],
            "count": len(profiles[:limit])
        })
    except Exception as e:
        logger.error(f"Community profiles error: {describe_db_error(e)}")
        return error_response("Failed to retrieve profiles", 500)


@admin_bp.route('/<int:community_id>/bot-detection')
@async_endpoint
@auth_required
async def get_bot_detection(community_id: int):
    """
    Get bot detection results for the community.

    Query params:
    - limit: Number of results to return (default: 50)
    - threshold: Minimum bot score to include (default: 0.5)
    - flagged_only: Only show flagged users (default: false)
    """
    limit = request.args.get('limit', 50, type=int)
    threshold = request.args.get('threshold', 50.0, type=float)
    flagged_only = request.args.get('flagged_only', 'false').lower() == 'true'

    try:
        # Build query
        if flagged_only:
            query = """
                SELECT id, platform, platform_user_id, platform_username,
                       confidence_score, behavioral_patterns, timing_regularity,
                       response_latency_avg, emote_text_ratio, copy_paste_frequency,
                       account_age_days, recommended_action, is_reviewed, admin_notes,
                       created_at
                FROM ai_bot_detection_results
                WHERE community_id = $1
                  AND confidence_score >= $2
                  AND is_reviewed = FALSE
                ORDER BY confidence_score DESC
                LIMIT $3
            """
        else:
            query = """
                SELECT id, platform, platform_user_id, platform_username,
                       confidence_score, behavioral_patterns, timing_regularity,
                       response_latency_avg, emote_text_ratio, copy_paste_frequency,
                       account_age_days, recommended_action, is_reviewed, admin_notes,
                       created_at
                FROM ai_bot_detection_results
                WHERE community_id = $1
                  AND confidence_score >= $2
                ORDER BY confidence_score DESC
                LIMIT $3
            """

        rows = await dal.execute(query, [community_id, threshold, limit])

        results = []
        for row in (rows or []):
            results.append({
                'id': row['id'],
                'platform': row['platform'],
                'platform_user_id': row['platform_user_id'],
                'platform_username': row['platform_username'],
                'confidence_score': float(row['confidence_score']),
                'behavioral_patterns': row['behavioral_patterns'] or {},
                'timing_regularity': float(row['timing_regularity']) if row['timing_regularity'] else None,
                'response_latency_avg': float(row['response_latency_avg']) if row['response_latency_avg'] else None,
                'emote_text_ratio': float(row['emote_text_ratio']) if row['emote_text_ratio'] else None,
                'copy_paste_frequency': row['copy_paste_frequency'],
                'account_age_days': row['account_age_days'],
                'recommended_action': row['recommended_action'],
                'is_reviewed': row['is_reviewed'],
                'admin_notes': row['admin_notes'],
                'created_at': str(row['created_at'])
            })

        return success_response({
            "success": True,
            "community_id": community_id,
            "results": results,
            "count": len(results),
            "threshold": threshold
        })
    except Exception as e:
        logger.error(f"Failed to get bot detection results: {describe_db_error(e)}")
        return error_response("Failed to retrieve bot detection results", 500)


# Register blueprints
app.register_blueprint(api_bp)
app.register_blueprint(researcher_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(game_bp)
app.register_blueprint(patch_bp)
app.register_blueprint(build_bp)
app.register_blueprint(tech_bp)
app.register_blueprint(price_bp)
app.register_blueprint(clip_researcher_bp)
app.register_blueprint(event_lookup_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
