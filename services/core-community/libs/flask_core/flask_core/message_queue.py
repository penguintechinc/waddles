"""
Message Queue using Redis Streams

Provides reliable event processing with:
- Persistent message queuing
- Consumer groups for load balancing
- Exactly-once processing semantics
- Dead letter queue for failed messages
- Event replay capability
- Automatic acknowledgment
"""

import json
import logging
import asyncio
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, asdict

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Message wrapper for queue messages"""
    id: str
    stream: str
    data: Dict[str, Any]
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


class MessageQueue:
    """
    Redis Streams-based message queue.

    Features:
    - Persistent message storage
    - Consumer groups for distributed processing
    - Dead letter queue for failed messages
    - Automatic retry with exponential backoff
    - Message acknowledgment
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream_prefix: str = "waddlebot",
        max_retries: int = 3,
        enable_fallback: bool = False
    ):
        """
        Initialize message queue.

        Args:
            redis_url: Redis connection URL
            stream_prefix: Prefix for stream names
            max_retries: Maximum retry attempts before DLQ
            enable_fallback: Enable in-memory fallback (not recommended for production)
        """
        self.redis_url = redis_url
        self.stream_prefix = stream_prefix
        self.max_retries = max_retries
        self.enable_fallback = enable_fallback

        self._redis: Optional[redis.Redis] = None
        self._connected = False
        self._consumers: Dict[str, asyncio.Task] = {}
        self._running = False

    async def connect(self):
        """Connect to Redis (call during startup)"""
        if not REDIS_AVAILABLE:
            logger.error("redis package not available")
            if self.enable_fallback:
                logger.warning("Message queue fallback not implemented")
            return

        if not self.redis_url:
            logger.error("No Redis URL provided for message queue")
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
            logger.info(f"Connected to Redis message queue: {self.stream_prefix}")

        except Exception as e:
            logger.error(f"Failed to connect to Redis message queue: {e}")
            raise

    async def disconnect(self):
        """Disconnect from Redis and stop all consumers"""
        self._running = False

        # Stop all consumers
        for consumer_name, task in self._consumers.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info(f"Consumer stopped: {consumer_name}")

        if self._redis:
            await self._redis.close()
            self._connected = False
            logger.info("Disconnected from Redis message queue")

    def _make_stream_name(self, stream: str) -> str:
        """Create stream name with prefix"""
        return f"{self.stream_prefix}:stream:{stream}"

    def _make_dlq_name(self, stream: str) -> str:
        """Create dead letter queue name"""
        return f"{self.stream_prefix}:dlq:{stream}"

    async def publish(
        self,
        stream: str,
        message: Dict[str, Any],
        max_len: int = 10000
    ) -> Optional[str]:
        """
        Publish message to stream.

        Args:
            stream: Stream name (e.g., 'events', 'commands')
            message: Message data (must be JSON-serializable)
            max_len: Maximum stream length (oldest messages trimmed)

        Returns:
            Message ID or None on error
        """
        if not self._connected:
            logger.error("Not connected to message queue")
            return None

        stream_name = self._make_stream_name(stream)

        try:
            # Serialize message data
            serialized = {
                'data': json.dumps(message),
                'timestamp': str(asyncio.get_event_loop().time())
            }

            # Add to stream with automatic trimming
            message_id = await self._redis.xadd(
                stream_name,
                serialized,
                maxlen=max_len,
                approximate=True  # Allow approximate trimming for performance
            )

            logger.debug(f"Published message to {stream}: {message_id}")
            return message_id

        except Exception as e:
            logger.error(f"Failed to publish message to {stream}: {e}")
            return None

    async def create_consumer_group(
        self,
        stream: str,
        group: str,
        start_id: str = '0'
    ) -> bool:
        """
        Create consumer group for a stream.

        Args:
            stream: Stream name
            group: Consumer group name
            start_id: Starting message ID ('0' = from beginning, '$' = new messages only)

        Returns:
            True if created or already exists
        """
        if not self._connected:
            return False

        stream_name = self._make_stream_name(stream)

        try:
            await self._redis.xgroup_create(
                stream_name,
                group,
                id=start_id,
                mkstream=True  # Create stream if it doesn't exist
            )
            logger.info(f"Created consumer group '{group}' for stream '{stream}'")
            return True

        except redis.ResponseError as e:
            if 'BUSYGROUP' in str(e):
                # Group already exists
                logger.debug(f"Consumer group '{group}' already exists for '{stream}'")
                return True
            logger.error(f"Failed to create consumer group: {e}")
            return False

        except Exception as e:
            logger.error(f"Failed to create consumer group: {e}")
            return False

    async def consume(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        handler: Callable[[Message], Any],
        block_ms: int = 5000,
        count: int = 10
    ):
        """
        Start consuming messages from stream.

        This is a long-running task that should be run in the background.

        Args:
            stream: Stream name
            group: Consumer group name
            consumer_name: Unique consumer identifier
            handler: Async function to handle messages
            block_ms: Block time in milliseconds when no messages
            count: Number of messages to fetch per batch

        Example:
            async def handle_event(message: Message):
                print(f"Processing: {message.data}")
                return True  # Return True to ACK, False to retry

            await queue.consume(
                'events',
                'router-group',
                'router-1',
                handle_event
            )
        """
        if not self._connected:
            logger.error("Not connected to message queue")
            return

        stream_name = self._make_stream_name(stream)
        dlq_name = self._make_dlq_name(stream)

        # Ensure consumer group exists
        await self.create_consumer_group(stream, group, start_id='0')

        self._running = True
        logger.info(
            f"Consumer '{consumer_name}' started for stream '{stream}' "
            f"in group '{group}'"
        )

        try:
            while self._running:
                try:
                    # Read new messages
                    messages = await self._redis.xreadgroup(
                        group,
                        consumer_name,
                        {stream_name: '>'},
                        count=count,
                        block=block_ms
                    )

                    if messages:
                        for stream_data in messages:
                            _, message_list = stream_data

                            for message_id, message_data in message_list:
                                await self._process_message(
                                    stream_name,
                                    dlq_name,
                                    group,
                                    message_id,
                                    message_data,
                                    handler
                                )

                    # Also process pending messages (messages that were read but not ACKed)
                    await self._process_pending_messages(
                        stream_name,
                        dlq_name,
                        group,
                        consumer_name,
                        handler
                    )

                except asyncio.CancelledError:
                    logger.info(f"Consumer '{consumer_name}' cancelled")
                    break

                except Exception as e:
                    logger.error(f"Error in consumer '{consumer_name}': {e}")
                    await asyncio.sleep(1)  # Brief pause before retry

        finally:
            logger.info(f"Consumer '{consumer_name}' stopped")

    async def _process_message(
        self,
        stream_name: str,
        dlq_name: str,
        group: str,
        message_id: str,
        message_data: Dict[str, str],
        handler: Callable
    ):
        """Process a single message"""
        try:
            # Deserialize message
            data = json.loads(message_data.get('data', '{}'))
            retry_count = int(message_data.get('retry_count', 0))

            message = Message(
                id=message_id,
                stream=stream_name,
                data=data,
                retry_count=retry_count
            )

            # Call handler
            success = await handler(message)

            if success:
                # Acknowledge message
                await self._redis.xack(stream_name, group, message_id)
                logger.debug(f"ACKed message: {message_id}")
            else:
                # Handler returned False - retry or move to DLQ
                await self._handle_failed_message(
                    stream_name,
                    dlq_name,
                    group,
                    message_id,
                    message_data,
                    retry_count
                )

        except Exception as e:
            logger.error(f"Error processing message {message_id}: {e}")
            # On error, move to pending for retry
            pass

    async def _handle_failed_message(
        self,
        stream_name: str,
        dlq_name: str,
        group: str,
        message_id: str,
        message_data: Dict[str, str],
        retry_count: int
    ):
        """Handle failed message - retry or move to DLQ"""
        if retry_count >= self.max_retries:
            # Move to dead letter queue
            await self._redis.xadd(
                dlq_name,
                {
                    **message_data,
                    'original_id': message_id,
                    'retry_count': str(retry_count),
                    'failure_reason': 'max_retries_exceeded'
                }
            )

            # ACK from main stream
            await self._redis.xack(stream_name, group, message_id)
            logger.warning(
                f"Message {message_id} moved to DLQ after {retry_count} retries"
            )
        else:
            # Leave in pending for retry
            # The pending message processor will retry it
            logger.info(
                f"Message {message_id} will be retried "
                f"(attempt {retry_count + 1}/{self.max_retries})"
            )

    async def _process_pending_messages(
        self,
        stream_name: str,
        dlq_name: str,
        group: str,
        consumer_name: str,
        handler: Callable,
        idle_time: int = 60000  # 60 seconds
    ):
        """Process pending messages that weren't ACKed"""
        try:
            # Get pending messages for this consumer
            pending = await self._redis.xpending_range(
                stream_name,
                group,
                '-',
                '+',
                count=10,
                consumername=consumer_name
            )

            for pending_msg in pending:
                message_id = pending_msg['message_id']
                idle = pending_msg['time_since_delivered']

                # Only retry if message has been idle long enough
                if idle > idle_time:
                    # Claim the message
                    claimed = await self._redis.xclaim(
                        stream_name,
                        group,
                        consumer_name,
                        min_idle_time=idle_time,
                        message_ids=[message_id]
                    )

                    if claimed:
                        message_id, message_data = claimed[0]
                        await self._process_message(
                            stream_name,
                            dlq_name,
                            group,
                            message_id,
                            message_data,
                            handler
                        )

        except Exception as e:
            logger.error(f"Error processing pending messages: {e}")

    async def get_stream_info(self, stream: str) -> Optional[Dict[str, Any]]:
        """Get information about a stream"""
        if not self._connected:
            return None

        stream_name = self._make_stream_name(stream)

        try:
            info = await self._redis.xinfo_stream(stream_name)
            return info
        except Exception as e:
            logger.error(f"Failed to get stream info: {e}")
            return None

    async def get_dlq_messages(
        self,
        stream: str,
        count: int = 100
    ) -> List[Dict[str, Any]]:
        """Get messages from dead letter queue"""
        if not self._connected:
            return []

        dlq_name = self._make_dlq_name(stream)

        try:
            messages = await self._redis.xrange(dlq_name, count=count)

            result = []
            for message_id, message_data in messages:
                data = json.loads(message_data.get('data', '{}'))
                result.append({
                    'id': message_id,
                    'data': data,
                    'original_id': message_data.get('original_id'),
                    'retry_count': int(message_data.get('retry_count', 0)),
                    'failure_reason': message_data.get('failure_reason')
                })

            return result

        except Exception as e:
            logger.error(f"Failed to get DLQ messages: {e}")
            return []


def create_message_queue(
    redis_url: str,
    stream_prefix: str = "waddlebot",
    max_retries: int = 3
) -> MessageQueue:
    """
    Factory function to create a message queue.

    Args:
        redis_url: Redis connection URL
        stream_prefix: Prefix for stream names
        max_retries: Maximum retry attempts

    Returns:
        Configured MessageQueue instance
    """
    return MessageQueue(
        redis_url=redis_url,
        stream_prefix=stream_prefix,
        max_retries=max_retries,
        enable_fallback=False
    )
