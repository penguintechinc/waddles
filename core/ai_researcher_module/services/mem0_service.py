"""
mem0 Integration for Community Memory Management
=================================================

Provides semantic memory storage and retrieval using mem0 with:
- Ollama backend for LLM operations
- Qdrant vector store for semantic search
- Per-community memory isolation
- Automatic message processing and context aggregation
"""

import logging
from typing import Optional, Any
from dataclasses import dataclass, field

from mem0 import Memory

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Mem0Service:
    """
    mem0 integration for community memory management.

    Provides semantic memory capabilities for AI researcher:
    - Stores chat messages and community context
    - Enables semantic search across memories
    - Aggregates community-wide context
    - Supports per-user memory tracking
    """

    community_id: int
    config: dict[str, Any] = field(default_factory=dict)
    _memory: Optional[Memory] = field(default=None, init=False)

    def __post_init__(self):
        """Initialize mem0 with Ollama backend and Qdrant vector store"""
        try:
            # Build mem0 configuration
            mem0_config = self._build_mem0_config()

            # Initialize Memory instance
            self._memory = Memory.from_config(mem0_config)

            logger.info(
                f"Initialized mem0 service for community {self.community_id} "
                f"with Ollama backend and Qdrant store"
            )

        except Exception as e:
            logger.error(
                f"Failed to initialize mem0 service for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            raise

    def _build_mem0_config(self) -> dict[str, Any]:
        """
        Build mem0 configuration from Config class.

        Returns:
            Configuration dictionary for mem0
        """
        # Get Ollama connection details
        ollama_host = self.config.get(
            'ollama_host',
            Config.OLLAMA_HOST
        )
        ollama_port = self.config.get(
            'ollama_port',
            Config.OLLAMA_PORT
        )
        ollama_base_url = (
            f"http://{ollama_host}:{ollama_port}"
        )

        # Get AI model
        ai_model = self.config.get(
            'ai_model',
            Config.AI_MODEL
        )

        # Get embedder model
        embedder_model = self.config.get(
            'embedder_model',
            Config.MEM0_EMBEDDER_MODEL
        )

        # Get Qdrant configuration
        qdrant_url = self.config.get(
            'qdrant_url',
            Config.QDRANT_URL
        )
        qdrant_api_key = self.config.get(
            'qdrant_api_key',
            Config.QDRANT_API_KEY
        )

        # Build collection name for this community
        collection_name = f"community_{self.community_id}"

        # Build mem0 config
        mem0_config = {
            "llm": {
                "provider": "ollama",
                "config": {
                    "model": ai_model,
                    "ollama_base_url": ollama_base_url
                }
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": embedder_model,
                    "ollama_base_url": ollama_base_url
                }
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": collection_name,
                    "url": qdrant_url
                }
            }
        }

        # Add API key if provided
        if qdrant_api_key:
            mem0_config["vector_store"]["config"]["api_key"] = qdrant_api_key

        logger.debug(
            f"Built mem0 config for community {self.community_id}: "
            f"model={ai_model}, embedder={embedder_model}, "
            f"collection={collection_name}"
        )

        return mem0_config

    async def add_messages(self, messages: list[dict]) -> None:
        """
        Add chat messages to memory.

        Processes a batch of chat messages and stores them in the
        vector database for semantic search and context retrieval.

        Args:
            messages: List of message dictionaries with 'role' and 'content'

        Raises:
            ValueError: If messages format is invalid
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        if not messages:
            logger.warning("No messages provided to add_messages")
            return

        try:
            # Validate message format
            for msg in messages:
                if 'role' not in msg or 'content' not in msg:
                    raise ValueError(
                        "Messages must have 'role' and 'content' fields"
                    )

            # Add messages to mem0
            self._memory.add(
                messages=messages,
                user_id=f"community_{self.community_id}"
            )

            logger.info(
                f"Added {len(messages)} messages to memory for "
                f"community {self.community_id}"
            )

        except Exception as e:
            logger.error(
                f"Failed to add messages to memory for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            raise

    async def add_memory(
        self,
        content: str,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Add a single memory entry.

        Args:
            content: Memory content text
            user_id: Optional user ID to associate with memory
            metadata: Optional metadata dictionary

        Returns:
            Dictionary with memory ID and status

        Raises:
            ValueError: If content is empty
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty")

        try:
            # Use community user ID if not provided
            effective_user_id = (
                user_id or f"community_{self.community_id}"
            )

            # Add memory
            result = self._memory.add(
                content,
                user_id=effective_user_id,
                metadata=metadata or {}
            )

            logger.info(
                f"Added memory for community {self.community_id}, "
                f"user {effective_user_id}: {len(content)} chars"
            )

            return {
                "memory_id": result.get("id"),
                "status": "success",
                "user_id": effective_user_id
            }

        except Exception as e:
            logger.error(
                f"Failed to add memory for community {self.community_id}: {describe_db_error(e)}"
            )
            raise

    async def search(
        self,
        query: str,
        limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search memories using semantic search.

        Args:
            query: Search query text
            limit: Maximum number of results (default: 10)

        Returns:
            List of matching memories with scores

        Raises:
            ValueError: If query is empty
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        if not query or not query.strip():
            raise ValueError("Search query cannot be empty")

        try:
            # Perform semantic search
            results = self._memory.search(
                query=query,
                user_id=f"community_{self.community_id}",
                limit=limit
            )

            logger.info(
                f"Search for community {self.community_id} returned "
                f"{len(results)} results for query: '{query[:50]}...'"
            )

            return results

        except Exception as e:
            logger.error(
                f"Failed to search memories for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            raise

    async def get_all(
        self,
        user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get all memories for a user or community.

        Args:
            user_id: Optional specific user ID (defaults to community)

        Returns:
            List of all memories

        Raises:
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        try:
            # Use community user ID if not provided
            effective_user_id = (
                user_id or f"community_{self.community_id}"
            )

            # Get all memories
            memories = self._memory.get_all(user_id=effective_user_id)

            logger.info(
                f"Retrieved {len(memories)} memories for "
                f"community {self.community_id}, user {effective_user_id}"
            )

            return memories

        except Exception as e:
            logger.error(
                f"Failed to get all memories for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            raise

    async def delete_memory(self, memory_id: str) -> bool:
        """
        Delete a specific memory by ID.

        Args:
            memory_id: Memory ID to delete

        Returns:
            True if deletion was successful, False otherwise

        Raises:
            ValueError: If memory_id is empty
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        if not memory_id or not memory_id.strip():
            raise ValueError("Memory ID cannot be empty")

        try:
            # Delete memory
            self._memory.delete(memory_id)

            logger.info(
                f"Deleted memory {memory_id} for "
                f"community {self.community_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"Failed to delete memory {memory_id} for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            return False

    async def get_community_context(self) -> dict[str, Any]:
        """
        Get aggregated context for the community.

        Retrieves and aggregates all community memories to provide
        a comprehensive context summary.

        Returns:
            Dictionary with aggregated community context including:
            - total_memories: Count of stored memories
            - recent_memories: Most recent memory entries
            - context_summary: Aggregated context information

        Raises:
            RuntimeError: If mem0 is not initialized
        """
        if self._memory is None:
            raise RuntimeError(
                f"mem0 not initialized for community {self.community_id}"
            )

        try:
            # Get all community memories
            all_memories = await self.get_all()

            # Sort by timestamp if available
            sorted_memories = sorted(
                all_memories,
                key=lambda x: x.get('created_at', 0),
                reverse=True
            )

            # Get recent memories (last 10)
            recent_memories = sorted_memories[:10]

            # Build context dictionary
            context = {
                "community_id": self.community_id,
                "total_memories": len(all_memories),
                "recent_memories": [
                    {
                        "id": mem.get("id"),
                        "content": mem.get("memory", ""),
                        "created_at": mem.get("created_at"),
                        "metadata": mem.get("metadata", {})
                    }
                    for mem in recent_memories
                ],
                "context_summary": {
                    "memory_count": len(all_memories),
                    "has_context": len(all_memories) > 0
                }
            }

            logger.info(
                f"Retrieved community context for {self.community_id}: "
                f"{len(all_memories)} total memories, "
                f"{len(recent_memories)} recent"
            )

            return context

        except Exception as e:
            logger.error(
                f"Failed to get community context for "
                f"community {self.community_id}: {describe_db_error(e)}"
            )
            # Return empty context on error
            return {
                "community_id": self.community_id,
                "total_memories": 0,
                "recent_memories": [],
                "context_summary": {
                    "memory_count": 0,
                    "has_context": False,
                    "error": describe_db_error(e)
                }
            }

    async def health_check(self) -> bool:
        """
        Check if mem0 service is healthy.

        Returns:
            True if healthy, False otherwise
        """
        try:
            if self._memory is None:
                logger.error(
                    f"mem0 not initialized for community {self.community_id}"
                )
                return False

            # Try a simple search to verify functionality
            await self.search("health check", limit=1)

            logger.debug(
                f"Health check passed for community {self.community_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Health check failed for community {self.community_id}: {describe_db_error(e)}"
            )
            return False
