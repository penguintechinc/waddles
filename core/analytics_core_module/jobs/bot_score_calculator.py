"""
Bot Score Calculator - Daily batch job for calculating community bot scores.

Runs at 3 AM UTC daily via cron or can be triggered manually.
"""
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# Add libs to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'libs'))

from flask_core import setup_aaa_logging, init_database
from flask_core.db_errors import describe_db_error
from services.log_safety import log_failure

# Configuration
BATCH_SIZE = int(os.environ.get('BOT_SCORE_BATCH_SIZE', '100'))
CONCURRENCY = int(os.environ.get('BOT_SCORE_CONCURRENCY', '10'))
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://waddlebot:waddlebot@localhost:5432/waddlebot')

logger = setup_aaa_logging('bot_score_calculator', '1.0.0')


async def calculate_all_communities():
    """Calculate bot scores for all active communities."""
    start_time = datetime.utcnow()
    logger.system("Starting bot score calculation job", action="batch_start")

    try:
        # Initialize database
        dal = init_database(DATABASE_URL)

        # Import service
        from services.bot_score_service import BotScoreService
        bot_score_service = BotScoreService(dal, logger)

        # Get all active communities
        communities = dal.executesql(
            "SELECT id FROM communities WHERE is_active = true ORDER BY id"
        )

        total = len(communities)
        success_count = 0
        error_count = 0

        logger.system(f"Processing {total} communities", action="batch_process", total=total)

        # Process in batches
        for i in range(0, total, BATCH_SIZE):
            batch = communities[i:i + BATCH_SIZE]

            # Use ThreadPoolExecutor for parallel processing
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
                loop = asyncio.get_event_loop()
                tasks = [
                    loop.run_in_executor(
                        executor,
                        lambda c=c: asyncio.run(bot_score_service.calculate_score(c[0]))
                    )
                    for c in batch
                ]

                results = await asyncio.gather(*tasks, return_exceptions=True)

                for idx, result in enumerate(results):
                    if isinstance(result, Exception):
                        error_count += 1
                        log_failure(
                            logger,
                            "Failed to calculate score for community",
                            result,
                            community_id=batch[idx][0],
                            action="calculate_score",
                            result="FAILED"
                        )
                    else:
                        success_count += 1

            # Progress log
            processed = min(i + BATCH_SIZE, total)
            logger.system(
                f"Progress: {processed}/{total} communities processed",
                action="batch_progress",
                processed=processed,
                total=total
            )

        # Calculate duration
        duration = (datetime.utcnow() - start_time).total_seconds()

        logger.audit(
            "Bot score calculation completed",
            action="batch_complete",
            total=total,
            success=success_count,
            errors=error_count,
            duration_seconds=duration,
            result="SUCCESS"
        )

        return {
            'total': total,
            'success': success_count,
            'errors': error_count,
            'duration_seconds': duration
        }

    except Exception as e:
        log_failure(logger, "Bot score calculation job failed", e, action="batch_failed", result="FAILED")
        raise


def main():
    """Entry point for cron job."""
    try:
        result = asyncio.run(calculate_all_communities())
        print(f"Bot score calculation complete: {result}")
        return 0
    except Exception as e:
        print(f"Bot score calculation failed: {describe_db_error(e)}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
