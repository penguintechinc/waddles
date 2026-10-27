"""
Reputation Service - Core CRUD and calculation logic for reputation scores.
Handles both per-community and per-tenant reputation tracking (the tenant
aggregate never spans multiple tenants -- security.md Tenant Isolation).
"""
import math
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from decimal import Decimal

from config import Config


def _round_half_away_from_zero(value: float) -> int:
    """Round to the nearest int, ties rounding away from zero.

    Python's builtin `round()` uses banker's rounding (ties to even),
    which silently drops exact `.5` deltas at an even score --
    `round(600.5) == 600`. Reputation weights are admin-configured values
    (see `community_reputation_config`) where a `.5`-magnitude weight is
    expected to move the score on every event it fires; ties-to-even
    would make that depend on whether the current score happens to be
    even, which is not a rule anyone configuring a weight could predict.
    """
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


@dataclass
class ReputationInfo:
    """Complete reputation information for a user."""
    score: int
    tier: str
    tier_label: str
    total_events: int = 0
    last_event_at: Optional[str] = None


@dataclass
class AdjustmentResult:
    """Result of a reputation adjustment."""
    success: bool
    score_before: int
    score_after: int
    score_change: float
    event_id: Optional[int] = None
    error: Optional[str] = None


@dataclass
class ReputationEvent:
    """A single reputation event from history."""
    id: int
    event_type: str
    score_change: float
    score_before: int
    score_after: int
    reason: Optional[str]
    created_at: str
    metadata: Dict[str, Any]


class ReputationService:
    """
    Core reputation service for managing user scores.

    Handles:
    - Per-community reputation (stored in community_members table)
    - Per-tenant reputation, aggregated across a tenant's communities only
      -- never across tenants (stored in reputation_tenant table)
    - Score adjustments with audit logging
    - FICO-style tier calculation
    """

    def __init__(self, dal, weight_manager, logger):
        self.dal = dal
        self.weight_manager = weight_manager
        self.logger = logger

    def _get_tier(self, score: int) -> tuple:
        """Get tier name and label for a score."""
        for tier_name, tier_info in Config.REPUTATION_TIERS.items():
            if tier_info['min'] <= score <= tier_info['max']:
                return tier_name, tier_info['label']
        return 'poor', 'Poor'

    def _clamp_score(self, score: float, min_score: int, max_score: int) -> int:
        """Clamp `score` to `[min_score, max_score]`, rounding once via round-half-away-from-zero.

        `community_members.reputation` / `reputation_tenant.score` are
        INTEGER columns (`config/postgres/migrations/080_add_reputation_
        tables.sql` / `097_reputation_tenant_scope.sql`) with no column to
        carry a fractional remainder across separate events -- adding one is
        a migration, out of scope here (gh-310). Every caller (`adjust()`,
        `_update_tenant_reputation()`, `set_reputation()`) MUST pass `stored
        + delta` computed once and
        round/clamp exactly once here, never re-round an already-rounded
        stored value a second time.

        Documented, intentional consequence (no carried remainder): a
        per-event weight whose magnitude is < 0.5 truncates to a zero
        delta on that call, every call, forever -- it never accrues across
        repeated events. A weight whose magnitude is >= 0.5 moves the
        score by at least +/-1 on every single event (ties always round
        away from zero, unlike Python's `round()`). Default weights
        (`WeightManager.CommunityWeights`) are whole numbers for exactly
        this reason; only a premium community's custom
        `community_reputation_config` override can hit the truncation
        case, and that is a deliberate admin choice, not a bug.
        """
        return max(min_score, min(max_score, _round_half_away_from_zero(score)))

    async def get_reputation(
        self,
        community_id: int,
        user_id: int,
        platform: Optional[str] = None,
        platform_user_id: Optional[str] = None
    ) -> Optional[ReputationInfo]:
        """Get reputation for a user in a specific community.

        Can lookup by hub_user_id OR platform/platform_user_id. Matches
        against `community_members.user_id` -- that table has no
        `hub_user_id` column, only `user_id VARCHAR` storing
        `str(hub_user_id)` once a member is linked to a hub account (see
        `flask_core.community_access`'s identical
        `dal.community_members.user_id == str(user_id)` convention, and
        `core/svc_process/builtin_handlers/community_reputation_process.py`'s
        matching read path). The platform-identity lookup queries
        `community_members.platform`/`platform_user_id` directly -- those
        columns already live on the row itself, no join through
        `hub_users`/`user_identities` (the latter does not exist; the real
        table is `hub_user_identities`) is needed.
        Returns None if user not found in community.
        """
        try:
            if user_id:
                # Lookup by hub_user_id (stored as community_members.user_id)
                result = self.dal.executesql(
                    """SELECT cm.reputation, cm.updated_at, cm.user_id
                       FROM community_members cm
                       WHERE cm.community_id = %s AND cm.user_id = %s""",
                    [community_id, str(user_id)]
                )
            elif platform and platform_user_id:
                # Lookup by platform identity -- native columns on community_members
                result = self.dal.executesql(
                    """SELECT cm.reputation, cm.updated_at, cm.user_id
                       FROM community_members cm
                       WHERE cm.community_id = %s
                         AND cm.platform = %s
                         AND cm.platform_user_id = %s""",
                    [community_id, platform, platform_user_id]
                )
            else:
                return None

            if not result or len(result) == 0:
                return None

            row = result[0]
            score = row[0] if row[0] is not None else Config.REPUTATION_DEFAULT
            tier_name, tier_label = self._get_tier(score)

            # reputation_events is keyed by the INTEGER hub_user_id -- only
            # queryable once the member row is resolved to its hub link.
            event_count = 0
            last_event = None
            resolved_user_id = row[2]
            if resolved_user_id:
                stats = self.dal.executesql(
                    """SELECT COUNT(*), MAX(created_at) FROM reputation_events
                       WHERE community_id = %s AND hub_user_id = %s""",
                    [community_id, int(resolved_user_id)]
                )
                if stats:
                    event_count = stats[0][0] or 0
                    last_event = stats[0][1]

            return ReputationInfo(
                score=score,
                tier=tier_name,
                tier_label=tier_label,
                total_events=event_count,
                last_event_at=str(last_event) if last_event else None
            )

        except Exception as e:
            self.logger.error(f"Failed to get reputation: {e}")
            return None

    async def get_tenant_reputation(self, tenant_id: int, user_id: int) -> Optional[ReputationInfo]:
        """Get tenant-wide (cross-community, single-tenant) reputation for a user.

        `tenant_id` MUST come from a validated source (the caller's own
        `TenantContext.tenant_id`, published by `install_community_scoped_auth`
        from the bearer JWT's `tenant` claim) -- never client-supplied.
        Reputation is a hard tenant boundary (security.md Tenant Isolation):
        this never aggregates or returns another tenant's score.
        """
        try:
            result = self.dal.executesql(
                """SELECT score, total_events, last_event_at
                   FROM reputation_tenant
                   WHERE tenant_id = %s AND hub_user_id = %s""",
                [tenant_id, user_id]
            )

            if not result or len(result) == 0:
                # Return default if no tenant record exists yet
                tier_name, tier_label = self._get_tier(Config.REPUTATION_DEFAULT)
                return ReputationInfo(
                    score=Config.REPUTATION_DEFAULT,
                    tier=tier_name,
                    tier_label=tier_label,
                    total_events=0,
                    last_event_at=None
                )

            row = result[0]
            score = row[0]
            tier_name, tier_label = self._get_tier(score)

            return ReputationInfo(
                score=score,
                tier=tier_name,
                tier_label=tier_label,
                total_events=row[1] or 0,
                last_event_at=str(row[2]) if row[2] else None
            )

        except Exception as e:
            self.logger.error(f"Failed to get tenant reputation: {e}")
            return None

    async def adjust(
        self,
        community_id: int,
        user_id: int,
        event_type: str,
        platform: str,
        platform_user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
        amount_multiplier: float = 1.0
    ) -> AdjustmentResult:
        """Adjust reputation based on an event.

        Uses weight configuration to determine score change. Updates both
        community reputation (`community_members.reputation`) and, when the
        member is linked to a hub account, tenant-scoped reputation
        (`reputation_tenant.score`, resolved from `community_id` ->
        `communities.tenant_id`). Creates a `reputation_events` audit log
        entry.

        The community member row is matched by `(community_id, platform,
        platform_user_id)` -- the table's actual `UNIQUE` constraint (see
        `config/postgres/migrations/000_create_base_schema.sql`).
        `community_members` has no `hub_user_id` column; `user_id VARCHAR`
        stores `str(hub_user_id)` once the member is linked (see
        `flask_core.community_access`'s identical convention), and is left
        `NULL` for a platform-only member with no hub account yet -- so
        matching on the platform identity (always present, unlike the hub
        link) is the only way to find the correct row regardless of link
        state, instead of the previous `OR hub_user_id IS NULL` fallback
        which could grab any other unlinked member's row in the community.

        Args:
            community_id: Community where event occurred
            user_id: Hub user ID (can be None if not linked)
            event_type: Type of event (chatMessage, follow, ban, etc.)
            platform: Platform where event occurred
            platform_user_id: User's ID on the platform
            metadata: Additional event data (donation amount, etc.)
            reason: Human-readable reason for change
            amount_multiplier: Multiplier for scaled events (donations, cheers)
        """
        metadata = metadata or {}

        try:
            # Get weights for this community
            weights = await self.weight_manager.get_weights(community_id)
            base_weight = weights.get_weight(event_type)

            if base_weight == 0.0:
                # Event type not configured for reputation impact
                return AdjustmentResult(
                    success=True,
                    score_before=0,
                    score_after=0,
                    score_change=0.0,
                    error="Event type has no reputation weight"
                )

            # Calculate actual change with multiplier
            score_change = float(base_weight) * amount_multiplier

            # Get or create community membership -- matched by platform
            # identity (community_members has no hub_user_id column).
            member_result = self.dal.executesql(
                """SELECT cm.id, cm.reputation, cm.user_id
                   FROM community_members cm
                   WHERE cm.community_id = %s
                     AND cm.platform = %s
                     AND cm.platform_user_id = %s
                   LIMIT 1""",
                [community_id, platform, platform_user_id]
            )

            if not member_result or len(member_result) == 0:
                # Create new member record
                current_score = weights.starting_score
                self.dal.executesql(
                    """INSERT INTO community_members
                       (community_id, user_id, platform, platform_user_id,
                        reputation, role)
                       VALUES (%s, %s, %s, %s, %s, 'member')""",
                    [community_id, str(user_id) if user_id else None,
                     platform, platform_user_id, current_score]
                )
                self.dal.commit()
            else:
                row = member_result[0]
                current_score = row[1] if row[1] is not None else weights.starting_score
                stored_user_id = row[2]
                # Prefer the caller-supplied hub link; fall back to whatever
                # is already stored on the member row.
                user_id = user_id or (int(stored_user_id) if stored_user_id else None)

            score_before = current_score
            new_score = self._clamp_score(
                current_score + score_change,
                weights.min_score,
                weights.max_score
            )
            score_after = new_score

            self.logger.debug(
                "Community reputation weight applied",
                # `reputation_event_type`, not `event_type` -- see the
                # `.audit()` call below; `.debug()` reserves the same
                # positional `event_type` name internally.
                reputation_event_type=event_type,
                weight=base_weight,
                delta=score_change,
                community_id=community_id,
                score_before=score_before,
                score_after=score_after,
            )

            # Update community reputation
            self.dal.executesql(
                """UPDATE community_members
                   SET reputation = %s, updated_at = NOW()
                   WHERE community_id = %s AND platform = %s AND platform_user_id = %s""",
                [score_after, community_id, platform, platform_user_id]
            )

            # Create audit log entry
            import json
            self.dal.executesql(
                """INSERT INTO reputation_events
                   (community_id, hub_user_id, platform, platform_user_id,
                    event_type, score_change, score_before, score_after,
                    reason, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                [community_id, user_id, platform, platform_user_id,
                 event_type, score_change, score_before, score_after,
                 reason, json.dumps(metadata)]
            )

            # Update tenant-scoped reputation if user is linked
            if user_id:
                await self._update_tenant_reputation(
                    community_id, user_id, score_change, event_type, base_weight
                )

            self.dal.commit()

            self.logger.audit(
                "Reputation adjusted",
                user=str(user_id) if user_id else platform_user_id,
                community=str(community_id),
                result="success",
                # `reputation_event_type`, not `event_type` -- AAALogger.
                # audit()'s own `_build_extra(event_type: str, **kwargs)`
                # reserves the bare `event_type` kwarg name for its own
                # "AUDIT"/"AUTH"/etc. category positional arg; a caller
                # extra kwarg of the same name collides ("got multiple
                # values for argument 'event_type'") -- caught here by
                # `tests/test_reputation_service_audit.py`.
                community_id=community_id,
                user_id=user_id,
                reputation_event_type=event_type,
                score_before=score_before,
                score_after=score_after,
                change=score_change
            )

            return AdjustmentResult(
                success=True,
                score_before=score_before,
                score_after=score_after,
                score_change=score_change
            )

        except Exception as e:
            self.logger.error(f"Failed to adjust reputation: {e}")
            return AdjustmentResult(
                success=False,
                score_before=0,
                score_after=0,
                score_change=0.0,
                error=str(e)
            )

    def _resolve_tenant_id(self, community_id: int) -> int:
        """Resolve `community_id` -> its owning `communities.tenant_id`.

        The ONLY tenant source for internal write paths (`adjust()`'s
        callers never supply a tenant_id directly -- only a trusted,
        already-validated `community_id`, the same boundary every other
        community-scoped table in this schema uses, see migration 097's
        header comment). `communities.tenant_id` is NOT NULL (migration
        058), so a missing/NULL result here means `community_id` itself
        doesn't resolve to a real row -- raised loudly, never defaulted to
        a guessed tenant (security.md: tenant is a hard isolation boundary,
        never silently assumed).
        """
        row = self.dal.executesql(
            "SELECT tenant_id FROM communities WHERE id = %s",
            [community_id]
        )
        if not row or row[0][0] is None:
            raise ValueError(f"community {community_id} has no resolvable tenant_id")
        return int(row[0][0])

    async def _update_tenant_reputation(
        self,
        community_id: int,
        user_id: int,
        score_change: float,
        event_type: str = '',
        weight: float = 0.0
    ) -> None:
        """Update the tenant-scoped reputation aggregate for a linked user.

        Tenant is resolved from `community_id` via `_resolve_tenant_id()`
        (never client-supplied) and the update is confined to
        `(tenant_id, hub_user_id)` -- this NEVER touches, aggregates, or
        leaks another tenant's row (security.md Tenant Isolation).

        Applies `score_change` -- the same raw delta already computed once
        by the community-scope caller, never re-derived from
        `community_members`' rounded integer -- directly against the
        stored tenant score via `_clamp_score`, the identical
        round-half-away-from-zero rule the community scope uses (see its
        docstring). Computing the final integer once in Python before
        writing it keeps both scopes on one rounding implementation.

        Failure here (including an unresolvable tenant) is logged and
        swallowed, same as the prior global-reputation behavior: a failure
        on this secondary aggregate must not roll back the already-applied,
        already-committed community-level adjustment.
        """
        try:
            tenant_id = self._resolve_tenant_id(community_id)

            existing = self.dal.executesql(
                "SELECT score FROM reputation_tenant WHERE tenant_id = %s AND hub_user_id = %s",
                [tenant_id, user_id]
            )
            if existing:
                score_before = existing[0][0]
                score_after = self._clamp_score(
                    score_before + score_change,
                    Config.REPUTATION_MIN,
                    Config.REPUTATION_MAX
                )
                self.dal.executesql(
                    """UPDATE reputation_tenant
                       SET score = %s, total_events = total_events + 1,
                           last_event_at = NOW(), updated_at = NOW()
                       WHERE tenant_id = %s AND hub_user_id = %s""",
                    [score_after, tenant_id, user_id]
                )
            else:
                score_before = Config.REPUTATION_DEFAULT
                score_after = self._clamp_score(
                    score_before + score_change,
                    Config.REPUTATION_MIN,
                    Config.REPUTATION_MAX
                )
                # Select-then-insert, not INSERT ... ON CONFLICT: matches the
                # same non-atomic lookup-then-write pattern adjust() already
                # uses for community_members above -- a pre-existing,
                # out-of-scope race (two concurrent first-events for the same
                # never-before-seen (tenant_id, hub_user_id) pair) shared by
                # both, not introduced here.
                self.dal.executesql(
                    """INSERT INTO reputation_tenant (tenant_id, hub_user_id, score, total_events)
                       VALUES (%s, %s, %s, 1)""",
                    [tenant_id, user_id, score_after]
                )

            self.logger.debug(
                "Tenant reputation weight applied",
                reputation_event_type=event_type,
                weight=weight,
                delta=score_change,
                tenant_id=tenant_id,
                hub_user_id=user_id,
                score_before=score_before,
                score_after=score_after,
            )
        except Exception as e:
            self.logger.warning(f"Failed to update tenant reputation: {e}")

    async def set_reputation(
        self,
        community_id: int,
        user_id: int,
        score: int,
        reason: str,
        admin_id: int
    ) -> AdjustmentResult:
        """Manually set reputation score (admin action).

        Matches the target by `community_members.user_id` (`str(user_id)`)
        -- that table has no `hub_user_id` column, see `adjust()`'s
        docstring for the full convention. Creates audit log with admin
        attribution.
        """
        try:
            # Get weights for bounds checking
            weights = await self.weight_manager.get_weights(community_id)

            # Clamp to valid range
            new_score = self._clamp_score(score, weights.min_score, weights.max_score)

            # Get current score
            result = self.dal.executesql(
                """SELECT reputation FROM community_members
                   WHERE community_id = %s AND user_id = %s""",
                [community_id, str(user_id)]
            )

            if not result or len(result) == 0:
                return AdjustmentResult(
                    success=False,
                    score_before=0,
                    score_after=0,
                    score_change=0.0,
                    error="User not found in community"
                )

            score_before = result[0][0] or weights.starting_score
            score_change = new_score - score_before

            # Update score
            self.dal.executesql(
                """UPDATE community_members
                   SET reputation = %s, updated_at = NOW()
                   WHERE community_id = %s AND user_id = %s""",
                [new_score, community_id, str(user_id)]
            )

            # Audit log with admin attribution
            import json
            self.dal.executesql(
                """INSERT INTO reputation_events
                   (community_id, hub_user_id, platform, platform_user_id,
                    event_type, score_change, score_before, score_after,
                    reason, metadata)
                   VALUES (%s, %s, 'admin', %s, 'manual_set', %s, %s, %s, %s, %s)""",
                [community_id, user_id, str(admin_id),
                 score_change, score_before, new_score,
                 reason, json.dumps({'admin_id': admin_id})]
            )

            self.dal.commit()

            self.logger.audit(
                "Reputation manually set",
                user=str(user_id),
                community=str(community_id),
                result="success",
                community_id=community_id,
                user_id=user_id,
                admin_id=admin_id,
                score_before=score_before,
                score_after=new_score,
                reason=reason
            )

            return AdjustmentResult(
                success=True,
                score_before=score_before,
                score_after=new_score,
                score_change=score_change
            )

        except Exception as e:
            self.logger.error(f"Failed to set reputation: {e}")
            return AdjustmentResult(
                success=False,
                score_before=0,
                score_after=0,
                score_change=0.0,
                error=str(e)
            )

    async def get_history(
        self,
        community_id: int,
        user_id: int,
        limit: int = 50,
        offset: int = 0
    ) -> List[ReputationEvent]:
        """Get reputation event history for a user in a community."""
        try:
            result = self.dal.executesql(
                """SELECT id, event_type, score_change, score_before,
                          score_after, reason, created_at, metadata
                   FROM reputation_events
                   WHERE community_id = %s AND hub_user_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s OFFSET %s""",
                [community_id, user_id, limit, offset]
            )

            events = []
            for row in result:
                events.append(ReputationEvent(
                    id=row[0],
                    event_type=row[1],
                    score_change=float(row[2]),
                    score_before=row[3],
                    score_after=row[4],
                    reason=row[5],
                    created_at=str(row[6]),
                    metadata=row[7] if row[7] else {}
                ))

            return events

        except Exception as e:
            self.logger.error(f"Failed to get reputation history: {e}")
            return []

    async def get_leaderboard(
        self,
        community_id: int,
        limit: int = 25,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get reputation leaderboard for a community.

        Only shows members linked to a hub account (`community_members
        .user_id IS NOT NULL`) -- `community_members` has no `hub_user_id`
        column; `user_id VARCHAR` stores `str(hub_user_id)`, cast to
        `INTEGER` to join `hub_users`.
        """
        try:
            result = self.dal.executesql(
                """SELECT CAST(cm.user_id AS INTEGER) as hub_user_id,
                          hu.username, hu.avatar_url, cm.reputation,
                          RANK() OVER (ORDER BY cm.reputation DESC) as rank
                   FROM community_members cm
                   JOIN hub_users hu ON hu.id = CAST(cm.user_id AS INTEGER)
                   WHERE cm.community_id = %s AND cm.is_active = true
                     AND cm.user_id IS NOT NULL
                   ORDER BY cm.reputation DESC
                   LIMIT %s OFFSET %s""",
                [community_id, limit, offset]
            )

            leaderboard = []
            for row in result:
                score = row[3]
                tier_name, tier_label = self._get_tier(score)
                leaderboard.append({
                    'user_id': row[0],
                    'username': row[1],
                    'avatar_url': row[2],
                    'score': score,
                    'tier': tier_name,
                    'tier_label': tier_label,
                    'rank': row[4]
                })

            return leaderboard

        except Exception as e:
            self.logger.error(f"Failed to get leaderboard: {e}")
            return []

    async def get_tenant_leaderboard(
        self,
        tenant_id: int,
        limit: int = 25,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get the tenant-wide reputation leaderboard, scoped to `tenant_id`.

        `tenant_id` MUST come from a validated source (see
        `get_tenant_reputation`'s docstring) -- this never returns rows
        from another tenant.
        """
        try:
            result = self.dal.executesql(
                """SELECT rt.hub_user_id, hu.username, hu.avatar_url,
                          rt.score, rt.total_events,
                          RANK() OVER (ORDER BY rt.score DESC) as rank
                   FROM reputation_tenant rt
                   JOIN hub_users hu ON hu.id = rt.hub_user_id
                   WHERE rt.tenant_id = %s
                   ORDER BY rt.score DESC
                   LIMIT %s OFFSET %s""",
                [tenant_id, limit, offset]
            )

            leaderboard = []
            for row in result:
                score = row[3]
                tier_name, tier_label = self._get_tier(score)
                leaderboard.append({
                    'user_id': row[0],
                    'username': row[1],
                    'avatar_url': row[2],
                    'score': score,
                    'total_events': row[4],
                    'tier': tier_name,
                    'tier_label': tier_label,
                    'rank': row[5]
                })

            return leaderboard

        except Exception as e:
            self.logger.error(f"Failed to get tenant leaderboard: {e}")
            return []

    async def initialize_member(
        self,
        community_id: int,
        user_id: int
    ) -> bool:
        """Initialize reputation for a new community member.

        Matches by `community_members.user_id` (`str(user_id)`) -- see
        `adjust()`'s docstring for the full column convention.
        """
        try:
            weights = await self.weight_manager.get_weights(community_id)

            self.dal.executesql(
                """UPDATE community_members
                   SET reputation = %s
                   WHERE community_id = %s AND user_id = %s
                   AND reputation IS NULL""",
                [weights.starting_score, community_id, str(user_id)]
            )
            self.dal.commit()
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize member reputation: {e}")
            return False
