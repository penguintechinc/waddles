"""
Overlay Service for Browser Source Module
Validates overlay keys and manages unified browser source access
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Grace period for rotated keys (5 minutes)
KEY_GRACE_PERIOD_MINUTES = 5


class OverlayService:
    """Service for overlay token validation and access logging"""

    def __init__(self, dal):
        """
        Initialize overlay service.

        Args:
            dal: Database access layer instance
        """
        self.dal = dal

    async def validate_overlay_key(self, overlay_key: str) -> Optional[Dict[str, Any]]:
        """
        Validate an overlay key and return community info if valid.
        Checks current key first, then previous key if within grace period.

        Args:
            overlay_key: 64-character hex key to validate

        Returns:
            Dict with community_id and settings if valid, None otherwise
        """
        if not overlay_key or len(overlay_key) != 64:
            logger.warning(f"Invalid overlay key format: {overlay_key[:8] if overlay_key else 'None'}...")
            return None

        try:
            # Check current key
            query = """
                SELECT community_id, is_active, theme_config, enabled_sources,
                       overlay_key, previous_key, rotated_at
                FROM community_overlay_tokens
                WHERE overlay_key = $1 AND is_active = TRUE
            """
            rows = await self.dal.execute(query, [overlay_key])

            if rows and len(rows) > 0:
                row = rows[0]
                # Update access stats
                await self._update_access_stats(row['community_id'])
                return {
                    'community_id': row['community_id'],
                    'theme_config': row['theme_config'] or {},
                    'enabled_sources': row['enabled_sources'] or ['ticker', 'media', 'general'],
                    'is_current_key': True
                }

            # Check previous key (grace period)
            grace_query = """
                SELECT community_id, is_active, theme_config, enabled_sources,
                       overlay_key, previous_key, rotated_at
                FROM community_overlay_tokens
                WHERE previous_key = $1
                  AND is_active = TRUE
                  AND rotated_at > NOW() - INTERVAL '%s minutes'
            """ % KEY_GRACE_PERIOD_MINUTES

            rows = await self.dal.execute(grace_query, [overlay_key])

            if rows and len(rows) > 0:
                row = rows[0]
                logger.info(f"Overlay key validated via grace period for community {row['community_id']}")
                await self._update_access_stats(row['community_id'])
                return {
                    'community_id': row['community_id'],
                    'theme_config': row['theme_config'] or {},
                    'enabled_sources': row['enabled_sources'] or ['ticker', 'media', 'general'],
                    'is_current_key': False,
                    'grace_period': True
                }

            logger.warning(f"Invalid overlay key: {overlay_key[:8]}...")
            return None

        except Exception as e:
            logger.error(f"Error validating overlay key: {e}", exc_info=True)
            return None

    async def _update_access_stats(self, community_id: int) -> None:
        """Update access count and last accessed timestamp"""
        try:
            query = """
                UPDATE community_overlay_tokens
                SET access_count = access_count + 1,
                    last_accessed = NOW()
                WHERE community_id = $1
            """
            await self.dal.execute(query, [community_id])
        except Exception as e:
            logger.error(f"Error updating access stats: {e}")

    async def log_access(
        self,
        community_id: int,
        overlay_key: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        source_types: Optional[list] = None,
        was_valid: bool = True
    ) -> None:
        """
        Log an overlay access for analytics.

        Args:
            community_id: Community ID
            overlay_key: Key used for access
            ip_address: Client IP address
            user_agent: Client user agent
            source_types: List of source types requested
            was_valid: Whether the access was valid
        """
        try:
            query = """
                INSERT INTO overlay_access_log
                    (community_id, overlay_key, ip_address, user_agent,
                     source_types_requested, was_valid)
                VALUES ($1, $2, $3, $4, $5, $6)
            """
            await self.dal.execute(query, [
                community_id,
                overlay_key,
                ip_address,
                user_agent,
                source_types or [],
                was_valid
            ])
        except Exception as e:
            logger.error(f"Error logging overlay access: {e}")

    async def get_browser_source_tokens(self, community_id: int) -> Dict[str, str]:
        """
        Get all browser source tokens for a community.

        Args:
            community_id: Community ID

        Returns:
            Dict mapping source type to token
        """
        try:
            query = """
                SELECT source_type, token
                FROM browser_source_tokens
                WHERE community_id = $1 AND is_active = TRUE
            """
            rows = await self.dal.execute(query, [community_id])

            return {row['source_type']: row['token'] for row in (rows or [])}
        except Exception as e:
            logger.error(f"Error getting browser source tokens: {e}")
            return {}

    async def get_overlay_html(
        self,
        community_id: int,
        theme_config: Dict[str, Any],
        enabled_sources: list
    ) -> str:
        """
        Generate unified overlay HTML with iframes for each source type.

        Args:
            community_id: Community ID
            theme_config: Theme configuration
            enabled_sources: List of enabled source types

        Returns:
            HTML string for the overlay
        """
        # Get browser source tokens
        tokens = await self.get_browser_source_tokens(community_id)

        # Base URL for browser sources
        base_url = "/api/v1/browser-source"
        background = theme_config.get('background', 'transparent')

        # Build iframe elements for each enabled source
        iframes = []
        for source_type in enabled_sources:
            if source_type in tokens:
                token = tokens[source_type]
                iframes.append(f'''
                    <iframe
                        src="{base_url}/{source_type}/{token}"
                        class="source-frame source-{source_type}"
                        frameborder="0"
                        allowtransparency="true"
                    ></iframe>
                ''')

        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Waddles Overlay</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100%;
            height: 100%;
            background: {background};
            overflow: hidden;
        }}
        .overlay-container {{
            position: relative;
            width: 100%;
            height: 100%;
        }}
        .source-frame {{
            position: absolute;
            width: 100%;
            height: 100%;
            border: none;
            background: transparent;
        }}
        /* Source-specific positioning can be customized */
        .source-ticker {{
            bottom: 0;
            left: 0;
            height: 60px;
        }}
        .source-media {{
            top: 10px;
            right: 10px;
            width: 320px;
            height: 180px;
        }}
        .source-general {{
            top: 0;
            left: 0;
        }}
    </style>
</head>
<body>
    <div class="overlay-container">
        {''.join(iframes)}
    </div>
    <script>
        // Auto-reconnect on WebSocket disconnect
        window.addEventListener('error', function(e) {{
            console.log('Overlay error, reloading in 5s...', e);
            setTimeout(() => location.reload(), 5000);
        }});
    </script>
</body>
</html>'''
