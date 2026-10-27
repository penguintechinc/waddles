"""Scheduled marketplace-source update check (CronJob entrypoint, Phase 1 Task 8).

Records new upstream commits as `pending_commit_sha` for human re-approval and notifies admins.
Never advances a pin, scans, builds or installs. Exit 1 if any source check errored.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from config import HubAPIConfig
from services.bundle_install_dal import build_install_dal
from services.marketplace_source_update import rescan_all

logger = logging.getLogger("waddles.hub_api.marketplace_source_rescan")


async def _run() -> int:
    config = HubAPIConfig.from_env()
    dal = await build_install_dal(config.database_url, pool_size=2)
    try:
        summary = await rescan_all(dal)
    finally:
        await dal.close()
    logger.info(
        "marketplace-source-rescan: summary",
        extra={
            "examined": summary.examined,
            "unchanged": summary.unchanged,
            "pending": summary.pending,
            "skipped": summary.skipped,
            "errors": summary.errors,
        },
    )
    return 1 if summary.errors else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns a process exit code (0 = every source checked cleanly)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "").parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - script-execution guard
    sys.exit(main())
