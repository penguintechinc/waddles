"""Publishes the `dek-rotated` invalidation event the Rust data plane consumes.

The data plane (`core/bundle_host_crypto`, `run_dek_invalidation_listener`) caches
unwrapped tenant DEKs and evicts a tenant's entries when an entry on
:data:`DEK_INVALIDATION_STREAM` carries ``purpose == "at-rest"`` and a ``tenant`` field.
The event is a pure signal: tenant id, purpose and a reason, never key material or wrapped
bytes. It matches the existing spine invalidation streams (`bundle:grants:invalidate`),
which are unauthenticated plain `XADD`s protected by the Valkey ACL + TLS transport
(`valkey_admin_client.build_client`); a forged entry can only cause an extra cache
eviction, never a key disclosure.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from services import valkey_admin_client
from services.envelope import metrics
from services.envelope.errors import EnvelopeError

logger = logging.getLogger(__name__)

#: Stream the Rust listener reads (`DEK_INVALIDATION_STREAM` in bundle_host_crypto/provider.rs).
DEK_INVALIDATION_STREAM = "keys:tenant-dek:invalidate"
#: Purpose label the listener filters on (`BUNDLE_DATA_PURPOSE`).
AT_REST_PURPOSE = "at-rest"
#: Approximate cap so an unread stream cannot grow without bound (entries are tiny signals).
STREAM_MAXLEN = 10_000

InvalidationReason = Literal["rotated", "revoked"]
#: `async (tenant_id, reason) -> None`; raises :class:`DekInvalidationPublishError` on failure.
DekInvalidationPublisher = Callable[[int, InvalidationReason], Awaitable[None]]


class DekInvalidationPublishError(EnvelopeError):
    """The `dek-rotated` event could not be published (the DEK change itself already committed).

    Data-plane caches then rely on their TTL to drop the stale DEK, so this is an ops alert,
    never swallowed.
    """


async def publish_dek_invalidation(
    tenant_id: int, reason: InvalidationReason, *, client: Any | None = None
) -> None:
    """`XADD keys:tenant-dek:invalidate` with ``tenant``, ``purpose=at-rest`` and ``reason``.

    Raises:
        DekInvalidationPublishError: the Valkey write failed (also logged at ERROR and counted).
    """
    owned = client is None
    try:
        conn = client if client is not None else valkey_admin_client.build_client()
    except Exception as exc:
        metrics.record_operation("dek_invalidation_publish", "error")
        logger.exception("envelope.invalidation.client_failed", extra={"tenant_id": tenant_id})
        raise DekInvalidationPublishError("dek invalidation client unavailable") from exc
    try:
        await conn.xadd(
            DEK_INVALIDATION_STREAM,
            {"tenant": str(tenant_id), "purpose": AT_REST_PURPOSE, "reason": reason},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        metrics.record_operation("dek_invalidation_publish", "error")
        logger.error(
            "envelope.invalidation.publish_failed",
            extra={"tenant_id": tenant_id, "reason": reason, "alert": "dek-invalidation-lost"},
            exc_info=True,
        )
        raise DekInvalidationPublishError("dek invalidation publish failed") from exc
    finally:
        if owned:
            await conn.aclose()
    metrics.record_operation("dek_invalidation_publish", "ok")
    logger.info("envelope.invalidation.published", extra={"tenant_id": tenant_id, "reason": reason})
