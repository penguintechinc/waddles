"""
Safety Layer Service for AI Researcher Module

Provides prompt injection protection and topic filtering to ensure safe
and appropriate research queries and responses.

Features:
- Prompt injection detection using regex patterns
- Topic filtering with configurable blocklists
- Sanitization of dangerous patterns
- Comprehensive audit logging

Author: Waddles Team
License: PenguinTech License Server
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from flask_core.ai_guard import (
    CATEGORY_EXFIL_BEACON,
    fold_for_scan,
    neutralize_markup,
    normalize_untrusted,
    scan_for_injection,
)

# Configure logging
logger = logging.getLogger(__name__)

# Compiled regex patterns for prompt injection detection
INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions?', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+', re.IGNORECASE),
    re.compile(r'system\s*:\s*', re.IGNORECASE),
    re.compile(r'<\|?system\|?>', re.IGNORECASE),
    re.compile(r'\[\[SYSTEM\]\]', re.IGNORECASE),
    re.compile(r'forget\s+(everything|your\s+rules)', re.IGNORECASE),
    re.compile(r'roleplay\s+as', re.IGNORECASE),
    re.compile(r'pretend\s+(you\s+are|to\s+be)', re.IGNORECASE),
    re.compile(r'jailbreak', re.IGNORECASE),
    re.compile(r'DAN\s+mode', re.IGNORECASE),
    re.compile(r'ignore\s+safety', re.IGNORECASE),
    re.compile(r'bypass\s+(filter|restriction)', re.IGNORECASE),
    re.compile(r'act\s+as\s+if', re.IGNORECASE),
    re.compile(r'disregard\s+(all\s+)?instructions?', re.IGNORECASE),
    re.compile(r'override\s+(your\s+)?(rules|instructions)', re.IGNORECASE),
    re.compile(r'new\s+instructions?:\s*', re.IGNORECASE),
    re.compile(r'enable\s+developer\s+mode', re.IGNORECASE),
    re.compile(r'sudo\s+mode', re.IGNORECASE),
    re.compile(r'admin\s+override', re.IGNORECASE),
]

# Default blocked topics
DEFAULT_BLOCKED_TOPICS = [
    'politics',
    'medical',
    'legal',
    'financial',
    'violence',
    'adult',
]

# Topic detection keywords (expanded for better detection)
TOPIC_KEYWORDS = {
    'politics': [
        'election', 'political', 'democrat', 'republican', 'liberal',
        'conservative', 'government policy', 'legislation', 'congress',
        'parliament', 'voting', 'campaign', 'politician',
    ],
    'medical': [
        'diagnosis', 'medication', 'prescription', 'treatment', 'disease',
        'symptom', 'medical advice', 'doctor', 'physician', 'therapy',
        'cure', 'medicine', 'health advice', 'dosage',
    ],
    'legal': [
        'legal advice', 'lawsuit', 'attorney', 'lawyer', 'court case',
        'litigation', 'contract law', 'criminal law', 'sue', 'prosecution',
        'defense attorney', 'legal representation',
    ],
    'financial': [
        'investment advice', 'stock tip', 'financial planning', 'tax advice',
        'portfolio', 'securities', 'trading advice', 'financial advice',
        'retirement planning', 'wealth management', 'crypto investment',
    ],
    'violence': [
        'harm', 'attack', 'weapon', 'violence', 'assault', 'murder',
        'kill', 'bomb', 'explosive', 'terrorist', 'how to hurt',
        'self-harm', 'suicide',
    ],
    'adult': [
        'pornography', 'explicit content', 'nsfw', 'adult content',
        'sexual content', 'erotic', 'xxx',
    ],
}


@dataclass(slots=True, frozen=True)
class SafetyCheckResult:
    """
    Result of a safety check operation.

    Attributes:
        is_safe: Whether the content passed all safety checks
        blocked_reason: Reason for blocking (if not safe)
        detected_patterns: List of injection patterns detected
        detected_topics: List of blocked topics detected
    """
    is_safe: bool
    blocked_reason: Optional[str] = None
    detected_patterns: list[str] = field(default_factory=list)
    detected_topics: list[str] = field(default_factory=list)


class SafetyLayer:
    """
    Safety layer for AI research queries and responses.

    Provides comprehensive protection against:
    - Prompt injection attacks
    - Inappropriate topic requests
    - Malicious input patterns

    Features configurable topic blocklists and detailed audit logging.
    """

    def __init__(self, blocked_topics: Optional[list[str]] = None) -> None:
        """
        Initialize the safety layer.

        Args:
            blocked_topics: List of topics to block (defaults to DEFAULT_BLOCKED_TOPICS)
        """
        self.blocked_topics: set[str] = set(
            blocked_topics if blocked_topics is not None else DEFAULT_BLOCKED_TOPICS
        )

        logger.info(
            f"SYSTEM SafetyLayer initialized with {len(self.blocked_topics)} "
            f"blocked topics: {sorted(self.blocked_topics)}"
        )

    def check_prompt(self, prompt: str) -> SafetyCheckResult:
        """
        Perform comprehensive safety check on a prompt.

        Checks for both injection patterns and blocked topics.

        Args:
            prompt: The prompt text to check

        Returns:
            SafetyCheckResult with check results and details
        """
        if not prompt or not prompt.strip():
            logger.warning("AUDIT SafetyLayer received empty prompt")
            return SafetyCheckResult(
                is_safe=False,
                blocked_reason="Empty prompt",
                detected_patterns=[],
                detected_topics=[],
            )

        # Check for injection patterns
        injection_safe, detected_patterns = self.check_injection(prompt)

        # Check for blocked topics
        topic_safe, detected_topics = self.check_topics(prompt)

        # Determine overall safety
        is_safe = injection_safe and topic_safe
        blocked_reason = None

        if not injection_safe:
            blocked_reason = f"Prompt injection detected: {', '.join(detected_patterns)}"
            logger.warning(
                f"AUDIT SafetyLayer blocked prompt due to injection patterns: "
                f"{detected_patterns}"
            )
        elif not topic_safe:
            blocked_reason = f"Blocked topic detected: {', '.join(detected_topics)}"
            logger.warning(
                f"AUDIT SafetyLayer blocked prompt due to topics: {detected_topics}"
            )

        if is_safe:
            logger.debug("AUDIT SafetyLayer approved prompt")

        return SafetyCheckResult(
            is_safe=is_safe,
            blocked_reason=blocked_reason,
            detected_patterns=detected_patterns,
            detected_topics=detected_topics,
        )

    def check_injection(self, text: str) -> tuple[bool, list[str]]:
        """
        Check text for prompt injection patterns.

        Args:
            text: Text to check for injection patterns

        Returns:
            Tuple of (is_safe, detected_patterns)
            - is_safe: True if no injection patterns detected
            - detected_patterns: List of detected pattern descriptions
        """
        detected_patterns: list[str] = []

        # Match against the canonical folded view so zero-width / full-width /
        # lookalike-letter / spacing obfuscation cannot slip past the patterns.
        folded = fold_for_scan(text)
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(folded)
            if match:
                detected_patterns.append(match.group(0))

        # Directive-shaped attacks the phrase list does not cover: role/tenant/scope
        # escalation, exfiltration, tool abuse (closed-vocabulary category names are
        # reported, never the text). The remote-image beacon category is an
        # OUTPUT-side concern, so a user merely pasting a markdown image is not blocked.
        for category in sorted(
            scan_for_injection(text, ignore=frozenset({CATEGORY_EXFIL_BEACON})).categories
        ):
            if category not in detected_patterns:
                detected_patterns.append(category)

        is_safe = len(detected_patterns) == 0

        if not is_safe:
            logger.info(
                f"AUDIT SafetyLayer injection check failed: "
                f"found {len(detected_patterns)} patterns"
            )

        return is_safe, detected_patterns

    def check_topics(self, text: str) -> tuple[bool, list[str]]:
        """
        Check text for blocked topics using keyword matching.

        Args:
            text: Text to check for blocked topics

        Returns:
            Tuple of (is_safe, detected_topics)
            - is_safe: True if no blocked topics detected
            - detected_topics: List of detected blocked topics
        """
        detected_topics: list[str] = []
        text_lower = text.lower()

        for topic in self.blocked_topics:
            # Get keywords for this topic
            keywords = TOPIC_KEYWORDS.get(topic, [topic])

            # Check if any keyword appears in the text
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    detected_topics.append(topic)
                    logger.debug(
                        f"AUDIT SafetyLayer detected topic '{topic}' "
                        f"via keyword '{keyword}'"
                    )
                    break  # No need to check other keywords for this topic

        is_safe = len(detected_topics) == 0

        if not is_safe:
            logger.info(
                f"AUDIT SafetyLayer topic check failed: "
                f"found {len(detected_topics)} blocked topics: {detected_topics}"
            )

        return is_safe, detected_topics

    def sanitize_prompt(self, prompt: str) -> str:
        """
        Remove or escape dangerous patterns from a prompt.

        This is a best-effort sanitization and should not be relied upon
        as the sole security mechanism. Always use check_prompt() first.

        Args:
            prompt: The prompt to sanitize

        Returns:
            Sanitized prompt with dangerous patterns removed
        """
        # Normalise first (so obfuscated variants are matched) and defang any
        # delimiter / chat-template control tokens.
        sanitized = neutralize_markup(normalize_untrusted(prompt))

        # Remove detected injection patterns
        for pattern in INJECTION_PATTERNS:
            sanitized = pattern.sub('[REMOVED]', sanitized)

        # Clean up multiple spaces and [REMOVED] markers
        sanitized = re.sub(r'\[REMOVED\]\s*', '', sanitized)
        sanitized = re.sub(r'\s+', ' ', sanitized)
        sanitized = sanitized.strip()

        if sanitized != prompt:
            logger.info(
                f"AUDIT SafetyLayer sanitized prompt: "
                f"original length={len(prompt)}, sanitized length={len(sanitized)}"
            )

        return sanitized

    def add_blocked_topic(self, topic: str) -> None:
        """
        Add a topic to the blocklist.

        Args:
            topic: Topic name to block
        """
        topic_lower = topic.lower().strip()

        if topic_lower not in self.blocked_topics:
            self.blocked_topics.add(topic_lower)
            logger.info(f"SYSTEM SafetyLayer added blocked topic: {topic_lower}")
        else:
            logger.debug(f"SYSTEM SafetyLayer topic already blocked: {topic_lower}")

    def remove_blocked_topic(self, topic: str) -> None:
        """
        Remove a topic from the blocklist.

        Args:
            topic: Topic name to unblock
        """
        topic_lower = topic.lower().strip()

        if topic_lower in self.blocked_topics:
            self.blocked_topics.remove(topic_lower)
            logger.info(f"SYSTEM SafetyLayer removed blocked topic: {topic_lower}")
        else:
            logger.debug(
                f"SYSTEM SafetyLayer topic not in blocklist: {topic_lower}"
            )

    def get_blocked_topics(self) -> list[str]:
        """
        Get current list of blocked topics.

        Returns:
            Sorted list of blocked topic names
        """
        return sorted(self.blocked_topics)

    def is_topic_blocked(self, topic: str) -> bool:
        """
        Check if a specific topic is blocked.

        Args:
            topic: Topic name to check

        Returns:
            True if topic is blocked, False otherwise
        """
        return topic.lower().strip() in self.blocked_topics
