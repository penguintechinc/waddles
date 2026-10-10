"""
User Analytics Blueprint
=========================
User-scoped analytics endpoints. Hub is the auth boundary.
Expects X-Caller-User-ID, X-Caller-Role, X-Service-Key headers from hub.
"""
import logging

from quart import Blueprint, request, jsonify
from flask_core import success_response, error_response
from config import Config
from services.log_safety import INTERNAL_ERROR_MESSAGE, log_failure

user_bp = Blueprint('user', __name__, url_prefix='/api/v1/analytics/user')

_log = logging.getLogger(f"waddlebot.{Config.MODULE_NAME}")

# Service injected at startup
user_stats_service = None


def init_user_blueprint(service):
    global user_stats_service
    user_stats_service = service


@user_bp.route('/<int:hub_user_id>/self', methods=['GET'])
async def get_user_self_stats(hub_user_id: int):
    """Cross-community stats for a user. Scenario 1: self-stats."""
    try:
        stats = await user_stats_service.get_user_self_stats(hub_user_id)
        return jsonify(success_response(stats))
    except Exception as e:
        log_failure(_log, "User analytics request failed", e, endpoint="get_user_self_stats")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@user_bp.route('/<int:hub_user_id>/in-community/<int:community_id>', methods=['GET'])
async def get_user_community_stats(hub_user_id: int, community_id: int):
    """User stats within a specific community. Scenario 2: community admin view."""
    try:
        stats = await user_stats_service.get_user_stats_in_community(hub_user_id, community_id)
        return jsonify(success_response(stats))
    except Exception as e:
        log_failure(_log, "User analytics request failed", e, endpoint="get_user_community_stats")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@user_bp.route('/<int:hub_user_id>/reputation', methods=['GET'])
async def get_user_reputation(hub_user_id: int):
    """User reputation summary with trend."""
    try:
        summary = await user_stats_service.get_user_reputation_summary(hub_user_id)
        return jsonify(success_response(summary))
    except Exception as e:
        log_failure(_log, "User analytics request failed", e, endpoint="get_user_reputation")
        return error_response(INTERNAL_ERROR_MESSAGE, 500)
