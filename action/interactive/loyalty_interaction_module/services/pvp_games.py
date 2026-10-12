"""
PvP Games Service for Loyalty Module
Four competitive game modes with wagering and leaderboards:
1. Medieval Duel - Turn-based combat with attack/defend/special moves
2. Ground Combat - Squad-based tactical combat with positioning
3. Tank Battles - Vehicle combat with upgrades and tactics
4. Racing - High-speed racing with power-ups

Features:
- Challenge system with betting
- Accept/decline mechanics
- Turn-based gameplay with timeouts
- Winner takes pot (minus house cut)
- Per-game stats tracking
- Leaderboards per game type
"""
import logging
import random
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class GameType(Enum):
    """Available PvP game types"""
    MEDIEVAL_DUEL = 'medieval_duel'
    GROUND_COMBAT = 'ground_combat'
    TANK_BATTLES = 'tank_battles'
    RACING = 'racing'


class GameAction(Enum):
    """Game actions for turn-based combat"""
    # Medieval Duel
    ATTACK = 'attack'
    DEFEND = 'defend'
    SPECIAL = 'special'

    # Ground Combat
    ADVANCE = 'advance'
    HOLD = 'hold'
    FLANK = 'flank'
    RETREAT = 'retreat'

    # Tank Battles
    SHOOT = 'shoot'
    ARMOR = 'armor'
    REPAIR = 'repair'
    UPGRADE = 'upgrade'

    # Racing
    ACCELERATE = 'accelerate'
    DRIFT = 'drift'
    POWERUP = 'powerup'
    BLOCK = 'block'


@dataclass
class PvPChallenge:
    """PvP challenge data"""
    id: int
    community_id: int
    game_type: str
    challenger_id: int
    challenger_platform: str
    challenger_user_id: str
    defender_id: Optional[int]
    defender_platform: Optional[str]
    defender_user_id: Optional[str]
    bet_amount: int
    is_open: bool
    expires_at: datetime
    status: str


@dataclass
class GameState:
    """Current state of an active game"""
    match_id: int
    game_type: str
    player1_id: int
    player1_health: int
    player1_position: int
    player1_energy: int
    player2_id: int
    player2_health: int
    player2_position: int
    player2_energy: int
    current_turn: int
    current_player: int
    turn_timeout: datetime
    match_data: Dict[str, Any]


@dataclass
class GameResult:
    """Result of a game action"""
    success: bool
    message: str
    winner_id: Optional[int] = None
    loser_id: Optional[int] = None
    winnings: int = 0
    game_over: bool = False
    game_state: Optional[Dict[str, Any]] = None


class PvPGamesService:
    """
    Advanced PvP games system with multiple game modes.

    Supports:
    - Medieval Duel: Turn-based combat with attack/defend/special
    - Ground Combat: Squad battles with positioning mechanics
    - Tank Battles: Vehicle combat with upgrades
    - Racing: Race with power-ups and blocking
    """

    def __init__(self, dal, currency_service):
        """
        Initialize PvP games service.

        Args:
            dal: Database access layer
            currency_service: Currency service for betting
        """
        self.dal = dal
        self.currency_service = currency_service
        self.challenge_timeout_minutes = 5
        self.turn_timeout_seconds = 60
        self.house_cut_percent = 5  # 5% house cut on winnings

        # Game-specific configurations
        self.game_configs = {
            GameType.MEDIEVAL_DUEL.value: {
                'base_health': 100,
                'base_energy': 50,
                'max_turns': 20
            },
            GameType.GROUND_COMBAT.value: {
                'base_health': 150,
                'squad_size': 5,
                'max_position': 10,
                'max_turns': 15
            },
            GameType.TANK_BATTLES.value: {
                'base_health': 200,
                'base_armor': 50,
                'max_upgrades': 3,
                'max_turns': 15
            },
            GameType.RACING.value: {
                'track_length': 100,
                'base_speed': 5,
                'max_powerups': 3,
                'max_turns': 20
            }
        }

    async def create_challenge(
        self,
        community_id: int,
        game_type: str,
        challenger_id: int,
        challenger_platform: str,
        challenger_user_id: str,
        bet_amount: int,
        defender_id: Optional[int] = None,
        defender_platform: Optional[str] = None,
        defender_user_id: Optional[str] = None,
        is_open: bool = False
    ) -> Dict[str, Any]:
        """
        Create a PvP game challenge.

        Args:
            community_id: Community ID
            game_type: Type of game (medieval_duel, ground_combat, etc.)
            challenger_id: Hub user ID of challenger
            challenger_platform: Platform of challenger
            challenger_user_id: Platform user ID of challenger
            bet_amount: Amount to wager
            defender_id: Hub user ID of defender (optional for open challenges)
            defender_platform: Platform of defender (optional)
            defender_user_id: Platform user ID of defender (optional)
            is_open: Whether this is an open challenge anyone can accept

        Returns:
            Dict with success status and challenge details
        """
        try:
            # Validate game type
            if game_type not in [gt.value for gt in GameType]:
                return {'success': False, 'message': f'Invalid game type: {game_type}'}

            # Validate bet amount
            if bet_amount <= 0:
                return {'success': False, 'message': 'Bet amount must be positive'}

            # Check challenger balance
            balance = await self.currency_service.get_balance(
                community_id, challenger_platform, challenger_user_id
            )

            if balance.balance < bet_amount:
                return {
                    'success': False,
                    'message': f'Insufficient balance. You have {balance.balance}, need {bet_amount}'
                }

            # Check for existing pending challenge
            check_query = """
                SELECT id FROM pvp_match_history
                WHERE community_id = $1
                  AND game_type = $2
                  AND (player1_id = $3 OR player2_id = $3)
                  AND winner_id IS NULL
                  AND played_at > NOW() - INTERVAL '10 minutes'
            """
            existing = await self.dal.execute(check_query, [community_id, game_type, challenger_id])

            if existing:
                return {
                    'success': False,
                    'message': f'You already have an active {game_type.replace("_", " ")} challenge'
                }

            # Hold the bet amount
            hold_result = await self.currency_service.remove_currency(
                community_id, challenger_platform, challenger_user_id, bet_amount,
                f'pvp_{game_type}_bet_hold', f"{game_type} bet held"
            )

            if not hold_result.success:
                return {'success': False, 'message': hold_result.message}

            # Create match history entry as pending
            expires_at = datetime.utcnow() + timedelta(minutes=self.challenge_timeout_minutes)

            insert_query = """
                INSERT INTO pvp_match_history
                    (community_id, game_type, player1_id, player2_id, currency_wagered,
                     match_data, played_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """

            match_data = {
                'status': 'pending',
                'expires_at': expires_at.isoformat(),
                'is_open': is_open,
                'challenger_platform': challenger_platform,
                'challenger_user_id': challenger_user_id,
                'defender_platform': defender_platform,
                'defender_user_id': defender_user_id
            }

            result = await self.dal.execute(insert_query, [
                community_id, game_type, challenger_id, defender_id,
                bet_amount, json.dumps(match_data), datetime.utcnow()
            ])

            challenge_id = result[0]['id'] if result else None

            game_name = game_type.replace('_', ' ').title()

            if is_open:
                message = f"{game_name} challenge created for {bet_amount}! Anyone can accept with !pvp accept {game_type}"
            elif defender_user_id:
                message = f"{game_name} challenge sent to @{defender_user_id} for {bet_amount}! They have {self.challenge_timeout_minutes} minutes to accept."
            else:
                message = f"{game_name} challenge created for {bet_amount}!"

            return {
                'success': True,
                'challenge_id': challenge_id,
                'game_type': game_type,
                'bet_amount': bet_amount,
                'expires_at': expires_at.isoformat(),
                'message': message
            }

        except Exception as e:
            logger.error(f"Error creating PvP challenge: {e}")
            return {'success': False, 'message': 'Failed to create challenge'}

    async def accept_challenge(
        self,
        community_id: int,
        game_type: str,
        defender_id: int,
        defender_platform: str,
        defender_user_id: str,
        challenge_id: Optional[int] = None
    ) -> GameResult:
        """
        Accept a PvP challenge and start the game.

        Args:
            community_id: Community ID
            game_type: Type of game
            defender_id: Hub user ID of defender
            defender_platform: Platform of defender
            defender_user_id: Platform user ID of defender
            challenge_id: Specific challenge ID (optional)

        Returns:
            GameResult with game state and instructions
        """
        try:
            # Find challenge to accept
            if challenge_id:
                query = """
                    SELECT pmh.*, md.match_data
                    FROM pvp_match_history pmh
                    CROSS JOIN LATERAL (SELECT match_data FROM pvp_match_history WHERE id = pmh.id) md
                    WHERE pmh.id = $1
                      AND pmh.community_id = $2
                      AND pmh.game_type = $3
                      AND pmh.winner_id IS NULL
                """
                params = [challenge_id, community_id, game_type]
            else:
                # Find open challenge or direct challenge
                query = """
                    SELECT pmh.*, md.match_data
                    FROM pvp_match_history pmh
                    CROSS JOIN LATERAL (SELECT match_data FROM pvp_match_history WHERE id = pmh.id) md
                    WHERE pmh.community_id = $1
                      AND pmh.game_type = $2
                      AND pmh.winner_id IS NULL
                      AND pmh.played_at > NOW() - INTERVAL '10 minutes'
                    ORDER BY pmh.played_at DESC
                    LIMIT 1
                """
                params = [community_id, game_type]

            matches = await self.dal.execute(query, params)

            if not matches:
                return GameResult(
                    success=False,
                    message=f"No pending {game_type.replace('_', ' ')} challenge found"
                )

            match = matches[0]
            match_data = match['match_data'] if isinstance(match['match_data'], dict) else json.loads(match['match_data'])

            # Check if challenge is expired
            expires_at = datetime.fromisoformat(match_data.get('expires_at', ''))
            if datetime.utcnow() > expires_at:
                # Refund challenger
                await self._refund_challenge(match)
                return GameResult(success=False, message="Challenge has expired")

            # Can't accept your own challenge
            if match['player1_id'] == defender_id:
                return GameResult(success=False, message="You can't accept your own challenge")

            # Check if defender has enough balance
            balance = await self.currency_service.get_balance(
                community_id, defender_platform, defender_user_id
            )

            bet_amount = match['currency_wagered']

            if balance.balance < bet_amount:
                return GameResult(
                    success=False,
                    message=f"Insufficient balance. You need {bet_amount}, have {balance.balance}"
                )

            # Hold defender's bet
            hold_result = await self.currency_service.remove_currency(
                community_id, defender_platform, defender_user_id, bet_amount,
                f'pvp_{game_type}_bet_hold', f"{game_type} bet held"
            )

            if not hold_result.success:
                return GameResult(success=False, message=hold_result.message)

            # Initialize game state
            game_state = await self._initialize_game_state(
                match['id'], game_type, match['player1_id'], defender_id
            )

            # Update match with defender and game state
            update_query = """
                UPDATE pvp_match_history
                SET player2_id = $1,
                    match_data = $2,
                    played_at = NOW()
                WHERE id = $3
            """

            match_data['status'] = 'active'
            match_data['defender_id'] = defender_id
            match_data['defender_platform'] = defender_platform
            match_data['defender_user_id'] = defender_user_id
            match_data['game_state'] = game_state
            match_data['started_at'] = datetime.utcnow().isoformat()

            await self.dal.execute(update_query, [
                defender_id,
                json.dumps(match_data),
                match['id']
            ])

            game_name = game_type.replace('_', ' ').title()
            current_player = 'Player 1' if game_state['current_player'] == match['player1_id'] else 'Player 2'

            return GameResult(
                success=True,
                message=f"{game_name} has begun! {current_player}'s turn. Pot: {bet_amount * 2}",
                game_state=game_state
            )

        except Exception as e:
            logger.error(f"Error accepting challenge: {e}")
            return GameResult(success=False, message="Failed to accept challenge")

    async def make_move(
        self,
        community_id: int,
        game_type: str,
        match_id: int,
        player_id: int,
        action: str,
        action_data: Optional[Dict[str, Any]] = None
    ) -> GameResult:
        """
        Make a move in an active PvP game.

        Args:
            community_id: Community ID
            game_type: Type of game
            match_id: Match ID
            player_id: Hub user ID making the move
            action: Action to take (attack, defend, special, etc.)
            action_data: Additional action data

        Returns:
            GameResult with updated game state
        """
        try:
            # Get current match state
            query = """
                SELECT * FROM pvp_match_history
                WHERE id = $1 AND community_id = $2 AND game_type = $3 AND winner_id IS NULL
            """
            matches = await self.dal.execute(query, [match_id, community_id, game_type])

            if not matches:
                return GameResult(success=False, message="Match not found or already completed")

            match = matches[0]
            match_data = match['match_data'] if isinstance(match['match_data'], dict) else json.loads(match['match_data'])

            if match_data.get('status') != 'active':
                return GameResult(success=False, message="Match is not active")

            game_state = match_data.get('game_state', {})

            # Verify it's the player's turn
            if game_state.get('current_player') != player_id:
                return GameResult(success=False, message="It's not your turn")

            # Check turn timeout
            turn_timeout = datetime.fromisoformat(game_state.get('turn_timeout', ''))
            if datetime.utcnow() > turn_timeout:
                # Timeout - other player wins
                winner_id = match['player1_id'] if player_id == match['player2_id'] else match['player2_id']
                return await self._end_game(match, winner_id, 'timeout')

            # Process move based on game type
            if game_type == GameType.MEDIEVAL_DUEL.value:
                result = await self._process_medieval_duel_move(match, game_state, player_id, action, action_data)
            elif game_type == GameType.GROUND_COMBAT.value:
                result = await self._process_ground_combat_move(match, game_state, player_id, action, action_data)
            elif game_type == GameType.TANK_BATTLES.value:
                result = await self._process_tank_battle_move(match, game_state, player_id, action, action_data)
            elif game_type == GameType.RACING.value:
                result = await self._process_racing_move(match, game_state, player_id, action, action_data)
            else:
                return GameResult(success=False, message=f"Unknown game type: {game_type}")

            return result

        except Exception as e:
            logger.error(f"Error processing move: {e}")
            return GameResult(success=False, message="Failed to process move")

    async def _initialize_game_state(
        self,
        match_id: int,
        game_type: str,
        player1_id: int,
        player2_id: int
    ) -> Dict[str, Any]:
        """
        Initialize game state for a new match.

        Args:
            match_id: Match ID
            game_type: Type of game
            player1_id: First player ID
            player2_id: Second player ID

        Returns:
            Initial game state dict
        """
        config = self.game_configs.get(game_type, {})

        # Random starting player
        starting_player = random.choice([player1_id, player2_id])
        turn_timeout = datetime.utcnow() + timedelta(seconds=self.turn_timeout_seconds)

        base_state = {
            'match_id': match_id,
            'current_turn': 1,
            'current_player': starting_player,
            'turn_timeout': turn_timeout.isoformat(),
            'history': []
        }

        if game_type == GameType.MEDIEVAL_DUEL.value:
            return {
                **base_state,
                'player1_health': config['base_health'],
                'player1_energy': config['base_energy'],
                'player1_defense_bonus': 0,
                'player2_health': config['base_health'],
                'player2_energy': config['base_energy'],
                'player2_defense_bonus': 0,
                'max_turns': config['max_turns']
            }

        elif game_type == GameType.GROUND_COMBAT.value:
            return {
                **base_state,
                'player1_health': config['base_health'],
                'player1_position': 0,
                'player1_squad': config['squad_size'],
                'player2_health': config['base_health'],
                'player2_position': config['max_position'],
                'player2_squad': config['squad_size'],
                'max_turns': config['max_turns']
            }

        elif game_type == GameType.TANK_BATTLES.value:
            return {
                **base_state,
                'player1_health': config['base_health'],
                'player1_armor': config['base_armor'],
                'player1_upgrades': 0,
                'player1_ammo': 5,
                'player2_health': config['base_health'],
                'player2_armor': config['base_armor'],
                'player2_upgrades': 0,
                'player2_ammo': 5,
                'max_turns': config['max_turns']
            }

        elif game_type == GameType.RACING.value:
            return {
                **base_state,
                'player1_position': 0,
                'player1_speed': config['base_speed'],
                'player1_powerups': [],
                'player2_position': 0,
                'player2_speed': config['base_speed'],
                'player2_powerups': [],
                'track_length': config['track_length'],
                'max_turns': config['max_turns']
            }

        return base_state

    async def _process_medieval_duel_move(
        self,
        match: Dict[str, Any],
        game_state: Dict[str, Any],
        player_id: int,
        action: str,
        action_data: Optional[Dict[str, Any]]
    ) -> GameResult:
        """Process a Medieval Duel move."""
        is_player1 = player_id == match['player1_id']
        opponent_id = match['player2_id'] if is_player1 else match['player1_id']

        player_prefix = 'player1' if is_player1 else 'player2'
        opponent_prefix = 'player2' if is_player1 else 'player1'

        player_health = game_state[f'{player_prefix}_health']
        player_energy = game_state[f'{player_prefix}_energy']
        player_defense = game_state[f'{player_prefix}_defense_bonus']

        opponent_health = game_state[f'{opponent_prefix}_health']
        opponent_defense = game_state[f'{opponent_prefix}_defense_bonus']

        message_parts = []

        if action == GameAction.ATTACK.value:
            # Attack costs energy
            if player_energy < 10:
                return GameResult(success=False, message="Not enough energy to attack")

            # Calculate damage (10-30 base, reduced by opponent defense)
            base_damage = random.randint(10, 30)
            actual_damage = max(5, base_damage - opponent_defense)

            game_state[f'{player_prefix}_energy'] -= 10
            game_state[f'{opponent_prefix}_health'] -= actual_damage
            game_state[f'{opponent_prefix}_defense_bonus'] = 0  # Clear defense after hit

            message_parts.append(f"⚔️ Attack! Dealt {actual_damage} damage")

        elif action == GameAction.DEFEND.value:
            # Defend adds defense bonus for next turn
            defense_bonus = random.randint(5, 15)
            game_state[f'{player_prefix}_defense_bonus'] = defense_bonus
            game_state[f'{player_prefix}_energy'] = min(player_energy + 5, 50)  # Restore some energy

            message_parts.append(f"🛡️ Defend! +{defense_bonus} defense, +5 energy")

        elif action == GameAction.SPECIAL.value:
            # Special move - high damage, high energy cost
            if player_energy < 25:
                return GameResult(success=False, message="Not enough energy for special attack (need 25)")

            base_damage = random.randint(25, 50)
            actual_damage = max(10, base_damage - opponent_defense)

            game_state[f'{player_prefix}_energy'] -= 25
            game_state[f'{opponent_prefix}_health'] -= actual_damage
            game_state[f'{opponent_prefix}_defense_bonus'] = 0

            message_parts.append(f"💥 SPECIAL ATTACK! Dealt {actual_damage} damage")
        else:
            return GameResult(success=False, message=f"Invalid action: {action}")

        # Check win conditions
        if game_state[f'{opponent_prefix}_health'] <= 0:
            message_parts.append("🏆 Victory! Opponent defeated!")
            return await self._end_game(match, player_id, 'combat', message=' '.join(message_parts))

        # Check max turns
        if game_state['current_turn'] >= game_state['max_turns']:
            # Whoever has more health wins
            winner_id = match['player1_id'] if game_state['player1_health'] > game_state['player2_health'] else match['player2_id']
            return await self._end_game(match, winner_id, 'max_turns')

        # Add to history
        game_state['history'].append({
            'turn': game_state['current_turn'],
            'player': player_id,
            'action': action,
            'result': ' '.join(message_parts)
        })

        # Switch turns
        game_state['current_turn'] += 1
        game_state['current_player'] = opponent_id
        game_state['turn_timeout'] = (datetime.utcnow() + timedelta(seconds=self.turn_timeout_seconds)).isoformat()

        # Update match
        await self._update_game_state(match['id'], game_state)

        status = f"Turn {game_state['current_turn']} | HP: P1({game_state['player1_health']}) vs P2({game_state['player2_health']})"

        return GameResult(
            success=True,
            message=f"{' '.join(message_parts)} | {status}",
            game_state=game_state
        )

    async def _process_ground_combat_move(
        self,
        match: Dict[str, Any],
        game_state: Dict[str, Any],
        player_id: int,
        action: str,
        action_data: Optional[Dict[str, Any]]
    ) -> GameResult:
        """Process a Ground Combat move."""
        is_player1 = player_id == match['player1_id']
        opponent_id = match['player2_id'] if is_player1 else match['player1_id']

        player_prefix = 'player1' if is_player1 else 'player2'
        opponent_prefix = 'player2' if is_player1 else 'player1'

        player_position = game_state[f'{player_prefix}_position']
        opponent_position = game_state[f'{opponent_prefix}_position']
        player_squad = game_state[f'{player_prefix}_squad']
        opponent_squad = game_state[f'{opponent_prefix}_squad']

        message_parts = []

        if action == GameAction.ADVANCE.value:
            # Move forward and attack if in range
            if is_player1:
                game_state[f'{player_prefix}_position'] = min(player_position + 2, 10)
            else:
                game_state[f'{player_prefix}_position'] = max(player_position - 2, 0)

            distance = abs(game_state['player1_position'] - game_state['player2_position'])

            if distance <= 2:
                damage = random.randint(10, 20) * player_squad
                casualties = random.randint(0, 2)
                game_state[f'{opponent_prefix}_health'] -= damage
                game_state[f'{opponent_prefix}_squad'] = max(1, opponent_squad - casualties)
                message_parts.append(f"⚔️ Advanced and engaged! {damage} damage, {casualties} enemy casualties")
            else:
                message_parts.append("🏃 Advanced position")

        elif action == GameAction.HOLD.value:
            # Hold position and fortify
            healing = random.randint(5, 15)
            game_state[f'{player_prefix}_health'] = min(game_state[f'{player_prefix}_health'] + healing, 150)
            message_parts.append(f"🛡️ Held position! +{healing} HP")

        elif action == GameAction.FLANK.value:
            # Risky move - high damage but can backfire
            if random.random() < 0.6:  # 60% success rate
                damage = random.randint(20, 40) * player_squad
                game_state[f'{opponent_prefix}_health'] -= damage
                message_parts.append(f"🎯 Flank successful! {damage} damage")
            else:
                damage = random.randint(10, 20)
                game_state[f'{player_prefix}_health'] -= damage
                message_parts.append(f"❌ Flank failed! Took {damage} damage")

        elif action == GameAction.RETREAT.value:
            # Retreat to recover
            if is_player1:
                game_state[f'{player_prefix}_position'] = max(player_position - 2, 0)
            else:
                game_state[f'{player_prefix}_position'] = min(player_position + 2, 10)

            healing = random.randint(10, 25)
            game_state[f'{player_prefix}_health'] = min(game_state[f'{player_prefix}_health'] + healing, 150)
            message_parts.append(f"🔙 Retreated! +{healing} HP")
        else:
            return GameResult(success=False, message=f"Invalid action: {action}")

        # Check win conditions
        if game_state[f'{opponent_prefix}_health'] <= 0 or game_state[f'{opponent_prefix}_squad'] <= 0:
            message_parts.append("🏆 Victory! Enemy squad eliminated!")
            return await self._end_game(match, player_id, 'combat', message=' '.join(message_parts))

        # Check max turns
        if game_state['current_turn'] >= game_state['max_turns']:
            # Whoever has more health wins
            winner_id = match['player1_id'] if game_state['player1_health'] > game_state['player2_health'] else match['player2_id']
            return await self._end_game(match, winner_id, 'max_turns')

        # Add to history
        game_state['history'].append({
            'turn': game_state['current_turn'],
            'player': player_id,
            'action': action,
            'result': ' '.join(message_parts)
        })

        # Switch turns
        game_state['current_turn'] += 1
        game_state['current_player'] = opponent_id
        game_state['turn_timeout'] = (datetime.utcnow() + timedelta(seconds=self.turn_timeout_seconds)).isoformat()

        # Update match
        await self._update_game_state(match['id'], game_state)

        return GameResult(
            success=True,
            message=' '.join(message_parts),
            game_state=game_state
        )

    async def _process_tank_battle_move(
        self,
        match: Dict[str, Any],
        game_state: Dict[str, Any],
        player_id: int,
        action: str,
        action_data: Optional[Dict[str, Any]]
    ) -> GameResult:
        """Process a Tank Battle move."""
        is_player1 = player_id == match['player1_id']
        opponent_id = match['player2_id'] if is_player1 else match['player1_id']

        player_prefix = 'player1' if is_player1 else 'player2'
        opponent_prefix = 'player2' if is_player1 else 'player1'

        player_ammo = game_state[f'{player_prefix}_ammo']
        player_armor = game_state[f'{player_prefix}_armor']
        opponent_armor = game_state[f'{opponent_prefix}_armor']

        message_parts = []

        if action == GameAction.SHOOT.value:
            # Shoot requires ammo
            if player_ammo <= 0:
                return GameResult(success=False, message="Out of ammo! Use REPAIR to resupply")

            # Calculate damage based on armor
            base_damage = random.randint(30, 60)
            armor_reduction = min(opponent_armor, base_damage // 2)
            actual_damage = base_damage - armor_reduction

            game_state[f'{player_prefix}_ammo'] -= 1
            game_state[f'{opponent_prefix}_health'] -= actual_damage
            game_state[f'{opponent_prefix}_armor'] = max(0, opponent_armor - 10)  # Armor degrades

            message_parts.append(f"💥 BOOM! {actual_damage} damage (armor reduced by {armor_reduction})")

        elif action == GameAction.ARMOR.value:
            # Boost armor
            armor_boost = random.randint(15, 30)
            game_state[f'{player_prefix}_armor'] = min(player_armor + armor_boost, 100)
            message_parts.append(f"🛡️ Armor reinforced! +{armor_boost} armor")

        elif action == GameAction.REPAIR.value:
            # Repair and resupply
            healing = random.randint(15, 30)
            game_state[f'{player_prefix}_health'] = min(game_state[f'{player_prefix}_health'] + healing, 200)
            game_state[f'{player_prefix}_ammo'] = min(player_ammo + 2, 5)
            message_parts.append(f"🔧 Repaired! +{healing} HP, +2 ammo")

        elif action == GameAction.UPGRADE.value:
            # Permanent upgrade
            if game_state[f'{player_prefix}_upgrades'] >= 3:
                return GameResult(success=False, message="Maximum upgrades reached")

            game_state[f'{player_prefix}_upgrades'] += 1
            game_state[f'{player_prefix}_armor'] += 20
            message_parts.append("⬆️ Upgrade complete! +20 armor")
        else:
            return GameResult(success=False, message=f"Invalid action: {action}")

        # Check win conditions
        if game_state[f'{opponent_prefix}_health'] <= 0:
            message_parts.append("🏆 Victory! Enemy tank destroyed!")
            return await self._end_game(match, player_id, 'combat', message=' '.join(message_parts))

        # Check max turns
        if game_state['current_turn'] >= game_state['max_turns']:
            winner_id = match['player1_id'] if game_state['player1_health'] > game_state['player2_health'] else match['player2_id']
            return await self._end_game(match, winner_id, 'max_turns')

        # Add to history
        game_state['history'].append({
            'turn': game_state['current_turn'],
            'player': player_id,
            'action': action,
            'result': ' '.join(message_parts)
        })

        # Switch turns
        game_state['current_turn'] += 1
        game_state['current_player'] = opponent_id
        game_state['turn_timeout'] = (datetime.utcnow() + timedelta(seconds=self.turn_timeout_seconds)).isoformat()

        # Update match
        await self._update_game_state(match['id'], game_state)

        return GameResult(
            success=True,
            message=' '.join(message_parts),
            game_state=game_state
        )

    async def _process_racing_move(
        self,
        match: Dict[str, Any],
        game_state: Dict[str, Any],
        player_id: int,
        action: str,
        action_data: Optional[Dict[str, Any]]
    ) -> GameResult:
        """Process a Racing move."""
        is_player1 = player_id == match['player1_id']
        opponent_id = match['player2_id'] if is_player1 else match['player1_id']

        player_prefix = 'player1' if is_player1 else 'player2'
        opponent_prefix = 'player2' if is_player1 else 'player1'

        player_position = game_state[f'{player_prefix}_position']
        player_speed = game_state[f'{player_prefix}_speed']
        player_powerups = game_state[f'{player_prefix}_powerups']

        message_parts = []

        if action == GameAction.ACCELERATE.value:
            # Normal acceleration
            movement = player_speed + random.randint(0, 3)
            game_state[f'{player_prefix}_position'] += movement
            message_parts.append(f"🏎️ Accelerate! Moved {movement} units")

        elif action == GameAction.DRIFT.value:
            # Risky drift - can go very fast or lose control
            if random.random() < 0.7:  # 70% success
                movement = player_speed + random.randint(3, 8)
                game_state[f'{player_prefix}_position'] += movement
                game_state[f'{player_prefix}_speed'] = min(player_speed + 1, 10)
                message_parts.append(f"🎯 Perfect drift! Moved {movement} units, +1 speed")
            else:
                movement = max(1, player_speed - 2)
                game_state[f'{player_prefix}_position'] += movement
                message_parts.append(f"💨 Lost control! Only moved {movement} units")

        elif action == GameAction.POWERUP.value:
            # Pick up powerup
            if len(player_powerups) >= 3:
                return GameResult(success=False, message="Powerup inventory full!")

            powerup_types = ['boost', 'shield', 'sabotage']
            new_powerup = random.choice(powerup_types)
            game_state[f'{player_prefix}_powerups'].append(new_powerup)

            # Auto-use boost
            if new_powerup == 'boost':
                movement = player_speed + 5
                game_state[f'{player_prefix}_position'] += movement
                message_parts.append(f"⚡ Boost powerup! Moved {movement} units")
            elif new_powerup == 'shield':
                message_parts.append("🛡️ Shield powerup collected!")
            else:
                # Sabotage opponent
                game_state[f'{opponent_prefix}_speed'] = max(3, game_state[f'{opponent_prefix}_speed'] - 2)
                message_parts.append("🔧 Sabotage! Opponent speed reduced")

        elif action == GameAction.BLOCK.value:
            # Block opponent (if close)
            distance = abs(game_state['player1_position'] - game_state['player2_position'])
            if distance <= 5:
                game_state[f'{opponent_prefix}_speed'] = max(3, game_state[f'{opponent_prefix}_speed'] - 1)
                message_parts.append("🚧 Blocked opponent! Their speed reduced")
            else:
                message_parts.append("❌ Too far to block")
        else:
            return GameResult(success=False, message=f"Invalid action: {action}")

        # Check win conditions
        if game_state[f'{player_prefix}_position'] >= game_state['track_length']:
            message_parts.append("🏁 FINISH LINE! Victory!")
            return await self._end_game(match, player_id, 'race_finish', message=' '.join(message_parts))

        # Check max turns
        if game_state['current_turn'] >= game_state['max_turns']:
            # Whoever is further ahead wins
            winner_id = match['player1_id'] if game_state['player1_position'] > game_state['player2_position'] else match['player2_id']
            return await self._end_game(match, winner_id, 'max_turns')

        # Add to history
        game_state['history'].append({
            'turn': game_state['current_turn'],
            'player': player_id,
            'action': action,
            'result': ' '.join(message_parts)
        })

        # Switch turns
        game_state['current_turn'] += 1
        game_state['current_player'] = opponent_id
        game_state['turn_timeout'] = (datetime.utcnow() + timedelta(seconds=self.turn_timeout_seconds)).isoformat()

        # Update match
        await self._update_game_state(match['id'], game_state)

        progress = f"Position: P1({game_state['player1_position']}) vs P2({game_state['player2_position']}) | Finish: {game_state['track_length']}"

        return GameResult(
            success=True,
            message=f"{' '.join(message_parts)} | {progress}",
            game_state=game_state
        )

    async def _end_game(
        self,
        match: Dict[str, Any],
        winner_id: int,
        end_reason: str,
        message: str = None
    ) -> GameResult:
        """
        End a game and distribute winnings.

        Args:
            match: Match data
            winner_id: ID of winning player
            end_reason: Reason for game ending
            message: Optional victory message

        Returns:
            GameResult with final state
        """
        try:
            loser_id = match['player1_id'] if winner_id == match['player2_id'] else match['player2_id']
            pot = match['currency_wagered'] * 2
            house_cut = int(pot * (self.house_cut_percent / 100))
            winnings = pot - house_cut

            # Get match data for platform info
            match_data = match['match_data'] if isinstance(match['match_data'], dict) else json.loads(match['match_data'])

            # Determine winner platform info
            if winner_id == match['player1_id']:
                winner_platform = match_data.get('challenger_platform')
                winner_user_id = match_data.get('challenger_user_id')
            else:
                winner_platform = match_data.get('defender_platform')
                winner_user_id = match_data.get('defender_user_id')

            # Award winnings
            await self.currency_service.add_currency(
                match['community_id'],
                winner_platform,
                winner_user_id,
                winnings,
                f"pvp_{match['game_type']}_win",
                f"Won {match['game_type']} match"
            )

            # Update match record
            update_query = """
                UPDATE pvp_match_history
                SET winner_id = $1,
                    match_data = match_data || $2::jsonb,
                    played_at = NOW()
                WHERE id = $3
            """

            end_data = json.dumps({
                'status': 'completed',
                'end_reason': end_reason,
                'house_cut': house_cut,
                'winnings': winnings,
                'completed_at': datetime.utcnow().isoformat()
            })

            await self.dal.execute(update_query, [winner_id, end_data, match['id']])

            # Update player stats
            await self._update_player_stats(
                match['community_id'],
                match['game_type'],
                winner_id,
                True,
                match['currency_wagered']
            )
            await self._update_player_stats(
                match['community_id'],
                match['game_type'],
                loser_id,
                False,
                match['currency_wagered']
            )

            game_name = match['game_type'].replace('_', ' ').title()
            final_message = message or f"🏆 {game_name} complete!"
            final_message += f" Winner receives {winnings} (pot: {pot}, house cut: {house_cut})"

            return GameResult(
                success=True,
                message=final_message,
                winner_id=winner_id,
                loser_id=loser_id,
                winnings=winnings,
                game_over=True
            )

        except Exception as e:
            logger.error(f"Error ending game: {e}")
            return GameResult(success=False, message="Failed to end game")

    async def _update_game_state(self, match_id: int, game_state: Dict[str, Any]) -> None:
        """Update the game state in the database."""
        try:
            update_query = """
                UPDATE pvp_match_history
                SET match_data = match_data || jsonb_build_object('game_state', $1::jsonb)
                WHERE id = $2
            """
            await self.dal.execute(update_query, [json.dumps(game_state), match_id])
        except Exception as e:
            logger.error(f"Error updating game state: {e}")

    async def _update_player_stats(
        self,
        community_id: int,
        game_type: str,
        player_id: int,
        is_win: bool,
        wager: int
    ) -> None:
        """Update player stats for a game type."""
        try:
            # For now, we'll use the pvp_match_history for stats
            # A dedicated stats table could be added later
            pass
        except Exception as e:
            logger.error(f"Error updating player stats: {e}")

    async def _refund_challenge(self, match: Dict[str, Any]) -> None:
        """Refund a challenge that expired or was cancelled."""
        try:
            match_data = match['match_data'] if isinstance(match['match_data'], dict) else json.loads(match['match_data'])

            # Refund challenger
            await self.currency_service.add_currency(
                match['community_id'],
                match_data.get('challenger_platform'),
                match_data.get('challenger_user_id'),
                match['currency_wagered'],
                f"pvp_{match['game_type']}_refund",
                "Challenge expired or cancelled"
            )

            # Update match
            update_query = """
                UPDATE pvp_match_history
                SET match_data = match_data || '{"status": "expired"}'::jsonb
                WHERE id = $1
            """
            await self.dal.execute(update_query, [match['id']])

        except Exception as e:
            logger.error(f"Error refunding challenge: {e}")

    async def get_leaderboard(
        self,
        community_id: int,
        game_type: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get leaderboard for a game type.

        Args:
            community_id: Community ID
            game_type: Type of game
            limit: Number of results

        Returns:
            List of player stats
        """
        try:
            query = """
                SELECT
                    winner_id as player_id,
                    COUNT(*) as wins,
                    SUM(currency_wagered) as total_wagered,
                    SUM(CASE WHEN match_data->>'winnings' IS NOT NULL
                        THEN (match_data->>'winnings')::integer
                        ELSE 0 END) as total_won
                FROM pvp_match_history
                WHERE community_id = $1
                  AND game_type = $2
                  AND winner_id IS NOT NULL
                GROUP BY winner_id
                ORDER BY wins DESC, total_won DESC
                LIMIT $3
            """

            rows = await self.dal.execute(query, [community_id, game_type, limit])
            return [dict(row) for row in (rows or [])]

        except Exception as e:
            logger.error(f"Error getting leaderboard: {e}")
            return []

    async def get_player_stats(
        self,
        community_id: int,
        game_type: str,
        player_id: int
    ) -> Dict[str, Any]:
        """
        Get stats for a specific player in a game type.

        Args:
            community_id: Community ID
            game_type: Type of game
            player_id: Player ID

        Returns:
            Player stats dict
        """
        try:
            query = """
                SELECT
                    COUNT(*) as total_games,
                    SUM(CASE WHEN winner_id = $3 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN winner_id != $3 THEN 1 ELSE 0 END) as losses,
                    SUM(currency_wagered) as total_wagered
                FROM pvp_match_history
                WHERE community_id = $1
                  AND game_type = $2
                  AND (player1_id = $3 OR player2_id = $3)
                  AND winner_id IS NOT NULL
            """

            rows = await self.dal.execute(query, [community_id, game_type, player_id])

            if rows and rows[0]['total_games'] > 0:
                stats = dict(rows[0])
                stats['win_rate'] = round((stats['wins'] / stats['total_games']) * 100, 1)
                return stats

            return {
                'total_games': 0,
                'wins': 0,
                'losses': 0,
                'total_wagered': 0,
                'win_rate': 0.0
            }

        except Exception as e:
            logger.error(f"Error getting player stats: {e}")
            return {}

    async def cancel_expired_challenges(self, community_id: Optional[int] = None) -> int:
        """
        Cancel expired challenges and refund wagers.

        Args:
            community_id: Optional community ID to filter by

        Returns:
            Number of challenges cancelled
        """
        try:
            if community_id:
                query = """
                    SELECT * FROM pvp_match_history
                    WHERE community_id = $1
                      AND winner_id IS NULL
                      AND played_at < NOW() - INTERVAL '10 minutes'
                """
                params = [community_id]
            else:
                query = """
                    SELECT * FROM pvp_match_history
                    WHERE winner_id IS NULL
                      AND played_at < NOW() - INTERVAL '10 minutes'
                """
                params = []

            expired = await self.dal.execute(query, params)
            count = 0

            for match in (expired or []):
                match_data = match['match_data'] if isinstance(match['match_data'], dict) else json.loads(match['match_data'])

                if match_data.get('status') == 'pending':
                    await self._refund_challenge(match)
                    count += 1

            return count

        except Exception as e:
            logger.error(f"Error cancelling expired challenges: {e}")
            return 0
