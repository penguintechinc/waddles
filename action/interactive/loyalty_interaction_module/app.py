"""
WaddleBot Loyalty Interaction Module (Quart)
============================================

Comprehensive loyalty and currency system with:
- Virtual currency management (earn, spend, transfer)
- Giveaway system with reputation-based weighting
- Minigames (slots, coinflip, roulette)
- Player vs player duels with currency stakes
- Gear system for stat bonuses and customization

Converted to Quart for async performance.
"""

import os
import sys
import asyncio
from datetime import datetime
from typing import Optional

# Add libs to path for flask_core imports
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs')
)

from quart import Quart, Blueprint, request  # noqa: E402

from flask_core import (  # noqa: E402
    setup_aaa_logging,
    async_endpoint,
    auth_required,
    success_response,
    error_response,
    create_health_blueprint,
    init_database,
    validate_json,
    validate_query
)

from config import Config  # noqa: E402
from services.currency_service import CurrencyService  # noqa: E402
from services.earning_config_service import EarningConfigService  # noqa: E402
from services.giveaway_service import GiveawayService  # noqa: E402
from services.minigame_service import MinigameService  # noqa: E402
from services.duel_service import DuelService  # noqa: E402
from services.gear_service import GearService  # noqa: E402
from validation_models import (  # noqa: E402
    CurrencyTransferRequest,
    SetBalanceRequest,
    GearActionRequest,
    GearCreateRequest,
    CoinflipRequest,
    SlotsRequest,
    RouletteRequest,
    DuelChallengeRequest,
    DuelActionRequest,
    GiveawayCreateRequest,
    GiveawayEntryRequest,
    LeaderboardParams,
    EarningConfigUpdate,
    EventEarningRequest
)  # noqa: E402

# Initialize Quart app
app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# Create API blueprints - all nested under /api/v1/loyalty for Kong routing
currency_bp = Blueprint('currency', __name__, url_prefix='/api/v1/loyalty/currency')
config_bp = Blueprint('earning_config', __name__, url_prefix='/api/v1/loyalty')
giveaway_bp = Blueprint('giveaway', __name__, url_prefix='/api/v1/loyalty/giveaways')
games_bp = Blueprint('games', __name__, url_prefix='/api/v1/loyalty/games')
duel_bp = Blueprint('duels', __name__, url_prefix='/api/v1/loyalty/duels')
gear_bp = Blueprint('gear', __name__, url_prefix='/api/v1/loyalty/gear')
command_bp = Blueprint('command', __name__, url_prefix='/api/v1/loyalty')

# Setup logging
logger = setup_aaa_logging(
    module_name=Config.MODULE_NAME,
    version=Config.MODULE_VERSION
)

# Initialize services (will be set on startup)
dal = None
currency_service = None
earning_config_service = None
giveaway_service = None
minigame_service = None
duel_service = None
gear_service = None


@app.before_serving
async def startup():
    """Initialize services on startup"""
    global dal, currency_service, earning_config_service, giveaway_service
    global minigame_service, duel_service, gear_service

    logger.system("Starting Loyalty interaction module", action="startup")

    try:
        # Initialize database
        dal = init_database(Config.DATABASE_URL)
        app.config['dal'] = dal
        logger.system("Database initialized")

        # Initialize all services (order matters due to dependencies)
        currency_service = CurrencyService(dal)
        earning_config_service = EarningConfigService(dal, currency_service)
        giveaway_service = GiveawayService(dal, currency_service)
        minigame_service = MinigameService(dal, currency_service)
        gear_service = GearService(dal, currency_service)
        duel_service = DuelService(dal, currency_service, gear_service)

        logger.system("All services initialized", result="SUCCESS")

    except Exception as e:
        logger.error(f"Startup failed: {e}", action="startup", result="ERROR")
        raise


@app.after_serving
async def shutdown():
    """Cleanup on shutdown"""
    logger.system("Shutting down Loyalty interaction module", action="shutdown")


# Module info endpoint
@app.route('/', methods=['GET'])
@app.route('/index', methods=['GET'])
async def index():
    """Module information and status"""
    return success_response({
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "status": "operational",
        "features": [
            "currency_management",
            "earning_config",
            "giveaways",
            "minigames",
            "duels",
            "gear_system"
        ],
        "endpoints": {
            "currency": "/api/v1/currency",
            "config": "/api/v1/config",
            "giveaways": "/api/v1/giveaways",
            "games": "/api/v1/games",
            "duels": "/api/v1/duels",
            "gear": "/api/v1/gear",
            "command": "/api/v1/command"
        }
    })


# ============================================================================
# CURRENCY ENDPOINTS
# ============================================================================

@currency_bp.route('/<int:community_id>/balance/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_balance(community_id: int, user_id: str):
    """Get user's current balance"""
    try:
        platform = request.args.get('platform', 'twitch')

        balance = await currency_service.get_balance(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "balance": balance.balance,
            "lifetime_earned": balance.lifetime_earned,
            "lifetime_spent": balance.lifetime_spent
        })

    except Exception as e:
        logger.error(f"Get balance error: {e}", community=community_id, user=user_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/add', methods=['POST'])
@auth_required
@async_endpoint
async def add_currency(community_id: int):
    """Add currency to a user's balance"""
    try:
        data = await request.get_json()

        user_id = data.get('user_id')
        amount = data.get('amount')
        reason = data.get('reason', 'admin_add')
        platform = data.get('platform', 'twitch')

        if not user_id or amount is None:
            return error_response("user_id and amount are required", status_code=400)

        if amount <= 0:
            return error_response("Amount must be positive", status_code=400)

        result = await currency_service.add_currency(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id,
            amount=amount,
            reason=reason
        )

        if result.success:
            logger.audit(
                action="add_currency",
                community=community_id,
                user=user_id,
                result="SUCCESS",
                amount=amount
            )

        return success_response({
            "success": result.success,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Add currency error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/remove', methods=['POST'])
@auth_required
@async_endpoint
async def remove_currency(community_id: int):
    """Remove currency from a user's balance"""
    try:
        data = await request.get_json()

        user_id = data.get('user_id')
        amount = data.get('amount')
        reason = data.get('reason', 'admin_remove')
        platform = data.get('platform', 'twitch')

        if not user_id or amount is None:
            return error_response("user_id and amount are required", status_code=400)

        if amount <= 0:
            return error_response("Amount must be positive", status_code=400)

        result = await currency_service.remove_currency(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id,
            amount=amount,
            reason=reason
        )

        if result.success:
            logger.audit(
                action="remove_currency",
                community=community_id,
                user=user_id,
                result="SUCCESS",
                amount=amount
            )

        return success_response({
            "success": result.success,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Remove currency error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/transfer', methods=['POST'])
@validate_json(CurrencyTransferRequest)
@async_endpoint
async def transfer_currency(validated_data: CurrencyTransferRequest):
    """Transfer currency between users"""
    try:
        result = await currency_service.transfer(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            from_user_id=validated_data.from_user_id,
            to_user_id=validated_data.to_user_id,
            amount=validated_data.amount
        )

        if result.success:
            logger.audit(
                action="transfer_currency",
                community=validated_data.community_id,
                user=validated_data.from_user_id,
                result="SUCCESS",
                amount=validated_data.amount,
                to_user=validated_data.to_user_id
            )

        return success_response({
            "success": result.success,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Transfer currency error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/leaderboard', methods=['GET'])
@validate_query(LeaderboardParams)
@async_endpoint
async def get_leaderboard(query_params: LeaderboardParams, community_id: int):
    """Get top users by balance"""
    try:
        leaderboard = await currency_service.get_leaderboard(
            community_id=community_id,
            platform=query_params.platform,
            limit=query_params.limit
        )

        return success_response({
            "leaderboard": [
                {
                    "rank": idx + 1,
                    "user_id": entry.platform_user_id,
                    "balance": entry.balance,
                    "lifetime_earned": entry.lifetime_earned
                }
                for idx, entry in enumerate(leaderboard)
            ]
        })

    except Exception as e:
        logger.error(f"Leaderboard error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/balance/<string:user_id>', methods=['PUT'])
@auth_required
@validate_json(SetBalanceRequest)
@async_endpoint
async def set_balance(validated_data: SetBalanceRequest, community_id: int, user_id: str):
    """Set exact balance (admin only)"""
    try:
        result = await currency_service.set_balance(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            balance=validated_data.balance
        )

        if result.success:
            logger.audit(
                action="set_balance",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                balance=validated_data.balance
            )

        return success_response({
            "success": result.success,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Set balance error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@currency_bp.route('/<int:community_id>/wipe', methods=['DELETE'])
@auth_required
@async_endpoint
async def wipe_balances(community_id: int):
    """Wipe all balances (admin only)"""
    try:
        platform = request.args.get('platform', 'twitch')

        result = await currency_service.wipe_all_balances(
            community_id=community_id,
            platform=platform
        )

        if result:
            logger.audit(
                action="wipe_balances",
                user=str(request.current_user["user_id"]),
                community=community_id,
                result="SUCCESS",
                platform=platform
            )

        return success_response({
            "success": result,
            "message": "All balances wiped" if result else "Failed to wipe balances"
        })

    except Exception as e:
        logger.error(f"Wipe balances error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# EARNING CONFIG ENDPOINTS
# ============================================================================

@config_bp.route('/config/<int:community_id>', methods=['GET'])
@async_endpoint
async def get_earning_config(community_id: int):
    """Get earning configuration for community"""
    try:
        config = await earning_config_service.get_config(community_id)

        return success_response({
            "earn_chat": config.earn_chat,
            "earn_chat_cooldown": config.earn_chat_cooldown,
            "earn_watch_time": config.earn_watch_time,
            "earn_watch_interval": config.earn_watch_interval,
            "earn_follow": config.earn_follow,
            "earn_sub_t1": config.earn_sub_t1,
            "earn_sub_t2": config.earn_sub_t2,
            "earn_sub_t3": config.earn_sub_t3,
            "earn_sub_gift": config.earn_sub_gift,
            "earn_raid_per_viewer": config.earn_raid_per_viewer,
            "earn_cheer_per_bit": config.earn_cheer_per_bit
        })

    except Exception as e:
        logger.error(f"Get earning config error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@config_bp.route('/config/<int:community_id>', methods=['PUT'])
@auth_required
@validate_json(EarningConfigUpdate)
@async_endpoint
async def update_earning_config(validated_data: EarningConfigUpdate, community_id: int):
    """Update earning configuration"""
    try:
        # Extract only non-None fields for update
        update_data = {
            k: v for k, v in validated_data.dict().items()
            if v is not None and k != 'community_id'
        }

        result = await earning_config_service.update_config(
            community_id=validated_data.community_id,
            **update_data
        )

        if result:
            logger.audit(
                action="update_earning_config",
                community=validated_data.community_id,
                result="SUCCESS",
                user=request.current_user.get('username', 'unknown')
            )

        return success_response({
            "success": result,
            "message": "Configuration updated" if result else "Update failed"
        })

    except Exception as e:
        logger.error(f"Update earning config error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@config_bp.route('/earning/<int:community_id>/chat', methods=['POST'])
@async_endpoint
async def process_chat_earning(community_id: int):
    """Process chat message earning"""
    try:
        data = await request.get_json()

        user_id = data.get('user_id')
        platform = data.get('platform', 'twitch')

        if not user_id:
            return error_response("user_id is required", status_code=400)

        result = await earning_config_service.process_chat_earning(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "earned": result.earned,
            "amount": result.amount,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Process chat earning error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@config_bp.route('/earning/<int:community_id>/event', methods=['POST'])
@validate_json(EventEarningRequest)
@async_endpoint
async def process_event_earning(validated_data: EventEarningRequest, community_id: int):
    """Process event earning (follow, sub, raid, etc.)"""
    try:
        result = await earning_config_service.process_event_earning(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            event_type=validated_data.event_type,
            event_data=validated_data.event_data
        )

        if result.earned:
            logger.audit(
                action="event_earning",
                user=validated_data.user_id,
                community=validated_data.community_id,
                result="SUCCESS",
                source_event_type=validated_data.event_type,
                amount=result.amount
            )

        return success_response({
            "earned": result.earned,
            "amount": result.amount,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Process event earning error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# GIVEAWAY ENDPOINTS
# ============================================================================

@giveaway_bp.route('/<int:community_id>', methods=['POST'])
@auth_required
@validate_json(GiveawayCreateRequest)
@async_endpoint
async def create_giveaway(validated_data: GiveawayCreateRequest, community_id: int):
    """Create a new giveaway"""
    try:
        giveaway = await giveaway_service.create_giveaway(
            community_id=validated_data.community_id,
            title=validated_data.title,
            prize=validated_data.prize,
            entry_cost=validated_data.entry_cost,
            duration_minutes=validated_data.duration_minutes,
            max_entries=validated_data.max_entries,
            reputation_weighted=validated_data.reputation_weighted
        )

        logger.audit(
            action="create_giveaway",
            community=validated_data.community_id,
            result="SUCCESS",
            user=request.current_user.get('username', 'unknown'),
            giveaway_id=giveaway.giveaway_id
        )

        return success_response({
            "giveaway_id": giveaway.giveaway_id,
            "title": giveaway.title,
            "prize": giveaway.prize,
            "status": giveaway.status,
            "ends_at": giveaway.ends_at.isoformat()
        })

    except Exception as e:
        logger.error(f"Create giveaway error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>', methods=['GET'])
@async_endpoint
async def list_giveaways(community_id: int):
    """List giveaways with optional status filter"""
    try:
        status = request.args.get('status')  # active, ended, cancelled

        giveaways = await giveaway_service.list_giveaways(
            community_id=community_id,
            status=status
        )

        return success_response({
            "giveaways": [
                {
                    "giveaway_id": g.giveaway_id,
                    "title": g.title,
                    "prize": g.prize,
                    "status": g.status,
                    "entry_cost": g.entry_cost,
                    "entry_count": g.entry_count,
                    "max_entries": g.max_entries,
                    "ends_at": g.ends_at.isoformat() if g.ends_at else None
                }
                for g in giveaways
            ]
        })

    except Exception as e:
        logger.error(f"List giveaways error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>', methods=['GET'])
@async_endpoint
async def get_giveaway(community_id: int, giveaway_id: int):
    """Get giveaway details"""
    try:
        giveaway = await giveaway_service.get_giveaway(
            community_id=community_id,
            giveaway_id=giveaway_id
        )

        if not giveaway:
            return error_response("Giveaway not found", status_code=404)

        return success_response({
            "giveaway_id": giveaway.giveaway_id,
            "title": giveaway.title,
            "prize": giveaway.prize,
            "description": giveaway.description,
            "status": giveaway.status,
            "entry_cost": giveaway.entry_cost,
            "entry_count": giveaway.entry_count,
            "max_entries": giveaway.max_entries,
            "reputation_weighted": giveaway.reputation_weighted,
            "winner_user_id": giveaway.winner_user_id,
            "created_at": giveaway.created_at.isoformat(),
            "ends_at": giveaway.ends_at.isoformat() if giveaway.ends_at else None
        })

    except Exception as e:
        logger.error(f"Get giveaway error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/enter', methods=['POST'])
@validate_json(GiveawayEntryRequest)
@async_endpoint
async def enter_giveaway(validated_data: GiveawayEntryRequest, community_id: int, giveaway_id: int):
    """Enter a giveaway"""
    try:
        result = await giveaway_service.enter_giveaway(
            community_id=validated_data.community_id,
            giveaway_id=validated_data.giveaway_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id
        )

        if result.success:
            logger.audit(
                action="enter_giveaway",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                giveaway_id=validated_data.giveaway_id
            )

        return success_response({
            "success": result.success,
            "message": result.message,
            "entry_number": result.entry_number
        })

    except Exception as e:
        logger.error(f"Enter giveaway error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/draw', methods=['POST'])
@auth_required
@async_endpoint
async def draw_winner(community_id: int, giveaway_id: int):
    """Draw a winner for the giveaway"""
    try:
        result = await giveaway_service.draw_winner(
            community_id=community_id,
            giveaway_id=giveaway_id
        )

        if result.success:
            logger.audit(
                action="draw_giveaway_winner",
                community=community_id,
                result="SUCCESS",
                user=request.current_user.get('username', 'unknown'),
                giveaway_id=giveaway_id,
                winner=result.winner_user_id
            )

        return success_response({
            "success": result.success,
            "winner_user_id": result.winner_user_id,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Draw winner error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/end', methods=['PUT'])
@auth_required
@async_endpoint
async def end_giveaway(community_id: int, giveaway_id: int):
    """End a giveaway"""
    try:
        result = await giveaway_service.end_giveaway(
            community_id=community_id,
            giveaway_id=giveaway_id
        )

        if result:
            logger.audit(
                action="end_giveaway",
                community=community_id,
                result="SUCCESS",
                user=request.current_user.get('username', 'unknown'),
                giveaway_id=giveaway_id
            )

        return success_response({
            "success": result,
            "message": "Giveaway ended" if result else "Failed to end giveaway"
        })

    except Exception as e:
        logger.error(f"End giveaway error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# KEY GIVEAWAY ENDPOINTS
# ============================================================================

@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/keys', methods=['POST'])
@auth_required
@async_endpoint
async def add_giveaway_keys(community_id: int, giveaway_id: int):
    """Add game keys to a key-type giveaway (admin)."""
    try:
        data = await request.get_json()
        keys = data.get('keys', [])
        key_platform = data.get('key_platform')

        if not keys:
            return error_response("keys list is required", status_code=400)

        max_batch = getattr(Config, 'GIVEAWAY_MAX_KEYS_PER_BATCH', 100)
        if len(keys) > max_batch:
            return error_response(
                f"Maximum {max_batch} keys per batch", status_code=400,
            )

        result = await giveaway_service.add_keys(giveaway_id, keys, key_platform)

        logger.audit(
            action="add_giveaway_keys",
            community=community_id,
            result="SUCCESS",
            user=request.current_user.get('username', 'unknown'),
            giveaway_id=giveaway_id,
            keys_added=result['added'],
        )

        return success_response(result)

    except Exception as e:
        logger.error(f"Add keys error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/keys/count', methods=['GET'])
@auth_required
@async_endpoint
async def get_key_count(community_id: int, giveaway_id: int):
    """Get count of unclaimed keys for a giveaway."""
    try:
        counts = await giveaway_service.get_key_count(giveaway_id)
        return success_response(counts)
    except Exception as e:
        logger.error(f"Key count error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/eligibility', methods=['POST'])
@async_endpoint
async def check_eligibility(community_id: int, giveaway_id: int):
    """Check if a user meets giveaway eligibility requirements."""
    try:
        data = await request.get_json()
        result = await giveaway_service.check_eligibility(
            giveaway_id=giveaway_id,
            community_id=community_id,
            platform=data.get('platform', ''),
            platform_user_id=str(data.get('user_id', '')),
            is_subscriber=data.get('is_subscriber', False),
            account_age_days=data.get('account_age_days', 0),
            loyalty_points=data.get('loyalty_points', 0),
        )
        return success_response(result)
    except Exception as e:
        logger.error(f"Eligibility check error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/draw-winners', methods=['POST'])
@auth_required
@async_endpoint
async def draw_giveaway_winners(community_id: int, giveaway_id: int):
    """Draw N winners for a giveaway (admin)."""
    try:
        data = await request.get_json()
        count = data.get('count')

        max_winners = getattr(Config, 'GIVEAWAY_MAX_WINNERS', 10)
        if count and count > max_winners:
            return error_response(
                f"Maximum {max_winners} winners", status_code=400,
            )

        winners = await giveaway_service.draw_winners(giveaway_id, count)

        logger.audit(
            action="draw_giveaway_winners",
            community=community_id,
            result="SUCCESS",
            user=request.current_user.get('username', 'unknown'),
            giveaway_id=giveaway_id,
            winners_drawn=len(winners),
        )

        return success_response({
            'winners': winners,
            'count': len(winners),
        })

    except Exception as e:
        logger.error(f"Draw winners error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@giveaway_bp.route('/<int:community_id>/<int:giveaway_id>/winners', methods=['GET'])
@auth_required
@async_endpoint
async def list_giveaway_winners(community_id: int, giveaway_id: int):
    """List all winners for a giveaway."""
    try:
        winners = await giveaway_service.get_winners(giveaway_id)
        return success_response({
            'winners': winners,
            'count': len(winners),
        })
    except Exception as e:
        logger.error(f"List winners error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# MINIGAME ENDPOINTS
# ============================================================================

@games_bp.route('/<int:community_id>/slots', methods=['POST'])
@validate_json(SlotsRequest)
@async_endpoint
async def play_slots(validated_data: SlotsRequest, community_id: int):
    """Play slot machine"""
    try:
        # Additional config-based validation
        if validated_data.bet < Config.MIN_BET or validated_data.bet > Config.MAX_BET:
            return error_response(
                f"Bet must be between {Config.MIN_BET} and {Config.MAX_BET}",
                status_code=400
            )

        result = await minigame_service.play_slots(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            bet=validated_data.bet
        )

        if result.success:
            logger.audit(
                action="play_slots",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                bet=validated_data.bet,
                winnings=result.winnings
            )

        return success_response({
            "success": result.success,
            "symbols": result.symbols,
            "won": result.won,
            "winnings": result.winnings,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Play slots error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@games_bp.route('/<int:community_id>/coinflip', methods=['POST'])
@validate_json(CoinflipRequest)
@async_endpoint
async def play_coinflip(validated_data: CoinflipRequest, community_id: int):
    """Play coinflip"""
    try:
        # Additional config-based validation
        if validated_data.bet < Config.MIN_BET or validated_data.bet > Config.MAX_BET:
            return error_response(
                f"Bet must be between {Config.MIN_BET} and {Config.MAX_BET}",
                status_code=400
            )

        result = await minigame_service.play_coinflip(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            bet=validated_data.bet,
            choice=validated_data.choice
        )

        if result.success:
            logger.audit(
                action="play_coinflip",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                bet=validated_data.bet,
                won=result.won
            )

        return success_response({
            "success": result.success,
            "result": result.result,
            "won": result.won,
            "winnings": result.winnings,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Play coinflip error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@games_bp.route('/<int:community_id>/roulette', methods=['POST'])
@validate_json(RouletteRequest)
@async_endpoint
async def play_roulette(validated_data: RouletteRequest, community_id: int):
    """Play roulette"""
    try:
        # Additional config-based validation
        if validated_data.bet < Config.MIN_BET or validated_data.bet > Config.MAX_BET:
            return error_response(
                f"Bet must be between {Config.MIN_BET} and {Config.MAX_BET}",
                status_code=400
            )

        result = await minigame_service.play_roulette(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            bet=validated_data.bet,
            bet_type=validated_data.bet_type,
            bet_value=validated_data.bet_value
        )

        if result.success:
            logger.audit(
                action="play_roulette",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                bet=validated_data.bet,
                won=result.won
            )

        return success_response({
            "success": result.success,
            "number": result.number,
            "color": result.color,
            "won": result.won,
            "winnings": result.winnings,
            "new_balance": result.new_balance,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Play roulette error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@games_bp.route('/<int:community_id>/stats/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_game_stats(community_id: int, user_id: str):
    """Get user's game statistics"""
    try:
        platform = request.args.get('platform', 'twitch')

        stats = await minigame_service.get_user_stats(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "total_games": stats.total_games,
            "total_wagered": stats.total_wagered,
            "total_won": stats.total_won,
            "net_winnings": stats.net_winnings,
            "biggest_win": stats.biggest_win,
            "win_rate": stats.win_rate
        })

    except Exception as e:
        logger.error(f"Get game stats error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# DUEL ENDPOINTS
# ============================================================================

@duel_bp.route('/<int:community_id>/challenge', methods=['POST'])
@validate_json(DuelChallengeRequest)
@async_endpoint
async def create_duel_challenge(validated_data: DuelChallengeRequest, community_id: int):
    """Create a duel challenge"""
    try:
        # Additional config-based validation
        if validated_data.wager < Config.MIN_BET or validated_data.wager > Config.MAX_BET:
            return error_response(
                f"Wager must be between {Config.MIN_BET} and {Config.MAX_BET}",
                status_code=400
            )

        result = await duel_service.create_challenge(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            challenger_id=validated_data.challenger_id,
            opponent_id=validated_data.opponent_id,
            wager=validated_data.wager
        )

        if result.success:
            logger.audit(
                action="create_duel",
                community=validated_data.community_id,
                user=validated_data.challenger_id,
                result="SUCCESS",
                opponent=validated_data.opponent_id,
                wager=validated_data.wager
            )

        return success_response({
            "success": result.success,
            "duel_id": result.duel_id,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Create duel error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@duel_bp.route('/<int:community_id>/accept', methods=['POST'])
@validate_json(DuelActionRequest)
@async_endpoint
async def accept_duel(validated_data: DuelActionRequest, community_id: int):
    """Accept a duel challenge"""
    try:
        result = await duel_service.accept_challenge(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            duel_id=validated_data.duel_id
        )

        if result.success:
            logger.audit(
                action="accept_duel",
                user="system",
                community=validated_data.community_id,
                result="SUCCESS",
                duel_id=validated_data.duel_id,
                winner=result.winner_id
            )

        return success_response({
            "success": result.success,
            "winner_id": result.winner_id,
            "loser_id": result.loser_id,
            "winnings": result.winnings,
            "message": result.message
        })

    except Exception as e:
        logger.error(f"Accept duel error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@duel_bp.route('/<int:community_id>/decline', methods=['POST'])
@validate_json(DuelActionRequest)
@async_endpoint
async def decline_duel(validated_data: DuelActionRequest, community_id: int):
    """Decline a duel challenge"""
    try:
        result = await duel_service.decline_challenge(
            community_id=validated_data.community_id,
            duel_id=validated_data.duel_id
        )

        return success_response({
            "success": result,
            "message": "Duel declined" if result else "Failed to decline duel"
        })

    except Exception as e:
        logger.error(f"Decline duel error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@duel_bp.route('/<int:community_id>/pending/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_pending_duels(community_id: int, user_id: str):
    """Get pending duels for a user"""
    try:
        platform = request.args.get('platform', 'twitch')

        duels = await duel_service.get_pending_duels(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "pending_duels": [
                {
                    "duel_id": d.duel_id,
                    "challenger_id": d.challenger_id,
                    "opponent_id": d.opponent_id,
                    "wager": d.wager,
                    "created_at": d.created_at.isoformat()
                }
                for d in duels
            ]
        })

    except Exception as e:
        logger.error(f"Get pending duels error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@duel_bp.route('/<int:community_id>/stats/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_duel_stats(community_id: int, user_id: str):
    """Get duel statistics for a user"""
    try:
        platform = request.args.get('platform', 'twitch')

        stats = await duel_service.get_user_stats(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "total_duels": stats.total_duels,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_rate": stats.win_rate,
            "total_wagered": stats.total_wagered,
            "net_winnings": stats.net_winnings
        })

    except Exception as e:
        logger.error(f"Get duel stats error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@duel_bp.route('/<int:community_id>/leaderboard', methods=['GET'])
@async_endpoint
async def get_duel_leaderboard(community_id: int):
    """Get duel leaderboard"""
    try:
        limit = int(request.args.get('limit', 10))
        platform = request.args.get('platform', 'twitch')

        leaderboard = await duel_service.get_leaderboard(
            community_id=community_id,
            platform=platform,
            limit=limit
        )

        return success_response({
            "leaderboard": [
                {
                    "rank": idx + 1,
                    "user_id": entry.platform_user_id,
                    "wins": entry.wins,
                    "total_duels": entry.total_duels,
                    "win_rate": entry.win_rate
                }
                for idx, entry in enumerate(leaderboard)
            ]
        })

    except Exception as e:
        logger.error(f"Get duel leaderboard error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


# ============================================================================
# GEAR ENDPOINTS
# ============================================================================

@gear_bp.route('/<int:community_id>/shop', methods=['GET'])
@async_endpoint
async def get_shop_items(community_id: int):
    """Get available shop items"""
    try:
        category = request.args.get('category')

        items = await gear_service.get_shop_items(
            community_id=community_id,
            category=category
        )

        return success_response({
            "items": [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "description": item.description,
                    "category": item.category,
                    "price": item.price,
                    "stat_bonus": item.stat_bonus,
                    "available": item.available
                }
                for item in items
            ]
        })

    except Exception as e:
        logger.error(f"Get shop items error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/<int:community_id>/inventory/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_user_inventory(community_id: int, user_id: str):
    """Get user's inventory"""
    try:
        platform = request.args.get('platform', 'twitch')

        inventory = await gear_service.get_user_inventory(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "inventory": [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "category": item.category,
                    "equipped": item.equipped,
                    "stat_bonus": item.stat_bonus
                }
                for item in inventory
            ]
        })

    except Exception as e:
        logger.error(f"Get inventory error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/<int:community_id>/buy', methods=['POST'])
@validate_json(GearActionRequest)
@async_endpoint
async def buy_item(validated_data: GearActionRequest, community_id: int):
    """Buy a gear item"""
    try:
        result = await gear_service.buy_item(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            item_id=validated_data.item_id
        )

        if result.success:
            logger.audit(
                action="buy_gear",
                community=validated_data.community_id,
                user=validated_data.user_id,
                result="SUCCESS",
                item_id=validated_data.item_id
            )

        return success_response({
            "success": result.success,
            "message": result.message,
            "new_balance": result.new_balance
        })

    except Exception as e:
        logger.error(f"Buy item error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/<int:community_id>/equip', methods=['POST'])
@validate_json(GearActionRequest)
@async_endpoint
async def equip_item(validated_data: GearActionRequest, community_id: int):
    """Equip a gear item"""
    try:
        result = await gear_service.equip_item(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            item_id=validated_data.item_id
        )

        return success_response({
            "success": result,
            "message": "Item equipped" if result else "Failed to equip item"
        })

    except Exception as e:
        logger.error(f"Equip item error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/<int:community_id>/unequip', methods=['POST'])
@validate_json(GearActionRequest)
@async_endpoint
async def unequip_item(validated_data: GearActionRequest, community_id: int):
    """Unequip a gear item"""
    try:
        result = await gear_service.unequip_item(
            community_id=validated_data.community_id,
            platform=validated_data.platform,
            platform_user_id=validated_data.user_id,
            item_id=validated_data.item_id
        )

        return success_response({
            "success": result,
            "message": "Item unequipped" if result else "Failed to unequip item"
        })

    except Exception as e:
        logger.error(f"Unequip item error: {e}", community=validated_data.community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/<int:community_id>/equipped/<string:user_id>', methods=['GET'])
@async_endpoint
async def get_equipped_stats(community_id: int, user_id: str):
    """Get user's equipped gear and total stat bonuses"""
    try:
        platform = request.args.get('platform', 'twitch')

        stats = await gear_service.get_equipped_stats(
            community_id=community_id,
            platform=platform,
            platform_user_id=user_id
        )

        return success_response({
            "equipped_items": stats.equipped_items,
            "total_stats": stats.total_stats
        })

    except Exception as e:
        logger.error(f"Get equipped stats error: {e}", community=community_id)
        return error_response(str(e), status_code=500)


@gear_bp.route('/categories', methods=['GET'])
@async_endpoint
async def get_gear_categories():
    """Get all gear categories"""
    try:
        categories = await gear_service.get_categories()

        return success_response({
            "categories": categories
        })

    except Exception as e:
        logger.error(f"Get gear categories error: {e}")
        return error_response(str(e), status_code=500)


# ============================================================================
# CHAT COMMAND ENDPOINT (for router integration)
# ============================================================================

@command_bp.route('/command', methods=['POST'])
@async_endpoint
async def handle_command():
    """
    Handle chat commands from the router.

    Commands:
    - !balance, !bal - Check balance
    - !gamble <amount> - Play slots
    - !coinflip <heads|tails> <amount> - Flip a coin
    - !roulette <bet_type> <amount> - Play roulette
    - !duel @user <amount> - Challenge to duel
    - !gear shop - View shop
    - !gear buy <item_id> - Buy item
    - !gear - View inventory
    """
    try:
        data = await request.get_json()

        # Extract command data
        session_id = data.get('session_id')
        command = data.get('command', '').lower()
        args = data.get('args', [])
        user_id = data.get('user_id')
        community_id = data.get('community_id')
        platform = data.get('platform', 'twitch')

        if not all([session_id, command, user_id, community_id]):
            return error_response(
                "session_id, command, user_id, and community_id are required",
                status_code=400
            )

        # Route to appropriate handler
        response_message = ""

        if command in ['balance', 'bal']:
            balance = await currency_service.get_balance(
                community_id=community_id,
                platform=platform,
                platform_user_id=user_id
            )
            response_message = (
                f"Balance: {balance.balance} | "
                f"Earned: {balance.lifetime_earned} | "
                f"Spent: {balance.lifetime_spent}"
            )

        elif command == 'leaderboard':
            limit = int(args[0]) if args else 10
            leaderboard = await currency_service.get_leaderboard(
                community_id=community_id,
                platform=platform,
                limit=limit
            )
            top_5 = leaderboard[:5]
            response_message = "Top Balances: " + " | ".join(
                [f"{i+1}. {e.platform_user_id}: {e.balance}" for i, e in enumerate(top_5)]
            )

        elif command in ['gamble', 'slots']:
            if not args or not args[0].isdigit():
                response_message = f"Usage: !{command} <amount>"
            else:
                bet = int(args[0])
                result = await minigame_service.play_slots(
                    community_id=community_id,
                    platform=platform,
                    platform_user_id=user_id,
                    bet=bet
                )
                response_message = result.message

        elif command == 'coinflip':
            if len(args) < 2 or args[0].lower() not in ['heads', 'tails'] or not args[1].isdigit():
                response_message = "Usage: !coinflip <heads|tails> <amount>"
            else:
                choice = args[0].lower()
                bet = int(args[1])
                result = await minigame_service.play_coinflip(
                    community_id=community_id,
                    platform=platform,
                    platform_user_id=user_id,
                    bet=bet,
                    choice=choice
                )
                response_message = result.message

        elif command == 'gear':
            if not args:
                # Show inventory
                inventory = await gear_service.get_user_inventory(
                    community_id=community_id,
                    platform=platform,
                    platform_user_id=user_id
                )
                if inventory:
                    response_message = f"Your gear: {len(inventory)} items. Use !gear shop to browse."
                else:
                    response_message = "Your inventory is empty. Use !gear shop to browse items."
            elif args[0] == 'shop':
                items = await gear_service.get_shop_items(community_id=community_id)
                if items:
                    response_message = f"{len(items)} items available. Visit the web portal to browse!"
                else:
                    response_message = "No items available in shop."
            else:
                response_message = "Usage: !gear [shop]"

        else:
            response_message = "Unknown command. Try !balance, !gamble, !coinflip, or !gear"

        # Return response
        return success_response({
            "session_id": session_id,
            "response_action": "chat",
            "response_data": {
                "message": response_message
            }
        })

    except Exception as e:
        logger.error(f"Command handler error: {e}")
        return error_response(str(e), status_code=500)


# Register all blueprints
app.register_blueprint(currency_bp)
app.register_blueprint(config_bp)
app.register_blueprint(giveaway_bp)
app.register_blueprint(games_bp)
app.register_blueprint(duel_bp)
app.register_blueprint(gear_bp)
app.register_blueprint(command_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
