"""svc-ingest -- Quart control-plane + background stage-runner loop + supervised socket receivers.

Real async loop (`runner.IngestRunner`, started in `@app.before_serving`):
polls hub-api's distribution endpoint for the `ingest` stage's active
bundles (`flask_core.stage_runner.BundlePoller` -- interval + exponential
backoff on failure, graceful-degrade to the last-known bundle set, never
crashes on a hub-api outage), RPOPs each bundle's raw inbound events off
its own Valkey `:ingest` key, runs the bundle's real `normalize()`
entrypoint, and LPUSHes the normalized result onto that bundle's `:process`
key as a JSON envelope. `/health`/`/healthz`/`/metrics` come from
`flask_core`'s standard health blueprint, same as every other pipeline-
stage container (`core/svc_streaming/app.py`).

Alongside that poll-drain loop, svc-ingest ALSO runs any registered
platform-level inbound transports (`receivers/discord_gateway.py`'s
`DiscordGatewayReceiver`, `receivers/twitch_irc.py`'s `TwitchIrcReceiver`
-- one instance per configured channel, see that module's own docstring)
as `supervisor.ReceiverSupervisor`-supervised tasks -- 8-container
decision: these receivers were briefly a standalone `svc-gateway` 9th
container, folded back into svc-ingest since a persistent bot/IRC
connection is exactly the same "hold a persistent inbound socket,
normalize, feed the pipeline" shape this container already owns. Each
transport is guarded by a `socket_lease.SocketLease` (`waddles:socket-
owner:{provider}:{community}`, Valkey `SET NX PX`) so scaling
`pipeline.svcIngest.replicas` never opens duplicate sockets for the same
`(provider, community)` -- see `socket_lease.py`'s own module docstring
for the full design.

Fan-out (T9): every item either transport yields is routed at
`community=None` (tenant-wide) for this demo -- Discord's `guild_id`/
Twitch's `channel_name` are both carried in their own normalized dicts for
FUTURE use, but no guild/channel->community mapping table exists yet
anywhere in this codebase (documented, deferred slot; see each receiver's
own docstring).

Twitch's outbound chat sends for svc-action are handled by a SEPARATE
`outbound_drain.py` task, ALSO supervised and ALSO lease-guarded (2026-09-04
fix -- see that module's own docstring) -- `provider="twitch",
community=socket_lease.PLATFORM_COMMUNITY`, since the underlying relay
queue is provider-scoped, not per-channel, so only one live replica ever
drains/sends at a time. It opens a fresh short-lived `waddle_transports.
transports.irc.IrcTransport` connection per relayed message rather than
reusing any receiver's socket, and runs on its OWN dedicated Valkey
connection (`socket_timeout=outbound_drain.DRAIN_SOCKET_TIMEOUT_S`, built
below) rather than the shared `redis_client` -- sharing it would leave the
blocking BRPOP racing that client's own (shorter) default socket timeout,
which is exactly what caused the `Timeout reading from ...` false failures
this fix addresses.

The EventSub webhook (`POST /eventsub/twitch/webhook`, `eventsub.py`) is a
genuine inbound HTTP push (not a persistent socket) -- registered as a
plain Quart route, wired to the same `fanout.fan_out_event` machinery the
IRC receivers use. `POST /webhook/kick` (`builtin_handlers.kick_ingest.
handle_kick_webhook`, gh #287 S10) is the identical shape for Kick's own
signed mod/sub/stream-lifecycle webhook -- a SEPARATE delivery mechanism
from `receivers/kick_pusher.py`'s Pusher chat socket, always mounted
(unlike the conditionally-built Twitch handler), gracefully 503ing its
own self when `Config.KICK_WEBHOOK_SECRET` is unset.

DEPRECATED (chore/retire-python-dataplane, 2026-09-28): PenguinTech is
retiring Python from all live-traffic (data-plane) services in favor of
`core/svc_ingest`'s Rust build (`src/`, `Cargo.toml`) -- see
`critical-rules.md` Data Plane (In-Line of Traffic). This Python service
remains the ONLY functioning ingest today: the Rust build covers Discord
gateway + Twitch IRC/EventSub only (`src/ingest/discord.rs`,
`src/ingest/twitch.rs`, `src/ingest/twitch_eventsub.rs`) and has no
Kick/Slack/YouTube/Echo receivers, no multi-tenant `socket_lease.py`
equivalent, and only a single-tenant Twitch outbound drain
(`src/outbound.rs`'s own doc comment flags the per-tenant gap). Do not
remove this module or its Helm Deployment
(`k8s/helm/waddlebot/templates/svc-ingest.yaml`) until the Rust build
reaches full parity -- tracked as Rust follow-up work, not scheduled here.

Note: PR #440 (Python ingest identity encryption) is NOT moot -- this
Python service stays live (no Rust receiver parity yet, see above), so
#440's hardening still applies to real production traffic until Python
ingest is actually retired.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Mapping
from typing import Any, cast

import httpx
import redis.asyncio as redis
from flask_core import create_health_blueprint, install_security_headers, setup_aaa_logging
from flask_core.app_registry import AppRegistry
from flask_core.auth import create_jwt_token
from flask_core.logging_config import StructuredFormatter
from flask_core.stage_runner import BundlePoller
from quart import Blueprint, Quart, request

from builtin_handlers.discord_gateway_manifest import (
    register_default_bundles as register_discord_bundles,
)
from builtin_handlers.kick_gateway_manifest import register_default_bundles as register_kick_bundles
from builtin_handlers.kick_ingest import handle_kick_webhook
from builtin_handlers.slack_gateway_manifest import (
    register_default_bundles as register_slack_bundles,
)
from builtin_handlers.twitch_gateway_manifest import (
    register_default_bundles as register_twitch_bundles,
)
from builtin_handlers.youtube_live_ingest import (
    register_default_bundles as register_youtube_bundles,
)
from config import Config
from eventsub import TwitchEventSubHandler
from fanout import fan_out_event
from outbound_drain import DRAIN_SOCKET_TIMEOUT_S, TwitchOutboundDrain
from receivers.discord_gateway import CONSUMES_TAG as DISCORD_CONSUMES_TAG
from receivers.discord_gateway import DiscordGatewayReceiver
from receivers.kick_pusher import CONSUMES_TAG as KICK_CONSUMES_TAG
from receivers.kick_pusher import KickPusherReceiver
from receivers.slack_socket import CONSUMES_TAG as SLACK_CONSUMES_TAG
from receivers.slack_socket import SlackSocketReceiver
from receivers.twitch_irc import CONSUMES_TAG as TWITCH_CONSUMES_TAG
from receivers.twitch_irc import TwitchIrcReceiver
from receivers.youtube_live_poll import CONSUMES_TAG as YOUTUBE_CONSUMES_TAG
from receivers.youtube_live_poll import YouTubeLivePollReceiver
from runner import IngestRunner
from socket_lease import PLATFORM_COMMUNITY, LeasedReceiver
from supervisor import ReceiverSupervisor


def _configure_root_logging() -> None:
    """Give every plain `logging.getLogger(__name__)` module a real, visible handler.

    **2026-09-10 fix -- diagnosed a Discord receiver that appeared to exit
    silently on startup.** `setup_aaa_logging()` below only wires handlers
    onto its own isolated `"waddlebot.{module}"` logger (`propagate=False`
    -- `flask_core.logging_config.AAALogger.__init__`); every OTHER module
    in this container (`socket_lease.py`, `supervisor.py`, `receivers/
    discord_gateway.py`, `receivers/twitch_irc.py`, `runner.py`,
    `outbound_drain.py`, `eventsub.py`, `fanout.py`) logs via a plain
    `logging.getLogger(__name__)`, which was NEVER attached to any
    handler anywhere in this process. Reproduced directly: the Python
    stdlib root logger's own untouched defaults (level=WARNING, zero
    handlers) meant every `.info()`/`.debug()` call from those modules
    (`socket_lease.claimed`, `gateway.discord_ready`, ...) was dropped
    before it ever reached a handler, and every `.warning()`/`.error()`
    call (`socket_lease.degraded_running_without_lease`, `socket_lease.
    timeout`, `supervisor.receiver_failed`, ...) fell through to Python's
    unstructured `logging.lastResort` handler on STDERR -- a different
    stream, in a different format, than every other log line this
    service emits on stdout. A receiver silently vanishing from the logs
    is exactly the same shape as a receiver silently exiting; this must
    be fixed before any lease/supervisor/receiver log line can be
    trusted as evidence of what actually happened at runtime.

    Attaching a handler directly to the ROOT logger (the same pattern
    `video_proxy_module`/`engagement_module` already use) fixes every
    plain-named child logger in this process at once, with zero changes
    to which logger object each module holds -- and deliberately does
    NOT touch `propagate` on the AAA `"waddlebot.{module}"` logger, so
    its own dedicated console/file/syslog handlers keep working exactly
    as before.
    """
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredFormatter(Config.MODULE_NAME, Config.MODULE_VERSION))
    root_logger = logging.getLogger()
    root_logger.setLevel(Config.LOG_LEVEL.upper())
    root_logger.addHandler(console_handler)


_configure_root_logging()

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

eventsub_bp = Blueprint("eventsub", __name__, url_prefix="/eventsub")
webhook_bp = Blueprint("webhook", __name__, url_prefix="/webhook")

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)


def _jwt_provider() -> str:
    """Mint a fresh 1h service JWT for this runner's own tenant scope.

    Minted fresh on every call rather than cached-and-refreshed -- cheap
    (HS256 signing, no network round trip) and always valid, so there is no
    token-refresh-on-expiry state machine to get wrong. `expiration_hours=1`
    matches security.md's machine-access-token ceiling.
    """
    return cast(
        str,
        create_jwt_token(
            user_id="svc-ingest",
            username="svc-ingest",
            email="svc-ingest@internal.waddlebot",
            roles=["service"],
            secret_key=Config.SECRET_KEY,
            tenant=Config.RUNNER_TENANT_SLUG,
            scope=Config.JWT_SCOPE,
            expiration_hours=1,
        ),
    )


def _register_discord_receiver(
    supervisor: ReceiverSupervisor,
    *,
    redis_client: Any,
    registry: AppRegistry,
) -> None:
    """Build + lease-guard + supervise the Discord gateway receiver, if configured."""
    if not Config.DISCORD_BOT_TOKEN:
        logger.system(
            "svc-ingest starting with no Discord receiver -- DISCORD_BOT_TOKEN not configured",
            action="startup",
            result="SKIPPED",
        )
        return

    replica_id = uuid.uuid4().hex
    discord_receiver = DiscordGatewayReceiver()

    async def _on_discord_item(item: Mapping[str, Any]) -> None:
        """Fan one normalized Discord message dict out to every consuming bundle.

        T9: `community=None` (tenant-wide) for this demo -- see this
        module's own docstring for the deferred guild->community mapping
        slot.
        """
        await fan_out_event(
            item,
            consumes_tag=DISCORD_CONSUMES_TAG,
            tenant=Config.RUNNER_TENANT_SLUG,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

    leased_discord = LeasedReceiver(
        transport=discord_receiver,
        # `token_ref` is an env var *name*, resolved by `receive()` via
        # `waddle_transports.signing.resolve_secret` -- never a raw token
        # in this config dict.
        config={"token_ref": "DISCORD_BOT_TOKEN"},  # nosec B105 -- env var name, not a token value
        on_item=_on_discord_item,
        redis_client=redis_client,
        provider="discord",
        community=PLATFORM_COMMUNITY,
        owner_id=replica_id,
        ttl_s=Config.SOCKET_LEASE_TTL_S,
        renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
        claim_timeout_s=Config.SOCKET_LEASE_CLAIM_TIMEOUT_S,
        run_without_lease_on_unavailable=Config.SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE,
    )
    app.config["discord_leased_receiver"] = leased_discord
    supervisor.register("discord_gateway", leased_discord.run, transport=discord_receiver)
    logger.system(
        "svc-ingest registered Discord gateway receiver", action="startup", replica_id=replica_id
    )


def _register_slack_receiver(
    supervisor: ReceiverSupervisor,
    *,
    redis_client: Any,
    registry: AppRegistry,
) -> None:
    """Build + lease-guard + supervise the Slack Socket Mode receiver, if configured."""
    if not (Config.SLACK_APP_TOKEN and Config.SLACK_BOT_TOKEN):
        logger.warning(
            "slack disabled: SLACK_APP_TOKEN/SLACK_BOT_TOKEN not set",
            action="startup",
            result="SKIPPED",
        )
        return

    replica_id = uuid.uuid4().hex
    slack_receiver = SlackSocketReceiver()

    async def _on_slack_item(item: Mapping[str, Any]) -> None:
        """Fan one normalized Slack event dict out to every consuming bundle.

        T9: `community=None` (tenant-wide) for this demo, matching
        Discord/Twitch's own deferred channel->community mapping slot --
        see this module's own docstring.
        """
        await fan_out_event(
            item,
            consumes_tag=SLACK_CONSUMES_TAG,
            tenant=Config.RUNNER_TENANT_SLUG,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

    leased_slack = LeasedReceiver(
        transport=slack_receiver,
        # `*_token_ref` are env var *names*, resolved by `receive()` via
        # `waddle_transports.signing.resolve_secret` -- never raw tokens
        # in this config dict.
        config={  # nosec B105 -- env var names, not token values
            "app_token_ref": "SLACK_APP_TOKEN",
            "bot_token_ref": "SLACK_BOT_TOKEN",
        },
        on_item=_on_slack_item,
        redis_client=redis_client,
        provider="slack",
        community=PLATFORM_COMMUNITY,
        owner_id=replica_id,
        ttl_s=Config.SOCKET_LEASE_TTL_S,
        renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
        claim_timeout_s=Config.SOCKET_LEASE_CLAIM_TIMEOUT_S,
        run_without_lease_on_unavailable=Config.SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE,
    )
    app.config["slack_leased_receiver"] = leased_slack
    supervisor.register("slack_socket", leased_slack.run, transport=slack_receiver)
    logger.system(
        "svc-ingest registered Slack Socket Mode receiver", action="startup", replica_id=replica_id
    )


def _register_youtube_live_receiver(
    supervisor: ReceiverSupervisor,
    *,
    redis_client: Any,
    registry: AppRegistry,
) -> None:
    """Build + lease-guard + supervise one YouTube Live poll receiver per channel, if configured."""
    if not (Config.YOUTUBE_LIVE_CHANNELS and Config.youtube_credentials_configured()):
        logger.system(
            "svc-ingest starting with no YouTube Live poll receivers -- "
            "YOUTUBE_LIVE_CHANNELS/credentials not configured",
            action="startup",
            result="SKIPPED",
        )
        return

    replica_id = uuid.uuid4().hex
    leased_receivers = []

    async def _on_youtube_item(item: Mapping[str, Any]) -> None:
        """Fan one normalized YouTube Live chat message dict out to every consuming bundle.

        T9: `community=None` (tenant-wide) for this demo -- see this
        module's own docstring for the deferred channel->community
        mapping slot (the lease itself is still per-channel, `community=
        channel_id`, below -- matches Twitch's own split between lease
        scope and fan-out scope).
        """
        await fan_out_event(
            item,
            consumes_tag=YOUTUBE_CONSUMES_TAG,
            tenant=Config.RUNNER_TENANT_SLUG,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

    for channel_id in Config.YOUTUBE_LIVE_CHANNELS:
        youtube_receiver = YouTubeLivePollReceiver()
        # ONE lease per channel -- YouTubeLivePollReceiver.receive() polls
        # a single channel per call, so two replicas must never both poll
        # the SAME channel, but different channels are entirely
        # independent (never contend for the same lease key). Matches
        # TwitchIrcReceiver's own per-channel precedent.
        leased = LeasedReceiver(
            transport=youtube_receiver,
            config={
                "channel_id": channel_id,
                "api_key_ref": Config.YOUTUBE_API_KEY_REF,
                "client_id_ref": Config.YOUTUBE_CLIENT_ID_REF,
                "client_secret_ref": Config.YOUTUBE_CLIENT_SECRET_REF,
                "refresh_token_ref": Config.YOUTUBE_REFRESH_TOKEN_REF,
                "no_broadcast_backoff_s": Config.YOUTUBE_LIVE_POLL_NO_BROADCAST_BACKOFF_S,
                "max_consecutive_quota_errors": Config.YOUTUBE_LIVE_POLL_MAX_QUOTA_ERRORS,
                "chat_max_results": Config.YOUTUBE_LIVE_CHAT_MAX_RESULTS,
            },
            on_item=_on_youtube_item,
            redis_client=redis_client,
            provider="youtube",
            community=channel_id,
            owner_id=replica_id,
            ttl_s=Config.SOCKET_LEASE_TTL_S,
            renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
            claim_timeout_s=Config.SOCKET_LEASE_CLAIM_TIMEOUT_S,
            run_without_lease_on_unavailable=Config.SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE,
        )
        leased_receivers.append(leased)
        supervisor.register(
            f"youtube_live_poll:{channel_id}", leased.run, transport=youtube_receiver
        )

    app.config["youtube_leased_receivers"] = leased_receivers
    logger.system(
        "svc-ingest registered YouTube Live poll receivers",
        action="startup",
        replica_id=replica_id,
        channels=len(Config.YOUTUBE_LIVE_CHANNELS),
    )


def _register_kick_receivers(
    supervisor: ReceiverSupervisor,
    *,
    redis_client: Any,
    registry: AppRegistry,
) -> None:
    """Build + lease-guard + supervise one Kick Pusher chat receiver per channel, if configured."""
    if not Config.KICK_CHANNELS:
        logger.system(
            "svc-ingest starting with no Kick Pusher receivers -- KICK_CHANNELS not configured",
            action="startup",
            result="SKIPPED",
        )
        return

    replica_id = uuid.uuid4().hex
    leased_receivers = []

    async def _on_kick_item(item: Mapping[str, Any]) -> None:
        """Fan one normalized Kick chat message dict out to every consuming bundle.

        T9: `community=None` (tenant-wide) for this demo -- see this
        module's own docstring for the deferred channel->community
        mapping slot (the lease itself is still per-channel, `community=
        channel_slug`, below -- matches Twitch/YouTube's own split between
        lease scope and fan-out scope).
        """
        await fan_out_event(
            item,
            consumes_tag=KICK_CONSUMES_TAG,
            tenant=Config.RUNNER_TENANT_SLUG,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

    for channel in Config.KICK_CHANNELS:
        kick_receiver = KickPusherReceiver()
        # ONE lease per channel -- KickPusherReceiver.receive() resolves +
        # connects a single channel's Pusher chatroom per call, so two
        # replicas must never both hold the SAME channel's connection, but
        # different channels are entirely independent (never contend for
        # the same lease key). Matches TwitchIrcReceiver/
        # YouTubeLivePollReceiver's own per-channel precedent.
        leased = LeasedReceiver(
            transport=kick_receiver,
            config={
                "channel_slug": channel,
                "pusher_key": Config.KICK_PUSHER_KEY or None,
                "cluster": Config.KICK_PUSHER_CLUSTER or None,
            },
            on_item=_on_kick_item,
            redis_client=redis_client,
            provider="kick",
            community=channel,
            owner_id=replica_id,
            ttl_s=Config.SOCKET_LEASE_TTL_S,
            renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
            claim_timeout_s=Config.SOCKET_LEASE_CLAIM_TIMEOUT_S,
            run_without_lease_on_unavailable=Config.SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE,
        )
        leased_receivers.append(leased)
        supervisor.register(f"kick_pusher:{channel}", leased.run, transport=kick_receiver)

    app.config["kick_leased_receivers"] = leased_receivers
    logger.system(
        "svc-ingest registered Kick Pusher receivers",
        action="startup",
        replica_id=replica_id,
        channels=len(Config.KICK_CHANNELS),
    )


def _register_twitch_receivers(
    supervisor: ReceiverSupervisor,
    *,
    redis_client: Any,
    registry: AppRegistry,
) -> None:
    """Build + lease-guard + supervise one Twitch IRC receiver per channel, if configured."""
    if not (Config.TWITCH_BOT_TOKEN_REF and Config.TWITCH_CHANNELS):
        logger.system(
            "svc-ingest starting with no Twitch IRC receivers -- "
            "TWITCH_BOT_TOKEN_REF/TWITCH_CHANNELS not configured",
            action="startup",
            result="SKIPPED",
        )
        return

    replica_id = uuid.uuid4().hex
    irc_config_base = Config.twitch_irc_config_base()
    leased_receivers = []

    async def _on_twitch_item(item: Mapping[str, Any]) -> None:
        """Fan one normalized Twitch chat message dict out to every consuming bundle.

        T9: `community=None` (tenant-wide) for this demo -- see this
        module's own docstring for the deferred channel->community
        mapping slot.
        """
        await fan_out_event(
            item,
            consumes_tag=TWITCH_CONSUMES_TAG,
            tenant=Config.RUNNER_TENANT_SLUG,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )

    for channel in Config.TWITCH_CHANNELS:
        twitch_receiver = TwitchIrcReceiver()
        # ONE lease per channel -- IrcTransport.receive() is a single-
        # channel-per-connection contract, so two replicas must never
        # both hold the SAME channel's connection, but different channels
        # are entirely independent (never contend for the same lease
        # key). See receivers/twitch_irc.py's own docstring.
        leased = LeasedReceiver(
            transport=twitch_receiver,
            config={**irc_config_base, "channel": channel},
            on_item=_on_twitch_item,
            redis_client=redis_client,
            provider="twitch",
            community=channel,
            owner_id=replica_id,
            ttl_s=Config.SOCKET_LEASE_TTL_S,
            renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
            claim_timeout_s=Config.SOCKET_LEASE_CLAIM_TIMEOUT_S,
            run_without_lease_on_unavailable=Config.SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE,
        )
        leased_receivers.append(leased)
        supervisor.register(f"twitch_irc:{channel}", leased.run, transport=twitch_receiver)

    app.config["twitch_leased_receivers"] = leased_receivers

    # Dedicated Valkey connection for the drain's own blocking BRPOP --
    # NOT the shared redis_client above, whose socket_timeout
    # (Config.REDIS_SOCKET_TIMEOUT_S, explicit as of 2026-09-09 -- see
    # this function's own redis_client construction) equals the BRPOP
    # block timeout and would race it on every idle poll (see
    # outbound_drain.py's own module docstring for the full root-cause).
    # Closed in shutdown() below alongside redis_client.
    drain_redis_client = redis.from_url(
        Config.VALKEY_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=DRAIN_SOCKET_TIMEOUT_S,
    )
    app.config["twitch_outbound_drain_redis_client"] = drain_redis_client

    outbound_drain = TwitchOutboundDrain(
        redis_client=drain_redis_client,
        # The ORDINARY shared client (same one every other Twitch/Discord
        # lease already uses) for the drain's own claim/renew/release --
        # deliberately NOT drain_redis_client, see outbound_drain.py's own
        # "Two separate Valkey clients" docstring section for why sharing
        # one connection between a blocking BRPOP and lease SET/EVAL calls
        # is unsafe.
        lease_redis_client=redis_client,
        irc_config_base=irc_config_base,
        # Same replica_id as this replica's own per-channel leases above --
        # one owner identity per svc-ingest process across every Twitch
        # lease it may hold (inbound receive AND outbound transmit).
        owner_id=replica_id,
        ttl_s=Config.SOCKET_LEASE_TTL_S,
        renew_interval_s=Config.SOCKET_LEASE_RENEW_INTERVAL_S,
    )
    app.config["twitch_outbound_drain"] = outbound_drain
    supervisor.register("twitch_outbound_drain", outbound_drain.run)

    logger.system(
        "svc-ingest registered Twitch IRC receivers",
        action="startup",
        replica_id=replica_id,
        channels=len(Config.TWITCH_CHANNELS),
    )


def _register_twitch_eventsub(*, redis_client: Any, registry: AppRegistry) -> None:
    """Build the Twitch EventSub webhook handler, if configured."""
    if not Config.TWITCH_EVENTSUB_SECRET:
        logger.system(
            "svc-ingest starting with no Twitch EventSub handler -- "
            "TWITCH_EVENTSUB_SECRET not configured",
            action="startup",
            result="SKIPPED",
        )
        return

    app.config["twitch_eventsub_handler"] = TwitchEventSubHandler(
        secret=Config.TWITCH_EVENTSUB_SECRET,
        redis_client=redis_client,
        registry=registry,
        tenant_slug=Config.RUNNER_TENANT_SLUG,
    )
    logger.system("svc-ingest registered Twitch EventSub handler", action="startup")


@app.before_serving
async def startup() -> None:
    """Wire the httpx/Valkey clients, poller, and start the poll-drain loop + socket receivers."""
    http_client = httpx.AsyncClient()
    # Explicit connect/read timeout (2026-09-09 fix) -- previously unset,
    # relying entirely on redis-py's own version-dependent default. A
    # second, independent bound underneath socket_lease.py's own
    # asyncio-level `claim_timeout_s` guard (defense in depth, not a
    # substitute for it -- see LeasedReceiver.run()'s docstring for why
    # the asyncio-level guard is the one that actually matters).
    redis_client = redis.from_url(
        Config.VALKEY_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=Config.REDIS_SOCKET_TIMEOUT_S,
        socket_timeout=Config.REDIS_SOCKET_TIMEOUT_S,
    )

    poller = BundlePoller(
        http_client,
        Config.DISTRIBUTION_URL,
        stage=Config.PIPELINE_STAGE,
        jwt_provider=_jwt_provider,
        community_id=Config.RUNNER_COMMUNITY_ID,
        poll_interval_s=Config.POLL_INTERVAL_S,
        base_backoff_s=Config.BASE_BACKOFF_S,
        max_backoff_s=Config.MAX_BACKOFF_S,
    )
    runner = IngestRunner(
        poller=poller, redis_client=redis_client, tenant_slug=Config.RUNNER_TENANT_SLUG
    )

    app.config["http_client"] = http_client
    app.config["redis_client"] = redis_client
    app.config["runner"] = runner
    app.config["runner_task"] = asyncio.ensure_future(runner.run_forever())

    # Socket-owning transports (inbound waddle_transports.Transport
    # connections) -- supervised alongside the poll-drain loop above, each
    # guarded by a Valkey lease so scaling svc-ingest to N replicas never
    # opens N duplicate sockets for the same (provider, community). See
    # this module's own docstring and socket_lease.py for the full design.
    registry = AppRegistry()
    register_discord_bundles(registry)
    register_slack_bundles(registry)
    register_youtube_bundles(registry)
    register_kick_bundles(registry)
    register_twitch_bundles(registry)
    app.config["registry"] = registry

    supervisor = ReceiverSupervisor(
        base_backoff_s=Config.RECEIVER_BASE_BACKOFF_S,
        max_backoff_s=Config.RECEIVER_MAX_BACKOFF_S,
    )
    app.config["supervisor"] = supervisor

    # No silent startup exceptions (2026-09-10 fix, paired with
    # `_configure_root_logging()` above): a raised exception here would
    # otherwise propagate out of this `@app.before_serving` hook with
    # only Hypercorn's own traceback formatting as evidence -- a
    # different, unstructured path from every other log line this
    # service emits, exactly the kind of easy-to-miss channel that hid
    # the logging gap `_configure_root_logging()` fixes. Logged via the
    # properly-wired AAA `logger` (not the plain per-module loggers) with
    # exception type + message, then re-raised -- startup must still fail
    # loud and fail closed, never continue serving with a receiver that
    # never actually registered.
    try:
        _register_discord_receiver(supervisor, redis_client=redis_client, registry=registry)
        _register_slack_receiver(supervisor, redis_client=redis_client, registry=registry)
        _register_youtube_live_receiver(supervisor, redis_client=redis_client, registry=registry)
        _register_kick_receivers(supervisor, redis_client=redis_client, registry=registry)
        _register_twitch_receivers(supervisor, redis_client=redis_client, registry=registry)
        _register_twitch_eventsub(redis_client=redis_client, registry=registry)
        await supervisor.start()
    except Exception as exc:
        logger.error(
            f"svc-ingest receiver startup failed: {type(exc).__name__}: {exc}",
            action="startup",
            result="FAILED",
        )
        raise

    logger.system("svc-ingest started", action="startup", result="SUCCESS")


@app.after_serving
async def shutdown() -> None:
    """Stop the background loop, every supervised receiver, and close both clients."""
    supervisor = app.config.get("supervisor")
    if supervisor is not None:
        await supervisor.stop()

    runner = app.config.get("runner")
    if runner is not None:
        runner.stop()
    task = app.config.get("runner_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected -- this is our own cancel() above, not a failure
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(f"Error stopping runner task: {exc}")

    http_client = app.config.get("http_client")
    if http_client is not None:
        await http_client.aclose()
    redis_client = app.config.get("redis_client")
    if redis_client is not None:
        await redis_client.aclose()
    drain_redis_client = app.config.get("twitch_outbound_drain_redis_client")
    if drain_redis_client is not None:
        await drain_redis_client.aclose()
    logger.system("svc-ingest shutdown complete", action="shutdown", result="SUCCESS")


@eventsub_bp.route("/twitch/webhook", methods=["POST"])
async def twitch_eventsub_webhook():  # type: ignore[no-untyped-def]
    """Real Twitch EventSub webhook endpoint -- signature-verified, fans out via `fanout.py`.

    `webhook_callback_verification` (subscription-setup handshake) must
    echo the bare challenge string back as `text/plain`, NOT JSON-wrapped
    -- Twitch's own subscription-verification contract, ported verbatim
    from the legacy module's identical special case
    (`trigger/receiver/twitch_module/app.py`'s `eventsub_webhook`).
    """
    handler = app.config.get("twitch_eventsub_handler")
    if handler is None:
        return {"error": "EventSub not configured"}, 503

    body = await request.get_data()
    body_json = await request.get_json()
    headers = dict(request.headers)

    response_body, status = await handler.handle_webhook(
        headers=headers, body=body, body_json=body_json or {}
    )
    if "challenge" in response_body:
        return response_body["challenge"], status, {"Content-Type": "text/plain"}
    return response_body, status


app.register_blueprint(eventsub_bp)


@webhook_bp.route("/kick", methods=["POST"])
async def kick_webhook():  # type: ignore[no-untyped-def]
    """Real Kick webhook endpoint -- signature-verified, fans StreamStart/StreamEnd via `fanout.py`.

    Always mounted (unlike `eventsub_bp`'s Twitch route, whose underlying
    handler is only conditionally built in `startup()`) --
    `builtin_handlers.kick_ingest.handle_kick_webhook` itself returns 503 when
    `Config.KICK_WEBHOOK_SECRET` is unset, the same graceful
    "not configured yet" posture without needing a second app.config
    presence check here. `redis_client`/`registry` are unconditionally set
    by `startup()` before this app ever serves a request.
    """
    raw_body = await request.get_data()
    # `get_data()`'s own stub type is `str | bytes` regardless of the
    # (default-False) `as_text` arg, but the DEFAULT call always returns
    # `bytes` at runtime -- narrowed here rather than `# type: ignore`,
    # since `handle_kick_webhook`'s `body: bytes` param is a real,
    # strictly-checked signature (unlike `handler.handle_webhook(...)`
    # just above, whose `Any`-typed `app.config.get(...)` receiver hides
    # this exact same latent mismatch from mypy for the Twitch route).
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
    body_json = await request.get_json()
    headers = dict(request.headers)

    response_body, status = await handle_kick_webhook(
        headers=headers,
        body=body,
        body_json=body_json or {},
        secret=Config.KICK_WEBHOOK_SECRET,
        redis_client=app.config["redis_client"],
        registry=app.config["registry"],
        tenant=Config.RUNNER_TENANT_SLUG,
    )
    return response_body, status


app.register_blueprint(webhook_bp)


if __name__ == "__main__":  # pragma: no cover - process entrypoint, not exercised by unit tests
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
