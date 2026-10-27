"""Waddles Router Module (Quart) - Central command routing system"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from quart import Quart  # noqa: E402
from flask_core import (  # noqa: E402
    create_health_blueprint,
    init_database,
    setup_aaa_logging,
    StreamPipeline,
)
from config import Config  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION, Config.LOG_LEVEL)

# Initialize services
dal = None
command_processor = None
cache_manager = None
rate_limiter = None
session_manager = None
stream_pipeline = None
stream_consumers_tasks = []


@app.before_serving
async def startup():
    global dal, command_processor, cache_manager, rate_limiter, session_manager, stream_pipeline, stream_consumers_tasks
    from services.command_processor import CommandProcessor
    from services.cache_manager import CacheManager
    from services.rate_limiter import RateLimiter
    from services.session_manager import SessionManager
    from services.command_registry import CommandRegistry
    from services.ai_chatter_config_cache import AiChatterConfigCache

    logger.system("Starting router module", action="startup")

    dal = init_database(
        Config.DATABASE_URL,
        pool_size=Config.ROUTER_MAX_WORKERS,
        read_replica_uri=Config.READ_REPLICA_URL,
    )
    app.config['dal'] = dal

    cache_manager = CacheManager()
    rate_limiter = RateLimiter(redis_url=Config.REDIS_URL)
    await rate_limiter.connect()  # Connect to Redis on startup
    session_manager = SessionManager()
    command_registry = CommandRegistry(dal, cache_manager)
    await command_registry.initialize()  # Load initial commands from database
    ai_chatter_config_cache = AiChatterConfigCache(cache_manager.redis, dal)
    command_processor = CommandProcessor(
        dal,
        cache_manager,
        rate_limiter,
        session_manager,
        command_registry,
        ai_chatter_config_cache=ai_chatter_config_cache,
    )

    app.config['command_processor'] = command_processor
    app.config['cache_manager'] = cache_manager
    app.config['rate_limiter'] = rate_limiter
    app.config['session_manager'] = session_manager

    # Start command reload listener (background task for marketplace module updates)
    task = asyncio.create_task(_command_reload_listener(
        command_registry,
        Config.REDIS_URL
    ))
    stream_consumers_tasks.append(task)
    logger.system("Started command reload listener", action="command_reload_listener_start")

    # Initialize StreamPipeline if enabled
    if Config.STREAM_PIPELINE_ENABLED:
        stream_pipeline = StreamPipeline(
            redis_url=rate_limiter.redis_url,
            batch_size=Config.STREAM_BATCH_SIZE,
            block_ms=Config.STREAM_BLOCK_TIME,
            enabled=True
        )
        await stream_pipeline.connect()
        app.config['stream_pipeline'] = stream_pipeline

        # Start stream consumers as background tasks
        for i in range(Config.STREAM_CONSUMER_COUNT):
            consumer_name = f"{Config.STREAM_CONSUMER_NAME}-{i}"
            task = asyncio.create_task(_stream_consumer_worker(
                stream_pipeline,
                Config.STREAM_CONSUMER_GROUP,
                consumer_name
            ))
            stream_consumers_tasks.append(task)
            logger.system(f"Started stream consumer: {consumer_name}", action="stream_consumer_start")

    logger.system("Router module started successfully", result="SUCCESS")


async def _stream_consumer_worker(
    pipeline: StreamPipeline,
    consumer_group: str,
    consumer_name: str
):
    """
    Background worker to consume and process stream events.
    Runs continuously until the stream consumers tasks are cancelled.
    """
    while True:
        try:
            # Consume events from inbound stream
            events = await pipeline.consume_events(
                'events:inbound',
                consumer_group,
                consumer_name
            )

            # Process each event
            for event in events:
                try:
                    logger.debug(
                        f"Processing stream event: {event['id']}",
                        data=event['data']
                    )

                    # Acknowledge successful processing
                    await pipeline.acknowledge_event(
                        'events:inbound',
                        consumer_group,
                        event['id']
                    )

                except Exception as e:
                    logger.error(
                        f"Error processing stream event {event['id']}: {e}",
                        error=str(e)
                    )

                    # Move to DLQ if max retries exceeded
                    if event.get('retry_count', 0) >= pipeline.max_retries:
                        await pipeline.move_to_dlq(
                            'events:inbound',
                            event['id'],
                            f"Max retries exceeded: {str(e)}",
                            event_data=event['data'],
                            retry_count=event.get('retry_count', 0)
                        )
                        # Acknowledge to remove from pending
                        await pipeline.acknowledge_event(
                            'events:inbound',
                            consumer_group,
                            event['id']
                        )

        except asyncio.CancelledError:
            logger.system(f"Stream consumer {consumer_name} shutting down", action="stream_consumer_stop")
            break
        except Exception as e:
            logger.error(f"Error in stream consumer {consumer_name}: {e}")
            await asyncio.sleep(1)  # Brief delay before retry


async def _command_reload_listener(command_registry, redis_url):
    """Listen for command:reload events from marketplace module updates.

    Subscribes to Redis pub/sub channel 'command:reload' and refreshes the
    command registry cache when marketplace modules are added, updated, or removed.
    """
    # Try to use redis.asyncio (newer), fall back to aioredis (legacy)
    aioredis_lib = None
    try:
        import redis.asyncio as aioredis_lib
    except ImportError:
        try:
            import aioredis as aioredis_lib
        except ImportError:
            logger.warning("No async Redis library available for command:reload listener")
            return

    try:
        redis = await aioredis_lib.from_url(redis_url or 'redis://redis:6379')
        pubsub = redis.pubsub()
        await pubsub.subscribe('command:reload')
        logger.system("Subscribed to command:reload Redis channel", action="pubsub_subscribe")

        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    import json
                    data = json.loads(message['data']) if isinstance(message['data'], str) else message['data']
                    logger.info(
                        "Command reload event received from marketplace",
                        data=data
                    )

                    # Reload commands from database to pick up marketplace module changes
                    await command_registry.reload_commands()
                    logger.system(
                        "Command registry reloaded in response to marketplace event",
                        action="command_registry_reload",
                        data=data
                    )
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON in command:reload event: {message['data']}")
                except Exception as e:
                    logger.error(f"Error processing command:reload event: {e}")
    except asyncio.CancelledError:
        logger.system("Command reload listener shutting down", action="command_reload_listener_stop")
        try:
            await pubsub.unsubscribe('command:reload')
            await redis.close()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Command reload listener error: {e}")


@app.after_serving
async def shutdown():
    """Cleanup handler for graceful shutdown"""
    global stream_pipeline, stream_consumers_tasks

    logger.system("Shutting down router module", action="shutdown")

    # Stop stream consumers
    if stream_consumers_tasks:
        for task in stream_consumers_tasks:
            task.cancel()

        # Wait for all tasks to complete
        if stream_consumers_tasks:
            await asyncio.gather(*stream_consumers_tasks, return_exceptions=True)

        logger.system("Stream consumers stopped", action="stream_consumers_stop")

    # Disconnect stream pipeline
    if stream_pipeline and stream_pipeline._connected:
        await stream_pipeline.disconnect()
        logger.system("Stream pipeline disconnected", action="stream_pipeline_disconnect")

    logger.system("Router module shutdown complete", result="SUCCESS")


# Register blueprints
from controllers.router import router_bp  # noqa: E402
from controllers.admin import admin_bp  # noqa: E402

app.register_blueprint(router_bp, url_prefix='/api/v1/router')
app.register_blueprint(admin_bp, url_prefix='/api/v1/admin')

if __name__ == '__main__':
    import asyncio
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
