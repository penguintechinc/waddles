"""Valkey-backed snapshot of the public landing-page platform stats.

A CronJob (`cli/refresh_landing_stats.py`) computes the stats and stores one JSON
snapshot here; the public endpoint only ever reads it, never queries the DB per request.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.valkey_admin_client import build_client

logger = logging.getLogger("waddles.hub_api.landing_stats_cache")

#: Valkey key holding the JSON snapshot (versioned so a shape change never reads stale data).
SNAPSHOT_KEY = "waddles:landing:stats:v1"
#: ~4x the 5-minute refresh cadence: tolerates a few failed CronJob runs before going cold.
SNAPSHOT_TTL_SECONDS = 20 * 60
#: `app.config` slot caching the lazily-built Valkey client for the endpoint.
CLIENT_CONFIG_KEY = "landing_stats_redis"


def get_client(app_config: Any) -> Any:
    """The endpoint's shared Valkey client, built lazily and cached in `app.config`."""
    client = app_config.get(CLIENT_CONFIG_KEY)
    if client is None:
        client = build_client()
        app_config[CLIENT_CONFIG_KEY] = client
    return client


async def write_snapshot(client: Any, stats: dict[str, Any]) -> dict[str, Any]:
    """Store `stats` plus a `computed_at` ISO timestamp under `SNAPSHOT_KEY` with the TTL."""
    snapshot = {"computed_at": datetime.now(UTC).isoformat(), "stats": stats}
    await client.set(SNAPSHOT_KEY, json.dumps(snapshot), ex=SNAPSHOT_TTL_SECONDS)
    return snapshot


async def read_snapshot(client: Any) -> dict[str, Any] | None:
    """The cached snapshot, or None on a miss. A corrupt payload is logged and treated as a miss."""
    raw = await client.get(SNAPSHOT_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "stats" not in data or "computed_at" not in data:
            raise ValueError("snapshot missing stats/computed_at")
    except ValueError:
        logger.warning("landing stats snapshot is corrupt; treating as cache miss")
        return None
    return data
