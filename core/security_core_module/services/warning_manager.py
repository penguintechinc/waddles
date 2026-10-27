"""
Warning Manager Service - Manages user warnings and auto-escalation
"""
from typing import Optional, Dict, List
from datetime import datetime, timedelta


class WarningManager:
    """Manages user warnings with expiration and auto-escalation."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def issue_automated_warning(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        warning_type: str,
        warning_reason: str,
        trigger_message: Optional[str] = None
    ) -> Dict:
        """Issue automated warning."""
        try:
            # Get hub_user_id if exists
            hub_user_id = await self._get_hub_user_id(platform, platform_user_id)

            # Get config
            config = await self._get_config(community_id)

            # Calculate expiration
            expires_at = datetime.utcnow() + timedelta(days=config['warning_decay_days'])

            # Insert warning
            self.dal.executesql(
                """INSERT INTO security_warnings
                   (community_id, hub_user_id, platform, platform_user_id,
                    warning_type, warning_reason, auto_generated, trigger_message, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s)""",
                [community_id, hub_user_id, platform, platform_user_id,
                 warning_type, warning_reason, trigger_message, expires_at]
            )
            self.dal.commit()

            # Check if escalation needed
            await self._check_escalation(community_id, platform, platform_user_id, config)

            self.logger.audit(
                action="issue_warning",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                detail="Automated warning issued",
                platform=platform,
                platform_user_id=platform_user_id,
                warning_type=warning_type
            )

            return {
                'warning_issued': True,
                'warning_type': warning_type,
                'expires_at': expires_at.isoformat() + 'Z'
            }

        except Exception as e:
            self.logger.error(f"Failed to issue automated warning: {e}")
            raise

    async def issue_manual_warning(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        warning_reason: str,
        issued_by: Optional[int] = None
    ) -> Dict:
        """Issue manual warning from moderator."""
        try:
            hub_user_id = await self._get_hub_user_id(platform, platform_user_id)
            config = await self._get_config(community_id)

            expires_at = datetime.utcnow() + timedelta(days=config['warning_decay_days'])

            self.dal.executesql(
                """INSERT INTO security_warnings
                   (community_id, hub_user_id, platform, platform_user_id,
                    warning_type, warning_reason, auto_generated, issued_by, expires_at)
                   VALUES (%s, %s, %s, %s, 'manual', %s, false, %s, %s)""",
                [community_id, hub_user_id, platform, platform_user_id,
                 warning_reason, issued_by, expires_at]
            )
            self.dal.commit()

            self.logger.audit(
                action="issue_manual_warning",
                user=str(issued_by or "system"),
                community=str(community_id),
                result="SUCCESS",
                detail="Manual warning issued",
                platform=platform,
                platform_user_id=platform_user_id
            )

            return {
                'warning_issued': True,
                'expires_at': expires_at.isoformat() + 'Z'
            }

        except Exception as e:
            self.logger.error(f"Failed to issue manual warning: {e}")
            raise

    async def revoke_warning(
        self,
        warning_id: int,
        revoked_by: Optional[int] = None,
        revoke_reason: Optional[str] = None
    ) -> Dict:
        """Revoke a warning."""
        try:
            self.dal.executesql(
                """UPDATE security_warnings
                   SET is_active = false, revoked_at = NOW(),
                       revoked_by = %s, revoke_reason = %s
                   WHERE id = %s""",
                [revoked_by, revoke_reason, warning_id]
            )
            self.dal.commit()

            self.logger.audit(
                action="revoke_warning",
                user=str(revoked_by or "system"),
                community="",
                result="SUCCESS",
                detail="Warning revoked",
                warning_id=warning_id
            )

            return {'revoked': True}

        except Exception as e:
            self.logger.error(f"Failed to revoke warning: {e}")
            raise

    async def get_warnings(
        self,
        community_id: int,
        status: str = 'active',
        page: int = 1,
        limit: int = 25
    ) -> Dict:
        """Get warnings for community."""
        try:
            offset = (page - 1) * limit

            # Build where clause
            where_clause = "WHERE community_id = %s"
            params = [community_id]

            if status == 'active':
                where_clause += " AND is_active = true AND (expires_at IS NULL OR expires_at > NOW())"
            elif status == 'expired':
                where_clause += " AND (is_active = false OR expires_at <= NOW())"

            result = self.dal.executesql(
                f"""SELECT id, platform, platform_user_id, platform_username,
                           warning_type, warning_reason, is_active, expires_at, created_at
                    FROM security_warnings
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s""",
                params + [limit, offset]
            )

            warnings = []
            if result:
                for row in result:
                    warnings.append({
                        'id': row[0],
                        'platform': row[1],
                        'platform_user_id': row[2],
                        'platform_username': row[3],
                        'warning_type': row[4],
                        'warning_reason': row[5],
                        'is_active': row[6],
                        'expires_at': row[7].isoformat() + 'Z' if row[7] else None,
                        'created_at': row[8].isoformat() + 'Z' if row[8] else None
                    })

            return {
                'warnings': warnings,
                'page': page,
                'limit': limit,
                'total': len(warnings)
            }

        except Exception as e:
            self.logger.error(f"Failed to get warnings: {e}")
            raise

    async def _get_hub_user_id(self, platform: str, platform_user_id: str) -> Optional[int]:
        """Get hub_user_id from platform identity."""
        try:
            result = self.dal.executesql(
                """SELECT hub_user_id FROM hub_user_identities
                   WHERE platform = %s AND platform_user_id = %s""",
                [platform, platform_user_id]
            )
            return result[0][0] if result else None
        except Exception:
            return None

    async def _get_config(self, community_id: int) -> Dict:
        """Get warning config."""
        result = self.dal.executesql(
            """SELECT warning_enabled, warning_threshold_timeout,
                      warning_threshold_ban, warning_decay_days
               FROM security_config
               WHERE community_id = %s""",
            [community_id]
        )

        if not result:
            from config import Config
            return {
                'warning_enabled': True,
                'warning_threshold_timeout': Config.DEFAULT_WARNING_THRESHOLD_TIMEOUT,
                'warning_threshold_ban': Config.DEFAULT_WARNING_THRESHOLD_BAN,
                'warning_decay_days': Config.DEFAULT_WARNING_DECAY_DAYS
            }

        row = result[0]
        return {
            'warning_enabled': row[0],
            'warning_threshold_timeout': row[1],
            'warning_threshold_ban': row[2],
            'warning_decay_days': row[3]
        }

    async def _check_escalation(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        config: Dict
    ):
        """Check if user should be auto-escalated."""
        try:
            # Count active warnings
            result = self.dal.executesql(
                """SELECT COUNT(*) FROM security_warnings
                   WHERE community_id = %s AND platform = %s AND platform_user_id = %s
                   AND is_active = true AND (expires_at IS NULL OR expires_at > NOW())""",
                [community_id, platform, platform_user_id]
            )

            warning_count = result[0][0] if result else 0

            # Check thresholds
            if warning_count >= config['warning_threshold_ban']:
                self.logger.audit(
                    action="auto_escalation_ban",
                    user="system",
                    community=str(community_id),
                    result="ESCALATED",
                    detail="Auto-escalation: ban threshold reached",
                    platform=platform,
                    platform_user_id=platform_user_id,
                    warning_count=warning_count
                )
                # Trigger ban (would call moderation action)

            elif warning_count >= config['warning_threshold_timeout']:
                self.logger.audit(
                    action="auto_escalation_timeout",
                    user="system",
                    community=str(community_id),
                    result="ESCALATED",
                    detail="Auto-escalation: timeout threshold reached",
                    platform=platform,
                    platform_user_id=platform_user_id,
                    warning_count=warning_count
                )
                # Trigger timeout (would call moderation action)

        except Exception as e:
            self.logger.error(f"Failed to check escalation: {e}")
