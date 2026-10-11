"""
Interactive Media Service

Combines three media interaction modules into a single Quart application:
- Clip interaction (Twitch clip creation, bookmarks, highlights, reels)
- Spotify integration
- YouTube Music integration

All endpoints available under /api/v1/
"""
import asyncio
import os
import sys

from quart import Blueprint, Quart, request
from flask_core import (
    async_endpoint,
    create_health_blueprint,
    error_response,
    init_database,
    setup_aaa_logging,
    success_response,
)
from config import Config

# Import services from modules
from clip_interaction_module.services.twitch_clip_service import (  # noqa: E402
    TwitchClipService,
)
from clip_interaction_module.services.clip_service import ClipService  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
twitch_clip_service = None
clip_service = None


@app.before_serving
async def startup():
    global dal, twitch_clip_service, clip_service
    logger.system("Starting interactive_media_service", action="startup")

    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    twitch_clip_service = TwitchClipService(Config)
    clip_service = ClipService(dal, Config)

    logger.system("interactive_media_service started", result="SUCCESS")


# =====================================================
# CLIP ENDPOINTS
# =====================================================

@api_bp.route('/clips/<int:community_id>/create', methods=['POST'])
@async_endpoint
async def create_clip(community_id: int):
    """Create a Twitch clip via the action-twitch module proxy.

    Request JSON:
    {
        "user_id": "123456",
        "platform": "twitch"
    }
    """
    try:
        data = await request.get_json()

        user_id = data.get('user_id')
        platform = data.get('platform', 'twitch')

        if not user_id:
            return error_response("user_id is required", status_code=400)

        logger.audit(
            action="create_clip",
            user=user_id,
            community=community_id,
            result="STARTED",
        )

        result = await twitch_clip_service.create_clip(
            community_id, user_id, platform
        )

        if result.get('error'):
            logger.audit(
                action="create_clip",
                user=user_id,
                community=community_id,
                result="FAILED",
                error=result['error'],
            )
            return error_response(result['error'], status_code=502)

        logger.audit(
            action="create_clip",
            user=user_id,
            community=community_id,
            result="SUCCESS",
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to create clip: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/clips/<int:community_id>/bookmark', methods=['POST'])
@async_endpoint
async def bookmark_clip(community_id: int):
    """Bookmark a clip.

    Request JSON:
    {
        "clip_id": "AbcDef123",
        "clip_url": "https://clips.twitch.tv/AbcDef123",
        "title": "Amazing play",
        "game": "Fortnite",
        "tags": ["funny", "clutch"],
        "bookmarked_by": "username"
    }
    """
    try:
        data = await request.get_json()

        clip_id = data.get('clip_id')
        clip_url = data.get('clip_url')

        if not clip_id or not clip_url:
            return error_response(
                "clip_id and clip_url are required", status_code=400
            )

        result = await clip_service.bookmark_clip(
            community_id=community_id,
            clip_id=clip_id,
            clip_url=clip_url,
            title=data.get('title'),
            game=data.get('game'),
            tags=data.get('tags'),
            bookmarked_by=data.get('bookmarked_by'),
        )

        if result.get('error'):
            return error_response(result['error'], status_code=500)

        logger.audit(
            action="bookmark_clip",
            user="system",
            community=community_id,
            result="SUCCESS",
            clip_id=clip_id,
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to bookmark clip: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/clips/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_clips(community_id: int):
    """List clips for a community.

    Query params:
    - game: Filter by game name
    - tag: Filter by tag
    - highlights: If "true", only return highlighted clips
    - limit: Max results (default 50)
    """
    try:
        game = request.args.get('game')
        tag = request.args.get('tag')
        highlights = request.args.get('highlights', '').lower() == 'true'
        limit = int(request.args.get('limit', 50))

        clips = await clip_service.get_clips(
            community_id=community_id,
            game=game,
            tag=tag,
            highlights_only=highlights,
            limit=limit,
        )

        return success_response({"clips": clips, "count": len(clips)})

    except Exception as e:
        logger.error(f"Failed to get clips: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route(
    '/clips/<int:community_id>/<clip_id>/tags', methods=['PUT']
)
@async_endpoint
async def update_tags(community_id: int, clip_id: str):
    """Update tags for a clip.

    Request JSON:
    {
        "tags": ["funny", "clutch", "highlight"]
    }
    """
    try:
        data = await request.get_json()
        tags = data.get('tags', [])

        result = await clip_service.update_tags(
            community_id=community_id,
            clip_id=clip_id,
            tags=tags,
        )

        if result.get('error'):
            return error_response(result['error'], status_code=400)

        logger.audit(
            action="update_tags",
            user="system",
            community=community_id,
            result="SUCCESS",
            clip_id=clip_id,
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to update tags: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route(
    '/clips/<int:community_id>/<clip_id>/highlight', methods=['POST']
)
@async_endpoint
async def mark_highlight(community_id: int, clip_id: str):
    """Mark or unmark a clip as a highlight.

    Request JSON:
    {
        "is_highlight": true
    }
    """
    try:
        data = await request.get_json()
        is_highlight = data.get('is_highlight', True)

        result = await clip_service.mark_highlight(
            community_id=community_id,
            clip_id=clip_id,
            is_highlight=is_highlight,
        )

        if result.get('error'):
            return error_response(result['error'], status_code=404)

        logger.audit(
            action="mark_highlight",
            user="system",
            community=community_id,
            result="SUCCESS",
            clip_id=clip_id,
            is_highlight=is_highlight,
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to mark highlight: {e}")
        return error_response(str(e), status_code=500)


# =====================================================
# REEL ENDPOINTS
# =====================================================

@api_bp.route('/reels/<int:community_id>', methods=['POST'])
@async_endpoint
async def create_reel(community_id: int):
    """Create a highlight reel.

    Request JSON:
    {
        "name": "Best of January",
        "description": "Top clips from January streams",
        "clip_ids": ["AbcDef123", "GhiJkl456"],
        "created_by": "username"
    }
    """
    try:
        data = await request.get_json()

        name = data.get('name')
        if not name:
            return error_response("name is required", status_code=400)

        result = await clip_service.create_reel(
            community_id=community_id,
            name=name,
            description=data.get('description'),
            clip_ids=data.get('clip_ids', []),
            created_by=data.get('created_by'),
        )

        if result.get('error'):
            return error_response(result['error'], status_code=400)

        logger.audit(
            action="create_reel",
            user="system",
            community=community_id,
            result="SUCCESS",
            reel_name=name,
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to create reel: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route(
    '/reels/<int:community_id>/<int:reel_id>', methods=['GET']
)
@async_endpoint
async def get_reel(community_id: int, reel_id: int):
    """Get a highlight reel with its clips."""
    try:
        result = await clip_service.get_reel(community_id, reel_id)

        if result.get('error'):
            return error_response(result['error'], status_code=404)

        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to get reel: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route(
    '/reels/<int:community_id>/<int:reel_id>/publish', methods=['PUT']
)
@async_endpoint
async def publish_reel(community_id: int, reel_id: int):
    """Publish a highlight reel."""
    try:
        result = await clip_service.publish_reel(community_id, reel_id)

        if result.get('error'):
            return error_response(result['error'], status_code=404)

        logger.audit(
            action="publish_reel",
            user="system",
            community=community_id,
            result="SUCCESS",
            reel_id=reel_id,
        )
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to publish reel: {e}")
        return error_response(str(e), status_code=500)


# =====================================================
# OVERLAY ENDPOINT
# =====================================================

@api_bp.route('/overlay/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_overlay_data(community_id: int):
    """Get OBS browser source overlay data with latest highlights."""
    try:
        result = await clip_service.get_overlay_data(community_id)
        return success_response(result)

    except Exception as e:
        logger.error(f"Failed to get overlay data: {e}")
        return error_response(str(e), status_code=500)


# =====================================================
# SPOTIFY ENDPOINTS
# =====================================================

@api_bp.route('/spotify/status', methods=['GET'])
@async_endpoint
async def spotify_status():
    """Spotify integration status."""
    return success_response({
        "status": "operational",
        "module": "spotify_interaction"
    })


# =====================================================
# YOUTUBE MUSIC ENDPOINTS
# =====================================================

@api_bp.route('/youtube-music/status', methods=['GET'])
@async_endpoint
async def youtube_music_status():
    """YouTube Music integration status."""
    return success_response({
        "status": "operational",
        "module": "youtube_music_interaction"
    })


# =====================================================
# GENERAL STATUS ENDPOINT
# =====================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Service status endpoint."""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "features": ["clips", "spotify", "youtube-music"]
    })


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    hconfig = HyperConfig()
    hconfig.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hconfig))
