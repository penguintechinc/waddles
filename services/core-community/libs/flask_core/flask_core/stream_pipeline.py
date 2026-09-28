"""
Redis Streams Pipeline for Event-Driven Architecture

Provides a high-level event streaming pipeline with:
- Multiple event streams (inbound, commands, actions, responses)
- Dead letter queue support
- Consumer group management
- Event acknowledgment and retry logic
- Stream monitoring and diagnostics
"""

import os
import json
import logging
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict, field
from datetime import datetime

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Event wrapper for stream events"""
    id: str
    stream: str
    data: Dict[str, Any]
    retry_count: int = 0
    timestamp: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


class StreamPipeline:
    """
    Redis Streams Pipeline for Waddles event processing.

    Manages multiple event streams with dedicated purposes:
    - inbound: External events entering the system
    - commands: User commands to be executed
    - actions: System actions to be performed
    - responses: Responses to be sent back

    Features:
    - Configurable stream names
    - Dead letter queue for failed events
    - Consumer group management
    - Event acknowledgment
    - Retry logic with max attempts
    - Stream monitoring and diagnostics
    """

    # Default stream names
    STREAM_INBOUND = "events:inbound"
    STREAM_COMMANDS = "events:commands"
    STREAM_ACTIONS = "events:actions"
    STREAM_RESPONSES = "events:responses"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream_prefix: str = "waddlebot:stream",
        dlq_prefix: str = "waddlebot:dlq",
        max_retries: Optional[int] = None,
        batch_size: Optional[int] = None,
        block_ms: Optional[int] = None,
        enabled: Optional[bool] = None
    ):
        """
        Initialize stream pipeline.

        Args:
            redis_url: Redis connection URL
            stream_prefix: Prefix for stream names
            dlq_prefix: Prefix for dead letter queue names
            max_retries: Maximum retry attempts (from env or default: 3)
            batch_size: Events per batch (from env or default: 10)
            block_ms: Block time in ms (from env or default: 5000)
            enabled: Enable pipeline (from env or default: False)
        """
        self.redis_url = redis_url
        self.stream_prefix = stream_prefix
        self.dlq_prefix = dlq_prefix

        # Configuration from environment with defaults
        self.enabled = enabled if enabled is not None else self._get_env_bool('STREAM_PIPELINE_ENABLED', False)
        self.max_retries = max_retries if max_retries is not None else int(os.getenv('STREAM_MAX_RETRIES', '3'))
        self.batch_size = batch_size if batch_size is not None else int(os.getenv('STREAM_BATCH_SIZE', '10'))
        self.block_ms = block_ms if block_ms is not None else int(os.getenv('STREAM_BLOCK_MS', '5000'))

        self._redis: Optional[redis.Redis] = None
        self._connected = False
        self._running = False

        logger.info(
            f"StreamPipeline initialized: enabled={self.enabled}, "
            f"max_retries={self.max_retries}, batch_size={self.batch_size}, "
            f"block_ms={self.block_ms}"
        )

    @staticmethod
    def _get_env_bool(key: str, default: bool = False) -> bool:
        """Get boolean value from environment variable"""
        value = os.getenv(key, '').lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        elif value in ('false', '0', 'no', 'off'):
            return False
        return default

    async def connect(self):
        """Connect to Redis (call during startup)"""
        if not self.enabled:
            logger.info("Stream pipeline is disabled")
            return

        if not REDIS_AVAILABLE:
            logger.error("redis package not available - cannot enable stream pipeline")
            self.enabled = False
            return

        if not self.redis_url:
            logger.error("No Redis URL provided for stream pipeline")
            self.enabled = False
            return

        try:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )

            # Test connection
            await self._redis.ping()
            self._connected = True
            logger.info(f"Connected to Redis stream pipeline: {self.stream_prefix}")

        except Exception as e:
            logger.error(f"Failed to connect to Redis stream pipeline: {e}")
            self.enabled = False
            raise

    async def disconnect(self):
        """Disconnect from Redis"""
        self._running = False

        if self._redis:
            await self._redis.close()
            self._connected = False
            logger.info("Disconnected from Redis stream pipeline")

    def _make_stream_name(self, stream: str) -> str:
        """Create full stream name with prefix"""
        return f"{self.stream_prefix}:{stream}"

    def _make_dlq_name(self, stream: str) -> str:
        """Create dead letter queue name for a stream"""
        # Extract just the stream name without prefix
        stream_name = stream.replace(f"{self.stream_prefix}:", "")
        return f"{self.dlq_prefix}:{stream_name}"

    async def publish_event(
        self,
        stream_name: str,
        event_data: Dict[str, Any],
        max_len: int = 10000
    ) -> Optional[str]:
        """
        Publish event to a stream.

        Args:
            stream_name: Stream name (e.g., 'events:inbound', 'events:commands')
            event_data: Event data (must be JSON-serializable)
            max_len: Maximum stream length (oldest events trimmed)

        Returns:
            Message ID or None on error

        Example:
            message_id = await pipeline.publish_event(
                'events:commands',
                {'command': 'translate', 'text': 'Hello'}
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return None

        full_stream_name = self._make_stream_name(stream_name)

        try:
            # Serialize event data with timestamp
            serialized = {
                'data': json.dumps(event_data),
                'timestamp': datetime.utcnow().isoformat(),
                'retry_count': '0'
            }

            # Add to stream with automatic trimming
            message_id = await self._redis.xadd(
                full_stream_name,
                serialized,
                maxlen=max_len,
                approximate=True  # Allow approximate trimming for performance
            )

            logger.debug(f"Published event to {stream_name}: {message_id}")
            return message_id

        except Exception as e:
            logger.error(f"Failed to publish event to {stream_name}: {e}")
            return None

    async def consume_events(
        self,
        stream_name: str,
        consumer_group: str,
        consumer_name: str,
        count: Optional[int] = None,
        block_ms: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Consume events from a stream (single batch).

        Args:
            stream_name: Stream name to consume from
            consumer_group: Consumer group name
            consumer_name: Unique consumer identifier
            count: Number of events to fetch (default: batch_size)
            block_ms: Block time in ms (default: self.block_ms)

        Returns:
            List of event dictionaries with 'id', 'stream', 'data', 'retry_count'

        Example:
            events = await pipeline.consume_events(
                'events:commands',
                'router-group',
                'router-1'
            )
            for event in events:
                print(f"Processing: {event['data']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)
        count = count if count is not None else self.batch_size
        block_ms = block_ms if block_ms is not None else self.block_ms

        # Ensure consumer group exists
        await self.create_consumer_group(stream_name, consumer_group)

        try:
            # Read new messages
            messages = await self._redis.xreadgroup(
                consumer_group,
                consumer_name,
                {full_stream_name: '>'},
                count=count,
                block=block_ms
            )

            events = []
            if messages:
                for stream_data in messages:
                    _, message_list = stream_data

                    for message_id, message_data in message_list:
                        try:
                            # Deserialize event
                            data = json.loads(message_data.get('data', '{}'))
                            retry_count = int(message_data.get('retry_count', 0))
                            timestamp = message_data.get('timestamp')

                            events.append({
                                'id': message_id,
                                'stream': full_stream_name,
                                'data': data,
                                'retry_count': retry_count,
                                'timestamp': timestamp
                            })
                        except Exception as e:
                            logger.error(f"Failed to deserialize event {message_id}: {e}")

            return events

        except Exception as e:
            logger.error(f"Failed to consume events from {stream_name}: {e}")
            return []

    async def acknowledge_event(
        self,
        stream_name: str,
        consumer_group: str,
        message_id: str
    ) -> bool:
        """
        Acknowledge an event (marks it as processed).

        Args:
            stream_name: Stream name
            consumer_group: Consumer group name
            message_id: Message ID to acknowledge

        Returns:
            True if acknowledged successfully

        Example:
            success = await pipeline.acknowledge_event(
                'events:commands',
                'router-group',
                '1234567890-0'
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            ack_count = await self._redis.xack(
                full_stream_name,
                consumer_group,
                message_id
            )

            if ack_count > 0:
                logger.debug(f"Acknowledged event {message_id} from {stream_name}")
                return True
            else:
                logger.warning(f"Failed to acknowledge event {message_id} - may already be ACKed")
                return False

        except Exception as e:
            logger.error(f"Failed to acknowledge event {message_id}: {e}")
            return False

    async def move_to_dlq(
        self,
        stream_name: str,
        message_id: str,
        error_reason: str,
        event_data: Optional[Dict[str, Any]] = None,
        retry_count: int = 0
    ) -> bool:
        """
        Move an event to the dead letter queue.

        Args:
            stream_name: Original stream name
            message_id: Original message ID
            error_reason: Reason for failure
            event_data: Event data (if available)
            retry_count: Number of retry attempts

        Returns:
            True if moved successfully

        Example:
            success = await pipeline.move_to_dlq(
                'events:commands',
                '1234567890-0',
                'max_retries_exceeded',
                event_data={'command': 'translate'},
                retry_count=3
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)
        dlq_name = self._make_dlq_name(full_stream_name)

        try:
            # Prepare DLQ entry
            dlq_data = {
                'original_id': message_id,
                'original_stream': full_stream_name,
                'failure_reason': error_reason,
                'retry_count': str(retry_count),
                'timestamp': datetime.utcnow().isoformat()
            }

            # Include event data if provided
            if event_data:
                dlq_data['data'] = json.dumps(event_data)

            # Add to DLQ
            dlq_id = await self._redis.xadd(dlq_name, dlq_data)

            logger.warning(
                f"Event {message_id} moved to DLQ: {error_reason} "
                f"(retries: {retry_count})"
            )

            return dlq_id is not None

        except Exception as e:
            logger.error(f"Failed to move event to DLQ: {e}")
            return False

    async def create_consumer_group(
        self,
        stream_name: str,
        group_name: str,
        start_id: str = '0'
    ) -> bool:
        """
        Create a consumer group for a stream.

        Args:
            stream_name: Stream name
            group_name: Consumer group name
            start_id: Starting message ID ('0' = from beginning, '$' = new only)

        Returns:
            True if created or already exists

        Example:
            success = await pipeline.create_consumer_group(
                'events:commands',
                'router-group'
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            await self._redis.xgroup_create(
                full_stream_name,
                group_name,
                id=start_id,
                mkstream=True  # Create stream if it doesn't exist
            )
            logger.info(
                f"Created consumer group '{group_name}' for stream '{stream_name}'"
            )
            return True

        except redis.ResponseError as e:
            if 'BUSYGROUP' in str(e):
                # Group already exists
                logger.debug(
                    f"Consumer group '{group_name}' already exists for '{stream_name}'"
                )
                return True
            logger.error(f"Failed to create consumer group: {e}")
            return False

        except Exception as e:
            logger.error(f"Failed to create consumer group: {e}")
            return False

    async def get_pending_events(
        self,
        stream_name: str,
        consumer_group: str,
        consumer_name: Optional[str] = None,
        count: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get pending events (read but not acknowledged).

        Args:
            stream_name: Stream name
            consumer_group: Consumer group name
            consumer_name: Specific consumer (None = all consumers)
            count: Maximum number of pending events to return

        Returns:
            List of pending event info with message_id, consumer, idle_time, delivery_count

        Example:
            pending = await pipeline.get_pending_events(
                'events:commands',
                'router-group'
            )
            for event in pending:
                print(f"Pending: {event['message_id']}, retries: {event['delivery_count']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)

        try:
            # Get pending messages
            pending = await self._redis.xpending_range(
                full_stream_name,
                consumer_group,
                '-',
                '+',
                count=count,
                consumername=consumer_name
            )

            result = []
            for pending_msg in pending:
                result.append({
                    'message_id': pending_msg['message_id'],
                    'consumer': pending_msg['consumer'],
                    'idle_time_ms': pending_msg['time_since_delivered'],
                    'delivery_count': pending_msg['times_delivered']
                })

            return result

        except Exception as e:
            logger.error(f"Failed to get pending events: {e}")
            return []

    async def get_stream_info(self, stream_name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a stream.

        Args:
            stream_name: Stream name

        Returns:
            Stream info with length, first_entry, last_entry, consumer_groups, etc.

        Example:
            info = await pipeline.get_stream_info('events:commands')
            print(f"Stream length: {info['length']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return None

        full_stream_name = self._make_stream_name(stream_name)

        try:
            info = await self._redis.xinfo_stream(full_stream_name)

            # Convert to more readable format
            result = {
                'length': info.get('length', 0),
                'radix_tree_keys': info.get('radix-tree-keys', 0),
                'radix_tree_nodes': info.get('radix-tree-nodes', 0),
                'groups': info.get('groups', 0),
                'last_generated_id': info.get('last-generated-id'),
                'first_entry': info.get('first-entry'),
                'last_entry': info.get('last-entry')
            }

            return result

        except redis.ResponseError as e:
            if 'no such key' in str(e).lower():
                logger.debug(f"Stream '{stream_name}' does not exist yet")
                return {'length': 0, 'exists': False}
            logger.error(f"Failed to get stream info: {e}")
            return None

        except Exception as e:
            logger.error(f"Failed to get stream info: {e}")
            return None

    async def get_dlq_events(
        self,
        stream_name: str,
        count: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get events from dead letter queue.

        Args:
            stream_name: Original stream name
            count: Maximum number of DLQ events to return

        Returns:
            List of DLQ events with id, data, original_id, failure_reason, etc.

        Example:
            dlq_events = await pipeline.get_dlq_events('events:commands')
            for event in dlq_events:
                print(f"Failed: {event['failure_reason']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)
        dlq_name = self._make_dlq_name(full_stream_name)

        try:
            messages = await self._redis.xrange(dlq_name, count=count)

            result = []
            for message_id, message_data in messages:
                event = {
                    'id': message_id,
                    'original_id': message_data.get('original_id'),
                    'original_stream': message_data.get('original_stream'),
                    'failure_reason': message_data.get('failure_reason'),
                    'retry_count': int(message_data.get('retry_count', 0)),
                    'timestamp': message_data.get('timestamp')
                }

                # Include data if present
                if 'data' in message_data:
                    try:
                        event['data'] = json.loads(message_data['data'])
                    except Exception:
                        event['data'] = message_data['data']

                result.append(event)

            return result

        except Exception as e:
            logger.error(f"Failed to get DLQ events: {e}")
            return []

    async def trim_stream(
        self,
        stream_name: str,
        max_len: int = 1000,
        approximate: bool = True
    ) -> bool:
        """
        Trim a stream to a maximum length.

        Args:
            stream_name: Stream name to trim
            max_len: Maximum number of events to keep
            approximate: Allow approximate trimming (more efficient)

        Returns:
            True if trimmed successfully
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            await self._redis.xtrim(
                full_stream_name,
                maxlen=max_len,
                approximate=approximate
            )
            logger.info(f"Trimmed stream '{stream_name}' to max {max_len} events")
            return True

        except Exception as e:
            logger.error(f"Failed to trim stream: {e}")
            return False


def create_stream_pipeline(
    redis_url: str,
    stream_prefix: str = "waddlebot:stream",
    dlq_prefix: str = "waddlebot:dlq",
    max_retries: Optional[int] = None,
    batch_size: Optional[int] = None,
    block_ms: Optional[int] = None,
    enabled: Optional[bool] = None
) -> StreamPipeline:
    """
    Factory function to create a stream pipeline.

    Args:
        redis_url: Redis connection URL
        stream_prefix: Prefix for stream names
        dlq_prefix: Prefix for DLQ names
        max_retries: Maximum retry attempts
        batch_size: Events per batch
        block_ms: Block time in ms
        enabled: Enable pipeline

    Returns:
        Configured StreamPipeline instance

    Example:
        pipeline = create_stream_pipeline(
            redis_url='redis://localhost:6379',
            enabled=True
        )
        await pipeline.connect()
    """
    return StreamPipeline(
        redis_url=redis_url,
        stream_prefix=stream_prefix,
        dlq_prefix=dlq_prefix,
        max_retries=max_retries,
        batch_size=batch_size,
        block_ms=block_ms,
        enabled=enabled
    )
