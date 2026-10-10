"""Configuration for svc-ingest.

Env var names follow the repo-standard `MODULE_NAME`/`MODULE_PORT` pattern
(`core/svc_streaming/config.py`), plus this stage-runner's own distribution-
poll and Valkey wiring. `SECRET_KEY` mirrors `flask_core.tenancy`/`authz`'s
own `os.getenv("SECRET_KEY", ...)` lookup -- the runner mints its own
short-lived service JWT (`app.py`'s `_jwt_provider`) with the same shared
secret hub-api's `tenant_middleware`/`require_scope` verify against
(security.md Service-to-Service Auth: short-lived signed machine JWT,
OIDC-machine-JWT fallback where SPIFFE/SPIRE isn't deployed in this
environment yet -- this service is SPIFFE-ready in the sense that nothing
here precludes swapping the JWT for an mTLS/X.509-SVID identity later, that
wiring itself is out of scope for this PR).

Also carries svc-ingest's socket-owning receiver config (8-container
decision, folded in from the standalone svc-gateway skeleton): persistent
inbound transports (`receivers/discord_gateway.py`, `receivers/
twitch_irc.py`) run as `supervisor.ReceiverSupervisor`-supervised tasks
alongside the poll-drain loop above, each guarded by a `socket_lease.
SocketLease` so scaling `pipeline.svcIngest.replicas` never opens
duplicate sockets for the same `(provider, community)`.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()


def _optional_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


class Config:
    """Runtime configuration for the svc-ingest stage-runner."""

    MODULE_NAME = os.getenv("MODULE_NAME", "svc-ingest")
    MODULE_VERSION = "0.1.0"
    MODULE_PORT = int(os.getenv("MODULE_PORT", "8210"))
    PIPELINE_STAGE = "ingest"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Distribution API (hub_api/blueprints/v1/distribution.py) poll wiring.
    HUB_API_URL = os.getenv("HUB_API_URL", "http://hub-api:8204")
    DISTRIBUTION_URL = os.getenv("DISTRIBUTION_URL", f"{HUB_API_URL}/api/v1/distribution/bundles")
    POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "5.0"))
    BASE_BACKOFF_S = float(os.getenv("BASE_BACKOFF_S", "1.0"))
    MAX_BACKOFF_S = float(os.getenv("MAX_BACKOFF_S", "60.0"))

    # This runner instance's own tenant/community scope -- security.md
    # Tenant Isolation: never widened at request time, fixed at deploy time
    # via this env var, matching the JWT `tenant` claim the runner mints
    # for itself.
    RUNNER_TENANT_SLUG = os.getenv("RUNNER_TENANT_SLUG", "global")
    RUNNER_COMMUNITY_ID = _optional_int(os.getenv("RUNNER_COMMUNITY_ID"))

    # Shared HS256 secret -- mirrors flask_core.tenancy/authz's own
    # os.getenv("SECRET_KEY", "change-me-in-production") fallback exactly,
    # so a token minted here verifies against hub-api's own decorators.
    SECRET_KEY = require_secret_key()
    JWT_SCOPE = "distribution:read"

    # Helm's `-secrets` Secret only ever defines `REDIS_URL` (in-cluster
    # host + auth password -- k8s/helm/waddlebot/templates/secrets.yaml);
    # `svc-ingest.yaml`'s own "KNOWN GAP" comment already flagged the
    # symptom (falls back to a dev default in a real cluster) without
    # naming the cause -- this env var name never matched what the chart
    # actually injects, so the shared Valkey client silently pointed at
    # `redis://localhost:6379/0` (no auth, wrong host) instead of the
    # real `infra-redis`. `VALKEY_URL` stays checked first (an explicit
    # override some other deploy path may still set); `REDIS_URL` is the
    # real in-cluster value -- same fallback chain
    # `flask_core.http_rate_limit` already uses for the identical reason.
    VALKEY_URL = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"

    # Explicit connect/read timeout for the shared Valkey client (`app.py`'s
    # `redis_client`) -- was previously unset, relying entirely on
    # redis-py's own version-dependent default. Hardened here as a second,
    # independent bound underneath `SOCKET_LEASE_CLAIM_TIMEOUT_S`'s
    # asyncio-level guard (socket_lease.py) -- defense in depth, not a
    # substitute for it.
    REDIS_SOCKET_TIMEOUT_S = float(os.getenv("REDIS_SOCKET_TIMEOUT_S", "5.0"))

    # Discord gateway receiver. Empty string (never committed, never
    # logged) disables the receiver entirely -- `app.py`'s startup skips
    # it gracefully, matching `trigger/receiver/discord_module/app.py`'s
    # own "DISCORD_BOT_TOKEN not configured" skip behavior.
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

    # Slack Socket Mode receiver -- app-level token opens the Socket Mode
    # WebSocket itself, bot token builds the Web API client (`auth.test`
    # validation + per-call `apps.connections.open` override -- see
    # receivers/slack_socket.py's own docstring). Either empty (never
    # committed, never logged) disables the receiver entirely -- `app.py`'s
    # startup skips it gracefully, matching Discord's own skip behavior.
    SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")

    # Twitch IRC receiver -- waddle_transports.transports.irc.IrcTransport's
    # own config shape (`host`/`port`/`nick`/`password_ref`/`use_tls`), one
    # connection per channel (that transport's own single-channel-per-call
    # contract -- see receivers/twitch_irc.py's docstring).
    # `TWITCH_BOT_TOKEN_REF` is an env-var *name* (never a raw token) --
    # `waddle_transports.signing.resolve_secret` resolves it at connect
    # time; the referenced value must already carry Twitch's own `oauth:`
    # prefix (this transport is Twitch-agnostic, it does not add one).
    # Empty channel list disables the receiver entirely -- `app.py`'s
    # startup skips it gracefully, matching Discord's own skip behavior.
    TWITCH_IRC_HOST = os.getenv("TWITCH_IRC_HOST", "irc.chat.twitch.tv")
    TWITCH_IRC_PORT = int(os.getenv("TWITCH_IRC_PORT", "6697"))
    TWITCH_IRC_USE_TLS = os.getenv("TWITCH_IRC_USE_TLS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    TWITCH_BOT_NICK = os.getenv("TWITCH_BOT_NICK", "waddlebot")
    TWITCH_BOT_TOKEN_REF = os.getenv("TWITCH_BOT_TOKEN_REF", "")
    # Comma-separated channel names -- no DB-backed channel list in this
    # MVP receiver (documented gap, same precedent Discord's single-
    # connection-serves-everything model sets, adapted here to N
    # independent per-channel connections -- see receivers/twitch_irc.py).
    TWITCH_CHANNELS = [
        c.strip().lower() for c in os.getenv("TWITCH_CHANNELS", "").split(",") if c.strip()
    ]

    # ReceiverSupervisor backoff bounds for socket receivers -- same shape
    # as POLL_INTERVAL_S's BASE_BACKOFF_S/MAX_BACKOFF_S above, kept as
    # separate env vars since a died gateway socket and a failed
    # distribution poll are unrelated failure domains that may need
    # different tuning.
    RECEIVER_BASE_BACKOFF_S = float(os.getenv("RECEIVER_BASE_BACKOFF_S", "1.0"))
    RECEIVER_MAX_BACKOFF_S = float(os.getenv("RECEIVER_MAX_BACKOFF_S", "60.0"))

    # socket_lease.SocketLease TTL/renew wiring -- renew_interval_s must
    # stay comfortably below ttl_s so a normal renewal cadence never
    # brushes up against expiry (a missed renewal or two should not cost
    # the lease).
    SOCKET_LEASE_TTL_S = float(os.getenv("SOCKET_LEASE_TTL_S", "30.0"))
    SOCKET_LEASE_RENEW_INTERVAL_S = float(os.getenv("SOCKET_LEASE_RENEW_INTERVAL_S", "10.0"))

    # Bounds every claim/renew/release Redis round-trip
    # (`socket_lease.LeasedReceiver`) -- was previously unbounded, letting
    # a stalled/unresponsive Valkey connection hang a receiver (and this
    # replica's entire inbound path for that provider) forever with no
    # error or log line. See socket_lease.py's own LeasedReceiver.run()
    # docstring for the pooled-connection-reuse hazard this also guards
    # against.
    SOCKET_LEASE_CLAIM_TIMEOUT_S = float(os.getenv("SOCKET_LEASE_CLAIM_TIMEOUT_S", "5.0"))

    # Alpha runs `pipeline.svcIngest.replicas: 1` -- no contention over a
    # socket lease is possible, so a lease backend that's unreachable/
    # timing out must never permanently block ingest. Default True is
    # safe for single-replica alpha (proceed without a confirmed lease,
    # loudly logged); set False for any deployment actually running >1
    # svc-ingest replica, where running without a confirmed lease risks
    # duplicate gateway sockets on the same platform bot token.
    SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE = os.getenv(
        "SOCKET_LEASE_RUN_WITHOUT_ON_UNAVAILABLE", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Twitch EventSub webhook (`eventsub.py`, mounted at
    # POST /eventsub/twitch/webhook). Empty secret disables the endpoint's
    # signature verification path entirely -- `app.py`'s startup skips
    # registering the handler, matching the IRC receiver's own
    # empty-token skip behavior.
    TWITCH_EVENTSUB_SECRET = os.getenv("TWITCH_EVENTSUB_SECRET", "")

    @classmethod
    def twitch_irc_config_base(cls) -> dict[str, object]:
        """The shared (non-channel) `IrcTransport` config.

        `app.py` adds `channel` per receiver.
        """
        return {
            "host": cls.TWITCH_IRC_HOST,
            "port": cls.TWITCH_IRC_PORT,
            "use_tls": cls.TWITCH_IRC_USE_TLS,
            "nick": cls.TWITCH_BOT_NICK,
            "password_ref": cls.TWITCH_BOT_TOKEN_REF or None,
        }

    # YouTube Live poll receiver (receivers/youtube_live_poll.py) -- Data
    # API v3 polling (no persistent gateway socket, no PubSubHubbub push --
    # see that receiver's own module docstring for why this MVP polls
    # instead of reusing the legacy trigger/receiver/youtube_live_module's
    # webhook approach). One poller per configured channel, socket_lease-
    # guarded per channel exactly like TWITCH_CHANNELS above --
    # `community=<channel_id>` (see receivers/youtube_live_poll.py and
    # builtin_handlers/youtube_live_ingest.py's own docstrings). Comma-separated
    # channel ids -- same "no DB-backed channel list yet" MVP posture as
    # TWITCH_CHANNELS documents.
    YOUTUBE_LIVE_CHANNELS = [
        c.strip() for c in os.getenv("YOUTUBE_LIVE_CHANNELS", "").split(",") if c.strip()
    ]

    # Credential env var *names* (never raw values) resolved at connect
    # time via `waddle_transports.signing.resolve_secret` -- fixed to the
    # Helm chart's own `-secrets` Secret key names (already provisioned
    # for `hub_api/services/music_providers/youtube.py`'s identical
    # credential set), not independently configurable the way
    # TWITCH_BOT_TOKEN_REF's *value* is -- there is only one place these
    # four secrets live in this deployment. Precedence (API key first,
    # then the OAuth trio) mirrors that same hub_api module exactly; see
    # receivers/youtube_live_poll.py's own docstring for the duplicated
    # (not imported -- a different service/process) refresh-token helper.
    YOUTUBE_API_KEY_REF = "YOUTUBE_API_KEY"
    YOUTUBE_CLIENT_ID_REF = "YOUTUBE_CLIENT_ID"
    YOUTUBE_CLIENT_SECRET_REF = "YOUTUBE_CLIENT_SECRET"  # noqa: S105 - an env var name, not a secret
    YOUTUBE_REFRESH_TOKEN_REF = "YOUTUBE_REFRESH_TOKEN"  # noqa: S105 - an env var name, not a secret

    # Poll-loop tuning, all overridable per-deployment -- defaults match
    # receivers/youtube_live_poll.py's own module-level fallback constants
    # exactly (used whenever this config wiring is bypassed, e.g. a unit
    # test constructing `YouTubeLivePollReceiver` directly).
    YOUTUBE_LIVE_POLL_NO_BROADCAST_BACKOFF_S = float(
        os.getenv("YOUTUBE_LIVE_POLL_NO_BROADCAST_BACKOFF_S", "30.0")
    )
    YOUTUBE_LIVE_POLL_MAX_QUOTA_ERRORS = int(os.getenv("YOUTUBE_LIVE_POLL_MAX_QUOTA_ERRORS", "5"))
    YOUTUBE_LIVE_CHAT_MAX_RESULTS = int(os.getenv("YOUTUBE_LIVE_CHAT_MAX_RESULTS", "200"))

    @classmethod
    def youtube_credentials_configured(cls) -> bool:
        """Presence-only check (no network I/O) -- a usable API key or a full OAuth trio.

        Mirrors `hub_api/services/music_providers/youtube.py`'s
        `youtube_credentials_configured()` precedence exactly (API key
        first, then ALL three OAuth env vars); duplicated rather than
        imported since hub_api is a separate service/process from
        svc-ingest. Used by `app.py`'s `_register_youtube_live_receiver`
        to decide whether to register any poller at all -- missing creds
        skip registration with a WARN, matching Discord/Twitch's own
        empty-token skip behavior.
        """
        if os.getenv(cls.YOUTUBE_API_KEY_REF):
            return True
        return bool(
            os.getenv(cls.YOUTUBE_CLIENT_ID_REF)
            and os.getenv(cls.YOUTUBE_CLIENT_SECRET_REF)
            and os.getenv(cls.YOUTUBE_REFRESH_TOKEN_REF)
        )

    # Kick Pusher chat receiver (receivers/kick_pusher.py) -- one Pusher
    # WebSocket connection per channel slug, matching TWITCH_CHANNELS'
    # identical "no DB-backed channel list yet" MVP posture (see that env
    # var's own comment above). Comma-separated channel *slugs* (Kick's
    # username-shaped channel identifier, not a numeric id -- resolved to
    # a chatroom id by the receiver itself at connect time). Empty list
    # disables Kick ingest entirely -- a future app.py wiring would skip
    # registering any receiver, matching Discord/Twitch/Slack's own
    # empty-config skip behavior.
    KICK_CHANNELS = [
        c.strip().lower() for c in os.getenv("KICK_CHANNELS", "").split(",") if c.strip()
    ]

    # Kick Pusher app key/cluster overrides -- see receivers/kick_pusher.py's
    # own module docstring for why the default (Kick's own PUBLIC Pusher
    # client key) is not a secret and is never resolved via resolve_secret.
    # Empty string leaves receivers/kick_pusher.py's own DEFAULT_PUSHER_KEY/
    # DEFAULT_CLUSTER constants in effect.
    KICK_PUSHER_KEY = os.getenv("KICK_PUSHER_KEY", "")
    KICK_PUSHER_CLUSTER = os.getenv("KICK_PUSHER_CLUSTER", "")

    # Kick webhook (mod/sub/stream lifecycle events -- StreamStart/
    # StreamEnd/Subscription/etc. -- a SEPARATE delivery path from Pusher
    # chat). Empty secret disables the endpoint's signature verification
    # path entirely -- see builtin_handlers/kick_ingest.py's own
    # handle_kick_webhook()/verify_kick_webhook_signature() docstrings;
    # NOT yet mounted by app.py (out of this PR's scope, see that
    # module's own docstring for where it should be mounted).
    KICK_WEBHOOK_SECRET = os.getenv("KICK_WEBHOOK_SECRET", "")

    # RSI Spectrum one-way ingest (receivers/spectrum_poll.py, gh #101).
    # Gated by the `waddles.spectrum-integration` flag: ENV baseline
    # `FLAG_WADDLES_SPECTRUM_INTEGRATION` (default OFF), overridden by PostHog
    # when connected -- see `spectrum_flag_baseline()`. Comma-separated source
    # ids, one lease-guarded poller each (no DB-backed list yet -- same MVP
    # posture as TWITCH_CHANNELS/YOUTUBE_LIVE_CHANNELS).
    SPECTRUM_FORUM_CHANNELS = [
        c.strip() for c in os.getenv("SPECTRUM_FORUM_CHANNELS", "").split(",") if c.strip()
    ]
    SPECTRUM_LOBBIES = [
        c.strip() for c in os.getenv("SPECTRUM_LOBBIES", "").split(",") if c.strip()
    ]

    # Env var *name* (never a value) holding the RSI session token, resolved at
    # connect time via `waddle_transports.signing.resolve_secret`.
    SPECTRUM_RSI_TOKEN_REF = "SPECTRUM_RSI_TOKEN"  # noqa: S105 - an env var name, not a secret
    SPECTRUM_API_BASE = os.getenv("SPECTRUM_API_BASE", "")
    SPECTRUM_POLL_INTERVAL_S = float(os.getenv("SPECTRUM_POLL_INTERVAL_S", "15.0"))
    SPECTRUM_MAX_CONSECUTIVE_ERRORS = int(os.getenv("SPECTRUM_MAX_CONSECUTIVE_ERRORS", "8"))
    SPECTRUM_EMIT_BACKLOG = os.getenv("SPECTRUM_EMIT_BACKLOG", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Spectrum org sync -- roster join/leave/role changes + event create/update/RSVP
    # (receivers/spectrum_poll.py `roster`/`events` kinds, gh #101). Needs BOTH
    # `waddles.spectrum-integration` and `waddles.spectrum-org-sync` ON (ENV baseline
    # `FLAG_WADDLES_SPECTRUM_ORG_SYNC`, default OFF -- see `spectrum_org_flag_baseline()`).
    # Comma-separated Spectrum community ids; each gets a lease-guarded roster poller
    # and events poller sharing SPECTRUM_RSI_TOKEN_REF.
    SPECTRUM_ORG_COMMUNITIES = [
        c.strip() for c in os.getenv("SPECTRUM_ORG_COMMUNITIES", "").split(",") if c.strip()
    ]
    # Org snapshots change slowly and a roster fetch is several paged requests: poll
    # slowly (the receiver additionally enforces a 60s floor).
    SPECTRUM_ORG_POLL_INTERVAL_S = float(os.getenv("SPECTRUM_ORG_POLL_INTERVAL_S", "300.0"))
    # Shrink guard: a roster that loses more than this fraction in one poll is treated as
    # an RSI glitch (snapshot not advanced, poller backs off) instead of mass-departure
    # events. Raise to 1.0 to accept a genuine mass change; see docs/integrations/spectrum.md.
    SPECTRUM_ROSTER_MAX_DEPARTURE_RATIO = float(
        os.getenv("SPECTRUM_ROSTER_MAX_DEPARTURE_RATIO", "0.5")
    )
    # Emit the whole current roster/event list as joined/created on the FIRST poll (a
    # deliberate bootstrap -- default false: connecting only primes, it never floods).
    SPECTRUM_ORG_EMIT_BACKLOG = os.getenv("SPECTRUM_ORG_EMIT_BACKLOG", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Valkey TTL of a stored org snapshot; after this long without a poll the next run
    # simply re-primes (no events) instead of diffing against a stale picture.
    SPECTRUM_SNAPSHOT_TTL_S = int(os.getenv("SPECTRUM_SNAPSHOT_TTL_S", str(30 * 24 * 3600)))

    @classmethod
    def spectrum_flag_baseline(cls) -> bool:
        """ENV baseline for `waddles.spectrum-integration` (default OFF; PostHog overrides)."""
        return os.getenv("FLAG_WADDLES_SPECTRUM_INTEGRATION", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @classmethod
    def spectrum_org_flag_baseline(cls) -> bool:
        """ENV baseline for `waddles.spectrum-org-sync` (default OFF; PostHog overrides)."""
        return os.getenv("FLAG_WADDLES_SPECTRUM_ORG_SYNC", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @classmethod
    def spectrum_token_configured(cls) -> bool:
        """Presence-only check (no network I/O) for the RSI session token env var."""
        return bool(os.getenv(cls.SPECTRUM_RSI_TOKEN_REF))
