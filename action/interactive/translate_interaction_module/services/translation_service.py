"""
Translation Service - Multi-level caching with provider fallback
=================================================================

Provides intelligent translation with three-tier caching:
1. In-memory LRU cache (1000 entries, 1 hour TTL)
2. Redis cache (24 hour TTL)
3. Database cache (persistent)

Provider fallback chain:
1. Google Cloud API (if configured)
2. GoogleTransProvider (free, always available)
3. WaddleAIProvider (fallback if both fail)

Automatic skip conditions:
- Translation disabled in config
- Message too short (< min_words)
- Confidence too low (< confidence_threshold)
- Already in target language

Token Preservation (via preprocessor):
- @mentions, !commands, emails, URLs
- Platform-specific emotes (Twitch/BTTV/FFZ/7TV, Discord, Slack)
- AI-based decision for uncertain patterns (configurable)
"""

import hashlib
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from cachetools import TTLCache

from services.translation_providers.base_provider import (
    TranslationProvider,
    TranslationResult
)
from services.translation_preprocessor import (
    TranslationPreprocessor,
    PreprocessResult,
    AIDecisionService
)
from services.emote_service import EmoteService

logger = logging.getLogger(__name__)


class TranslationService:
    """
    Multi-level caching translation service with provider fallback.

    Provides efficient translation with intelligent caching and automatic
    fallback between multiple translation providers. Handles skip conditions
    and configuration-based behavior.
    """

    def __init__(self, dal, cache_manager):
        """
        Initialize TranslationService.

        Args:
            dal: Database access layer (AsyncDAL instance)
            cache_manager: Redis cache manager instance
        """
        self.dal = dal
        self.cache = cache_manager

        # In-memory LRU cache (1000 entries, 1 hour TTL)
        self._memory_cache = TTLCache(maxsize=1000, ttl=3600)

        # Translation providers (will be initialized on first use)
        self._providers: Dict[str, TranslationProvider] = {}
        self._providers_initialized = False

        # Emote service for platform emote lookup
        self._emote_service = EmoteService(dal, cache_manager)

        # Preprocessor for token preservation (initialized lazily with AI service)
        self._preprocessor: Optional[TranslationPreprocessor] = None
        self._ai_decision_service: Optional[AIDecisionService] = None

        # Ensemble language detector (initialized lazily)
        self._ensemble_detector = None
        self._ensemble_detector_initialized = False

        logger.info("TranslationService initialized with multi-level caching and ensemble detection")

    def _initialize_providers(self, config: Dict) -> None:
        """
        Lazy initialization of translation providers based on config.

        Args:
            config: Translation configuration dict from community settings
        """
        if self._providers_initialized:
            return

        try:
            # Import providers only when needed (some are optional)
            from services.translation_providers.googletrans_provider import GoogleTransProvider
            from services.translation_providers.waddleai_provider import WaddleAIProvider
            from services.translation_providers import GOOGLE_CLOUD_AVAILABLE

            # Initialize Google Cloud provider if API key configured AND package available
            google_api_key = config.get('google_api_key_encrypted')
            if google_api_key and GOOGLE_CLOUD_AVAILABLE:
                try:
                    from services.translation_providers.google_cloud_provider import GoogleCloudProvider
                    self._providers['google_cloud'] = GoogleCloudProvider(
                        api_key=google_api_key
                    )
                    logger.info("Google Cloud Translation API provider initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize Google Cloud provider: {e}")
            elif google_api_key and not GOOGLE_CLOUD_AVAILABLE:
                logger.info("Google Cloud provider not available (google-cloud-translate not installed)")

            # Initialize free GoogleTrans provider (always available)
            try:
                self._providers['googletrans'] = GoogleTransProvider()
                logger.info("GoogleTrans (free) provider initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize GoogleTrans provider: {e}")

            # Initialize WaddleAI provider (fallback)
            try:
                self._providers['waddleai'] = WaddleAIProvider()
                logger.info("WaddleAI provider initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize WaddleAI provider: {e}")

            self._providers_initialized = True

        except ImportError as e:
            logger.error(f"Failed to import translation providers: {e}")
            # Continue with empty providers - service will gracefully skip

    async def translate(
        self,
        text: str,
        target_lang: str,
        community_id: int,
        config: Dict,
        platform: str = "unknown",
        channel_id: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Translate text with multi-level caching and provider fallback.

        Preserves tokens (@mentions, !commands, emails, URLs, emotes) during
        translation using placeholder substitution.

        Args:
            text: Text to translate
            target_lang: Target language code (e.g., 'en', 'es')
            community_id: Community ID for context
            config: Translation configuration from community settings
            platform: Platform name (twitch, discord, slack, kick)
            channel_id: Optional channel ID for channel-specific emotes

        Returns:
            Dictionary with translation result:
            {
                'translated_text': str,
                'detected_lang': str,
                'target_lang': str,
                'confidence': float,
                'provider': str,
                'cached': bool,
                'tokens_preserved': int,
                'original_text': str
            }
            Returns None if translation should be skipped.

        Example:
            >>> result = await service.translate(
            ...     "@user Hola mundo !help",
            ...     "en",
            ...     community_id=123,
            ...     config={'enabled': True, 'min_words': 2},
            ...     platform="twitch"
            ... )
            >>> print(result['translated_text'])
            '@user Hello world !help'
        """
        try:
            # Check if translation should be skipped
            should_skip, skip_reason = await self._should_skip_translation(
                text, target_lang, config
            )
            if should_skip:
                logger.debug(f"Skipping translation: {skip_reason}")
                return None

            # Initialize providers if needed
            self._initialize_providers(config)

            # Initialize ensemble detector if needed
            if not self._ensemble_detector_initialized:
                self._initialize_ensemble_detector()

            # Initialize preprocessor if needed
            self._initialize_preprocessor(config)

            # Preprocess: detect and replace tokens with placeholders
            preprocess_config = config.get('preprocessing', {})
            preprocess_result = await self._preprocessor.preprocess(
                text=text,
                platform=platform,
                channel_id=channel_id,
                config=preprocess_config
            )

            # Use preprocessed text for translation
            text_to_translate = preprocess_result.processed_text
            tokens_preserved = len(preprocess_result.tokens)

            if tokens_preserved > 0:
                logger.debug(
                    f"Preserved {tokens_preserved} tokens: "
                    f"{[t.original_text for t in preprocess_result.tokens]}"
                )

            # Detect language first (on preprocessed text)
            detected_lang, confidence = await self._detect_language(text_to_translate)

            # Skip if already in target language
            if detected_lang == target_lang:
                logger.debug(f"Text already in target language ({target_lang})")
                return None

            # Skip if confidence too low
            confidence_threshold = config.get('confidence_threshold', 0.7)
            if confidence < confidence_threshold:
                logger.debug(
                    f"Language detection confidence {confidence:.2f} below "
                    f"threshold {confidence_threshold:.2f}"
                )
                return None

            # Generate cache key (include platform for emote-aware caching)
            cache_key = self._get_cache_key(text, detected_lang, target_lang)

            # Check in-memory cache first (fastest)
            if cache_key in self._memory_cache:
                logger.debug("Translation found in memory cache")
                cached_result = self._memory_cache[cache_key].copy()
                cached_result['cached'] = True
                return cached_result

            # Check Redis cache (fast)
            redis_key = f"translation:{cache_key}"
            cached_redis = await self.cache.get(redis_key)
            if cached_redis:
                logger.debug("Translation found in Redis cache")
                try:
                    result = json.loads(cached_redis)
                    result['cached'] = True
                    # Store in memory cache for faster future access
                    self._memory_cache[cache_key] = result
                    return result
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in Redis cache for key {redis_key}")

            # Check database cache (persistent)
            db_result = await self._get_db_cache(cache_key, detected_lang, target_lang)
            if db_result:
                logger.debug("Translation found in database cache")
                # Promote to higher cache levels
                self._memory_cache[cache_key] = db_result
                await self.cache.set(
                    redis_key,
                    json.dumps(db_result),
                    ttl=86400  # 24 hours
                )
                db_result['cached'] = True
                return db_result

            # No cache hit - perform translation with fallback
            translation_result = await self._translate_with_fallback(
                text_to_translate, detected_lang, target_lang, confidence
            )

            if not translation_result:
                logger.warning("All translation providers failed")
                return None

            # Postprocess: restore preserved tokens
            final_text = self._preprocessor.postprocess(
                translated_text=translation_result.translated_text,
                tokens=preprocess_result.tokens
            )

            # Build response dict
            result = {
                'translated_text': final_text,
                'detected_lang': translation_result.detected_lang,
                'target_lang': translation_result.target_lang,
                'confidence': translation_result.confidence,
                'provider': translation_result.provider,
                'cached': False,
                'tokens_preserved': tokens_preserved,
                'original_text': text
            }

            # Store in all cache levels
            self._memory_cache[cache_key] = result
            await self.cache.set(redis_key, json.dumps(result), ttl=86400)
            await self._store_db_cache(
                cache_key,
                detected_lang,
                target_lang,
                final_text,
                translation_result.provider,
                translation_result.confidence
            )

            logger.info(
                f"Translation completed: {detected_lang} -> {target_lang} "
                f"via {translation_result.provider}, {tokens_preserved} tokens preserved"
            )
            return result

        except Exception as e:
            logger.error(f"Translation error: {e}", exc_info=True)
            return None

    def _initialize_ensemble_detector(self) -> None:
        """
        Initialize the ensemble language detector.

        Lazy initialization to avoid blocking on startup.
        """
        if self._ensemble_detector_initialized:
            return

        try:
            from services.translation_providers.ensemble_detector import EnsembleLanguageDetector

            self._ensemble_detector = EnsembleLanguageDetector()
            self._ensemble_detector_initialized = True
            logger.info("Ensemble language detector initialized")

        except ImportError as e:
            logger.warning(f"Failed to initialize ensemble detector: {e}. Falling back to provider detection.")
            self._ensemble_detector = None
            self._ensemble_detector_initialized = True

    def _initialize_preprocessor(self, config: Dict) -> None:
        """
        Initialize the translation preprocessor.

        Args:
            config: Translation configuration
        """
        if self._preprocessor is not None:
            return

        preprocess_config = config.get('preprocessing', {
            'enabled': True,
            'preserve_mentions': True,
            'preserve_commands': True,
            'preserve_emails': True,
            'preserve_urls': True,
            'preserve_emotes': True,
        })

        # Initialize AI decision service if configured
        ai_config = config.get('ai_decision', {})
        ai_mode = ai_config.get('mode', 'never')

        if ai_mode != 'never' and 'waddleai' in self._providers:
            self._ai_decision_service = AIDecisionService(
                waddleai_provider=self._providers['waddleai'],
                cache_manager=self.cache,
                dal=self.dal
            )

        self._preprocessor = TranslationPreprocessor(
            emote_service=self._emote_service,
            ai_decision_service=self._ai_decision_service,
            config=preprocess_config
        )

        logger.info("Translation preprocessor initialized")

    async def _detect_language(self, text: str) -> Tuple[str, float]:
        """
        Detect language of text using tiered confidence approach.

        Tiered Confidence Strategy:
        - <70% confidence: Return as-is (will be rejected by threshold)
        - 70-90% confidence: Verify with WaddleAI/TinyLlama for second opinion
        - 90%+ confidence: Accept directly (high confidence from ensemble)

        Priority:
        1. Ensemble detector (fastText + Lingua + langdetect) for high confidence
        2. AI verification for uncertain detections (70-90%)
        3. Provider detection fallback if ensemble fails

        Args:
            text: Text to detect language for

        Returns:
            Tuple of (language_code, confidence_score)

        Raises:
            Exception: If all detection methods fail
        """
        # Initialize ensemble detector if needed
        if not self._ensemble_detector_initialized:
            self._initialize_ensemble_detector()

        # Try ensemble detector first (highest accuracy)
        if self._ensemble_detector:
            try:
                lang_code, confidence = await self._ensemble_detector.detect_language(text)
                logger.debug(
                    f"Language detected as '{lang_code}' with {confidence:.2%} "
                    f"confidence via ensemble detector"
                )

                # Tiered confidence handling
                if confidence >= 0.90:
                    # High confidence - accept directly
                    logger.debug(f"High confidence ({confidence:.2%}) - accepting without AI verification")
                    return lang_code, confidence

                elif confidence >= 0.70:
                    # Medium confidence (70-90%) - verify with AI
                    ai_result = await self._verify_with_ai(text, lang_code, confidence)
                    if ai_result:
                        return ai_result
                    # If AI verification fails, return original detection
                    return lang_code, confidence

                else:
                    # Low confidence (<70%) - return as-is (will be rejected by threshold)
                    logger.debug(f"Low confidence ({confidence:.2%}) - no AI verification needed")
                    return lang_code, confidence

            except Exception as e:
                logger.warning(f"Ensemble detection failed, falling back to providers: {e}")

        # Fallback to provider detection
        for provider_name, provider in self._providers.items():
            try:
                lang_code, confidence = await provider.detect_language(text)
                logger.debug(
                    f"Language detected as '{lang_code}' with {confidence:.2%} "
                    f"confidence via {provider_name}"
                )
                return lang_code, confidence
            except Exception as e:
                logger.warning(
                    f"Language detection failed with {provider_name}: {e}"
                )
                continue

        # If all methods fail, default to 'unknown' with low confidence
        logger.error("All language detection methods (ensemble + providers) failed")
        return 'unknown', 0.0

    async def _verify_with_ai(
        self,
        text: str,
        detected_lang: str,
        ensemble_confidence: float
    ) -> Optional[Tuple[str, float]]:
        """
        Verify uncertain language detection with WaddleAI/TinyLlama.

        Called when ensemble detection confidence is between 70-90%.
        Uses AI to get a second opinion and either confirm or override.

        Args:
            text: Text to verify language for
            detected_lang: Language detected by ensemble
            ensemble_confidence: Confidence from ensemble detection

        Returns:
            Tuple of (language_code, confidence) if AI verification succeeds,
            None if AI is unavailable or fails (fall back to ensemble result)
        """
        waddleai = self._providers.get('waddleai')
        if not waddleai:
            logger.debug("WaddleAI not available for verification, using ensemble result")
            return None

        try:
            # Get AI's opinion
            ai_lang, ai_confidence = await waddleai.detect_language(text)

            logger.info(
                f"AI verification: ensemble={detected_lang} ({ensemble_confidence:.2%}), "
                f"AI={ai_lang} ({ai_confidence:.2%})"
            )

            if ai_lang == detected_lang:
                # Agreement - boost confidence to 95%
                logger.debug(f"AI agrees with ensemble ({detected_lang}) - boosting confidence")
                return detected_lang, 0.95

            else:
                # Disagreement - use AI's opinion if more confident
                if ai_confidence > ensemble_confidence:
                    logger.info(
                        f"AI disagrees and is more confident - using AI result: "
                        f"{ai_lang} ({ai_confidence:.2%})"
                    )
                    return ai_lang, ai_confidence
                else:
                    # Ensemble was more confident, but mark as uncertain
                    logger.debug(
                        "AI disagrees but less confident - keeping ensemble result "
                        "with reduced confidence"
                    )
                    # Reduce confidence due to disagreement
                    return detected_lang, ensemble_confidence * 0.9

        except Exception as e:
            logger.warning(f"AI verification failed: {e}")
            return None

    async def _translate_with_fallback(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        confidence: float
    ) -> Optional[TranslationResult]:
        """
        Translate text with provider fallback chain.

        Tries providers in order:
        1. Google Cloud API (if configured)
        2. GoogleTrans (free)
        3. WaddleAI (fallback)

        Args:
            text: Text to translate
            source_lang: Source language code
            target_lang: Target language code
            confidence: Language detection confidence

        Returns:
            TranslationResult if successful, None if all providers fail
        """
        # Define fallback chain
        fallback_order = ['google_cloud', 'googletrans', 'waddleai']

        for provider_name in fallback_order:
            provider = self._providers.get(provider_name)
            if not provider:
                continue

            try:
                # Check provider health first
                if not await provider.health_check():
                    logger.warning(f"Provider {provider_name} failed health check")
                    continue

                # Attempt translation
                result = await provider.translate(
                    text=text,
                    target_lang=target_lang,
                    source_lang=source_lang
                )

                logger.info(f"Translation successful via {provider_name}")
                return result

            except Exception as e:
                logger.warning(
                    f"Translation failed with {provider_name}: {e}"
                )
                continue

        # All providers failed
        return None

    async def _should_skip_translation(
        self,
        text: str,
        target_lang: str,
        config: Dict
    ) -> Tuple[bool, str]:
        """
        Determine if translation should be skipped.

        Args:
            text: Text to potentially translate
            target_lang: Target language code
            config: Translation configuration

        Returns:
            Tuple of (should_skip: bool, reason: str)
        """
        # Check if translation is enabled
        if not config.get('enabled', False):
            return True, "Translation disabled in config"

        # Check minimum word count
        min_words = config.get('min_words', 5)
        word_count = len(text.split())
        if word_count < min_words:
            return True, f"Message too short ({word_count} < {min_words} words)"

        # Check if text is empty or whitespace
        if not text or not text.strip():
            return True, "Empty or whitespace-only text"

        return False, ""

    def _get_cache_key(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """
        Generate SHA-256 cache key for translation.

        Args:
            text: Source text
            source_lang: Source language code
            target_lang: Target language code

        Returns:
            64-character SHA-256 hex digest
        """
        # Include language codes in hash to separate different translation pairs
        key_material = f"{source_lang}:{target_lang}:{text}"
        return hashlib.sha256(key_material.encode('utf-8')).hexdigest()

    async def _get_db_cache(
        self,
        text_hash: str,
        source_lang: str,
        target_lang: str
    ) -> Optional[Dict]:
        """
        Retrieve translation from database cache.

        Updates access_count and last_accessed on cache hit.

        Args:
            text_hash: SHA-256 hash of source text
            source_lang: Source language code
            target_lang: Target language code

        Returns:
            Cached translation dict or None if not found
        """
        try:
            # Query cache table
            result = self.dal.executesql(
                """
                SELECT translated_text, provider, confidence_score
                FROM translation_cache
                WHERE source_text_hash = %s
                  AND source_lang = %s
                  AND target_lang = %s
                LIMIT 1
                """,
                [text_hash, source_lang, target_lang]
            )

            if not result or len(result) == 0:
                return None

            row = result[0]
            translated_text = row[0]
            provider = row[1]
            confidence = float(row[2]) if row[2] is not None else 0.0

            # Update access statistics (fire-and-forget)
            self.dal.executesql(
                """
                UPDATE translation_cache
                SET access_count = access_count + 1,
                    last_accessed = NOW()
                WHERE source_text_hash = %s
                  AND source_lang = %s
                  AND target_lang = %s
                """,
                [text_hash, source_lang, target_lang]
            )
            self.dal.commit()

            logger.debug(f"Database cache hit for hash {text_hash[:16]}...")

            return {
                'translated_text': translated_text,
                'detected_lang': source_lang,
                'target_lang': target_lang,
                'confidence': confidence,
                'provider': provider,
                'cached': True
            }

        except Exception as e:
            logger.error(f"Error reading from database cache: {e}", exc_info=True)
            return None

    async def _store_db_cache(
        self,
        text_hash: str,
        source_lang: str,
        target_lang: str,
        translated_text: str,
        provider: str,
        confidence: float
    ) -> None:
        """
        Store translation in database cache.

        Uses INSERT ... ON CONFLICT to handle race conditions.

        Args:
            text_hash: SHA-256 hash of source text
            source_lang: Source language code
            target_lang: Target language code
            translated_text: Translated text
            provider: Provider name that performed translation
            confidence: Language detection confidence score
        """
        try:
            # Insert with ON CONFLICT to handle duplicates gracefully
            self.dal.executesql(
                """
                INSERT INTO translation_cache (
                    source_text_hash,
                    source_lang,
                    target_lang,
                    translated_text,
                    provider,
                    confidence_score,
                    created_at,
                    access_count,
                    last_accessed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_text_hash, source_lang, target_lang)
                DO UPDATE SET
                    access_count = translation_cache.access_count + 1,
                    last_accessed = NOW()
                """,
                [
                    text_hash,
                    source_lang,
                    target_lang,
                    translated_text,
                    provider,
                    confidence,
                    datetime.now(timezone.utc),
                    1,
                    datetime.now(timezone.utc)
                ]
            )
            self.dal.commit()

            logger.debug(f"Translation cached in database (hash: {text_hash[:16]}...)")

        except Exception as e:
            logger.error(f"Error storing to database cache: {e}", exc_info=True)
            # Don't raise - cache storage failures shouldn't break translations

    async def get_cache_stats(self) -> Dict:
        """
        Get cache statistics for monitoring.

        Returns:
            Dictionary with cache statistics:
            {
                'memory_cache_size': int,
                'memory_cache_max_size': int,
                'db_cache_total_entries': int,
                'db_cache_high_use_entries': int (access_count >= 5)
            }
        """
        try:
            # Memory cache stats
            memory_stats = {
                'memory_cache_size': len(self._memory_cache),
                'memory_cache_max_size': self._memory_cache.maxsize
            }

            # Database cache stats
            db_result = self.dal.executesql(
                """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE access_count >= 5) as high_use
                FROM translation_cache
                """
            )

            db_stats = {}
            if db_result and len(db_result) > 0:
                row = db_result[0]
                db_stats = {
                    'db_cache_total_entries': row[0] or 0,
                    'db_cache_high_use_entries': row[1] or 0
                }

            return {**memory_stats, **db_stats}

        except Exception as e:
            logger.error(f"Error getting cache stats: {e}", exc_info=True)
            return {
                'memory_cache_size': len(self._memory_cache),
                'memory_cache_max_size': self._memory_cache.maxsize,
                'error': str(e)
            }

    async def cleanup_cache(self) -> Dict:
        """
        Clean up old, low-use cache entries.

        Calls database cleanup function to remove entries with:
        - access_count < 5
        - last_accessed > 30 days ago

        Returns:
            Dictionary with cleanup results:
            {
                'success': bool,
                'entries_removed': int (if available)
            }
        """
        try:
            # Call database cleanup function
            self.dal.executesql("SELECT cleanup_translation_cache()")
            self.dal.commit()

            logger.info("Translation cache cleanup completed")
            return {'success': True}

        except Exception as e:
            logger.error(f"Cache cleanup failed: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
