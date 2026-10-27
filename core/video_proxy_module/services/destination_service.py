"""Stream destination management service."""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from .database import get_db

logger = logging.getLogger(__name__)


async def add_destination(
    config_id: int,
    platform: str,
    rtmp_url: str,
    stream_key: str,
    resolution: str = "1080p",
    bitrate: int = 6000,
) -> Dict[str, Any]:
    """
    Add a new stream destination.
    
    Args:
        config_id: Stream configuration ID
        platform: Platform name (twitch, youtube, facebook, custom)
        rtmp_url: RTMP server URL
        stream_key: Platform stream key
        resolution: Output resolution (1080p, 2K, 4K)
        bitrate: Output bitrate in kbps
        
    Returns:
        Created destination dict
        
    Raises:
        ValueError: If config_id doesn't exist or validation fails
    """
    db = get_db()

    # Validate config exists
    config = db(db.stream_configurations.id == config_id).select().first()
    if not config:
        raise ValueError(f"Stream configuration {config_id} not found")

    # Validate resolution
    valid_resolutions = ["1080p", "2K", "4K"]
    if resolution not in valid_resolutions:
        raise ValueError(f"Invalid resolution. Must be one of: {valid_resolutions}")

    try:
        dest_id = db.stream_destinations.insert(
            config_id=config_id,
            platform=platform,
            rtmp_url=rtmp_url,
            stream_key=stream_key,
            resolution=resolution,
            bitrate=bitrate,
            is_enabled=True,
            force_cut=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.commit()

        logger.info(f"Added destination {dest_id} for config {config_id}: {platform} @ {resolution}")

        return {
            "destination_id": dest_id,
            "config_id": config_id,
            "platform": platform,
            "rtmp_url": rtmp_url,
            "resolution": resolution,
            "bitrate": bitrate,
            "is_enabled": True,
            "force_cut": False,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to add destination: {e}")
        raise


async def remove_destination(destination_id: int) -> bool:
    """
    Remove a stream destination.
    
    Args:
        destination_id: Destination ID to remove
        
    Returns:
        True if removed, False if not found
    """
    db = get_db()

    try:
        deleted = db(db.stream_destinations.id == destination_id).delete()
        db.commit()

        if deleted:
            logger.info(f"Removed destination {destination_id}")
            return True
        else:
            logger.warning(f"Destination {destination_id} not found")
            return False
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to remove destination: {e}")
        raise


async def update_destination(destination_id: int, **kwargs) -> Optional[Dict[str, Any]]:
    """
    Update destination properties.
    
    Args:
        destination_id: Destination ID
        **kwargs: Fields to update (platform, rtmp_url, stream_key, resolution, bitrate, is_enabled)
        
    Returns:
        Updated destination dict, or None if not found
    """
    db = get_db()

    # Validate destination exists
    dest = db(db.stream_destinations.id == destination_id).select().first()
    if not dest:
        logger.warning(f"Destination {destination_id} not found")
        return None

    # Filter allowed fields
    allowed_fields = {"platform", "rtmp_url", "stream_key", "resolution", "bitrate", "is_enabled", "force_cut"}
    update_data = {k: v for k, v in kwargs.items() if k in allowed_fields}

    if not update_data:
        logger.warning("No valid fields to update")
        return None

    # Add updated timestamp
    update_data["updated_at"] = datetime.utcnow()

    try:
        db(db.stream_destinations.id == destination_id).update(**update_data)
        db.commit()

        # Fetch updated record
        updated = db(db.stream_destinations.id == destination_id).select().first()

        logger.info(f"Updated destination {destination_id}: {list(update_data.keys())}")

        return {
            "destination_id": updated.id,
            "config_id": updated.config_id,
            "platform": updated.platform,
            "rtmp_url": updated.rtmp_url,
            "resolution": updated.resolution,
            "bitrate": updated.bitrate,
            "is_enabled": updated.is_enabled,
            "force_cut": updated.force_cut,
            "updated_at": updated.updated_at.isoformat() if updated.updated_at else None,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update destination: {e}")
        raise


async def list_destinations(config_id: int) -> List[Dict[str, Any]]:
    """
    List all destinations for a stream configuration.
    
    Args:
        config_id: Stream configuration ID
        
    Returns:
        List of destination dicts
    """
    db = get_db()

    destinations = db(db.stream_destinations.config_id == config_id).select()

    return [
        {
            "destination_id": dest.id,
            "config_id": dest.config_id,
            "platform": dest.platform,
            "rtmp_url": dest.rtmp_url,
            "resolution": dest.resolution,
            "bitrate": dest.bitrate,
            "is_enabled": dest.is_enabled,
            "force_cut": dest.force_cut,
            "created_at": dest.created_at.isoformat() if dest.created_at else None,
        }
        for dest in destinations
    ]


async def set_force_cut(destination_id: int, enabled: bool) -> bool:
    """
    Enable or disable force cut for a destination.
    
    Args:
        destination_id: Destination ID
        enabled: True to enable force cut, False to disable
        
    Returns:
        True if updated, False if not found
    """
    db = get_db()

    try:
        updated = db(db.stream_destinations.id == destination_id).update(
            force_cut=enabled,
            updated_at=datetime.utcnow(),
        )
        db.commit()

        if updated:
            logger.info(f"Set force_cut={enabled} for destination {destination_id}")
            return True
        else:
            logger.warning(f"Destination {destination_id} not found")
            return False
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set force_cut: {e}")
        raise


async def count_2k_destinations(config_id: int) -> int:
    """
    Count number of 2K+ resolution destinations for license limit checking.
    
    Args:
        config_id: Stream configuration ID
        
    Returns:
        Count of 2K and 4K destinations
    """
    db = get_db()

    count = db(
        (db.stream_destinations.config_id == config_id) &
        (db.stream_destinations.resolution.belongs(["2K", "4K"]))
    ).count()

    return count
