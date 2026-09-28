"""
Channel Sharding for 1000+ Channel Support

Implements consistent hashing for distributing channels across multiple
receiver module instances (pods).
"""

import hashlib
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class ConsistentHashRing:
    """
    Consistent hash ring for channel distribution.

    Features:
    - Minimal redistribution on node add/remove
    - Virtual nodes for better distribution
    - Deterministic channel→pod assignment
    """

    def __init__(self, virtual_nodes: int = 150):
        """
        Initialize hash ring.

        Args:
            virtual_nodes: Number of virtual nodes per physical node
        """
        self.virtual_nodes = virtual_nodes
        self.ring: Dict[int, str] = {}
        self.sorted_keys: List[int] = []
        self.nodes: set = set()

    def _hash(self, key: str) -> int:
        """Map a key onto the ring.

        MD5 is used purely to spread keys evenly across virtual nodes, never
        to protect anything, so it is flagged as not-for-security.
        """
        return int(
            hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(), 16
        )

    def add_node(self, node_id: str):
        """
        Add a node (pod) to the ring.

        Args:
            node_id: Unique node identifier (e.g., pod-0, pod-1)
        """
        if node_id in self.nodes:
            logger.warning(f"Node {node_id} already in ring")
            return

        self.nodes.add(node_id)

        # Add virtual nodes
        for i in range(self.virtual_nodes):
            virtual_key = f"{node_id}:{i}"
            hash_value = self._hash(virtual_key)
            self.ring[hash_value] = node_id

        # Re-sort keys
        self.sorted_keys = sorted(self.ring.keys())

        logger.info(
            f"Added node {node_id} with {self.virtual_nodes} virtual nodes"
        )

    def remove_node(self, node_id: str):
        """
        Remove a node from the ring.

        Args:
            node_id: Node identifier to remove
        """
        if node_id not in self.nodes:
            logger.warning(f"Node {node_id} not in ring")
            return

        self.nodes.remove(node_id)

        # Remove all virtual nodes
        keys_to_remove = [
            k for k, v in self.ring.items() if v == node_id
        ]
        for key in keys_to_remove:
            del self.ring[key]

        # Re-sort keys
        self.sorted_keys = sorted(self.ring.keys())

        logger.info(f"Removed node {node_id}")

    def get_node(self, channel_id: str) -> Optional[str]:
        """
        Get node responsible for channel.

        Args:
            channel_id: Channel identifier (e.g., twitch:12345)

        Returns:
            Node ID or None if no nodes
        """
        if not self.ring:
            logger.error("No nodes in ring")
            return None

        hash_value = self._hash(channel_id)

        # Find first node with hash >= channel hash
        for key in self.sorted_keys:
            if key >= hash_value:
                return self.ring[key]

        # Wrap around to first node
        return self.ring[self.sorted_keys[0]]

    def get_all_nodes(self) -> List[str]:
        """Get list of all nodes in ring."""
        return sorted(list(self.nodes))

    def get_channel_distribution(
        self,
        channels: List[str]
    ) -> Dict[str, List[str]]:
        """
        Get distribution of channels across nodes.

        Args:
            channels: List of channel IDs

        Returns:
            Dictionary mapping node_id -> list of channels
        """
        distribution: Dict[str, List[str]] = {
            node: [] for node in self.nodes
        }

        for channel in channels:
            node = self.get_node(channel)
            if node:
                distribution[node].append(channel)

        return distribution


class ChannelShardManager:
    """
    Manages channel sharding across receiver module instances.

    Features:
    - Automatic channel assignment
    - Ownership tracking
    - Graceful rebalancing on scale events
    - Health monitoring
    """

    def __init__(self, dal, redis_client, pod_id: str, total_pods: int):
        """
        Initialize shard manager.

        Args:
            dal: Database access layer
            redis_client: Redis client for coordination
            pod_id: This pod's identifier (e.g., receiver-twitch-0)
            total_pods: Total number of pods
        """
        self.dal = dal
        self.redis = redis_client
        self.pod_id = pod_id
        self.total_pods = total_pods
        self.hash_ring = ConsistentHashRing()

        # Initialize hash ring with all pods
        for i in range(total_pods):
            self.hash_ring.add_node(f"pod-{i}")

    async def get_my_channels(self, platform: str) -> List[Dict[str, Any]]:
        """
        Get channels assigned to this pod.

        Args:
            platform: Platform name (twitch, discord, etc.)

        Returns:
            List of channel dictionaries
        """
        try:
            # Get all channels for platform
            result = self.dal.executesql(
                """SELECT id, platform_server_id, platform_server_name,
                          community_id
                   FROM community_servers
                   WHERE platform = %s AND status = 'approved'""",
                [platform]
            )

            all_channels = [
                {
                    'id': row[0],
                    'server_id': row[1],
                    'server_name': row[2],
                    'community_id': row[3]
                }
                for row in result
            ]

            # Filter channels assigned to this pod
            my_channels = []
            for channel in all_channels:
                channel_key = f"{platform}:{channel['server_id']}"
                assigned_pod = self.hash_ring.get_node(channel_key)

                if assigned_pod == self.pod_id:
                    my_channels.append(channel)

            logger.info(
                f"Pod {self.pod_id} responsible for "
                f"{len(my_channels)}/{len(all_channels)} channels"
            )

            return my_channels

        except Exception as e:
            logger.error(f"Failed to get channels: {e}")
            return []

    async def claim_channel_ownership(
        self,
        channel_id: str,
        ttl_seconds: int = 60
    ) -> bool:
        """
        Claim ownership of a channel with distributed lock.

        Args:
            channel_id: Channel identifier
            ttl_seconds: Lock TTL

        Returns:
            True if ownership claimed
        """
        try:
            lock_key = f"waddlebot:shard:lock:{channel_id}"
            result = await self.redis.set(
                lock_key,
                self.pod_id,
                ex=ttl_seconds,
                nx=True  # Only set if not exists
            )

            if result:
                logger.debug(f"Claimed ownership of {channel_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to claim ownership: {e}")
            return False

    async def renew_channel_ownership(
        self,
        channel_id: str,
        ttl_seconds: int = 60
    ) -> bool:
        """
        Renew ownership lock for a channel.

        Args:
            channel_id: Channel identifier
            ttl_seconds: Lock TTL

        Returns:
            True if renewed
        """
        try:
            lock_key = f"waddlebot:shard:lock:{channel_id}"

            # Check current owner
            current_owner = await self.redis.get(lock_key)
            if current_owner != self.pod_id:
                logger.warning(
                    f"Cannot renew {channel_id} - owned by {current_owner}"
                )
                return False

            # Renew
            await self.redis.expire(lock_key, ttl_seconds)
            return True

        except Exception as e:
            logger.error(f"Failed to renew ownership: {e}")
            return False

    async def release_channel_ownership(self, channel_id: str) -> bool:
        """
        Release ownership of a channel.

        Args:
            channel_id: Channel identifier

        Returns:
            True if released
        """
        try:
            lock_key = f"waddlebot:shard:lock:{channel_id}"

            # Only delete if we own it
            current_owner = await self.redis.get(lock_key)
            if current_owner == self.pod_id:
                await self.redis.delete(lock_key)
                logger.info(f"Released ownership of {channel_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to release ownership: {e}")
            return False

    async def rebalance_on_scale_event(self):
        """
        Rebalance channels when pods are added/removed.

        This should be called when Kubernetes HPA scales the deployment.
        """
        try:
            # Get current pod count from Kubernetes
            # (In production, query k8s API or use service discovery)

            logger.info("Rebalancing channels across pods...")

            # Get all my current channels
            my_channels = await self.get_my_channels('twitch')

            # Check if each channel still belongs to me
            channels_to_release = []
            for channel in my_channels:
                channel_key = f"twitch:{channel['server_id']}"
                assigned_pod = self.hash_ring.get_node(channel_key)

                if assigned_pod != self.pod_id:
                    channels_to_release.append(channel_key)

            # Release channels that no longer belong to us
            for channel_id in channels_to_release:
                await self.release_channel_ownership(channel_id)
                logger.info(f"Released {channel_id} during rebalance")

            logger.info(
                f"Rebalance complete: released {len(channels_to_release)} "
                f"channels"
            )

        except Exception as e:
            logger.error(f"Rebalance failed: {e}")

    def get_shard_statistics(self) -> Dict[str, Any]:
        """
        Get sharding statistics.

        Returns:
            Statistics dictionary
        """
        distribution = self.hash_ring.get_channel_distribution([])

        return {
            'pod_id': self.pod_id,
            'total_pods': self.total_pods,
            'nodes_in_ring': len(self.hash_ring.nodes),
            'virtual_nodes_per_physical': self.hash_ring.virtual_nodes
        }
