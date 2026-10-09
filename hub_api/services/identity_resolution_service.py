"""Platform identity -> stable UUID resolution (PII boundary, issue #429).

Raw platform identifiers and handles exist only inside hub-api; callers
outside it (svc-ingest/svc-process/svc-action, bundles) get UUIDs only.
The resolution logic itself lives in the ``resolve_identity_uuid()``
Postgres function (alembic 0045) so the membership trigger, the backfill
and this service share one implementation. Every failure raises -- there
is no default/fallback UUID.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("hub_api.identity")
_meter = metrics.get_meter("hub_api.identity")
_resolve_counter = _meter.create_counter(
    "hub_api.identity.resolutions", description="Identity resolutions by outcome"
)
_resolve_latency = _meter.create_histogram(
    "hub_api.identity.resolve_duration_ms", unit="ms", description="Batch resolve latency"
)
_batch_size = _meter.create_histogram(
    "hub_api.identity.batch_size", description="Items per mint/resolve batch"
)

MAX_BATCH = 100
_PLATFORM_RE = re.compile(r"^[a-z0-9_-]{1,50}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class IdentityResolutionError(Exception):
    """Base for resolution failures; never swallowed into a default UUID."""


class IdentityValidationError(IdentityResolutionError):
    """Caller input failed validation (maps to INVALID_ARGUMENT)."""


class TenantNotFoundError(IdentityResolutionError):
    """Tenant id/slug did not resolve (maps to NOT_FOUND)."""


@dataclass(slots=True, frozen=True)
class IdentityRequest:
    """One (tenant, platform, platform_user_id) tuple to resolve."""

    tenant_id: str
    platform: str
    platform_user_id: str
    handle: str = ""


@dataclass(slots=True, frozen=True)
class ResolvedIdentity:
    """Resolved identity: only the platform id echoed back plus its UUID."""

    platform_user_id: str
    uuid: uuid.UUID


def _validate(item: IdentityRequest) -> None:
    """Reject malformed input at the boundary before touching the DB."""
    if not item.tenant_id or len(item.tenant_id) > 255 or _CONTROL_RE.search(item.tenant_id):
        raise IdentityValidationError("tenant_id invalid")
    if not _PLATFORM_RE.match(item.platform):
        raise IdentityValidationError("platform invalid")
    if (
        not item.platform_user_id
        or len(item.platform_user_id) > 255
        or _CONTROL_RE.search(item.platform_user_id)
    ):
        raise IdentityValidationError("platform_user_id invalid")
    if len(item.handle) > 255 or _CONTROL_RE.search(item.handle):
        raise IdentityValidationError("handle invalid")


async def _tenant_pk(async_dal: Any, tenant_ref: str) -> int:
    """Resolve a numeric id or slug to tenants.id; unknown tenant fails loud."""
    rows = await async_dal.executesql_async(
        "SELECT id FROM tenants WHERE id::text = %s OR slug = %s "
        "ORDER BY (id::text = %s) DESC LIMIT 1",
        [tenant_ref, tenant_ref, tenant_ref],
    )
    if not rows:
        raise TenantNotFoundError("tenant not found")
    return int(rows[0][0])


async def resolve_identities(
    async_dal: Any, items: Sequence[IdentityRequest]
) -> list[ResolvedIdentity]:
    """Resolve each tuple to its stable UUID (linked hub user, else pseudonym).

    Batch is bounded (<=100), validated, de-duplicated per (tenant, platform,
    platform_user_id); response order follows first occurrence. Any failure
    raises -- partial results are never returned.
    """
    if not items or len(items) > MAX_BATCH:
        raise IdentityValidationError(f"batch must contain 1..{MAX_BATCH} items")
    for item in items:
        _validate(item)
    started = time.monotonic()
    with _tracer.start_as_current_span("identity.resolve") as span:
        span.set_attribute("identity.batch_size", len(items))
        _batch_size.record(len(items))
        try:
            tenant_cache: dict[str, int] = {}
            seen: dict[tuple[str, str, str], ResolvedIdentity] = {}
            for item in items:
                if item.tenant_id not in tenant_cache:
                    tenant_cache[item.tenant_id] = await _tenant_pk(async_dal, item.tenant_id)
                key = (str(tenant_cache[item.tenant_id]), item.platform, item.platform_user_id)
                if key in seen:
                    continue
                rows = await async_dal.executesql_async(
                    "SELECT resolve_identity_uuid(%s, %s, %s, NULL, %s)",
                    [
                        tenant_cache[item.tenant_id],
                        item.platform,
                        item.platform_user_id,
                        item.handle or None,
                    ],
                )
                if not rows or rows[0][0] is None:
                    raise IdentityResolutionError("resolution returned no uuid")
                seen[key] = ResolvedIdentity(item.platform_user_id, uuid.UUID(str(rows[0][0])))
        except IdentityResolutionError as exc:
            _resolve_counter.add(len(items), {"outcome": type(exc).__name__})
            logger.warning(
                "identity resolution rejected",
                extra={"action": "identity_resolve", "result": type(exc).__name__},
            )
            raise
        except Exception as exc:
            _resolve_counter.add(len(items), {"outcome": "error"})
            logger.error(
                "identity resolution failed",
                extra={"action": "identity_resolve", "result": type(exc).__name__},
            )
            raise IdentityResolutionError("resolution failed") from exc
        _resolve_counter.add(len(items), {"outcome": "ok"})
        _resolve_latency.record((time.monotonic() - started) * 1000.0)
        logger.debug(
            "identity resolved",
            extra={"action": "identity_resolve", "count": len(seen), "result": "ok"},
        )
        return list(seen.values())


async def resolve_identity(
    async_dal: Any, tenant_id: str, platform: str, platform_user_id: str
) -> uuid.UUID:
    """Resolve one platform identity to its UUID (the index secret/lastseen key on)."""
    out = await resolve_identities(
        async_dal, [IdentityRequest(tenant_id, platform, platform_user_id)]
    )
    return out[0].uuid
