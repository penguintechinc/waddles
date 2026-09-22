"""svc-action's real poll -> RPOP -> load -> dispatch -> record loop.

Mirrors `core/svc_process/runner.py`'s shape exactly (poll hub-api's
distribution endpoint, RPOP each active bundle's own Valkey stage key,
load its real entrypoint via `flask_core.stage_runner.load_entrypoint`) --
action is the pipeline's terminal stage though, so where process LPUSHes
a transformed event onto the next stage's key, action instead *invokes*
the bundle's entrypoint against each popped envelope with
retry-with-backoff, then records the outcome to `action_dispatch_log`
(`services/dispatch_log.py`). See `config.py`'s module docstring for why
action's config (and this runner) genuinely differs from ingest/process
in exactly two ways: a DB connection for the audit log, and
retry-with-backoff around each dispatch (a real external system, not a
pure in-memory transform).

An action-stage entrypoint's contract is `async def <name>(envelope:
StageEnvelope, config: Mapping[str, Any], *, http_client: httpx.
AsyncClient) -> TransportResult` -- `StageEnvelope` is `flask_core`'s
shared, frozen stage-to-stage contract (`flask_core.stream_pipeline`),
not a local svc-action type; a bundle reaches message data at
`envelope.event.payload[...]`, never `envelope.payload[...]`. Richer than
ingest/process's `fn(event: dict) -> dict` pure-transform contract, since
a bundle dispatching externally needs the full envelope (tenant/
community/ts, not just the event payload), an HTTP client, and must
report back a classified success/failure (`waddle_transports.base.
TransportResult`/`RetryableTransportError`/`NonRetryableTransportError`)
rather than just returning a value.

Delivery primitives (`waddle_transports.transports.{http,message_queue,
irc,socket,overlay,email}` -- `libs/waddle_transports`, a shared library
also imported by svc-ingest for inbound) are NOT wired into this loop's
routing -- they are a library an action bundle's own script may import
and call for its actual delivery mechanism
(`waddle_transports.registry.get_transport`), or, like `bundles/
discord_send_action.py`, a bundle may implement its own connector-
specific API logic entirely. Routing -- *which* bundle handles a given
envelope -- is solely "whichever bundle's own `:action` queue key the
envelope was popped from", exactly mirroring ingest/process; there is no
central transport-type-driven registry deciding that.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx
from flask_core import AsyncDAL, StageEnvelope, bundle_context
from flask_core.circuit_breaker import retry_with_backoff
from flask_core.stage_runner import (
    BundleDistribution,
    BundlePoller,
    EntrypointLoadError,
    load_entrypoint,
)
from flask_core.stream_pipeline import bundle_stream_key
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult

from services.dispatch_log import record_dispatch
from services.envelope import EnvelopeError, parse_envelope

logger = logging.getLogger(__name__)


class TenantResolutionError(RuntimeError):
    """Raised when an envelope's tenant slug has no matching `tenants` row.

    Caught by `_record`'s own broad handler -- an unresolvable tenant slug
    must fail the *audit write*, never the dispatch outcome it's trying to
    record.
    """


def _parse_envelope_ts(ts: str) -> datetime | None:
    """Best-effort ISO8601 parse of `envelope.ts` -- audit-log-only, never fatal."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class ActionRunner:
    """One poll+drain+dispatch cycle per `run_once()`; `run_forever()` loops it in production."""

    def __init__(
        self,
        *,
        poller: BundlePoller,
        redis_client: Any,
        dal: AsyncDAL,
        http_client: httpx.AsyncClient,
        tenant_slug: str,
        max_retries: int,
        retry_initial_delay: float,
        retry_max_delay: float,
    ) -> None:
        """Build a runner bound to one poller, one Valkey client, one DAL, one tenant scope."""
        self._poller = poller
        self._redis = redis_client
        self._dal = dal
        self._http_client = http_client
        self._tenant_slug = tenant_slug
        self._max_retries = max_retries
        self._retry_initial_delay = retry_initial_delay
        self._retry_max_delay = retry_max_delay
        self._running = False
        # tenant slug -> tenants.id, memoized per runner process -- one
        # runner instance dispatches for one tenant scope (`tenant_slug`
        # above) for its entire lifetime, so this is at most a handful of
        # entries, never an unbounded cache.
        self._tenant_id_cache: dict[str, int] = {}

    def stop(self) -> None:
        """Signal `run_forever()` to exit after its current iteration."""
        self._running = False

    async def run_forever(self) -> None:
        """Production loop: poll, drain+dispatch every active bundle's queue, sleep, repeat."""
        self._running = True
        while self._running:
            await self.run_once()
            await asyncio.sleep(self._poller.next_delay_s)

    async def run_once(self) -> int:
        """One poll+drain+dispatch cycle; returns total envelopes dispatched. Never raises."""
        bundles = await self._poller.poll_once()
        total = 0
        for bundle in bundles:
            total += await self._dispatch_bundle(bundle)
        return total

    async def _dispatch_bundle(self, bundle: BundleDistribution) -> int:
        if bundle.entrypoint is None:
            logger.info("action.no_entrypoint app_id=%s -- skipping", bundle.app_id)
            return 0

        try:
            entrypoint_fn = load_entrypoint(bundle.entrypoint)
        except EntrypointLoadError as exc:
            logger.error(
                "action.entrypoint_load_failed app_id=%s entrypoint=%s error=%s",
                bundle.app_id,
                bundle.entrypoint,
                exc,
            )
            return 0

        community_str: str | None = (
            str(bundle.community_id) if bundle.community_id is not None else None
        )
        action_key = bundle_stream_key(self._tenant_slug, community_str, bundle.app_id, "action")

        count = 0
        while True:
            raw = await self._redis.rpop(action_key)
            if raw is None:
                break
            count += await self._handle_envelope(raw, entrypoint_fn, bundle)
        return count

    async def _handle_envelope(
        self, raw: Any, entrypoint_fn: Any, bundle: BundleDistribution
    ) -> int:
        try:
            envelope = parse_envelope(raw)
        except EnvelopeError as exc:
            logger.error("action.bad_envelope app_id=%s error=%s", bundle.app_id, exc)
            return 0

        attempt_count = 0

        async def _attempt() -> TransportResult:
            nonlocal attempt_count
            attempt_count += 1
            try:
                # Action bundles already receive the full `envelope`
                # (tenant/community/app_id), unlike process's bare
                # `PlatformEvent` -- `bundle_context` is set here anyway so
                # both stages expose the identical `get_bundle_context()`
                # accessor (docs/APP_BUNDLE_AUTHORING.md, 'Accessing the
                # database / shared state').
                with bundle_context(
                    tenant=envelope.tenant,
                    community=envelope.community,
                    app_id=envelope.app_id,
                ):
                    result = await entrypoint_fn(
                        envelope, bundle.config, http_client=self._http_client
                    )
            except (RetryableTransportError, NonRetryableTransportError):
                raise
            except Exception as exc:  # noqa: BLE001 -- unclassified bundle-script bug, not transient
                raise NonRetryableTransportError(
                    f"bundle entrypoint {bundle.entrypoint!r} raised: {exc}"
                ) from exc
            if not isinstance(result, TransportResult):
                raise NonRetryableTransportError(
                    f"bundle entrypoint {bundle.entrypoint!r} returned "
                    f"{type(result).__name__}, expected TransportResult"
                )
            return result

        try:
            result = await retry_with_backoff(
                _attempt,
                max_retries=self._max_retries,
                initial_delay=self._retry_initial_delay,
                max_delay=self._retry_max_delay,
                exceptions=(RetryableTransportError,),
            )
        except NonRetryableTransportError as exc:
            logger.error(
                "action.dispatch_failed_non_retryable app_id=%s entrypoint=%s error=%s",
                bundle.app_id,
                bundle.entrypoint,
                exc,
            )
            await self._record(
                envelope,
                bundle,
                target_type="bundle",
                status="non_retryable_failure",
                attempt=attempt_count,
                http_status=exc.http_status,
                detail=str(exc),
            )
            return 0
        except RetryableTransportError as exc:
            logger.error(
                "action.dispatch_failed_retries_exhausted app_id=%s entrypoint=%s error=%s",
                bundle.app_id,
                bundle.entrypoint,
                exc,
            )
            await self._record(
                envelope,
                bundle,
                target_type="bundle",
                status="retryable_failure",
                attempt=attempt_count,
                http_status=exc.http_status,
                detail=str(exc),
            )
            return 0

        target_type = (
            result.transport if result.sub_type is None else f"{result.transport}:{result.sub_type}"
        )
        await self._record(
            envelope,
            bundle,
            target_type=target_type,
            status="success",
            attempt=attempt_count,
            http_status=result.http_status,
            detail=result.detail,
        )
        return 1

    async def _resolve_tenant_id(self, tenant_slug: str) -> int:
        """Resolve a `StageEnvelope.tenant` slug to its `tenants.id` FK, memoized per slug.

        `action_dispatch_log.tenant_id` is an integer FK (migration 074),
        while `envelope.tenant` is a slug string (e.g. `"global"`,
        `config.py`'s own `RUNNER_TENANT_SLUG` default) -- `int(envelope.
        tenant)` crashes on any real slug. Looks the slug up against the
        `tenants` table discovered by `penguin_dal.AsyncDB.reflect()` at
        startup (`app.py::startup()`). Raises `TenantResolutionError` if no
        row matches -- `_record`'s own broad `except Exception` catches it,
        same as any other audit-write failure.
        """
        cached = self._tenant_id_cache.get(tenant_slug)
        if cached is not None:
            return cached

        rows = await self._dal(self._dal.tenants.slug == tenant_slug).select(limitby=(0, 1))
        row = rows.first()
        if row is None:
            raise TenantResolutionError(f"tenant slug {tenant_slug!r} has no matching tenants row")

        tenant_id = int(row.id)
        self._tenant_id_cache[tenant_slug] = tenant_id
        return tenant_id

    async def _record(
        self,
        envelope: StageEnvelope,
        bundle: BundleDistribution,
        *,
        target_type: str,
        status: str,
        attempt: int,
        http_status: int | None,
        detail: str,
    ) -> None:
        try:
            tenant_id = await self._resolve_tenant_id(envelope.tenant)
            await record_dispatch(
                self._dal,
                tenant_id=tenant_id,
                community_id=int(envelope.community) if envelope.community else None,
                app_id=bundle.app_id,
                target_type=target_type,
                status=status,
                attempt=attempt,
                http_status=http_status,
                detail=detail,
                envelope_ts=_parse_envelope_ts(envelope.ts),
            )
        except Exception as exc:  # noqa: BLE001 -- audit-log write failure must never mask the outcome
            logger.error("action.audit_log_write_failed app_id=%s error=%s", bundle.app_id, exc)
