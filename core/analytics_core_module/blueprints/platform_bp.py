"""
Platform Analytics Blueprint
==============================
Platform-wide aggregate analytics. No PII in responses.
Requires X-Service-Key header (validated by before_request middleware in app.py).
"""
import logging

from quart import Blueprint, request, jsonify
from flask_core import success_response, error_response
from config import Config
from services.log_safety import INTERNAL_ERROR_MESSAGE, log_failure

platform_bp = Blueprint('platform', __name__, url_prefix='/api/v1/analytics/platform')

_log = logging.getLogger(f"waddlebot.{Config.MODULE_NAME}")

# Service injected at startup
platform_stats_service = None


def init_platform_blueprint(service):
    global platform_stats_service
    platform_stats_service = service


@platform_bp.route('/summary', methods=['GET'])
async def get_platform_summary():
    """Platform summary: users, communities, avg reputation."""
    try:
        summary = await platform_stats_service.get_platform_summary()
        return jsonify(success_response(summary))
    except Exception as e:
        log_failure(_log, "Platform analytics request failed", e, endpoint="get_platform_summary")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@platform_bp.route('/reputation', methods=['GET'])
async def get_reputation_distribution():
    """Reputation score distribution histogram."""
    try:
        data = await platform_stats_service.get_reputation_distribution()
        return jsonify(success_response(data))
    except Exception as e:
        log_failure(_log, "Platform analytics request failed", e, endpoint="get_reputation_distribution")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@platform_bp.route('/growth', methods=['GET'])
async def get_growth_trends():
    """Growth trends: new users + communities per bucket."""
    try:
        period = request.args.get('period', '90d')
        if period not in ('30d', '90d', '1y'):
            return error_response("period must be 30d, 90d, or 1y", 400)
        data = await platform_stats_service.get_growth_trends(period)
        return jsonify(success_response(data))
    except Exception as e:
        log_failure(_log, "Platform analytics request failed", e, endpoint="get_growth_trends")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@platform_bp.route('/activity', methods=['GET'])
async def get_activity_breakdown():
    """Active user segments by login recency."""
    try:
        data = await platform_stats_service.get_activity_breakdown()
        return jsonify(success_response(data))
    except Exception as e:
        log_failure(_log, "Platform analytics request failed", e, endpoint="get_activity_breakdown")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@platform_bp.route('/community-health', methods=['GET'])
async def get_community_health_summaries():
    """Per-community health summaries. No individual user data."""
    try:
        limit = min(200, max(1, int(request.args.get('limit', '50'))))
        data = await platform_stats_service.get_community_health_summaries(limit)
        return jsonify(success_response(data))
    except Exception as e:
        log_failure(_log, "Platform analytics request failed", e, endpoint="get_community_health_summaries")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)
