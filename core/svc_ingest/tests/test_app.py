"""Smoke tests for svc-ingest's Quart app -- health endpoints + real startup/shutdown lifecycle.

`app.test_client()`'s own `async with` does NOT run the ASGI lifespan
(confirmed via `QuartClient.__aenter__` source -- it only sets
`preserve_context`); `app.test_app()` is Quart's actual lifespan-triggering
context manager (`before_serving`/`after_serving` run on enter/exit) and is
what `TestLifespan` below uses. `VALKEY_URL` defaults to
`redis://localhost:6379/0` (config.py) -- `redis.from_url()` itself never
opens a socket until the first command, so startup succeeds without a live
Valkey; the background `run_forever()` task's first `poll_once()` fails
closed (httpx connection refused to the default `hub-api:8204`) and
degrades to an empty bundle set, exactly the graceful-degrade contract
`BundlePoller` guarantees -- proven separately, with a real fakeredis round
trip, by `test_runner.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from typing import Any
from unittest.mock import patch

import fakeredis
import pytest
from flask_core.app_manifest import parse_manifest
from flask_core.stream_pipeline import bundle_stream_key
from quart.testing.app import LifespanError

import app as app_module
from app import app as quart_app
from builtin_handlers.kick_ingest import EVENTSUB_CONSUMES_TAG
from config import Config
from receivers.spectrum_org import RedisSnapshotStore


@pytest.fixture
def client() -> Any:
    return quart_app.test_client()


class TestHealthEndpoints:
    async def test_health(self, client: Any) -> None:
        async with client as c:
            response = await c.get("/health")
            assert response.status_code == 200
            body = await response.get_json()
            assert body["module"] == "svc-ingest"

    async def test_healthz(self, client: Any) -> None:
        """Assert `/healthz` returns 200 with mocked, deterministic `psutil` readings.

        `/healthz` (`flask_core.api_utils`) derives from real host CPU/memory via
        `psutil` -- mock both so this assertion is deterministic regardless of host
        load. Without this, `psutil.cpu_percent` reading a transient spike (e.g. a
        heavily loaded shared dev/CI host with unrelated concurrent processes) trips
        the >95% threshold and flips this test to a flaky, intermittent 503 with no
        relation to svc-ingest's own health. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 10.0
            async with client as c:
                response = await c.get("/healthz")
                assert response.status_code == 200

    async def test_healthz_reports_503_when_resources_degraded(self, client: Any) -> None:
        """Regression: the degraded-resource branch of `/healthz` must still 503.

        Pinned via mocked `psutil` (independent of real host state) so this can't
        silently bitrot into an always-200 endpoint while `test_healthz` above is
        also mocked healthy. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 95.0
            async with client as c:
                response = await c.get("/healthz")
                assert response.status_code == 503
                body = await response.get_json()
                assert body["status"] == "degraded"

    async def test_metrics(self, client: Any) -> None:
        async with client as c:
            response = await c.get("/metrics")
            assert response.status_code == 200


class TestLifespan:
    async def test_startup_wires_runner_and_shutdown_stops_it_cleanly(self) -> None:
        """The real `@app.before_serving`/`@app.after_serving` hooks run without raising.

        Proves the background task actually starts (config populated,
        `runner_task` present) and that `stop()` + task cancellation on
        shutdown terminates cleanly -- no hang, no unhandled exception.
        """
        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.get("/health")
            assert response.status_code == 200
            assert quart_app.config["runner"] is not None
            assert not quart_app.config["runner_task"].done()
        # test_app's __aexit__ runs the ASGI lifespan shutdown, which calls
        # our after_serving hook -- by the time this block exits, the
        # runner task must be finished (stopped, not hung).
        assert quart_app.config["runner_task"].done()

    async def test_startup_wires_supervisor_with_no_receivers_when_no_token(self) -> None:
        """No `DISCORD_BOT_TOKEN` -- the supervisor still starts, with zero receivers.

        `discord_leased_receiver` is never populated (graceful skip,
        matching `trigger/receiver/discord_module/app.py`'s own
        precedent) -- test env has no token set by default.
        """
        async with quart_app.test_app():
            assert quart_app.config["supervisor"] is not None
            assert quart_app.config["registry"] is not None
            assert "discord_leased_receiver" not in quart_app.config

    async def test_startup_registers_both_discord_and_twitch_under_the_one_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both connectors' receivers register under the SAME `ReceiverSupervisor` instance.

        `Config` attributes are read fresh at `startup()` time (not
        cached at import), so monkeypatching the class directly (not env
        vars, which `Config` only reads once at import) takes effect for
        this one lifespan. `SocketLease.try_claim()`'s own real Valkey
        call is never reached by this assertion -- `supervisor.register()`
        happens before `supervisor.start()`, so a missing local Valkey
        (this test env has none) never prevents the registration itself
        from being observed.
        """
        monkeypatch.setattr(Config, "DISCORD_BOT_TOKEN", "fake-discord-token")  # noqa: S105
        monkeypatch.setattr(Config, "TWITCH_BOT_TOKEN_REF", "FAKE_TWITCH_TOKEN_REF")
        monkeypatch.setattr(Config, "TWITCH_CHANNELS", ["somechannel"])

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert "discord_gateway" in registered
            assert "twitch_irc:somechannel" in registered
            assert "twitch_outbound_drain" in registered
            assert quart_app.config["discord_leased_receiver"] is not None
            assert len(quart_app.config["twitch_leased_receivers"]) == 1


class TestLeaseClientSharesAuthenticatedRedisClient:
    """Regression: every socket-lease Redis client must be THE SAME object as the shared client.

    Must be `Config.VALKEY_URL`-authenticated -- never a separately constructed, unauthenticated
    one.

    2026-09-10 fix (paired with `test_config.py`'s `TestValkeyUrlAuthFallback`): an earlier
    debugging pass observed `AuthenticationError: HELLO must be called with the client already
    authenticated` on the Discord receiver's lease claim while the ordinary ingest->process
    fan-out (same process) kept working -- the apparent divergence was traced to
    `Config.VALKEY_URL` itself silently falling back to a bare, credential-free dev default in a
    real cluster (`test_config.py`), not to `socket_lease.py`/`outbound_drain.py` building a
    second, differently-authenticated client. `socket_lease.SocketLease` never constructs its own
    client -- it only ever receives one from a caller (`LeaseRedisLike`/`socket_lease.py`'s own
    docstring) -- so asserting object identity here is the strongest guarantee against that
    divergence ever silently reappearing: a future edit that builds ANY new `redis.from_url(...)`
    call for a lease client, instead of reusing `app.py`'s one shared `redis_client`, fails this
    test immediately.
    """

    async def test_discord_and_twitch_leases_reuse_the_one_shared_redis_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "DISCORD_BOT_TOKEN", "fake-discord-token")  # noqa: S105
        monkeypatch.setattr(Config, "TWITCH_BOT_TOKEN_REF", "FAKE_TWITCH_TOKEN_REF")
        monkeypatch.setattr(Config, "TWITCH_CHANNELS", ["somechannel"])

        async with quart_app.test_app():
            shared_redis_client = quart_app.config["redis_client"]

            discord_leased = quart_app.config["discord_leased_receiver"]
            assert discord_leased.redis_client is shared_redis_client

            twitch_leased = quart_app.config["twitch_leased_receivers"][0]
            assert twitch_leased.redis_client is shared_redis_client

            outbound_drain = quart_app.config["twitch_outbound_drain"]
            assert outbound_drain.lease_redis_client is shared_redis_client
            # The drain's BLOCKING-BRPOP connection is deliberately a
            # SEPARATE, dedicated client (outbound_drain.py's own "Two
            # separate Valkey clients" docstring) -- but still built from
            # the same authenticated `Config.VALKEY_URL`, never a bare one.
            assert outbound_drain.redis_client is not shared_redis_client


class TestConfigureRootLogging:
    """Regression for the 2026-09-10 diagnosis (`app._configure_root_logging`'s own docstring).

    A Discord receiver appeared to exit silently on startup; the real
    cause was that `socket_lease.py`/`supervisor.py`/`receivers/
    discord_gateway.py` (and siblings) log via a plain
    `logging.getLogger(__name__)` that was never attached to any handler
    -- reproduced directly: the untouched Python root logger defaults
    (level=WARNING, zero handlers) silently dropped every `.info()` call
    and routed every `.warning()`/`.error()` call to an unstructured
    STDERR fallback, a different stream/format than every other log line
    this service emits. `_configure_root_logging()` runs at `app.py`
    import time (module-level call), so this test observes its
    already-applied, process-wide effect.
    """

    def test_root_logger_has_a_stdout_handler_at_config_level(self) -> None:
        root_logger = logging.getLogger()
        assert any(
            isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
            for h in root_logger.handlers
        )
        assert root_logger.getEffectiveLevel() <= logging.INFO

    def test_plain_child_logger_would_have_been_dropped_before_the_fix(self) -> None:
        """The exact shape `socket_lease.py`'s `logging.getLogger(__name__)` uses.

        Asserts at the logging-API level (not `capsys`, which can't see
        writes through a handler that captured `sys.stdout` at import
        time, before pytest's own capture substitution) -- before this
        fix, `isEnabledFor(INFO)` was False (root defaulted to WARNING),
        which is the exact condition that silently dropped
        `socket_lease.claimed`/`gateway.discord_ready` at the source,
        before any handler was even consulted.
        """
        child_logger = logging.getLogger("socket_lease")
        assert child_logger.isEnabledFor(logging.INFO)
        assert child_logger.handlers == []  # relies entirely on the root's handler
        assert len(logging.getLogger().handlers) >= 1


class TestStartupFailureLogging:
    """`startup()`'s receiver-registration guard -- see `app.py`'s own inline comment."""

    async def test_registration_failure_is_logged_with_exception_info_and_reraised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raised exception during receiver setup is never silent: logged, then re-raised.

        Proves `startup()` fails loud and fails closed rather than ever
        continuing to serve with a receiver that never actually
        registered.
        """
        logged: list[tuple[str, dict[str, Any]]] = []

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(app_module, "_register_discord_receiver", _boom)
        monkeypatch.setattr(
            app_module.logger,
            "error",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        # Quart's own lifespan protocol converts a `before_serving` hook's
        # raised exception into a `lifespan.startup.failed` ASGI message
        # before it ever reaches test/production code -- `test_app()`'s
        # harness re-raises that as `LifespanError`, not the original
        # `RuntimeError` directly. What this test actually proves is that
        # `startup()` still fails (never silently continues) AND that our
        # own `logger.error()` ran with the exception's type + message
        # before Quart's own re-raise -- both asserted below.
        with pytest.raises(LifespanError, match="boom"):
            async with quart_app.test_app():
                pass

        assert len(logged) == 1
        message, kwargs = logged[0]
        assert "RuntimeError" in message
        assert "boom" in message
        assert kwargs["result"] == "FAILED"


class TestSlackReceiverRegistration:
    """`_register_slack_receiver`'s disabled-without-tokens path + the manifest wiring.

    gh-318: without `builtin_handlers/slack_gateway_manifest.py` registered into
    the `AppRegistry`, `fanout.fan_out_event` finds zero consumers for
    `slack.message` and every inbound Slack event is silently dropped
    (`gateway.fanout_no_consumers`) regardless of whether the receiver
    itself is enabled. regression: gh-318
    """

    async def test_slack_app_manifest_is_registered_even_without_tokens(self) -> None:
        """`register_slack_bundles` always runs at startup, independent of the receiver.

        The registry entry is what `fanout.fan_out_event` needs present
        so a `slack.message` event has a resolvable consumer whenever the
        receiver DOES eventually run -- not just when Slack happens to be
        configured this particular startup.
        """
        async with quart_app.test_app():
            registry = quart_app.config["registry"]
            manifest = registry.get("waddles.bot.slack.default")
            assert manifest.app_id == "waddles.bot.slack.default"
            assert manifest.stage_specs["ingest"].consumes == ("slack.message",)

    async def test_disabled_and_logs_warning_without_both_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither `SLACK_APP_TOKEN` nor `SLACK_BOT_TOKEN` set -- registers nothing, warns once.

        Test env has both unset by default (`Config`'s own
        `os.getenv(..., "")` fallback) -- set explicitly here so this
        test's intent survives a future env change. Matches
        `_register_discord_receiver`'s own analogous "SKIPPED" precedent
        for a missing `DISCORD_BOT_TOKEN`.
        """
        monkeypatch.setattr(Config, "SLACK_APP_TOKEN", "")
        monkeypatch.setattr(Config, "SLACK_BOT_TOKEN", "")

        logged: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            app_module.logger,
            "warning",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert "slack_socket" not in registered
            assert "slack_leased_receiver" not in quart_app.config

        assert len(logged) == 1
        message, kwargs = logged[0]
        assert "SLACK_APP_TOKEN" in message
        assert "SLACK_BOT_TOKEN" in message
        assert kwargs["result"] == "SKIPPED"

    async def test_registers_under_the_supervisor_when_both_tokens_are_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both tokens set -- a lease-guarded receiver registers under the shared supervisor.

        Matches `test_startup_registers_both_discord_and_twitch_under_the_
        one_supervisor`'s own precedent: `supervisor.register()` happens
        before `supervisor.start()` (fire-and-forget `asyncio.
        ensure_future`), so no real Slack Socket Mode connection (this
        test env has no live Slack app/bot token) is ever attempted by
        this assertion.
        """
        monkeypatch.setattr(Config, "SLACK_APP_TOKEN", "xapp-fake-app-token")  # noqa: S105
        monkeypatch.setattr(Config, "SLACK_BOT_TOKEN", "xoxb-fake-bot-token")  # noqa: S105

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert "slack_socket" in registered
            assert quart_app.config["slack_leased_receiver"] is not None


class TestYouTubeLiveReceiverRegistration:
    """`_register_youtube_live_receiver`'s disabled-without-config path + the manifest wiring.

    gh-318: without `builtin_handlers/youtube_live_ingest.py` registered into the
    `AppRegistry`, `fanout.fan_out_event` finds zero consumers for
    `youtube.message` and every inbound YouTube Live chat message is
    silently dropped (`gateway.fanout_no_consumers`) regardless of
    whether any channel poller is enabled -- matches
    `TestSlackReceiverRegistration`'s own precedent.
    """

    async def test_youtube_app_manifest_is_registered_even_without_config(self) -> None:
        """`register_youtube_bundles` always runs at startup, independent of any poller."""
        async with quart_app.test_app():
            registry = quart_app.config["registry"]
            manifest = registry.get("waddles.bot.youtube.default")
            assert manifest.app_id == "waddles.bot.youtube.default"
            assert manifest.stage_specs["ingest"].consumes == ("youtube.message",)

    async def test_disabled_and_logs_skipped_without_channels_or_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `YOUTUBE_LIVE_CHANNELS`/credentials -- registers nothing, logs one SKIPPED.

        Test env has no channels configured by default (`Config`'s own
        `os.getenv(..., "")` fallback) -- set explicitly here so this
        test's intent survives a future env change. Matches
        `_register_discord_receiver`'s own analogous "SKIPPED" precedent
        for a missing `DISCORD_BOT_TOKEN`.
        """
        monkeypatch.setattr(Config, "YOUTUBE_LIVE_CHANNELS", [])

        logged: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            app_module.logger,
            "system",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert not any(name.startswith("youtube_live_poll:") for name in registered)
            assert "youtube_leased_receivers" not in quart_app.config

        # `logger.system` also fires for the unrelated Discord/Twitch
        # "SKIPPED" startup lines (this test env has neither configured
        # either) and the final "svc-ingest started" SUCCESS line --
        # filter to this receiver's own line specifically.
        youtube_skipped = [
            (message, kwargs)
            for message, kwargs in logged
            if kwargs.get("result") == "SKIPPED" and "YouTube" in message
        ]
        assert len(youtube_skipped) == 1
        message, kwargs = youtube_skipped[0]
        assert "YOUTUBE_LIVE_CHANNELS" in message
        assert kwargs["action"] == "startup"

    async def test_registers_one_leased_receiver_per_channel_with_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N configured channels + a usable API key -- N leased receivers register.

        `YouTubeLivePollReceiver` is replaced with a stub whose `receive()`
        never makes a real Data API v3 call -- `supervisor.register()`
        happens before `supervisor.start()`'s fire-and-forget tasks are
        ever awaited by this test (matching `test_startup_registers_both_
        discord_and_twitch_under_the_one_supervisor`'s own precedent), but
        unlike Discord/Twitch's socket connections, an unmocked poller
        would still issue a real outbound HTTP request to
        `googleapis.com` once the supervisor's background task starts
        running -- undesirable in a unit test regardless of timing.
        """
        monkeypatch.setattr(Config, "YOUTUBE_LIVE_CHANNELS", ["channelA", "channelB"])
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake-youtube-api-key")

        class _StubYouTubeReceiver:
            """Drop-in `YouTubeLivePollReceiver` replacement -- never calls the real Data API."""

            async def receive(self, config: dict[str, Any]) -> Any:
                return
                yield  # pragma: no cover - makes this an async generator, never reached

        monkeypatch.setattr(app_module, "YouTubeLivePollReceiver", _StubYouTubeReceiver)

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert "youtube_live_poll:channelA" in registered
            assert "youtube_live_poll:channelB" in registered
            assert len(quart_app.config["youtube_leased_receivers"]) == 2


class TestKickReceiverRegistration:
    """`_register_kick_receivers`'s disabled-without-config path + the manifest wiring.

    gh-318: without `builtin_handlers/kick_gateway_manifest.py` registered into the
    `AppRegistry`, `fanout.fan_out_event` finds zero consumers for
    `kick.message` and every inbound Kick chat message is silently dropped
    (`gateway.fanout_no_consumers`) regardless of whether any channel
    receiver is enabled -- matches `TestYouTubeLiveReceiverRegistration`'s
    own precedent.
    """

    async def test_kick_app_manifest_is_registered_even_without_config(self) -> None:
        """`register_kick_bundles` always runs at startup, independent of any receiver."""
        async with quart_app.test_app():
            registry = quart_app.config["registry"]
            manifest = registry.get("waddles.bot.kick.default")
            assert manifest.app_id == "waddles.bot.kick.default"
            assert manifest.stage_specs["ingest"].consumes == ("kick.message",)

    async def test_disabled_and_logs_skipped_without_channels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `KICK_CHANNELS` -- registers nothing, logs one SKIPPED.

        Test env has no channels configured by default (`Config`'s own
        `os.getenv(..., "")` fallback) -- set explicitly here so this
        test's intent survives a future env change. Matches
        `_register_youtube_live_receiver`'s own analogous "SKIPPED"
        precedent for missing channels/credentials.
        """
        monkeypatch.setattr(Config, "KICK_CHANNELS", [])

        logged: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            app_module.logger,
            "system",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert not any(name.startswith("kick_pusher:") for name in registered)
            assert "kick_leased_receivers" not in quart_app.config

        # `logger.system` also fires for the unrelated Discord/Slack/
        # YouTube "SKIPPED" startup lines (this test env has none of them
        # configured either) and the final "svc-ingest started" SUCCESS
        # line -- filter to this receiver's own line specifically.
        kick_skipped = [
            (message, kwargs)
            for message, kwargs in logged
            if kwargs.get("result") == "SKIPPED" and "Kick" in message
        ]
        assert len(kick_skipped) == 1
        message, kwargs = kick_skipped[0]
        assert "KICK_CHANNELS" in message
        assert kwargs["action"] == "startup"

    async def test_registers_one_leased_receiver_per_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N configured channels -- N leased receivers register.

        `KickPusherReceiver` is replaced with a stub whose `receive()`
        never makes a real Pusher WebSocket connection --
        `supervisor.register()` happens before `supervisor.start()`'s
        fire-and-forget tasks are ever awaited by this test (matching
        `test_registers_one_leased_receiver_per_channel_with_credentials`'s
        own precedent), but unlike that mocked path, an unmocked receiver
        would still attempt a real outbound connection to `kick.com`/
        `pusher.com` once the supervisor's background task starts running
        -- undesirable in a unit test regardless of timing.
        """
        monkeypatch.setattr(Config, "KICK_CHANNELS", ["channelA", "channelB"])

        class _StubKickReceiver:
            """Drop-in `KickPusherReceiver` replacement -- never calls the real Pusher API."""

            async def receive(self, config: dict[str, Any]) -> Any:
                return
                yield  # pragma: no cover - makes this an async generator, never reached

        monkeypatch.setattr(app_module, "KickPusherReceiver", _StubKickReceiver)

        async with quart_app.test_app():
            supervisor = quart_app.config["supervisor"]
            registered = set(supervisor._receivers)  # noqa: SLF001 - test-only introspection

            assert "kick_pusher:channelA" in registered
            assert "kick_pusher:channelB" in registered
            assert len(quart_app.config["kick_leased_receivers"]) == 2


class TestSpectrumReceiverRegistration:
    """`_register_spectrum_receivers` -- skip paths, per-source leases, flag (gh #101)."""

    async def test_manifest_registered_even_without_config(self) -> None:
        async with quart_app.test_app():
            manifest = quart_app.config["registry"].get("waddles.bot.spectrum.default")
            assert manifest.stage_specs["ingest"].consumes == ("spectrum.message", "spectrum.org")

    async def test_skipped_without_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "SPECTRUM_FORUM_CHANNELS", [])
        monkeypatch.setattr(Config, "SPECTRUM_LOBBIES", [])
        async with quart_app.test_app():
            assert "spectrum_leased_receivers" not in quart_app.config

    async def test_skipped_loudly_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "SPECTRUM_LOBBIES", ["L1"])
        monkeypatch.delenv("SPECTRUM_RSI_TOKEN", raising=False)
        warned: list[str] = []
        monkeypatch.setattr(
            app_module.logger, "warning", lambda message, **kw: warned.append(message)
        )
        async with quart_app.test_app():
            assert "spectrum_leased_receivers" not in quart_app.config
        assert any("Spectrum ingest NOT started" in m for m in warned)

    async def test_registers_one_leased_receiver_per_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "SPECTRUM_FORUM_CHANNELS", ["F1"])
        monkeypatch.setattr(Config, "SPECTRUM_LOBBIES", ["L1", "L2"])
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", "tok")

        class _StubSpectrumReceiver:
            name = "spectrum_poll"

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def receive(self, config: dict[str, Any]) -> Any:
                return
                yield  # pragma: no cover

        monkeypatch.setattr(app_module, "SpectrumPollReceiver", _StubSpectrumReceiver)

        async with quart_app.test_app():
            registered = set(quart_app.config["supervisor"]._receivers)  # noqa: SLF001
            expected = {"spectrum_poll:forum-F1", "spectrum_poll:lobby-L1"}
            expected.add("spectrum_poll:lobby-L2")
            assert expected <= registered
            assert len(quart_app.config["spectrum_leased_receivers"]) == 3

    @pytest.mark.parametrize("baseline", [True, False])
    async def test_flag_check_uses_env_baseline_as_default(
        self, monkeypatch: pytest.MonkeyPatch, baseline: bool
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_feature_enabled(key: str, **kw: Any) -> bool:
            seen.update(key=key, **kw)
            return bool(kw["default"])

        monkeypatch.setattr("flask_core.feature_flags.feature_enabled", fake_feature_enabled)
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_INTEGRATION", "true" if baseline else "")
        assert await app_module._spectrum_flag_enabled() is baseline  # noqa: SLF001
        assert seen["key"] == "waddles.spectrum-integration"
        assert seen["default"] is baseline

    async def test_org_sources_register_roster_and_events_pollers_with_a_snapshot_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "SPECTRUM_FORUM_CHANNELS", [])
        monkeypatch.setattr(Config, "SPECTRUM_LOBBIES", ["L1"])
        monkeypatch.setattr(Config, "SPECTRUM_ORG_COMMUNITIES", ["org1", "org2"])
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", "tok")
        built: list[_StubOrgSpectrumReceiver] = []

        class _StubOrgSpectrumReceiver:
            name = "spectrum_poll"

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                built.append(self)

            async def receive(self, config: dict[str, Any]) -> Any:
                return
                yield  # pragma: no cover

        monkeypatch.setattr(app_module, "SpectrumPollReceiver", _StubOrgSpectrumReceiver)

        async with quart_app.test_app():
            registered = set(quart_app.config["supervisor"]._receivers)  # noqa: SLF001
            assert {
                "spectrum_poll:lobby-L1",
                "spectrum_poll:roster-org1",
                "spectrum_poll:events-org1",
                "spectrum_poll:roster-org2",
                "spectrum_poll:events-org2",
            } <= registered
            leased = quart_app.config["spectrum_leased_receivers"]
            assert len(leased) == 5
            by_kind = {lr.config["kind"] + "-" + lr.config["source_id"]: lr for lr in leased}
            roster = by_kind["roster-org1"].config
            assert roster["poll_interval_s"] == Config.SPECTRUM_ORG_POLL_INTERVAL_S
            assert (
                roster["roster_max_departure_ratio"] == Config.SPECTRUM_ROSTER_MAX_DEPARTURE_RATIO
            )
            assert roster["emit_backlog"] is Config.SPECTRUM_ORG_EMIT_BACKLOG
            assert by_kind["lobby-L1"].config["poll_interval_s"] == Config.SPECTRUM_POLL_INTERVAL_S

        # Org pollers get the org flag + the Valkey snapshot store; message pollers get neither.
        org_kwargs = [b.kwargs for b in built if b.kwargs.get("snapshot_store") is not None]
        assert len(org_kwargs) == 4
        assert all(k["flag_check"] is app_module._spectrum_org_flag_enabled for k in org_kwargs)  # noqa: SLF001
        assert all(isinstance(k["snapshot_store"], RedisSnapshotStore) for k in org_kwargs)
        msg_kwargs = [b.kwargs for b in built if b.kwargs.get("snapshot_store") is None]
        assert len(msg_kwargs) == 1
        assert msg_kwargs[0]["flag_check"] is app_module._spectrum_flag_enabled  # noqa: SLF001

    async def test_org_and_message_items_fan_out_under_their_own_consume_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "SPECTRUM_FORUM_CHANNELS", [])
        monkeypatch.setattr(Config, "SPECTRUM_LOBBIES", ["L1"])
        monkeypatch.setattr(Config, "SPECTRUM_ORG_COMMUNITIES", ["org1"])
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", "tok")
        tags: list[str] = []

        async def fake_fan_out(item: Any, *, consumes_tag: str, **kw: Any) -> int:
            tags.append(consumes_tag)
            return 1

        monkeypatch.setattr(app_module, "fan_out_event", fake_fan_out)

        async with quart_app.test_app():
            for leased in quart_app.config["spectrum_leased_receivers"]:
                await leased.on_item({"x": 1})
        assert sorted(tags) == ["spectrum.message", "spectrum.org", "spectrum.org"]

    @pytest.mark.parametrize(
        ("master", "org_flag", "expected"),
        [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
    )
    async def test_org_flag_requires_master_and_org_flag(
        self, monkeypatch: pytest.MonkeyPatch, master: bool, org_flag: bool, expected: bool
    ) -> None:
        asked: list[str] = []

        async def fake_feature_enabled(key: str, **kw: Any) -> bool:
            asked.append(key)
            return master if key == "waddles.spectrum-integration" else org_flag

        monkeypatch.setattr("flask_core.feature_flags.feature_enabled", fake_feature_enabled)
        assert await app_module._spectrum_org_flag_enabled() is expected  # noqa: SLF001
        # Master OFF short-circuits: the org flag is never even evaluated.
        assert asked == (
            ["waddles.spectrum-integration"]
            if not master
            else ["waddles.spectrum-integration", "waddles.spectrum-org-sync"]
        )

    async def test_org_flag_baseline_is_the_default_for_the_second_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_feature_enabled(key: str, **kw: Any) -> bool:
            seen[key] = kw["default"]
            return bool(kw["default"])

        monkeypatch.setattr("flask_core.feature_flags.feature_enabled", fake_feature_enabled)
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_INTEGRATION", "true")
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_ORG_SYNC", "")
        assert await app_module._spectrum_org_flag_enabled() is False  # noqa: SLF001
        assert seen["waddles.spectrum-org-sync"] is False
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_ORG_SYNC", "on")
        assert await app_module._spectrum_org_flag_enabled() is True  # noqa: SLF001

    def test_org_config_defaults_are_conservative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FLAG_WADDLES_SPECTRUM_ORG_SYNC", raising=False)
        assert Config.spectrum_org_flag_baseline() is False  # default OFF
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_ORG_SYNC", "yes")
        assert Config.spectrum_org_flag_baseline() is True
        assert Config.SPECTRUM_ORG_POLL_INTERVAL_S >= 60.0
        assert 0.0 < Config.SPECTRUM_ROSTER_MAX_DEPARTURE_RATIO <= 1.0
        assert Config.SPECTRUM_ORG_EMIT_BACKLOG is False  # connecting never floods by default
        assert Config.SPECTRUM_SNAPSHOT_TTL_S >= 24 * 3600

    def test_token_presence_and_flag_baseline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPECTRUM_RSI_TOKEN", raising=False)
        monkeypatch.delenv("FLAG_WADDLES_SPECTRUM_INTEGRATION", raising=False)
        assert Config.spectrum_token_configured() is False
        assert Config.spectrum_flag_baseline() is False  # default OFF
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", "t")
        monkeypatch.setenv("FLAG_WADDLES_SPECTRUM_INTEGRATION", "ON")
        assert Config.spectrum_token_configured() is True
        assert Config.spectrum_flag_baseline() is True


class TestKickWebhookRoute:
    """`POST /webhook/kick` (gh #287 S10) -- mounted like `eventsub_bp`'s own Twitch route."""

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    async def test_returns_503_when_secret_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "KICK_WEBHOOK_SECRET", "")
        body = b'{"type":"StreamStart"}'

        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.post(
                "/webhook/kick",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 503

    async def test_invalid_signature_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "KICK_WEBHOOK_SECRET", "test-webhook-secret")  # noqa: S105
        body = b'{"type":"StreamStart"}'

        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.post(
                "/webhook/kick",
                data=body,
                headers={"Content-Type": "application/json", "X-Kick-Signature": "bad"},
            )
            assert response.status_code == 401

    async def test_valid_signature_acks_a_non_lifecycle_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "test-webhook-secret"  # noqa: S105
        monkeypatch.setattr(Config, "KICK_WEBHOOK_SECRET", secret)
        body = b'{"type":"ChannelFollow"}'
        signature = self._sign(body, secret)

        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.post(
                "/webhook/kick",
                data=body,
                headers={"Content-Type": "application/json", "X-Kick-Signature": signature},
            )
            assert response.status_code == 200
            assert await response.get_json() == {"received": True, "event_type": "follow"}

    async def test_stream_start_fans_out_through_the_shared_redis_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real `startup()`-wired `redis_client`/`registry` are what the route reads.

        Swapped to a `fakeredis.FakeAsyncRedis` post-startup (matching
        `test_builtin_kick_ingest.py`'s own fan-out fixture) so this
        exercises a genuine LPUSH round trip, not a mocked call, without
        needing a live Valkey in this test env.
        """
        secret = "test-webhook-secret"  # noqa: S105
        monkeypatch.setattr(Config, "KICK_WEBHOOK_SECRET", secret)
        body_json = {
            "type": "StreamStart",
            "channel_slug": "acme",
            "channel_id": "555",
            "started_at": "2026-09-11T12:00:00Z",
        }
        body = json.dumps(body_json).encode()
        signature = self._sign(body, secret)

        async with quart_app.test_app() as test_app:
            fake_redis = fakeredis.FakeAsyncRedis(decode_responses=True)
            quart_app.config["redis_client"] = fake_redis
            registry = quart_app.config["registry"]
            registry.register(
                parse_manifest(
                    {
                        "app_id": "waddles.bot.kickevents.eventsub",
                        "name": "waddles.bot.kickevents.eventsub",
                        "version": "1.0.0",
                        "feature": "waddles.bot.kickevents",
                        "module": "bot",
                        "provider": "builtin",
                        "is_default": True,
                        "stages": {
                            "ingest": {
                                "entrypoint": "builtin_handlers.kick_ingest:normalize",
                                "consumes": [EVENTSUB_CONSUMES_TAG],
                            }
                        },
                    }
                )
            )

            client = test_app.test_client()
            response = await client.post(
                "/webhook/kick",
                data=body,
                headers={"Content-Type": "application/json", "X-Kick-Signature": signature},
            )
            assert response.status_code == 200
            assert await response.get_json() == {"received": True, "event_type": "stream_start"}

            ingest_key = bundle_stream_key(
                Config.RUNNER_TENANT_SLUG, None, "waddles.bot.kickevents.eventsub", "ingest"
            )
            raw = await fake_redis.rpop(ingest_key)
            assert raw is not None
            event = json.loads(raw)
            assert event["platform"] == "kick"
            assert event["event_type"] == "stream.online"
            assert event["payload"]["channel_slug"] == "acme"
            assert event["payload"]["channel_id"] == "555"
