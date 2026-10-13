"""License validation and tier management service."""

import os
import logging
import httpx
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Tier limits
FREE_LIMITS = {
    "max_destinations": 3,
    "max_2k_destinations": 0,
    "max_bitrate": 6000,
    "analytics_retention_days": 7,
}

PREMIUM_LIMITS = {
    "max_destinations": 10,
    "max_2k_destinations": 5,
    "max_bitrate": 15000,
    "analytics_retention_days": 90,
}

LICENSE_SERVER_URL = os.getenv("LICENSE_SERVER_URL", "https://license.penguintech.io")
RELEASE_MODE = os.getenv("RELEASE_MODE", "false").lower() == "true"


async def is_premium(community_id: str, request_domain: str) -> bool:
    """
    Check if community has premium license.
    
    Args:
        community_id: Community identifier
        request_domain: Request domain (waddlebot.penguintech.io gets free premium)
        
    Returns:
        True if premium, False otherwise
    """
    # Known beta/cloud hostnames always get premium (no license key required)
    PREMIUM_BYPASS_DOMAINS = {
        "waddlebot.penguintech.io",
        "waddles.penguintech.cloud",
        "waddles.penguincloud.io",
    }
    domain_lower = request_domain.lower()
    if any(d in domain_lower for d in PREMIUM_BYPASS_DOMAINS):
        logger.info(f"Premium granted for bypass domain: {request_domain}")
        return True

    # Development mode: all premium
    if not RELEASE_MODE:
        logger.info("Premium granted (development mode)")
        return True

    # Check license server
    license_key = os.getenv("LICENSE_KEY")
    if not license_key:
        logger.warning("No license key configured")
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{LICENSE_SERVER_URL}/api/v1/validate",
                json={
                    "license_key": license_key,
                    "community_id": community_id,
                    "product": "waddlebot-video-proxy",
                },
            )

            if response.status_code == 200:
                data = response.json()
                is_valid = data.get("valid", False)
                tier = data.get("tier", "free")

                logger.info(f"License validation: valid={is_valid}, tier={tier}")
                return is_valid and tier.lower() == "premium"
            else:
                logger.error(f"License server error: {response.status_code}")
                return False

    except httpx.TimeoutException:
        logger.error("License server timeout")
        return False
    except Exception as e:
        logger.error(f"License validation failed: {e}")
        return False


async def get_limits(community_id: str, request_domain: str) -> Dict[str, Any]:
    """
    Get tier limits for a community.
    
    Args:
        community_id: Community identifier
        request_domain: Request domain
        
    Returns:
        Dict of limits based on tier
    """
    if await is_premium(community_id, request_domain):
        logger.info(f"Using PREMIUM limits for {community_id}")
        return PREMIUM_LIMITS.copy()
    else:
        logger.info(f"Using FREE limits for {community_id}")
        return FREE_LIMITS.copy()


async def can_add_destination(community_id: str, current_count: int, request_domain: str = "") -> bool:
    """
    Check if community can add another destination.
    
    Args:
        community_id: Community identifier
        current_count: Current destination count
        request_domain: Request domain
        
    Returns:
        True if can add, False if at limit
    """
    limits = await get_limits(community_id, request_domain)
    max_destinations = limits["max_destinations"]

    can_add = current_count < max_destinations

    logger.info(
        f"Destination limit check: {current_count}/{max_destinations} - can_add={can_add}"
    )

    return can_add


async def can_add_2k_destination(
    community_id: str, current_2k_count: int, request_domain: str = ""
) -> bool:
    """
    Check if community can add a 2K+ resolution destination.
    
    Args:
        community_id: Community identifier
        current_2k_count: Current 2K+ destination count
        request_domain: Request domain
        
    Returns:
        True if can add, False if at limit
    """
    limits = await get_limits(community_id, request_domain)
    max_2k = limits["max_2k_destinations"]

    can_add = current_2k_count < max_2k

    logger.info(
        f"2K destination limit check: {current_2k_count}/{max_2k} - can_add={can_add}"
    )

    return can_add
