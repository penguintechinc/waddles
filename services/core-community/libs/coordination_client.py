"""
Coordination client for collector modules to interact with the router coordination system
Handles claiming entities, status updates, and heartbeats
"""

import os
import uuid
import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class EntityStatus:
    """Entity status information"""
    entity_id: str
    is_live: bool = False
    viewer_count: int = 0
    metadata: Dict = None
    has_activity: bool = False

class CoordinationClient:
    """Client for interacting with router coordination system"""
    
    def __init__(self, 
                 router_url: str,
                 platform: str,
                 container_id: str = None,
                 max_claims: int = 5,
                 heartbeat_interval: int = 300):
        self.router_url = router_url.rstrip('/')
        self.platform = platform
        self.container_id = container_id or self.generate_container_id()
        self.max_claims = max_claims
        self.heartbeat_interval = heartbeat_interval
        
        # State
        self.claimed_entities = []
        self.session = None
        self.heartbeat_task = None
        
        logger.info(f"CoordinationClient initialized: {self.container_id} for {platform}")
    
    def generate_container_id(self) -> str:
        """Generate unique container ID"""
        hostname = os.environ.get('HOSTNAME', 'unknown')
        platform = self.platform
        unique_id = uuid.uuid4().hex[:8]
        timestamp = int(datetime.utcnow().timestamp())
        
        return f"{platform}_{hostname}_{unique_id}_{timestamp}"
    
    async def get_session(self):
        """Get or create HTTP session"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session
    
    async def start(self):
        """Start the coordination client"""
        try:
            # Claim initial entities
            await self.claim_entities()
            
            # Start checkin/heartbeat task
            self.heartbeat_task = asyncio.create_task(self.checkin_loop())
            
            logger.info(f"CoordinationClient started with {len(self.claimed_entities)} entities")
            
        except Exception as e:
            logger.error(f"Error starting coordination client: {str(e)}")
            raise
    
    async def stop(self):
        """Stop the coordination client and release entities"""
        try:
            # Cancel heartbeat task
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
                try:
                    await self.heartbeat_task
                except asyncio.CancelledError:
                    pass
            
            # Release claimed entities
            await self.release_entities()
            
            # Close session
            if self.session and not self.session.closed:
                await self.session.close()
            
            logger.info("CoordinationClient stopped")
            
        except Exception as e:
            logger.error(f"Error stopping coordination client: {str(e)}")
    
    async def claim_entities(self) -> List[Dict]:
        """Claim available entities from coordination system"""
        try:
            session = await self.get_session()
            
            payload = {
                "platform": self.platform,
                "container_id": self.container_id,
                "max_claims": self.max_claims
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/claim",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("success"):
                        self.claimed_entities = result.get("claimed_entities", [])
                        logger.info(f"Claimed {len(self.claimed_entities)} entities")
                        return self.claimed_entities
                    else:
                        logger.warning(f"Failed to claim entities: {result.get('error_message')}")
                        return []
                else:
                    logger.error(f"Error claiming entities: HTTP {response.status}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error claiming entities: {str(e)}")
            return []
    
    async def release_entities(self, entity_ids: List[str] = None) -> bool:
        """Release claimed entities"""
        try:
            session = await self.get_session()
            
            payload = {
                "container_id": self.container_id,
                "entity_ids": entity_ids
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/release",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("success"):
                        if entity_ids:
                            # Remove specific entities from claimed list
                            self.claimed_entities = [
                                e for e in self.claimed_entities 
                                if e.get("entity_id") not in entity_ids
                            ]
                        else:
                            # Released all entities
                            self.claimed_entities = []
                        
                        logger.info(f"Released entities: {entity_ids or 'all'}")
                        return True
                    else:
                        logger.error("Failed to release entities")
                        return False
                else:
                    logger.error(f"Error releasing entities: HTTP {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error releasing entities: {str(e)}")
            return False
    
    async def update_entity_status(self, entity_status: EntityStatus) -> bool:
        """Update status of a claimed entity"""
        try:
            session = await self.get_session()
            
            payload = {
                "container_id": self.container_id,
                "entity_id": entity_status.entity_id,
                "is_live": entity_status.is_live,
                "viewer_count": entity_status.viewer_count,
                "metadata": entity_status.metadata,
                "has_activity": entity_status.has_activity
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/status",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("success", False)
                else:
                    logger.error(f"Error updating entity status: HTTP {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error updating entity status: {str(e)}")
            return False
    
    async def report_error(self, entity_id: str, error_message: str) -> bool:
        """Report error for an entity"""
        try:
            session = await self.get_session()
            
            payload = {
                "container_id": self.container_id,
                "entity_id": entity_id,
                "error_message": error_message
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/error",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("success", False)
                else:
                    logger.error(f"Error reporting entity error: HTTP {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error reporting entity error: {str(e)}")
            return False
    
    async def checkin(self) -> bool:
        """Send checkin to maintain claims"""
        try:
            session = await self.get_session()
            
            payload = {
                "container_id": self.container_id
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/checkin",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.debug(f"Checkin successful for container {self.container_id}")
                    return result.get("success", False)
                else:
                    logger.error(f"Checkin failed: HTTP {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error sending checkin: {str(e)}")
            return False

    async def heartbeat(self, extend_claims: bool = True) -> Dict:
        """Send heartbeat to coordination system"""
        try:
            session = await self.get_session()
            
            payload = {
                "container_id": self.container_id,
                "extend_claims": extend_claims
            }
            
            async with session.post(
                f"{self.router_url}/router/coordination/heartbeat",
                json=payload
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    
                    # Update local claimed entities list
                    if "entities" in result:
                        entity_ids = [e["entity_id"] for e in result["entities"]]
                        self.claimed_entities = [
                            e for e in self.claimed_entities 
                            if e.get("entity_id") in entity_ids
                        ]
                    
                    logger.debug(f"Heartbeat successful: {result.get('claimed_count', 0)} entities")
                    return result
                else:
                    logger.error(f"Heartbeat failed: HTTP {response.status}")
                    return {"error": f"HTTP {response.status}"}
                    
        except Exception as e:
            logger.error(f"Error sending heartbeat: {str(e)}")
            return {"error": str(e)}
    
    async def checkin_loop(self):
        """Background checkin loop - checks in every 5 minutes and manages offline entities"""
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                
                # Send checkin to maintain claims
                await self.checkin()
                
                # Release offline entities and claim new ones
                await self.check_and_release_offline_entities()
                
                # Reclaim if we have fewer entities than max
                await self.reclaim_if_needed()
                
            except asyncio.CancelledError:
                logger.info("Checkin loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in checkin loop: {str(e)}")
                # Continue the loop even if checkin fails
                continue
    
    async def check_and_release_offline_entities(self):
        """Check for offline entities and release them"""
        try:
            offline_entities = []
            
            # Check each claimed entity to see if it's still live
            for entity in self.claimed_entities.copy():
                entity_id = entity.get("entity_id")
                if not entity_id:
                    continue
                
                # This would be platform-specific logic to check if entity is live
                # For now, we'll simulate this check
                is_live = await self.check_entity_live_status(entity_id)
                
                # Update entity status
                status = EntityStatus(
                    entity_id=entity_id,
                    is_live=is_live,
                    has_activity=True  # Would be determined by recent activity
                )
                await self.update_entity_status(status)
                
                # If entity went offline, mark for release
                if not is_live and entity.get("is_live", True):
                    offline_entities.append(entity_id)
                    logger.info(f"Entity went offline: {entity_id}")
            
            # Release offline entities
            if offline_entities:
                await self.release_entities(offline_entities)
                logger.info(f"Released {len(offline_entities)} offline entities")
                
        except Exception as e:
            logger.error(f"Error checking offline entities: {str(e)}")
    
    async def check_entity_live_status(self, entity_id: str) -> bool:
        """Check if an entity is currently live (platform-specific implementation needed)"""
        # This is a placeholder - actual implementation would depend on platform
        # For Twitch: Check stream status via API
        # For Discord: Check if server/channel is active
        # For Slack: Check if workspace/channel is active
        
        # Simulate some entities going offline
        import random
        return random.random() > 0.1  # 90% chance of being live
    
    def get_claimed_entities(self) -> List[Dict]:
        """Get currently claimed entities"""
        return self.claimed_entities.copy()
    
    def get_entity_ids(self) -> List[str]:
        """Get list of claimed entity IDs"""
        return [entity.get("entity_id") for entity in self.claimed_entities]
    
    def is_entity_claimed(self, entity_id: str) -> bool:
        """Check if specific entity is claimed by this container"""
        return entity_id in self.get_entity_ids()
    
    async def reclaim_if_needed(self) -> bool:
        """Reclaim entities if we have fewer than max_claims"""
        try:
            current_count = len(self.claimed_entities)
            if current_count < self.max_claims:
                logger.info(f"Reclaiming entities: {current_count}/{self.max_claims}")
                new_entities = await self.claim_entities()
                return len(new_entities) > current_count
            return False
            
        except Exception as e:
            logger.error(f"Error reclaiming entities: {str(e)}")
            return False

# Example usage functions for collector modules

async def create_coordination_client(platform: str, max_claims: int = None) -> CoordinationClient:
    """Create and start coordination client for a collector module"""
    
    # Get configuration from environment
    router_url = os.environ.get("CORE_API_URL", "http://router-service:8000")
    container_id = os.environ.get("CONTAINER_ID")  # Optional override
    max_claims = max_claims or int(os.environ.get("MAX_CLAIMS", "5"))
    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
    
    client = CoordinationClient(
        router_url=router_url,
        platform=platform,
        container_id=container_id,
        max_claims=max_claims,
        heartbeat_interval=heartbeat_interval
    )
    
    await client.start()
    return client

async def coordination_example_usage():
    """Example of how to use coordination client in a collector module"""
    
    # Create client
    client = await create_coordination_client("twitch", max_claims=5)
    
    try:
        # Get claimed entities
        entities = client.get_claimed_entities()
        print(f"Claimed {len(entities)} entities")
        
        # Process each entity
        for entity in entities:
            entity_id = entity["entity_id"]
            print(f"Processing entity: {entity_id}")
            
            # Simulate checking if stream is live
            is_live = True  # Would check Twitch API
            viewer_count = 150  # Would get from Twitch API
            
            # Update entity status
            status = EntityStatus(
                entity_id=entity_id,
                is_live=is_live,
                viewer_count=viewer_count,
                has_activity=True,
                metadata={"game": "Just Chatting", "language": "en"}
            )
            
            await client.update_entity_status(status)
        
        # The heartbeat loop runs automatically in background
        
        # Simulate running for some time
        await asyncio.sleep(60)
        
    finally:
        # Clean shutdown
        await client.stop()

if __name__ == "__main__":
    # For testing
    asyncio.run(coordination_example_usage())