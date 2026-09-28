"""svc-ingest's real poll -> pull -> normalize -> enqueue loop.

Separated from `app.py` so `run_once()`/`_process_bundle()` are directly
unit-testable (mock the distribution poll, real in-memory/fakeredis Valkey
round-trip) without booting a Quart app or an infinite loop. Production
wiring (`app.py`'s `@app.before_serving`) calls `run_forever()`.

Per-bundle Valkey isolation keys reuse `flask_core.stream_pipeline.
bundle_stream_key` exactly (`waddles:t:{tenant}:c:{community}:app:{app_id}:
{stage}`, App Bundle SDK Phase C4): ingest RPOPs raw inbound events off its
OWN `:ingest` key (populated by whatever external receiver/webhook -- out
of scope for this PR) and LPUSHes the normalized result onto that same
bundle's `:process` key, symmetric with `process` popping `:process` and
pushing `:action` (task-specified). This is this PR's own interpretation of
where a normalized ingest event goes next -- the task only specified
process's destination explicitly; using the bundle's own 3-key convention
for ingest's output keeps the naming self-consistent rather than inventing
a fourth key.

Wave 2 (pipeline-standardization): the enqueued message is now the frozen
typed contract, `flask_core.StageEnvelope` (`event` field holding a
`flask_core.PlatformEvent`), serialized via `.to_dict()` -- not an ad-hoc
dict. A raw `:ingest` entry is always a bare, transport-specific event dict
(never our own envelope shape -- `fanout.fan_out_event`/the receivers only
ever push raw platform dicts); the old defensive "unwrap if it looks like
one of our own envelopes" heuristic is gone with it -- ALPHA has no
legacy-shape tolerance, so a bundle entrypoint that doesn't return a
`PlatformEvent` is a hard `EnvelopeError`, not silently coerced.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from flask_core import EnvelopeError, PlatformEvent, StageEnvelope
from flask_core.stage_runner import (
    BundleDistribution,
    BundlePoller,
    EntrypointLoadError,
    load_entrypoint,
)
from flask_core.stream_pipeline import bundle_stream_key
from prometheus_client import Counter

from identity_crypto import DekProvider, DekUnavailableError, encrypt_identity_value

logger = logging.getLogger(__name__)

# critical-rules.md PII Tokenization / security.md: the `:process` Valkey key
# lives outside the PII boundary (only hub-api holds raw PII), so `actor`
# (the one identity field this pipeline carries -- username/login/display
# name/handle) is AES-256-GCM envelope-encrypted here before LPUSH, never
# written plaintext. See `identity_crypto` module doc for the wire format
# and the (documented, not-yet-implemented) hub-api keystore dependency.
_identity_encryption_total = Counter(
    "waddlebot_ingest_identity_encryption_total",
    "Identity-field encryption outcomes on the ingest->process stream",
    ["tenant", "status"],
)


class IngestRunner:
    """One poll+drain cycle per call to `run_once()`; `run_forever()` loops it in production."""

    def __init__(
        self,
        *,
        poller: BundlePoller,
        redis_client: Any,
        tenant_slug: str,
        dek_provider: DekProvider,
    ) -> None:
        """Build a runner bound to one `BundlePoller`, one Valkey client, and one tenant scope.

        `dek_provider` resolves this tenant's identity-field DEK (see
        `identity_crypto.DekProvider`) -- required, never optional: a
        runner with no way to resolve a DEK must fail closed on every
        event rather than silently write plaintext.
        """
        self._poller = poller
        self._redis = redis_client
        self._tenant_slug = tenant_slug
        self._dek_provider = dek_provider
        self._running = False

    def stop(self) -> None:
        """Signal `run_forever()` to exit after its current iteration."""
        self._running = False

    async def run_forever(self) -> None:
        """Production loop: poll, drain every active bundle's ingest queue, sleep, repeat."""
        self._running = True
        while self._running:
            await self.run_once()
            import asyncio

            await asyncio.sleep(self._poller.next_delay_s)

    async def run_once(self) -> int:
        """One poll+drain cycle; returns total events normalized+enqueued. Never raises."""
        bundles = await self._poller.poll_once()
        total = 0
        for bundle in bundles:
            total += await self._process_bundle(bundle)
        return total

    async def _process_bundle(self, bundle: BundleDistribution) -> int:
        if bundle.entrypoint is None:
            logger.info("ingest.no_entrypoint app_id=%s -- skipping", bundle.app_id)
            return 0

        try:
            normalize_fn = load_entrypoint(bundle.entrypoint)
        except EntrypointLoadError as exc:
            logger.error(
                "ingest.entrypoint_load_failed app_id=%s entrypoint=%s error=%s",
                bundle.app_id,
                bundle.entrypoint,
                exc,
            )
            return 0

        community_str: str | None = (
            str(bundle.community_id) if bundle.community_id is not None else None
        )
        ingest_key = bundle_stream_key(self._tenant_slug, community_str, bundle.app_id, "ingest")
        process_key = bundle_stream_key(self._tenant_slug, community_str, bundle.app_id, "process")

        count = 0
        while True:
            raw = await self._redis.rpop(ingest_key)
            if raw is None:
                break
            count += await self._normalize_and_enqueue(
                raw,
                normalize_fn,
                bundle=bundle,
                community=community_str,
                process_key=process_key,
            )
        return count

    async def _normalize_and_enqueue(
        self,
        raw: Any,
        normalize_fn: Any,
        *,
        bundle: BundleDistribution,
        community: str | None,
        process_key: str,
    ) -> int:
        """Parse + normalize one raw event; LPUSH the typed `StageEnvelope`.

        Returns 1 on success, 0 on skip. `raw` is always a bare,
        transport-specific event dict (`fanout.fan_out_event`/the
        receivers never push our own envelope shape onto `:ingest`) -- no
        unwrap heuristic. A `normalize_fn` that fails, or that doesn't
        return a `PlatformEvent`, is logged and the one bad event skipped;
        the poll loop itself never dies on it.
        """
        try:
            raw_event = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.error("ingest.bad_json app_id=%s error=%s", bundle.app_id, exc)
            return 0

        try:
            event = await normalize_fn(raw_event)
            if not isinstance(event, PlatformEvent):
                raise EnvelopeError(
                    f"bundle entrypoint {bundle.entrypoint!r} must return a PlatformEvent, "
                    f"got {type(event).__name__}"
                )
        except Exception as exc:  # noqa: BLE001 - one bad event must never kill the loop
            logger.error("ingest.normalize_failed app_id=%s error=%s", bundle.app_id, exc)
            return 0

        # D30's `event_id` is minted here (svc-ingest is the first stage to
        # see this event) and copied verbatim downstream -- it doubles as
        # the identity-field AAD's row-equivalent binding, so a ciphertext
        # can never be replayed against a different event.
        event_id = str(uuid.uuid4())

        if event.actor is not None:
            try:
                dek, dek_version = await self._dek_provider.get_dek(self._tenant_slug)
            except DekUnavailableError as exc:
                # Fail closed (critical-rules.md Verification Integrity /
                # security.md): never write actor plaintext to a Valkey key
                # outside the PII boundary. Hold the raw pre-normalized
                # event in a dedicated dead-letter key for retry once the
                # tenant's DEK is available again, instead of dropping it.
                _identity_encryption_total.labels(
                    tenant=self._tenant_slug, status="deadlettered"
                ).inc()
                logger.error(
                    "ingest.identity_dek_unavailable app_id=%s tenant=%s event_id=%s error=%s "
                    "-- dead-lettering, never writing plaintext identity",
                    bundle.app_id,
                    self._tenant_slug,
                    event_id,
                    exc,
                )
                deadletter_key = f"{process_key}:identity_deadletter"
                await self._redis.lpush(
                    deadletter_key,
                    json.dumps({"raw": raw_event, "event_id": event_id, "app_id": bundle.app_id}),
                )
                return 0

            ciphertext_envelope = encrypt_identity_value(
                event.actor,
                dek,
                dek_version=dek_version,
                tenant_id=self._tenant_slug,
                stream=process_key,
                field="actor",
                event_id=event_id,
            )
            event = replace(event, actor=json.dumps(ciphertext_envelope))
            _identity_encryption_total.labels(tenant=self._tenant_slug, status="success").inc()

        envelope = StageEnvelope(
            tenant=self._tenant_slug,
            community=community,
            app_id=bundle.app_id,
            stage="process",
            event=event,
            ts=datetime.now(UTC).isoformat(),
            event_id=event_id,
        )

        await self._redis.lpush(process_key, json.dumps(envelope.to_dict()))
        return 1
