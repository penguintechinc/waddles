"""Consumer-group lifecycle only -- hub-api never reads/writes stream entries (spec Sec5.2, Sec9.5).

TLS/auth defaults mirror spec Sec11.6.4: `security.transport.tls`
(env `SECURITY_TRANSPORT_TLS`, default `true`) refuses a plaintext
`redis://` URL at construction time, matching every Rust service's own
startup check -- the opt-out is explicit and visible, never silent.

hub-api's role here is only consumer-group lifecycle -- creating the
`hub_api_usage_aggregator` group on `waddles:usage` (spec Sec5.12) --
never reading or writing stream entries; that is exclusively the Rust
stages' job (spec Sec5.2: "Bundles hold no Valkey connection... the
stage is the enforcement point").
"""

from __future__ import annotations

import os
from typing import Any

import redis.asyncio as redis_asyncio
import redis.exceptions


def _tls_required() -> bool:
    return os.environ.get("SECURITY_TRANSPORT_TLS", "true").lower() != "false"


def build_client() -> Any:
    """A `redis.asyncio.Redis` from `VALKEY_URL`. Refuses a plaintext URL when TLS is required."""
    url = os.environ.get("VALKEY_URL", "rediss://valkey:6379/0")
    if _tls_required() and not url.startswith("rediss://"):
        raise ValueError(
            f"VALKEY_URL must use rediss:// when security.transport.tls is true (got {url!r})"
        )
    return redis_asyncio.from_url(url)


async def ensure_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP CREATE {stream} {group} $ MKSTREAM`.

    Tolerant of an already-existing group (BUSYGROUP).
    """
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def destroy_group(client: Any, *, stream: str, group: str) -> None:
    """`XGROUP DESTROY {stream} {group}`, tolerant of a stream/group that no longer exists."""
    try:
        await client.xgroup_destroy(stream, group)
    except redis.exceptions.ResponseError as exc:
        if "no such key" not in str(exc).lower() and "no such" not in str(exc).lower():
            raise
