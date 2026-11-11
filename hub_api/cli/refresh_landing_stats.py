"""Scheduled refresh of the public landing-page stats snapshot in Valkey.

Computes the same numbers `public_service.get_stats()` always produced and stores them
(with `computed_at`) at `services.landing_stats_cache.SNAPSHOT_KEY`. The public endpoint
serves only this snapshot, so the landing page never triggers a DB query. Run every
5 minutes by `templates/landing-stats-cronjob.yaml`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from config import HubAPIConfig
from services import landing_stats_cache, public_service
from services.valkey_admin_client import build_client

logger = logging.getLogger("waddles.hub_api.refresh_landing_stats")


async def refresh(async_dal: Any, dal: Any, cfg: HubAPIConfig, client: Any) -> dict[str, Any]:
    """Compute the stats for the default tenant and write the snapshot; returns the snapshot."""
    ctx = await public_service.resolve_default_tenant(async_dal, dal, cfg)
    stats = await public_service.get_stats(async_dal, dal, ctx)
    snapshot = await landing_stats_cache.write_snapshot(client, stats)
    logger.info(
        "refresh-landing-stats: snapshot written",
        extra={"computed_at": snapshot["computed_at"], "communities": stats["communities"]},
    )
    return snapshot


async def _run() -> int:
    from flask_core import init_database  # noqa: PLC0415 - heavy import, CLI-only

    cfg = HubAPIConfig.from_env()
    async_dal = init_database(cfg.database_url, pool_size=2)
    client = build_client()
    try:
        await refresh(async_dal, async_dal.dal, cfg, client)
    except Exception:
        logger.exception("refresh-landing-stats: failed")
        return 1
    finally:
        await client.aclose()
        await async_dal.close_async()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- exit code 0 only when a snapshot was written."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "").parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - script-execution guard
    sys.exit(main())
