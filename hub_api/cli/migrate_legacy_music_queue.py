"""One-shot control-plane job: import the retired Music Station queue into music-requests.

Idempotent and safe to run concurrently or repeatedly (per-community advisory lock plus the
`settings.imported` marker; see `services.music_legacy_migration`). Also fires automatically
after the music-requests bundle is installed. Logs counts only. Exit 0 unless a community failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from config import HubAPIConfig
from services import music_legacy_migration
from services.bundle_install_dal import build_install_dal

logger = logging.getLogger("waddles.hub_api.migrate_legacy_music_queue")


async def _run() -> int:
    """Open the install DAL, run the import, and map failures to a non-zero exit code."""
    cfg = HubAPIConfig.from_env()
    install_dal = await build_install_dal(cfg.database_url, 2)
    try:
        summary = await music_legacy_migration.migrate_all(install_dal)
    finally:
        await install_dal.close()
    return 1 if summary.failed else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- exit code 0 only when no community failed."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "").parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - script-execution guard
    sys.exit(main())
