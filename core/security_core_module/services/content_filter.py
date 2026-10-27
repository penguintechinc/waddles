"""
Content Filter Service - Filters blocked words and regex patterns
"""
import re
from typing import Optional, Dict, Tuple, List

from data.profanity_wordlist import DEFAULT_PROFANITY_WORDS


class ContentFilter:
    """Content filtering for blocked words and patterns."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def check_message(
        self,
        community_id: int,
        message: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if message contains blocked content.

        Returns:
            (is_blocked, matched_pattern)
        """
        try:
            config = await self._get_config(community_id)

            if not config['content_filter_enabled']:
                return (False, None)

            # Merge default profanity list if enabled
            words = list(config['blocked_words'])
            if config.get('use_default_profanity_list', False):
                words = words + DEFAULT_PROFANITY_WORDS

            # Check blocked words
            for word in words:
                if word.lower() in message.lower():
                    await self._log_filter_match(
                        community_id=community_id,
                        filter_type='blocked_word',
                        matched_pattern=word,
                        original_message=message
                    )
                    return (True, word)

            # Check regex patterns
            for pattern in config['blocked_patterns']:
                try:
                    if re.search(pattern, message, re.IGNORECASE):
                        await self._log_filter_match(
                            community_id=community_id,
                            filter_type='regex_pattern',
                            matched_pattern=pattern,
                            original_message=message
                        )
                        return (True, pattern)
                except re.error as e:
                    self.logger.warning(f"Invalid regex pattern: {pattern} - {e}")
                    continue

            return (False, None)

        except Exception as e:
            self.logger.error(f"Failed to check message: {e}")
            return (False, None)

    async def _get_config(self, community_id: int) -> Dict:
        """Get content filter config."""
        result = self.dal.executesql(
            """SELECT content_filter_enabled, blocked_words, blocked_patterns,
                      filter_action, use_default_profanity_list
               FROM security_config
               WHERE community_id = %s""",
            [community_id]
        )

        if not result:
            return {
                'content_filter_enabled': True,
                'blocked_words': [],
                'blocked_patterns': [],
                'filter_action': 'delete',
                'use_default_profanity_list': False,
            }

        row = result[0]
        return {
            'content_filter_enabled': row[0],
            'blocked_words': row[1] or [],
            'blocked_patterns': row[2] or [],
            'filter_action': row[3],
            'use_default_profanity_list': row[4] if row[4] is not None else False,
        }

    async def add_blocked_words(
        self,
        community_id: int,
        words: List[str]
    ) -> Dict:
        """Add words to blocked list."""
        try:
            # Get current config
            config = await self._get_config(community_id)
            current_words = set(config['blocked_words'])

            # Add new words
            current_words.update(words)

            # Update database
            self.dal.executesql(
                """UPDATE security_config
                   SET blocked_words = %s, updated_at = NOW()
                   WHERE community_id = %s""",
                [list(current_words), community_id]
            )
            self.dal.commit()

            self.logger.audit(
                action="add_blocked_words",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                detail="Blocked words added",
                words_count=len(words)
            )

            return {
                'blocked_words': list(current_words),
                'added': words
            }

        except Exception as e:
            self.logger.error(f"Failed to add blocked words: {e}")
            raise

    async def remove_blocked_words(
        self,
        community_id: int,
        words: List[str]
    ) -> Dict:
        """Remove words from blocked list."""
        try:
            config = await self._get_config(community_id)
            current_words = set(config['blocked_words'])

            # Remove words
            current_words.difference_update(words)

            # Update database
            self.dal.executesql(
                """UPDATE security_config
                   SET blocked_words = %s, updated_at = NOW()
                   WHERE community_id = %s""",
                [list(current_words), community_id]
            )
            self.dal.commit()

            self.logger.audit(
                action="remove_blocked_words",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                detail="Blocked words removed",
                words_count=len(words)
            )

            return {
                'blocked_words': list(current_words),
                'removed': words
            }

        except Exception as e:
            self.logger.error(f"Failed to remove blocked words: {e}")
            raise

    async def get_filter_matches(
        self,
        community_id: int,
        page: int = 1,
        limit: int = 50
    ) -> Dict:
        """Get filter match log."""
        try:
            offset = (page - 1) * limit

            result = self.dal.executesql(
                """SELECT id, platform, platform_user_id, platform_username,
                          filter_type, matched_pattern, action_taken, created_at
                   FROM security_filter_matches
                   WHERE community_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s OFFSET %s""",
                [community_id, limit, offset]
            )

            matches = []
            if result:
                for row in result:
                    matches.append({
                        'id': row[0],
                        'platform': row[1],
                        'platform_user_id': row[2],
                        'platform_username': row[3],
                        'filter_type': row[4],
                        'matched_pattern': row[5],
                        'action_taken': row[6],
                        'created_at': row[7].isoformat() + 'Z' if row[7] else None
                    })

            return {
                'matches': matches,
                'page': page,
                'limit': limit,
                'total': len(matches)
            }

        except Exception as e:
            self.logger.error(f"Failed to get filter matches: {e}")
            raise

    async def _log_filter_match(
        self,
        community_id: int,
        filter_type: str,
        matched_pattern: str,
        original_message: str,
        platform: Optional[str] = None,
        platform_user_id: Optional[str] = None
    ):
        """Log content filter match."""
        try:
            self.dal.executesql(
                """INSERT INTO security_filter_matches
                   (community_id, platform, platform_user_id, filter_type,
                    matched_pattern, original_message, action_taken)
                   VALUES (%s, %s, %s, %s, %s, %s, 'delete')""",
                [community_id, platform, platform_user_id,
                 filter_type, matched_pattern, original_message]
            )
            self.dal.commit()
        except Exception as e:
            self.logger.error(f"Failed to log filter match: {e}")
