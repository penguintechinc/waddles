"""
Test script for Mem0Service
============================

Example usage of the mem0 service for community memory management.

This is a demonstration script showing how to:
- Initialize the service
- Add messages and memories
- Search memories
- Retrieve community context
"""

import asyncio
import logging
from mem0_service import Mem0Service

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s'
)

logger = logging.getLogger(__name__)


async def test_mem0_service():
    """Test the mem0 service with example operations"""

    # Initialize service for community 123
    config = {
        'ollama_host': 'localhost',
        'ollama_port': '11434',
        'ai_model': 'tinyllama',
        'embedder_model': 'nomic-embed-text',
        'qdrant_url': 'http://localhost:6333',
    }

    logger.info("Initializing Mem0Service for community 123...")
    service = Mem0Service(community_id=123, config=config)

    # Test 1: Add chat messages
    logger.info("\n--- Test 1: Adding chat messages ---")
    messages = [
        {
            "role": "user",
            "content": "What's the weather like today?"
        },
        {
            "role": "assistant",
            "content": "I don't have access to weather data."
        },
        {
            "role": "user",
            "content": "Can you help me with Python?"
        }
    ]

    try:
        await service.add_messages(messages)
        logger.info(f"Successfully added {len(messages)} messages")
    except Exception as e:
        logger.error(f"Failed to add messages: {e}")

    # Test 2: Add a single memory
    logger.info("\n--- Test 2: Adding single memory ---")
    try:
        result = await service.add_memory(
            content="The user prefers Python programming",
            user_id=Mem0Service.user_key("00000000-0000-4000-8000-000000000123"),
            metadata={"category": "preference"}
        )
        logger.info(f"Added memory: {result}")
    except Exception as e:
        logger.error(f"Failed to add memory: {e}")

    # Test 3: Search memories
    logger.info("\n--- Test 3: Searching memories ---")
    try:
        results = await service.search(
            query="programming preferences",
            limit=5
        )
        logger.info(f"Found {len(results)} matching memories")
        for i, result in enumerate(results, 1):
            logger.info(f"  {i}. {result.get('memory', '')[:100]}")
    except Exception as e:
        logger.error(f"Failed to search memories: {e}")

    # Test 4: Get all memories
    logger.info("\n--- Test 4: Getting all memories ---")
    try:
        all_memories = await service.get_all()
        logger.info(f"Total memories: {len(all_memories)}")
    except Exception as e:
        logger.error(f"Failed to get all memories: {e}")

    # Test 5: Get community context
    logger.info("\n--- Test 5: Getting community context ---")
    try:
        context = await service.get_community_context()
        logger.info("Community context:")
        logger.info(f"  Total memories: {context['total_memories']}")
        logger.info(f"  Recent memories: {len(context['recent_memories'])}")
        logger.info(
            f"  Has context: {context['context_summary']['has_context']}"
        )
    except Exception as e:
        logger.error(f"Failed to get community context: {e}")

    # Test 6: Health check
    logger.info("\n--- Test 6: Health check ---")
    try:
        is_healthy = await service.health_check()
        logger.info(f"Service health: {'OK' if is_healthy else 'FAILED'}")
    except Exception as e:
        logger.error(f"Health check failed: {e}")


if __name__ == "__main__":
    logger.info("Starting Mem0Service tests...")
    logger.info("=" * 60)

    try:
        asyncio.run(test_mem0_service())
        logger.info("\n" + "=" * 60)
        logger.info("All tests completed!")
    except Exception as e:
        logger.error(f"\nTest suite failed: {e}", exc_info=True)
