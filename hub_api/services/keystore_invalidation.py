"""`keys:tenant-dek:invalidate` publisher (spec Sec5c).

A Valkey STREAM (not pub/sub -- durable, replayable, matching the
existing `waddles:usage` XADD convention in
`services/usage_aggregator_service.py` rather than introducing a second
messaging primitive). `svc_ingest`/`svc_process` MUST drop their cached
`(tenant_id, purpose)` key on receipt of any entry (spec Sec5c) -- this
module only publishes; the consumer-side contract lives in PR #443.
"""

from __future__ import annotations

from typing import Any

from services.valkey_admin_client import build_client

INVALIDATION_STREAM = "keys:tenant-dek:invalidate"


def build_invalidation_publisher() -> Any:
    """Return an `(tenant_id, purpose, old_dek_version, reason) -> None` async callable.

    Matches `TenantKeystore.invalidation_publisher`'s shape exactly.
    Lazily builds its own Valkey client on first call (mirrors
    `valkey_admin_client.build_client()`'s own no-connect-until-used
    posture) so constructing this at import/startup time never blocks or
    fails if Valkey happens to be unreachable at that instant -- the
    first rotation/shred after Valkey recovers succeeds normally.
    """
    client_holder: dict[str, Any] = {}

    async def _publish(tenant_id: int, purpose: str, old_dek_version: int, reason: str) -> None:
        client = client_holder.get("client")
        if client is None:
            client = build_client()
            client_holder["client"] = client
        await client.xadd(
            INVALIDATION_STREAM,
            {
                "tenant_id": str(tenant_id),
                "purpose": purpose,
                "version": str(old_dek_version),
                "reason": reason,
            },
        )

    return _publish
