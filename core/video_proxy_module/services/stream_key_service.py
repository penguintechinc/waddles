"""Stream key management service."""

import secrets
import hashlib
import logging
from typing import Optional, Dict, Any
from datetime import datetime

from .database import get_db

logger = logging.getLogger(__name__)


async def generate_stream_key(community_id: str) -> Dict[str, Any]:
    """
    Generate a unique stream key for a community.
    
    Args:
        community_id: Unique community identifier
        
    Returns:
        Dict containing stream_key, config_id, and other metadata
        
    Raises:
        ValueError: If community already has an active stream key
    """
    db = get_db()

    # Check if community already has a config
    existing = db(db.stream_configurations.community_id == community_id).select().first()
    if existing and existing.is_active:
        raise ValueError(f"Community {community_id} already has an active stream key")

    # Generate secure random stream key
    stream_key = secrets.token_urlsafe(32)
    stream_key_hash = hashlib.sha256(stream_key.encode()).hexdigest()

    try:
        config_id = db.stream_configurations.insert(
            community_id=community_id,
            stream_key=stream_key,
            stream_key_hash=stream_key_hash,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()

        logger.info(f"Generated stream key for community {community_id}")

        return {
            "config_id": config_id,
            "community_id": community_id,
            "stream_key": stream_key,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to generate stream key: {e}")
        raise


async def validate_stream_key(stream_key: str) -> Optional[Dict[str, Any]]:
    """
    Validate a stream key and return associated configuration.
    
    Args:
        stream_key: Stream key to validate
        
    Returns:
        Configuration dict if valid and active, None otherwise
    """
    db = get_db()

    config = db(db.stream_configurations.stream_key == stream_key).select().first()

    if not config or not config.is_active:
        logger.warning("Invalid or inactive stream key attempted")
        return None

    return {
        "config_id": config.id,
        "community_id": config.community_id,
        "stream_key": config.stream_key,
        "is_active": config.is_active,
        "max_bitrate": config.max_bitrate,
        "max_resolution": config.max_resolution,
    }


async def regenerate_stream_key(community_id: str) -> Dict[str, Any]:
    """
    Regenerate stream key for a community, invalidating the old one.
    
    Args:
        community_id: Community identifier
        
    Returns:
        New stream key configuration
        
    Raises:
        ValueError: If community has no existing configuration
    """
    db = get_db()

    # Find existing config
    existing = db(db.stream_configurations.community_id == community_id).select().first()
    if not existing:
        raise ValueError(f"No stream configuration found for community {community_id}")

    # Generate new key
    new_stream_key = secrets.token_urlsafe(32)
    new_stream_key_hash = hashlib.sha256(new_stream_key.encode()).hexdigest()

    try:
        # Update existing config
        db(db.stream_configurations.id == existing.id).update(
            stream_key=new_stream_key,
            stream_key_hash=new_stream_key_hash,
            is_active=True,
            updated_at=datetime.utcnow(),
        )
        db.commit()

        logger.info(f"Regenerated stream key for community {community_id}")

        return {
            "config_id": existing.id,
            "community_id": community_id,
            "stream_key": new_stream_key,
            "is_active": True,
            "updated_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to regenerate stream key: {e}")
        raise


async def get_stream_config(community_id: str) -> Optional[Dict[str, Any]]:
    """
    Get stream configuration for a community.
    
    Args:
        community_id: Community identifier
        
    Returns:
        Configuration dict if exists, None otherwise
    """
    db = get_db()

    config = db(db.stream_configurations.community_id == community_id).select().first()

    if not config:
        return None

    return {
        "config_id": config.id,
        "community_id": config.community_id,
        "stream_key": config.stream_key,
        "is_active": config.is_active,
        "max_bitrate": config.max_bitrate,
        "max_resolution": config.max_resolution,
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }
