"""
License Service for Workflow Module
====================================

Handles premium license validation with PenguinTech License Server integration.

Features:
- License status checking with PenguinTech integration
- Workflow creation/execution validation against license tier
- Redis caching with 5-minute TTL
- Graceful degradation in development mode (RELEASE_MODE=false)
- HTTP 402 Payment Required error handling
- AAA logging for license events

License Tiers:
- Free: 0 workflows allowed
- Premium: Unlimited workflows
"""

from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from enum import Enum
import asyncio

from flask_core import setup_aaa_logging

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

# Module-level fallback logger for callers that construct LicenseService
# without an explicit logger_instance -- every self.logger.audit()/.system()
# call below requires the AAALogger interface, not a bare stdlib Logger
# (logging.getLogger(__name__) has no .audit()/.system() and raised
# AttributeError the first time either was hit, previously undetected
# because this module had 0% test coverage).
logger = setup_aaa_logging("workflow_core.license_service", "1.0.0")


class LicenseTier(str, Enum):
    """License tier enumeration"""
    FREE = "free"
    PREMIUM = "premium"


class LicenseStatus(str, Enum):
    """License status enumeration"""
    ACTIVE = "active"
    EXPIRED = "expired"
    INVALID = "invalid"
    UNLICENSED = "unlicensed"


class LicenseException(Exception):
    """Base exception for license errors"""
    pass


class LicenseValidationException(LicenseException):
    """Raised when license validation fails (402 Payment Required)"""
    def __init__(self, message: str, community_id: int):
        self.status_code = 402
        self.message = message
        self.community_id = community_id
        super().__init__(message)


class LicenseService:
    """
    Service for validating licenses and managing workflow feature access.

    Handles:
    - License validation against PenguinTech License Server
    - Workflow creation/execution permission checks
    - License info caching in Redis
    - Graceful fallback in development mode
    """

    # License server endpoint for validation
    LICENSE_VALIDATE_ENDPOINT = "/api/v1/validate"

    # Redis cache configuration
    CACHE_TTL_SECONDS = 300  # 5 minutes
    CACHE_KEY_PREFIX = "license"

    def __init__(
        self,
        license_server_url: str,
        redis_url: Optional[str] = None,
        release_mode: bool = False,
        logger_instance: Optional[Any] = None
    ):
        """
        Initialize license service.

        Args:
            license_server_url: Base URL of PenguinTech License Server
                (e.g., 'https://license.penguintech.io')
            redis_url: Redis connection URL for caching
            release_mode: Whether to enforce license checks (true = enforce)
            logger_instance: Logger instance for AAA logging
        """
        self.license_server_url = license_server_url
        self.redis_url = redis_url
        self.release_mode = release_mode
        self.logger = logger_instance or logger

        self._redis: Optional[redis.Redis] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._redis_connected = False

        # In-memory cache fallback
        self._cache: Dict[str, Dict[str, Any]] = {}

    async def connect(self) -> None:
        """
        Initialize connections (call during startup).
        Connects to Redis for caching.
        """
        if not AIOHTTP_AVAILABLE:
            self.logger.error(
                "aiohttp not available, license validation will fail",
                extra={"event_type": "SYSTEM", "result": "FAILURE"}
            )
            return

        # Connect to Redis if available
        if self.redis_url and REDIS_AVAILABLE:
            try:
                self._redis = redis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                await self._redis.ping()
                self._redis_connected = True
                self.logger.system(
                    "Connected to Redis for license caching",
                    action="license_cache_connect",
                    result="SUCCESS"
                )
            except Exception as e:
                self.logger.error(
                    f"Failed to connect to Redis: {str(e)}",
                    extra={"event_type": "SYSTEM", "result": "FAILURE"}
                )
                self._redis_connected = False

        # Create aiohttp session
        try:
            self._session = aiohttp.ClientSession()
            self.logger.system(
                "License service initialized",
                action="license_service_init",
                result="SUCCESS"
            )
        except Exception as e:
            self.logger.error(
                f"Failed to initialize license service: {str(e)}",
                extra={"event_type": "SYSTEM", "result": "FAILURE"}
            )

    async def disconnect(self) -> None:
        """Cleanup connections (call during shutdown)."""
        if self._session:
            await self._session.close()

        if self._redis:
            await self._redis.close()
            self._redis_connected = False

        self.logger.system(
            "License service disconnected",
            action="license_service_disconnect",
            result="SUCCESS"
        )

    async def check_license_status(
        self,
        community_id: int,
        license_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Check license status for a community.

        Args:
            community_id: Community ID
            license_key: Optional license key (fetched from DB if not provided)

        Returns:
            Dictionary with keys:
            - status: LicenseStatus enum value
            - tier: LicenseTier enum value
            - expires_at: ISO datetime string or None
            - features: Dict of feature -> enabled
            - cached: bool indicating if result was cached
            - error: Error message if status is invalid

        Raises:
            LicenseException: If license check fails
        """
        try:
            # Check cache first
            cached_status = await self._get_cached_license(community_id)
            if cached_status:
                cached_status["cached"] = True
                return cached_status

            # In dev mode without license key, assume premium
            if not self.release_mode:
                self.logger.audit(
                    action="license_check",
                    user="system",
                    community=str(community_id),
                    result="SUCCESS",
                    message=f"License check skipped in dev mode for community {community_id}",
                )
                return {
                    "status": LicenseStatus.ACTIVE.value,
                    "tier": LicenseTier.PREMIUM.value,
                    "expires_at": None,
                    "features": {"workflows": True},
                    "cached": False,
                    "dev_mode": True
                }

            # In release mode, validate with license server
            if not license_key:
                raise LicenseException(
                    f"No license key found for community {community_id}"
                )

            license_info = await self._validate_with_server(
                community_id,
                license_key
            )

            # Cache the result
            await self._cache_license(community_id, license_info)

            self.logger.audit(
                action="license_check",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                message=f"License validated for community {community_id}",
            )

            return license_info

        except LicenseException:
            raise
        except Exception as e:
            self.logger.error(
                f"License check failed: {str(e)}",
                extra={
                    "event_type": "ERROR",
                    "community": str(community_id),
                    "result": "FAILURE"
                }
            )
            raise LicenseException(f"License check failed: {str(e)}")

    async def validate_workflow_creation(
        self,
        community_id: int,
        entity_id: str,
        license_key: Optional[str] = None
    ) -> bool:
        """
        Validate if a community can create a new workflow.

        Checks license tier and workflow count limits.

        Args:
            community_id: Community ID
            entity_id: Entity/workflow ID being created
            license_key: Optional license key

        Returns:
            True if validation passes

        Raises:
            LicenseValidationException: If validation fails (402 Payment Required)
        """
        try:
            license_info = await self.check_license_status(
                community_id,
                license_key
            )

            # Check if license is active
            if license_info["status"] != LicenseStatus.ACTIVE.value:
                self.logger.audit(
                    action="workflow_creation_denied",
                    user=entity_id,
                    community=str(community_id),
                    result="FAILURE",
                    message="Workflow creation denied: invalid license status",
                )
                raise LicenseValidationException(
                    "License is not active",
                    community_id
                )

            # Check tier features
            tier = license_info["tier"]
            if tier == LicenseTier.FREE.value:
                # Free tier: 0 workflows allowed
                self.logger.audit(
                    action="workflow_creation_denied",
                    user=entity_id,
                    community=str(community_id),
                    result="FAILURE",
                    message="Workflow creation denied: free tier limit reached",
                )
                raise LicenseValidationException(
                    "Free tier does not support workflows. Upgrade to Premium.",
                    community_id
                )

            # Premium tier: unlimited workflows
            self.logger.audit(
                action="workflow_creation_validated",
                user=entity_id,
                community=str(community_id),
                result="SUCCESS",
                message="Workflow creation validated",
            )
            return True

        except LicenseValidationException:
            raise
        except Exception as e:
            self.logger.error(
                f"Workflow creation validation failed: {str(e)}",
                extra={
                    "event_type": "ERROR",
                    "community": str(community_id),
                    "user": entity_id,
                    "result": "FAILURE"
                }
            )
            raise LicenseValidationException(
                f"Workflow creation validation failed: {str(e)}",
                community_id
            )

    async def validate_workflow_execution(
        self,
        workflow_id: str,
        community_id: int,
        license_key: Optional[str] = None
    ) -> bool:
        """
        Validate if a workflow can be executed.

        Checks license status and tier.

        Args:
            workflow_id: Workflow ID
            community_id: Community ID
            license_key: Optional license key

        Returns:
            True if validation passes

        Raises:
            LicenseValidationException: If validation fails (402 Payment Required)
        """
        try:
            license_info = await self.check_license_status(
                community_id,
                license_key
            )

            # Check if license is active
            if license_info["status"] != LicenseStatus.ACTIVE.value:
                self.logger.audit(
                    action="workflow_execution_denied",
                    user=workflow_id,
                    community=str(community_id),
                    result="FAILURE",
                    message="Workflow execution denied: invalid license status",
                )
                raise LicenseValidationException(
                    "License is not active",
                    community_id
                )

            # Check if workflows are enabled in license
            features = license_info.get("features", {})
            if not features.get("workflows", False):
                self.logger.audit(
                    action="workflow_execution_denied",
                    user=workflow_id,
                    community=str(community_id),
                    result="FAILURE",
                    message="Workflow execution denied: workflows not in license",
                )
                raise LicenseValidationException(
                    "Workflows feature not enabled in license",
                    community_id
                )

            self.logger.audit(
                action="workflow_execution_validated",
                user=workflow_id,
                community=str(community_id),
                result="SUCCESS",
                message="Workflow execution validated",
            )
            return True

        except LicenseValidationException:
            raise
        except Exception as e:
            self.logger.error(
                f"Workflow execution validation failed: {str(e)}",
                extra={
                    "event_type": "ERROR",
                    "community": str(community_id),
                    "user": workflow_id,
                    "result": "FAILURE"
                }
            )
            raise LicenseValidationException(
                f"Workflow execution validation failed: {str(e)}",
                community_id
            )

    async def get_license_info(
        self,
        community_id: int,
        license_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get complete license information for a community.

        Args:
            community_id: Community ID
            license_key: Optional license key

        Returns:
            Dictionary with:
            - tier: "free" or "premium"
            - status: "active", "expired", "invalid", "unlicensed"
            - expires_at: ISO datetime string or None
            - features: Dict of feature -> enabled
            - workflow_limit: Number of workflows allowed (0 for free, None for unlimited)
            - cached: bool indicating if result was cached
        """
        try:
            license_status = await self.check_license_status(
                community_id,
                license_key
            )

            # Transform to public format
            tier = license_status.get("tier")
            workflow_limit = 0 if tier == LicenseTier.FREE.value else None

            return {
                "tier": tier,
                "status": license_status.get("status"),
                "expires_at": license_status.get("expires_at"),
                "features": license_status.get("features", {}),
                "workflow_limit": workflow_limit,
                "cached": license_status.get("cached", False)
            }

        except Exception as e:
            self.logger.error(
                f"Failed to get license info: {str(e)}",
                extra={
                    "event_type": "ERROR",
                    "community": str(community_id),
                    "result": "FAILURE"
                }
            )
            raise

    # Private methods

    async def _get_cached_license(
        self,
        community_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve cached license from Redis or in-memory cache.

        Args:
            community_id: Community ID

        Returns:
            Cached license info or None if not found
        """
        cache_key = f"{self.CACHE_KEY_PREFIX}:community:{community_id}"

        try:
            # Try Redis first
            if self._redis_connected:
                cached = await self._redis.get(cache_key)
                if cached:
                    import json
                    return json.loads(cached)
            else:
                # Use in-memory cache
                if cache_key in self._cache:
                    return self._cache[cache_key]

        except Exception as e:
            self.logger.error(
                f"Cache retrieval failed: {str(e)}",
                extra={"event_type": "ERROR", "result": "FAILURE"}
            )

        return None

    async def _cache_license(
        self,
        community_id: int,
        license_info: Dict[str, Any]
    ) -> None:
        """
        Cache license info in Redis or in-memory cache.

        Args:
            community_id: Community ID
            license_info: License information to cache
        """
        cache_key = f"{self.CACHE_KEY_PREFIX}:community:{community_id}"

        try:
            # Try Redis first
            if self._redis_connected:
                import json
                await self._redis.setex(
                    cache_key,
                    self.CACHE_TTL_SECONDS,
                    json.dumps(license_info)
                )
            else:
                # Use in-memory cache
                self._cache[cache_key] = license_info

            self.logger.system(
                f"License cached for community {community_id}",
                action="license_cache_set",
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(
                f"Cache storage failed: {str(e)}",
                extra={"event_type": "ERROR", "result": "FAILURE"}
            )

    async def _validate_with_server(
        self,
        community_id: int,
        license_key: str
    ) -> Dict[str, Any]:
        """
        Validate license with PenguinTech License Server.

        Args:
            community_id: Community ID
            license_key: License key (format: PENG-XXXX-XXXX-XXXX-XXXX-ABCD)

        Returns:
            License information from server

        Raises:
            LicenseException: If validation fails
        """
        if not self._session:
            raise LicenseException("License service not initialized")

        url = f"{self.license_server_url}{self.LICENSE_VALIDATE_ENDPOINT}"
        payload = {
            "community_id": community_id,
            "license_key": license_key
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "status": data.get("status", LicenseStatus.INVALID.value),
                        "tier": data.get("tier", LicenseTier.FREE.value),
                        "expires_at": data.get("expires_at"),
                        "features": data.get("features", {"workflows": False}),
                        "cached": False
                    }
                elif response.status == 404:
                    raise LicenseException(f"License not found: {license_key}")
                elif response.status == 401:
                    raise LicenseException("Invalid license key")
                else:
                    error_text = await response.text()
                    raise LicenseException(
                        f"License server error ({response.status}): {error_text}"
                    )

        except asyncio.TimeoutError:
            raise LicenseException("License server request timed out")
        except aiohttp.ClientError as e:
            raise LicenseException(f"License server connection failed: {str(e)}")

    async def invalidate_cache(self, community_id: int) -> None:
        """
        Invalidate cached license for a community.

        Args:
            community_id: Community ID
        """
        cache_key = f"{self.CACHE_KEY_PREFIX}:community:{community_id}"

        try:
            if self._redis_connected:
                await self._redis.delete(cache_key)
            else:
                self._cache.pop(cache_key, None)

            self.logger.system(
                f"License cache invalidated for community {community_id}",
                action="license_cache_invalidate",
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(
                f"Cache invalidation failed: {str(e)}",
                extra={"event_type": "ERROR", "result": "FAILURE"}
            )
