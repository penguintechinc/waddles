"""Reconcile every `app_source_bindings` row to a live Valkey consumer group (control-plane writer).

Why this exists (alpha incident 2026-11-14, `!ping` got no reply): a binding's consumer group is
created exactly once, by `bundle_approval_service` right after an activation commits
(`app_source_binding_service.provision_source_stream_groups`). The source stream key embeds the
tenant SLUG and community NAME (`waddles:t:{slug}:c:{name}:src:{platform}:{source_id}:events`,
penguin-spine `Scope::source_stream`), and both are mutable: migration 0098 renamed the catch-all
tenant `global` -> `default` (keeping its id). Bindings whose apps were activated BEFORE the rename
kept their groups on the old `...t:global:...` stream while svc-ingest XADDs to, and svc-process
`XREADGROUP`s from, the new `...t:default:...` stream -- where those apps never had a group. Every
read answered `NOGROUP` forever: nothing re-ran the provisioning step because nothing re-approves
an already-active app.

The DB (`app_source_bindings` joined to `tenants`/`communities`) is the source of truth for which
`(stream, group)` pairs must exist, so the fix is a level-triggered reconcile, not another
edge-triggered call site: this module computes every binding's CURRENT stream key and idempotently
`XGROUP CREATE`s the missing groups -- at hub-api startup and then every interval, in every replica
(`XGROUP CREATE` is idempotent: `BUSYGROUP` == already there). It also self-heals any future slug/
name rename, a flushed Valkey, or a failed best-effort provisioning call.

Why hub-api and not the data plane: hub-api is the control-plane writer of consumer-group lifecycle
(`valkey_admin_client` docstring, spec Sec5.2); svc-process reads only. The binding row IS the
grant, and a consumer group's existence on a stream is part of the grant surface an admin governs --
letting each data-plane replica mint groups would move that authority out of the control plane.

Start position is `$` (same as `valkey_admin_client.ensure_group`): a reconciled group sees only
events published AFTER it exists. `0` would replay the stream's whole retained backlog into a bundle
that never saw it -- re-running days-old `!roll`/`!giveaway` commands is a worse failure than the
single missed event, which the operator (and the WARN below) can account for. Events published
while the group was missing (the NOGROUP window) are therefore not recovered; that is logged.

Failures are LOUD: every failed `(stream, group)` is an ERROR log (type + message, PII-free) and a
`waddles_hub_source_group_reconcile_groups_total{result="failed"}` data point; a failed pass is
`waddles_hub_source_group_reconcile_passes_total{result="error"}`. One failing binding never stops
the rest of the pass, and a dead Valkey/DB never breaks hub-api serving (the loop retries).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import redis.exceptions
from flask_core.stream_pipeline import source_stream_key

from services import valkey_admin_client
from services.bundle_install_dal import raw_sql_rows
from services.bundle_telemetry import get_meter

logger = logging.getLogger(__name__)

#: `app_source_bindings.community_id` value for a tenant-wide binding (migration 0025); renders
#: as `c:_tenant` in the stream key (`source_stream_key(community=None)`).
TENANT_WIDE_COMMUNITY_SENTINEL: Final = 0

#: Seconds between reconcile passes. Env-tunable (`SOURCE_GROUP_RECONCILE_INTERVAL_SECONDS`).
DEFAULT_INTERVAL_SECONDS: Final = 60.0

_BINDINGS_SQL: Final = """
SELECT b.tenant_id, b.community_id, b.app_id, b.platform, b.source_id,
       t.slug AS tenant_slug, c.name AS community_name
  FROM app_source_bindings b
  JOIN tenants t ON t.id = b.tenant_id
  LEFT JOIN communities c ON c.id = b.community_id AND c.tenant_id = b.tenant_id
 ORDER BY b.tenant_id, b.community_id, b.app_id, b.platform, b.source_id
"""

#: Builds the (sync-constructed) async Valkey client for one pass.
ClientFactory = Callable[[], Any]


@dataclass(slots=True)
class ReconcileResult:
    """Outcome of one reconcile pass; `examined` is the denominator for every other count."""

    examined: int = 0
    created: int = 0
    already_present: int = 0
    failed: int = 0
    unresolvable: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every examined binding now has its group (and at least one was examined)."""
        return self.failed == 0 and self.unresolvable == 0


@dataclass(slots=True)
class _Instruments:
    groups: Any
    passes: Any
    duration_ms: Any


_INSTRUMENTS: _Instruments | None = None


def _instruments() -> _Instruments:
    """Lazily bind the counters/histogram to the process meter (no-op without a provider)."""
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        meter = get_meter()
        _INSTRUMENTS = _Instruments(
            groups=meter.create_counter(
                "waddles_hub_source_group_reconcile_groups_total",
                description="source-stream consumer groups reconciled, by result "
                "(created|exists|failed|unresolvable)",
            ),
            passes=meter.create_counter(
                "waddles_hub_source_group_reconcile_passes_total",
                description="source-group reconcile passes, by result (ok|error)",
            ),
            duration_ms=meter.create_histogram(
                "waddles_hub_source_group_reconcile_duration_ms",
                unit="ms",
                description="wall time of one source-group reconcile pass",
            ),
        )
    return _INSTRUMENTS


def _emit_group(result: str) -> None:
    """Count one binding's outcome; telemetry failure is logged, never raised."""
    try:
        _instruments().groups.add(1, {"result": result})
    except Exception:  # noqa: BLE001 - a dead exporter must never fail the reconcile
        logger.debug("source group reconcile: metric emit failed", exc_info=True)


def _emit_pass(result: str, started: float) -> None:
    """Count one pass and record its duration; never raises."""
    try:
        instruments = _instruments()
        instruments.passes.add(1, {"result": result})
        instruments.duration_ms.record((time.monotonic() - started) * 1000.0, {"result": result})
    except Exception:  # noqa: BLE001 - a dead exporter must never fail the reconcile
        logger.debug("source group reconcile: metric emit failed", exc_info=True)


async def _ensure(client: Any, *, stream: str, group: str) -> bool:
    """`XGROUP CREATE stream group $ MKSTREAM`; True if created, False if it already existed.

    `BUSYGROUP` is the idempotent "already there" answer; anything else (NOPERM, connection
    loss, WRONGTYPE) propagates to the caller, which counts and logs it.
    """
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            return False
        raise
    return True


async def reconcile_once(install_dal: Any, client: Any) -> ReconcileResult:
    """One level-triggered pass: ensure the group for EVERY `app_source_bindings` row.

    Args:
        install_dal: The hub-api `AsyncDB` (reads `app_source_bindings`/`tenants`/`communities`).
        client: A `redis.asyncio.Redis` authorised to `XGROUP CREATE` (real or test double).

    Returns:
        A `ReconcileResult`; `examined` is the number of binding rows read. Per-binding failures
        are recorded (ERROR log + metric) and never abort the pass.
    """
    rows = await raw_sql_rows(install_dal, _BINDINGS_SQL)
    result = ReconcileResult()
    for row in rows:
        result.examined += 1
        community_id = int(row.community_id)
        if community_id != TENANT_WIDE_COMMUNITY_SENTINEL and not row.community_name:
            result.unresolvable += 1
            _emit_group("unresolvable")
            logger.error(
                "source group reconcile: binding references a missing community, group not created",
                extra={
                    "app_id": row.app_id,
                    "tenant_id": row.tenant_id,
                    "community_id": community_id,
                    "platform": row.platform,
                    "op": "source_group_reconcile",
                },
            )
            continue
        segment = (
            None if community_id == TENANT_WIDE_COMMUNITY_SENTINEL else str(row.community_name)
        )
        stream = source_stream_key(str(row.tenant_slug), segment, row.platform, row.source_id)
        try:
            created = await _ensure(client, stream=stream, group=row.app_id)
        except Exception as exc:  # noqa: BLE001 - isolate per binding; counted + logged loudly
            result.failed += 1
            result.failures.append(f"{type(exc).__name__}: {exc}")
            _emit_group("failed")
            logger.error(
                "source group reconcile: XGROUP CREATE failed, consumers will see NOGROUP",
                extra={
                    "app_id": row.app_id,
                    "tenant_id": row.tenant_id,
                    "community_id": community_id,
                    "platform": row.platform,
                    "stream_key": stream,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "op": "source_group_reconcile",
                },
                exc_info=True,
            )
            continue
        if created:
            result.created += 1
            _emit_group("created")
            logger.warning(
                "source group reconcile: provisioned a missing consumer group (events published "
                "before this point were not delivered to it)",
                extra={
                    "app_id": row.app_id,
                    "tenant_id": row.tenant_id,
                    "community_id": community_id,
                    "platform": row.platform,
                    "stream_key": stream,
                    "start_id": "$",
                    "op": "source_group_reconcile",
                },
            )
        else:
            result.already_present += 1
            _emit_group("exists")
    return result


async def run_reconcile_loop(
    install_dal: Any,
    *,
    client_factory: ClientFactory | None = None,
    interval_seconds: float | None = None,
    is_ready: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Reconcile at startup, then every `interval_seconds`, forever; a bad pass is retried.

    Started once from hub-api startup, in every replica (idempotent). `is_ready` gates a pass
    until the schema bootstrap finished so a fresh/behind schema is not logged as a failure.
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else float(
            os.environ.get("SOURCE_GROUP_RECONCILE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        )
    )
    factory = client_factory or valkey_admin_client.build_client
    while True:
        if is_ready is None or is_ready():
            await run_pass(install_dal, factory)
        await sleep(interval)


async def run_pass(install_dal: Any, client_factory: ClientFactory) -> ReconcileResult | None:
    """One pass with its own client; any error is logged at ERROR + counted, never raised."""
    started = time.monotonic()
    client: Any = None
    try:
        client = client_factory()
        result = await reconcile_once(install_dal, client)
    except asyncio.CancelledError:
        raise
    except Exception:
        _emit_pass("error", started)
        logger.exception(
            "source group reconcile: pass failed", extra={"op": "source_group_reconcile"}
        )
        return None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - closing a dead connection must not mask the result
                logger.debug("source group reconcile: client close failed", exc_info=True)
    _emit_pass("ok" if result.ok else "error", started)
    log = logger.info if result.ok else logger.error
    log(
        "source group reconcile: pass complete",
        extra={
            # NB: never name an extra key `created` -- it is a reserved LogRecord attribute and
            # logging raises KeyError, which would kill the reconcile loop after a pass.
            "bindings_examined": result.examined,
            "groups_created": result.created,
            "groups_already_present": result.already_present,
            "groups_failed": result.failed,
            "bindings_unresolvable": result.unresolvable,
            "op": "source_group_reconcile",
        },
    )
    return result
