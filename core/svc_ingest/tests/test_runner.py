"""Tests for `runner.IngestRunner` -- the real poll -> RPOP -> normalize -> LPUSH loop.

`redis_client` is `fakeredis.FakeAsyncRedis` (real LIST semantics, not a
mock); the distribution poll is mocked at the HTTP transport layer
(`httpx.MockTransport`) since hub-api itself is out of process for a unit
test -- `BundlePoller`'s own contract (backoff/graceful-degrade) is already
covered by `libs/flask_core/tests/test_stage_runner.py`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from flask_core import StageEnvelope
from flask_core.stage_runner import BundlePoller
from flask_core.stream_pipeline import bundle_stream_key

from runner import IngestRunner

TENANT = "acme-corp"
APP_ID = "waddles.core.demo.echo"


def _distribution_handler(bundles: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "stage": "ingest", "bundles": bundles, "meta": {}}
        )

    return handler


def _make_poller(http_client_factory: Any, bundles: list[dict[str, Any]]) -> BundlePoller:
    client = http_client_factory(_distribution_handler(bundles))
    return BundlePoller(
        client,
        "http://hub-api/api/v1/distribution/bundles",
        stage="ingest",
        jwt_provider=lambda: "t",
    )


class TestRunOnce:
    async def test_no_bundles_processes_nothing(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        poller = _make_poller(http_client_factory, [])
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        assert await runner.run_once() == 0

    async def test_real_valkey_roundtrip_normalizes_and_enqueues(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """Fail-first proof: prove a real LPUSH/RPOP round trip, not a simulated one.

        Seed a raw event onto the bundle's real `:ingest` Valkey key
        (fakeredis LPUSH), run the real ingest loop against the real
        `bundles.echo_ingest.normalize` entrypoint, and assert the
        normalized envelope actually landed on the bundle's real
        `:process` key -- both ends of the round trip verified against
        genuine LIST semantics, not a mocked call.
        """
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": 42,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )

        ingest_key = bundle_stream_key(TENANT, "42", APP_ID, "ingest")
        await redis_client.lpush(
            ingest_key, json.dumps({"source": "twitch", "text": "  Hello  Waddlebot  "})
        )

        processed = await runner.run_once()
        assert processed == 1

        # The raw event is consumed off :ingest (real queue semantics).
        assert await redis_client.rpop(ingest_key) is None

        process_key = bundle_stream_key(TENANT, "42", APP_ID, "process")
        raw_out = await redis_client.rpop(process_key)
        assert raw_out is not None
        envelope = StageEnvelope.from_dict(json.loads(raw_out))
        assert envelope.tenant == TENANT
        assert envelope.community == "42"  # StageEnvelope.community is a string slug
        assert envelope.app_id == APP_ID
        assert envelope.stage == "process"
        assert envelope.event.platform == "twitch"
        assert envelope.event.payload["text"] == "Hello  Waddlebot"
        assert envelope.ts

    async def test_malformed_json_in_queue_is_skipped_not_fatal(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """A non-JSON raw value on the `:ingest` key must not crash the drain loop."""
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        await redis_client.lpush(ingest_key, "not valid json{{{")
        await redis_client.lpush(
            ingest_key, json.dumps({"source": "discord", "text": "still works"})
        )

        processed = await runner.run_once()
        assert processed == 1

    async def test_drains_multiple_events_for_same_bundle(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        for i in range(3):
            await redis_client.lpush(
                ingest_key, json.dumps({"source": "discord", "text": f"event-{i}"})
            )

        processed = await runner.run_once()
        assert processed == 3

        process_key = bundle_stream_key(TENANT, None, APP_ID, "process")
        remaining = 0
        while await redis_client.rpop(process_key) is not None:
            remaining += 1
        assert remaining == 3

    async def test_malformed_event_is_skipped_not_fatal(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """Fail-first note: a bad event must not kill the loop or block later good events.

        Temporarily removing the try/except around `normalize_fn(...)`
        makes this test raise out of `run_once()` instead of returning 1 --
        confirmed red, reverted, confirmed green.
        """
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        # Missing required 'text' field -- normalize() raises ValueError.
        await redis_client.lpush(ingest_key, json.dumps({"source": "discord"}))
        await redis_client.lpush(
            ingest_key, json.dumps({"source": "discord", "text": "still works"})
        )

        processed = await runner.run_once()
        assert processed == 1  # only the good event counted

    async def test_unknown_entrypoint_skips_bundle_gracefully(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.no_such_module:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        assert await runner.run_once() == 0

    async def test_bundle_with_no_entrypoint_is_skipped(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        poller = _make_poller(
            http_client_factory,
            [{"appId": APP_ID, "communityId": None, "entrypoint": None, "spec": {}, "config": {}}],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        assert await runner.run_once() == 0

    async def test_legacy_enveloped_shape_is_no_longer_unwrapped(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """ALPHA: no legacy-shape tolerance -- an envelope-shaped raw entry is NOT unwrapped.

        `normalize_fn` receives it as-is (the bare dict, `payload`/`stage`
        keys and all); `echo_ingest.normalize` requires top-level
        `source`/`text`, which this shape doesn't have, so it raises and
        the event is skipped -- proving the old defensive unwrap heuristic
        is gone, not silently reinstated.
        """
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        await redis_client.lpush(
            ingest_key,
            json.dumps(
                {
                    "tenant": TENANT,
                    "community": None,
                    "app_id": APP_ID,
                    "stage": "ingest",
                    "payload": {"source": "twitch", "text": "wrapped"},
                    "ts": "2026-01-01T00:00:00+00:00",
                }
            ),
        )

        processed = await runner.run_once()
        assert processed == 0

        process_key = bundle_stream_key(TENANT, None, APP_ID, "process")
        assert await redis_client.rpop(process_key) is None

    async def test_entrypoint_returning_non_platform_event_is_envelope_error_and_skipped(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """A bundle entrypoint that doesn't return a `PlatformEvent` is refused, not coerced."""
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "tests._bad_entrypoint:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        await redis_client.lpush(ingest_key, json.dumps({"source": "twitch", "text": "hi"}))

        processed = await runner.run_once()
        assert processed == 0

        process_key = bundle_stream_key(TENANT, None, APP_ID, "process")
        assert await redis_client.rpop(process_key) is None


class TestRunForeverLifecycle:
    async def test_stop_ends_run_forever(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        """`run_forever()` must actually terminate once `stop()` is called -- not hang."""
        import asyncio

        poller = _make_poller(http_client_factory, [])
        # Fast poll interval so the test doesn't wait long for the loop's
        # second iteration to observe `_running is False`.
        poller._poll_interval_s = 0.01  # noqa: SLF001 - test-only override
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )

        task = asyncio.ensure_future(runner.run_forever())
        await asyncio.sleep(0.05)
        runner.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()


class TestIdentityFieldEncryption:
    """The `actor` identity field must never reach `:process` as plaintext.

    Companion to `tests/test_identity_crypto.py` (which covers the crypto
    primitives in isolation) -- these exercise the actual runner wiring:
    `_normalize_and_enqueue` encrypting `event.actor` before LPUSH, and
    fail-closed dead-lettering when the tenant DEK can't be resolved.
    """

    async def test_actor_is_encrypted_not_plaintext_on_process_key(
        self, redis_client: Any, http_client_factory: Any, dek_provider: Any
    ) -> None:
        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller, redis_client=redis_client, tenant_slug=TENANT, dek_provider=dek_provider
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        secret_actor = "xX_SuperSecretHandle_Xx"
        await redis_client.lpush(
            ingest_key, json.dumps({"source": "twitch", "text": "hi", "actor": secret_actor})
        )

        assert await runner.run_once() == 1

        process_key = bundle_stream_key(TENANT, None, APP_ID, "process")
        raw_out = await redis_client.rpop(process_key)
        assert secret_actor not in raw_out  # never plaintext on the wire

        envelope = StageEnvelope.from_dict(json.loads(raw_out))
        assert envelope.event_id  # D30 event_id minted for AAD binding
        ciphertext_obj = json.loads(envelope.event.actor)
        assert set(ciphertext_obj) == {"v", "dek_version", "nonce", "ct"}

        from identity_crypto import decrypt_identity_value

        dek, _ = await dek_provider.get_dek(TENANT)
        recovered = decrypt_identity_value(
            ciphertext_obj,
            dek,
            tenant_id=TENANT,
            stream=process_key,
            field="actor",
            event_id=envelope.event_id,
        )
        assert recovered == secret_actor

    async def test_dek_unavailable_dead_letters_never_writes_plaintext(
        self, redis_client: Any, http_client_factory: Any
    ) -> None:
        """Fail closed (security.md/critical-rules.md): no DEK -> dead-letter, never plaintext."""

        class AlwaysUnavailableDekProvider:
            async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> Any:
                from identity_crypto import DekUnavailableError

                raise DekUnavailableError("simulated keystore outage")

        poller = _make_poller(
            http_client_factory,
            [
                {
                    "appId": APP_ID,
                    "communityId": None,
                    "entrypoint": "bundles.echo_ingest:normalize",
                    "spec": {},
                    "config": {},
                }
            ],
        )
        runner = IngestRunner(
            poller=poller,
            redis_client=redis_client,
            tenant_slug=TENANT,
            dek_provider=AlwaysUnavailableDekProvider(),
        )
        ingest_key = bundle_stream_key(TENANT, None, APP_ID, "ingest")
        secret_actor = "should-never-leak"
        await redis_client.lpush(
            ingest_key, json.dumps({"source": "twitch", "text": "hi", "actor": secret_actor})
        )

        processed = await runner.run_once()
        assert processed == 0  # never counted as successfully enqueued

        process_key = bundle_stream_key(TENANT, None, APP_ID, "process")
        assert await redis_client.rpop(process_key) is None  # nothing written to :process at all

        deadletter_key = f"{process_key}:identity_deadletter"
        raw_dl = await redis_client.rpop(deadletter_key)
        assert raw_dl is not None
        dl_entry = json.loads(raw_dl)
        assert dl_entry["raw"]["actor"] == secret_actor  # held for retry, not lost
