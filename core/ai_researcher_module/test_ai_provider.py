#!/usr/bin/env python3
"""
Test script for AI Provider Service
====================================

Basic tests to verify AI provider functionality:
- Initialization
- Health checks
- Generation (if Ollama available)
"""

import asyncio
import sys
from config import Config
from services.ai_provider import AIProviderService, AIProvider


async def test_initialization():
    """Test provider initialization"""
    print("\n=== Testing Initialization ===")

    try:
        service = AIProviderService(Config)
        print(f"✓ Service initialized: provider={service.provider.value}")
        print(f"✓ Base URL: {service.base_url}")
        print(f"✓ Timeout: {service.timeout}s")
        print(f"✓ Concurrency limit: {Config.MAX_CONCURRENT_LLM_CALLS}")
        return service
    except Exception as e:
        print(f"✗ Initialization failed: {e}")
        return None


async def test_health_check(service: AIProviderService):
    """Test health check"""
    print("\n=== Testing Health Check ===")

    try:
        is_healthy = await service.health_check()
        if is_healthy:
            print(f"✓ Provider is healthy: {service.provider.value}")
        else:
            print(f"✗ Provider is not available: {service.provider.value}")
        return is_healthy
    except NotImplementedError as e:
        print(f"⚠ Health check not implemented: {e}")
        return False
    except Exception as e:
        print(f"✗ Health check error: {e}")
        return False


async def test_generation(service: AIProviderService):
    """Test text generation"""
    print("\n=== Testing Text Generation ===")

    if service.provider != AIProvider.OLLAMA:
        print("⚠ Skipping generation test (only Ollama implemented)")
        return

    try:
        prompt = "What is the capital of France? Answer in one word."
        system_prompt = "You are a helpful assistant. Be concise."

        print(f"Prompt: {prompt}")
        print("Generating response...")

        response = await service.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.7,
            max_tokens=50
        )

        print("✓ Generation successful!")
        print(f"  Model: {response.model}")
        print(f"  Tokens: {response.tokens_used}")
        print(f"  Time: {response.processing_time_ms}ms")
        print(f"  Content: {response.content[:100]}...")

    except NotImplementedError as e:
        print(f"⚠ Generation not implemented: {e}")
    except Exception as e:
        print(f"✗ Generation failed: {e}")


async def test_concurrent_generation(service: AIProviderService):
    """Test concurrent generation with semaphore"""
    print("\n=== Testing Concurrent Generation ===")

    if service.provider != AIProvider.OLLAMA:
        print("⚠ Skipping concurrent test (only Ollama implemented)")
        return

    try:
        prompts = [
            "Count to 3",
            "What is 2+2?",
            "Name a color",
        ]

        print(f"Running {len(prompts)} concurrent requests...")

        tasks = [
            service.generate(prompt=p, max_tokens=20)
            for p in prompts
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(
            1 for r in responses if not isinstance(r, Exception)
        )

        print(f"✓ Completed {success_count}/{len(prompts)} requests")

        for i, response in enumerate(responses):
            if isinstance(response, Exception):
                print(f"  [{i}] Error: {response}")
            else:
                print(f"  [{i}] {response.processing_time_ms}ms, "
                      f"{response.tokens_used} tokens")

    except Exception as e:
        print(f"✗ Concurrent test failed: {e}")


async def test_embeddings(service: AIProviderService):
    """Test embedding generation"""
    print("\n=== Testing Embeddings ===")

    if service.provider != AIProvider.OLLAMA:
        print("⚠ Skipping embeddings test (only Ollama implemented)")
        return

    try:
        text = "This is a test sentence for embedding generation."
        print(f"Text: {text}")
        print("Generating embedding...")

        embedding = await service.embed(text)

        print("✓ Embedding generated!")
        print(f"  Dimensions: {len(embedding)}")
        print(f"  First 5 values: {embedding[:5]}")

    except NotImplementedError as e:
        print(f"⚠ Embeddings not implemented: {e}")
    except Exception as e:
        print(f"✗ Embedding generation failed: {e}")


async def main():
    """Run all tests"""
    print("=" * 60)
    print("AI Provider Service Test Suite")
    print("=" * 60)

    # Initialize service
    service = await test_initialization()
    if not service:
        print("\n✗ Cannot continue without service initialization")
        return 1

    try:
        # Run tests
        await test_health_check(service)
        await test_generation(service)
        await test_concurrent_generation(service)
        await test_embeddings(service)

        print("\n" + "=" * 60)
        print("Test suite completed!")
        print("=" * 60)

        return 0

    finally:
        # Cleanup
        await service.close()
        print("\n✓ Service closed")


if __name__ == '__main__':
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Test suite failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
