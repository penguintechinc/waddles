"""
Redis Streams Pipeline for Event-Driven Architecture

Provides a high-level event streaming pipeline with:
- Multiple event streams (inbound, process, actions, responses)
- Dead letter queue support
- Consumer group management
- Event acknowledgment and retry logic
- Stream monitoring and diagnostics
- Frozen typed stage-to-stage pipeline contract (PlatformEvent, StageEnvelope)
"""

from __future__ import annotations

import os
import json
import logging
import asyncio
from collections.abc import Mapping
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict, field
from datetime import datetime

# --- Per-(community x app) isolation keys (App Bundle SDK Phase C4) ---
#
# Design doc: docs/plans/2026-08-31-app-bundle-sdk-design.md Sec6.2/Sec7.2.
# Three stream-naming generations coexist in this codebase on purpose:
#   1. Legacy (STREAM_INBOUND etc. + _make_stream_name below) --
#      `{stream_prefix}:{stream}` -- no tenant/community/app. UNCHANGED.
#   2. `waddles:t:{tenant}:{stage}` -- tenant-scoped only, still one shared
#      stream per stage across every bundle. Not implemented here.
#   3. THIS: `waddles:t:{tenant}:c:{community}:app:{app_id}:{stage}` --
#      full per-(community, app) isolation. Additive only -- neither legacy
#      scheme above is modified or removed by these helpers.
#
# Reconciliation with the tenant ACL-user scheme (design doc Sec6.2,
# 2026-08-26-v3-scbm-apps-design.md:586-599, `waddles-t-{tenant}-{stage}`):
# that credential scheme is UNCHANGED -- a Valkey ACL user is still
# provisioned per (tenant, stage). Only the *key* underneath that
# credential changes: it now also carries `community` and `app_id`, so one
# (tenant, stage)-scoped credential addresses many per-community, per-app
# keys within its ACL key-prefix pattern (`waddles:t:{tenant}:*`) instead
# of a single shared stream per stage.
#
# `app_id` is a dot-delimited reverse-DNS-style identifier (e.g.
# `waddles.bot.shoutout.default`). Dots are valid Valkey/Redis key bytes,
# so `app_id` passes through these builders unescaped and intact.

BUNDLE_STAGES = ("ingest", "process", "action")

_TENANT_WIDE_COMMUNITY_SEGMENT = "_tenant"


def _validate_bundle_stage(stage: str) -> None:
    """Reject any stage outside the fixed ingest/process/action set.

    Keeps a malformed stage from silently producing a plausible-looking
    but wrong isolation key (e.g. a typo'd stage routing events into a
    stream nothing ever consumes).
    """
    if stage not in BUNDLE_STAGES:
        raise ValueError(
            f"invalid bundle stage {stage!r}; must be one of {BUNDLE_STAGES}"
        )


def _bundle_community_segment(community: Optional[str]) -> str:
    """Render the `c:` segment of a bundle isolation key.

    `community is None` denotes a tenant-wide activation (`AppInstallation`
    with `community_id IS NULL`). Per design doc Sec7.2 this renders as the
    literal `_tenant` segment rather than being omitted, so every key this
    module produces is uniformly parseable -- splitting on `:` always
    yields the same field count and field meaning regardless of activation
    scope.
    """
    return community if community is not None else _TENANT_WIDE_COMMUNITY_SEGMENT


def _bundle_key_base(tenant: str, community: Optional[str], app_id: str) -> str:
    """Shared `waddles:t:{tenant}:c:{community}:app:{app_id}` prefix."""
    return (
        f"waddles:t:{tenant}:c:{_bundle_community_segment(community)}:app:{app_id}"
    )


def bundle_stream_key(
    tenant: str, community: Optional[str], app_id: str, stage: str
) -> str:
    """Build the per-(community x app x stage) Valkey stream key.

    `stage` in {ingest, process, action}; each gets its own independent
    stream. `community=None` -> tenant-wide activation, rendered as
    `c:_tenant` (never omitted). See module-level docstring above for the
    full isolation-key rationale and ACL-scheme reconciliation.
    """
    _validate_bundle_stage(stage)
    return f"{_bundle_key_base(tenant, community, app_id)}:{stage}"


def bundle_config_key(tenant: str, community: Optional[str], app_id: str) -> str:
    """Build the per-(community x app) bundle config key (`...:cfg`)."""
    return f"{_bundle_key_base(tenant, community, app_id)}:cfg"


def bundle_state_key(tenant: str, community: Optional[str], app_id: str) -> str:
    """Build the per-(community x app) bundle state key (`...:state`)."""
    return f"{_bundle_key_base(tenant, community, app_id)}:state"


def bundle_consumer_group(app_id: str, stage: str) -> str:
    """Build the `{app_id}:{stage}-group` consumer group name.

    Feeds the existing `StreamPipeline.create_consumer_group` mechanism
    unchanged -- only the naming convention is new for Phase C4.
    """
    _validate_bundle_stage(stage)
    return f"{app_id}:{stage}-group"


@dataclass(frozen=True, slots=True)
class BundleIsolationKeys:
    """Typed per-(tenant x community x app_id) Valkey key namespace.

    Thin struct wrapper over `bundle_stream_key`/`bundle_config_key`/
    `bundle_state_key`/`bundle_consumer_group` for callers that want one
    object to carry the (tenant, community, app_id) triple instead of
    threading it through every call. Frozen + slots: a key namespace is an
    immutable value object, not mutable state.
    """

    tenant: str
    community: Optional[str]
    app_id: str

    def stream_key(self, stage: str) -> str:
        """Stream key for one stage; see `bundle_stream_key`."""
        return bundle_stream_key(self.tenant, self.community, self.app_id, stage)

    @property
    def config_key(self) -> str:
        """Config key; see `bundle_config_key`."""
        return bundle_config_key(self.tenant, self.community, self.app_id)

    @property
    def state_key(self) -> str:
        """State key; see `bundle_state_key`."""
        return bundle_state_key(self.tenant, self.community, self.app_id)

    def consumer_group(self, stage: str) -> str:
        """Consumer group name for one stage; see `bundle_consumer_group`."""
        return bundle_consumer_group(self.app_id, stage)


# --- Frozen typed stage-to-stage pipeline contract ---
#
# Root cause this fixes: stage handlers passed untyped dicts over Valkey, and
# one stage wrapped an already-normalized dict under a second `payload` key,
# leaving real fields one level too deep for the next stage to find -- a
# lookup that returns None silently instead of raising. `PlatformEvent`/
# `StageEnvelope` are the ONLY typed objects stage code should pass to each
# other from here on; the queue-crossing field is deliberately named `event`
# (never `payload`) precisely so that double-nesting a payload dict under a
# `payload` key can no longer happen structurally. Frozen + slots: a stage
# produces a NEW instance (`dataclasses.replace`) rather than mutating one in
# place.
#
# D30 (workstream identity, end-to-end trace, tenant wall): the Rust data
# plane's `penguin-spine` envelope type requires `schema_version`,
# `workstream_id`, `event_id` and `binding` on every envelope, with no
# dual-read of the pre-D30 shape. This module carries the SAME field names
# and shapes -- golden fixtures under `tests/fixtures/spine/` are asserted
# against by both readers -- but keeps all six new fields additive-optional
# (absent/null -> None) here rather than hard-required: this module is still
# imported by the pre-cut-over Python-only stage runners, which have not yet
# been migrated to mint them. A fully populated D30 envelope validates
# identically on both sides; a legacy envelope missing them stays valid
# here and would be refused by the Rust reader -- an intentional,
# documented asymmetry during the transition, not a gap.


class EnvelopeError(ValueError):
    """Raised when a queue-crossing pipeline object is malformed on read.

    Covers a missing/wrong-typed required field, an unknown top-level key,
    and, deliberately, any legacy pre-fix shape (e.g. a dict with no `event`
    key). A malformed or legacy-shaped message is refused, never silently
    coerced.
    """


def _require_str(d: Mapping[str, Any], key: str) -> str:
    """Fetch a required non-empty string field, or raise `EnvelopeError`."""
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"{key!r} must be a non-empty string, got {value!r}")
    return value


def _optional_str(d: Mapping[str, Any], key: str) -> str | None:
    """Fetch an optional string field (`None` allowed), or raise `EnvelopeError`."""
    value = d.get(key)
    if value is not None and not isinstance(value, str):
        raise EnvelopeError(f"{key!r} must be a string or null, got {value!r}")
    return value


def _require_object(d: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Fetch a required JSON-object field as a `dict`, or raise `EnvelopeError`."""
    value = d.get(key)
    if not isinstance(value, dict):
        raise EnvelopeError(
            f"{key!r} must be a JSON object, got {type(value).__name__}"
        )
    return value


def _reject_unknown_keys(d: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    """Raise `EnvelopeError` if `d` carries a key outside `allowed`.

    Shared strict-deserialization helper (spec Sec6.1.2): "an unknown
    top-level field ... is an error. No coercion, ever." Applied to every
    queue-crossing object in this module, not just `StageEnvelope`.
    """
    unknown = set(d) - allowed
    if unknown:
        raise EnvelopeError(f"unknown {what} field(s): {sorted(unknown)}")


@dataclass(slots=True, frozen=True)
class PlatformEvent:
    """A normalized inbound platform event.

    Transport-neutral metadata (`platform`, `event_type`, `actor`,
    `occurred_at`) plus a platform-specific `payload` dict (text,
    channel_id, guild_id, message_id, author_id, ...).
    """

    platform: str
    event_type: str
    actor: str | None
    payload: dict[str, Any]
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        return {
            "platform": self.platform,
            "event_type": self.event_type,
            "actor": self.actor,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PlatformEvent:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad shape."""
        platform = _require_str(d, "platform")
        event_type = _require_str(d, "event_type")
        actor = _optional_str(d, "actor")
        payload = _require_object(d, "payload")
        occurred_at = _require_str(d, "occurred_at")
        _reject_unknown_keys(d, _PLATFORM_EVENT_ALLOWED_KEYS, "PlatformEvent")
        return cls(
            platform=platform,
            event_type=event_type,
            actor=actor,
            payload=payload,
            occurred_at=occurred_at,
        )


_PLATFORM_EVENT_ALLOWED_KEYS = frozenset(
    {"platform", "event_type", "actor", "payload", "occurred_at"}
)


#: Reserved `PlatformEvent.payload` key a process-stage `transform()` sets
#: to request cross-app routing (gh #298). `transform_fn`'s frozen contract
#: is `PlatformEvent -> PlatformEvent | None` -- it never sees or returns a
#: `StageEnvelope` -- so this payload key is the only channel available for
#: a bundle to communicate a `target_app_id` back to the runner that DOES
#: build the outbound `StageEnvelope`. `core/svc_process/runner.py` pops
#: this key back out of the payload before enqueuing, so it never leaks
#: into the actual message data an action-stage bundle (or a relayed chat
#: reply) sees. General mechanism -- any process bundle may set it, not
#: forum-specific.
PROCESS_TARGET_APP_ID_KEY = "_target_app_id"

#: The only `StageEnvelope.schema_version` value this module recognizes as
#: the D30 shape (spec Sec6.1.2). Additive-optional here (see the module
#: note above): an ABSENT `schema_version` still means "pre-D30 legacy
#: shape" and is accepted, but a PRESENT value other than this one is
#: rejected outright -- no coercion, no silent reinterpretation.
ENVELOPE_SCHEMA_VERSION = 2


@dataclass(slots=True, frozen=True)
class Trace:
    """W3C trace context carried on every envelope (D30, spec Sec5.11/6.1.2).

    Supersedes the pre-D30 single-field `trace_context` (A11): absent means
    "no parent span", exactly like `target_app_id`. `traceparent` is
    required whenever a `Trace` object itself is present; `tracestate` is
    optional.
    """

    traceparent: str
    tracestate: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        return {"traceparent": self.traceparent, "tracestate": self.tracestate}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Trace:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad shape."""
        traceparent = _require_str(d, "traceparent")
        tracestate = _optional_str(d, "tracestate")
        _reject_unknown_keys(d, _TRACE_ALLOWED_KEYS, "trace")
        return cls(traceparent=traceparent, tracestate=tracestate)


_TRACE_ALLOWED_KEYS = frozenset({"traceparent", "tracestate"})


@dataclass(slots=True, frozen=True)
class Binding:
    """Envelope tenant-binding MAC (D30, spec Sec5.11/6.1.2): `{kid, mac}`.

    `kid` names the active HMAC key version; `mac` is the lowercase-hex
    HMAC-SHA256 output of the Sec5.11 formula. Computed and verified by the
    Rust stage services -- this module only carries the shape.
    """

    kid: str
    mac: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict."""
        return {"kid": self.kid, "mac": self.mac}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Binding:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad shape."""
        kid = _require_str(d, "kid")
        mac = _require_str(d, "mac")
        _reject_unknown_keys(d, _BINDING_ALLOWED_KEYS, "binding")
        return cls(kid=kid, mac=mac)


_BINDING_ALLOWED_KEYS = frozenset({"kid", "mac"})


def _optional_schema_version(d: Mapping[str, Any]) -> int | None:
    """Fetch the optional `schema_version` field (D30, spec Sec6.1.2).

    Additive-optional: absent or explicit `null` -> `None`, preserving every
    pre-D30 envelope's validity (a round-tripped envelope's own `to_dict()`
    always emits this key, so `null` must be treated identically to
    "absent" here). A PRESENT non-null value must equal
    `ENVELOPE_SCHEMA_VERSION` -- this module carries the D30 shape but,
    unlike the Rust reader, does not treat absence itself as an error (see
    the module note above).
    """
    value = d.get("schema_version")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise EnvelopeError(f"'schema_version' must be an integer, got {value!r}")
    if value != ENVELOPE_SCHEMA_VERSION:
        raise EnvelopeError(
            f"'schema_version' must equal {ENVELOPE_SCHEMA_VERSION}, got {value!r}"
        )
    return value


def _optional_trace(d: Mapping[str, Any]) -> Trace | None:
    """Fetch the optional `trace` object (D30, spec Sec5.11/6.1.2, A11).

    Supersedes the pre-D30 single-field `trace_context`; absent or explicit
    `null` -> `None` ("no parent span"), exactly like `target_app_id`. When
    present, strictly validated via `Trace.from_dict`.
    """
    value = d.get("trace")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EnvelopeError(
            f"'trace' must be a JSON object or null, got {type(value).__name__}"
        )
    return Trace.from_dict(value)


def _optional_binding(d: Mapping[str, Any]) -> Binding | None:
    """Fetch the optional `binding` object (D30, spec Sec5.11/6.1.2).

    Additive-optional for the same reason as `schema_version`: the
    tenant-binding MAC is computed and verified by the Rust stage services,
    not by this module, so a pre-D30 envelope with no `binding` key stays a
    valid legacy shape here. When present, strictly validated via
    `Binding.from_dict`.
    """
    value = d.get("binding")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EnvelopeError(
            f"'binding' must be a JSON object or null, got {type(value).__name__}"
        )
    return Binding.from_dict(value)


_STAGE_ENVELOPE_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "tenant",
        "community",
        "app_id",
        "stage",
        "event",
        "ts",
        "target_app_id",
        "workstream_id",
        "event_id",
        "session_id",
        "trace",
        "binding",
    }
)


@dataclass(slots=True, frozen=True)
class StageEnvelope:
    """One pipeline queue message routed between stages.

    `event` (a `PlatformEvent`) is the payload being carried -- NOT named
    `payload`, deliberately: this rename makes payload-under-payload
    double-nesting structurally impossible, since the real message data is
    always reached at `envelope.event.payload[...]`, never
    `envelope.event["payload"][...]`.

    `target_app_id` (gh #298, feature-bundle-routing): OPTIONAL, defaults to
    `None` for 100% backward compatibility with every existing envelope.
    Routing between stages is otherwise strict-by-queue-key -- a consumer
    dispatches whatever it popped from its own `app_id:stage` key, no
    content-based re-route (see `core/svc_action/runner.py`'s module
    docstring). `target_app_id` is the one sanctioned escape hatch: when a
    process-stage bundle produces an event that belongs to a DIFFERENT
    app's action stage (e.g. `bot_process` delegating `!forum` to the
    community-forums feature bundle), it sets this field and the enqueuing
    stage (`core/svc_process/runner.py`) pushes to that app's `:action` key
    instead of the originating `bundle.app_id`'s. This changes the
    destination QUEUE KEY only -- `tenant`/`community` above are still
    sourced exclusively from `flask_core.get_bundle_context()`, never from
    event payload, so the security/tenancy invariant is untouched.

    `schema_version`, `workstream_id`, `event_id`, `session_id`, `trace` and
    `binding` are the D30 workstream-identity/trace/tenant-wall fields
    (spec Sec5.11/6.1.2), minted once by svc-ingest and copied verbatim by
    every later stage -- never accepted from a bundle's own output. All six
    are additive-optional here (see the module note above): every existing
    envelope that predates D30 stays valid.
    """

    tenant: str
    community: str | None
    app_id: str
    stage: str
    event: PlatformEvent
    ts: str
    target_app_id: str | None = None
    schema_version: int | None = None
    workstream_id: str | None = None
    event_id: str | None = None
    session_id: str | None = None
    trace: Trace | None = None
    binding: Binding | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict; `event` nests under an `event` key."""
        return {
            "schema_version": self.schema_version,
            "tenant": self.tenant,
            "community": self.community,
            "app_id": self.app_id,
            "stage": self.stage,
            "event": self.event.to_dict(),
            "ts": self.ts,
            "target_app_id": self.target_app_id,
            "workstream_id": self.workstream_id,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "binding": self.binding.to_dict() if self.binding is not None else None,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> StageEnvelope:
        """Deserialize from a plain dict; raises `EnvelopeError` on a bad/legacy shape.

        Requires an `event` key holding a JSON object; a legacy pre-fix
        message shaped without one (e.g. carrying its data directly under
        `payload` at the top level instead) is refused, not coerced.
        `target_app_id` and the D30 fields (`schema_version`,
        `workstream_id`, `event_id`, `session_id`, `trace`, `binding`) are
        all optional -- absent or explicit `null` deserialize to `None`,
        preserving every pre-D30 envelope's validity. A PRESENT value for
        any of them is still strictly validated: wrong type, a malformed
        nested object, or a `schema_version` other than
        `ENVELOPE_SCHEMA_VERSION` is rejected outright, as is any unknown
        top-level key.
        """
        tenant = _require_str(d, "tenant")
        app_id = _require_str(d, "app_id")
        stage = _require_str(d, "stage")
        if stage not in BUNDLE_STAGES:
            raise EnvelopeError(f"'stage' {stage!r} is not one of {BUNDLE_STAGES}")
        community = _optional_str(d, "community")
        ts = _require_str(d, "ts")
        event = _require_object(d, "event")
        target_app_id = _optional_str(d, "target_app_id")

        schema_version = _optional_schema_version(d)
        workstream_id = _optional_str(d, "workstream_id")
        event_id = _optional_str(d, "event_id")
        session_id = _optional_str(d, "session_id")
        trace = _optional_trace(d)
        binding = _optional_binding(d)

        _reject_unknown_keys(d, _STAGE_ENVELOPE_ALLOWED_KEYS, "StageEnvelope")

        return cls(
            tenant=tenant,
            community=community,
            app_id=app_id,
            stage=stage,
            event=PlatformEvent.from_dict(event),
            ts=ts,
            target_app_id=target_app_id,
            schema_version=schema_version,
            workstream_id=workstream_id,
            event_id=event_id,
            session_id=session_id,
            trace=trace,
            binding=binding,
        )


try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Event wrapper for stream events"""
    id: str
    stream: str
    data: Dict[str, Any]
    retry_count: int = 0
    timestamp: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


class StreamPipeline:
    """
    Redis Streams Pipeline for Waddles event processing.

    Manages multiple event streams with dedicated purposes:
    - inbound: External events entering the system
    - process: Business logic processing of inbound events
    - actions: System actions to be performed
    - responses: Responses to be sent back

    Features:
    - Configurable stream names
    - Dead letter queue for failed events
    - Consumer group management
    - Event acknowledgment
    - Retry logic with max attempts
    - Stream monitoring and diagnostics
    """

    # Default stream names
    STREAM_INBOUND = "events:inbound"
    STREAM_PROCESS = "events:process"
    STREAM_ACTIONS = "events:actions"
    STREAM_RESPONSES = "events:responses"

    # GDPR: unbounded streams are an unbounded retention period. This is the
    # default applied to every stream write that does not pass an explicit
    # bound (main streams already require callers to pass max_len; the DLQ
    # bound lives here since poison events otherwise sit the longest).
    DEFAULT_DLQ_MAXLEN = 10000

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream_prefix: str = "waddlebot:stream",
        dlq_prefix: str = "waddlebot:dlq",
        max_retries: Optional[int] = None,
        batch_size: Optional[int] = None,
        block_ms: Optional[int] = None,
        enabled: Optional[bool] = None,
        dlq_maxlen: Optional[int] = None
    ):
        """
        Initialize stream pipeline.

        Args:
            redis_url: Redis connection URL
            stream_prefix: Prefix for stream names
            dlq_prefix: Prefix for dead letter queue names
            max_retries: Maximum retry attempts (from env or default: 3)
            batch_size: Events per batch (from env or default: 10)
            block_ms: Block time in ms (from env or default: 5000)
            enabled: Enable pipeline (from env or default: False)
            dlq_maxlen: Maximum DLQ length before oldest entries are trimmed
                (from env STREAM_DLQ_MAXLEN or default: 10000). GDPR
                retention-period control for the dead letter queue, which
                is otherwise an unbounded append-only log.
        """
        self.redis_url = redis_url
        self.stream_prefix = stream_prefix
        self.dlq_prefix = dlq_prefix

        # Configuration from environment with defaults
        self.enabled = enabled if enabled is not None else self._get_env_bool('STREAM_PIPELINE_ENABLED', False)
        self.max_retries = max_retries if max_retries is not None else int(os.getenv('STREAM_MAX_RETRIES', '3'))
        self.batch_size = batch_size if batch_size is not None else int(os.getenv('STREAM_BATCH_SIZE', '10'))
        self.block_ms = block_ms if block_ms is not None else int(os.getenv('STREAM_BLOCK_MS', '5000'))
        self.dlq_maxlen = dlq_maxlen if dlq_maxlen is not None else int(
            os.getenv('STREAM_DLQ_MAXLEN', str(self.DEFAULT_DLQ_MAXLEN))
        )

        self._redis: Optional[redis.Redis] = None
        self._connected = False
        self._running = False

        logger.info(
            f"StreamPipeline initialized: enabled={self.enabled}, "
            f"max_retries={self.max_retries}, batch_size={self.batch_size}, "
            f"block_ms={self.block_ms}, dlq_maxlen={self.dlq_maxlen}"
        )

    @staticmethod
    def _get_env_bool(key: str, default: bool = False) -> bool:
        """Get boolean value from environment variable"""
        value = os.getenv(key, '').lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        elif value in ('false', '0', 'no', 'off'):
            return False
        return default

    async def connect(self):
        """Connect to Redis (call during startup)"""
        if not self.enabled:
            logger.info("Stream pipeline is disabled")
            return

        if not REDIS_AVAILABLE:
            logger.error("redis package not available - cannot enable stream pipeline")
            self.enabled = False
            return

        if not self.redis_url:
            logger.error("No Redis URL provided for stream pipeline")
            self.enabled = False
            return

        try:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )

            # Test connection
            await self._redis.ping()
            self._connected = True
            logger.info(f"Connected to Redis stream pipeline: {self.stream_prefix}")

        except Exception as e:
            logger.error(f"Failed to connect to Redis stream pipeline: {e}")
            self.enabled = False
            raise

    async def disconnect(self):
        """Disconnect from Redis"""
        self._running = False

        if self._redis:
            await self._redis.close()
            self._connected = False
            logger.info("Disconnected from Redis stream pipeline")

    def _make_stream_name(self, stream: str) -> str:
        """Create full stream name with prefix"""
        return f"{self.stream_prefix}:{stream}"

    def _make_dlq_name(self, stream: str) -> str:
        """Create dead letter queue name for a stream"""
        # Extract just the stream name without prefix
        stream_name = stream.replace(f"{self.stream_prefix}:", "")
        return f"{self.dlq_prefix}:{stream_name}"

    async def publish_event(
        self,
        stream_name: str,
        event_data: Dict[str, Any],
        max_len: int = 10000
    ) -> Optional[str]:
        """
        Publish event to a stream.

        Args:
            stream_name: Stream name (e.g., 'events:inbound', 'events:process')
            event_data: Event data (must be JSON-serializable)
            max_len: Maximum stream length (oldest events trimmed)

        Returns:
            Message ID or None on error

        Example:
            message_id = await pipeline.publish_event(
                'events:process',
                {'type': 'translate', 'text': 'Hello'}
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return None

        full_stream_name = self._make_stream_name(stream_name)

        try:
            # Serialize event data with timestamp
            serialized = {
                'data': json.dumps(event_data),
                'timestamp': datetime.utcnow().isoformat(),
                'retry_count': '0'
            }

            # Add to stream with automatic trimming
            message_id = await self._redis.xadd(
                full_stream_name,
                serialized,
                maxlen=max_len,
                approximate=True  # Allow approximate trimming for performance
            )

            logger.debug(f"Published event to {stream_name}: {message_id}")
            return message_id

        except Exception as e:
            logger.error(f"Failed to publish event to {stream_name}: {e}")
            return None

    async def consume_events(
        self,
        stream_name: str,
        consumer_group: str,
        consumer_name: str,
        count: Optional[int] = None,
        block_ms: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Consume events from a stream (single batch).

        Args:
            stream_name: Stream name to consume from
            consumer_group: Consumer group name
            consumer_name: Unique consumer identifier
            count: Number of events to fetch (default: batch_size)
            block_ms: Block time in ms (default: self.block_ms)

        Returns:
            List of event dictionaries with 'id', 'stream', 'data', 'retry_count'

        Example:
            events = await pipeline.consume_events(
                'events:process',
                'router-group',
                'router-1'
            )
            for event in events:
                print(f"Processing: {event['data']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)
        count = count if count is not None else self.batch_size
        block_ms = block_ms if block_ms is not None else self.block_ms

        # Ensure consumer group exists
        await self.create_consumer_group(stream_name, consumer_group)

        try:
            # Read new messages
            messages = await self._redis.xreadgroup(
                consumer_group,
                consumer_name,
                {full_stream_name: '>'},
                count=count,
                block=block_ms
            )

            events = []
            if messages:
                for stream_data in messages:
                    _, message_list = stream_data

                    for message_id, message_data in message_list:
                        try:
                            # Deserialize event
                            data = json.loads(message_data.get('data', '{}'))
                            retry_count = int(message_data.get('retry_count', 0))
                            timestamp = message_data.get('timestamp')

                            events.append({
                                'id': message_id,
                                'stream': full_stream_name,
                                'data': data,
                                'retry_count': retry_count,
                                'timestamp': timestamp
                            })
                        except Exception as e:
                            logger.error(f"Failed to deserialize event {message_id}: {e}")

            return events

        except Exception as e:
            logger.error(f"Failed to consume events from {stream_name}: {e}")
            return []

    async def acknowledge_event(
        self,
        stream_name: str,
        consumer_group: str,
        message_id: str
    ) -> bool:
        """
        Acknowledge an event (marks it as processed).

        Args:
            stream_name: Stream name
            consumer_group: Consumer group name
            message_id: Message ID to acknowledge

        Returns:
            True if acknowledged successfully

        Example:
            success = await pipeline.acknowledge_event(
                'events:process',
                'router-group',
                '1234567890-0'
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            ack_count = await self._redis.xack(
                full_stream_name,
                consumer_group,
                message_id
            )

            if ack_count > 0:
                logger.debug(f"Acknowledged event {message_id} from {stream_name}")
                return True
            else:
                logger.warning(f"Failed to acknowledge event {message_id} - may already be ACKed")
                return False

        except Exception as e:
            logger.error(f"Failed to acknowledge event {message_id}: {e}")
            return False

    async def move_to_dlq(
        self,
        stream_name: str,
        message_id: str,
        error_reason: str,
        event_data: Optional[Dict[str, Any]] = None,
        retry_count: int = 0
    ) -> bool:
        """
        Move an event to the dead letter queue.

        Args:
            stream_name: Original stream name
            message_id: Original message ID
            error_reason: Reason for failure
            event_data: Event data (if available)
            retry_count: Number of retry attempts

        Returns:
            True if moved successfully

        Example:
            success = await pipeline.move_to_dlq(
                'events:process',
                '1234567890-0',
                'max_retries_exceeded',
                event_data={'type': 'translate'},
                retry_count=3
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)
        dlq_name = self._make_dlq_name(full_stream_name)

        try:
            # Prepare DLQ entry
            dlq_data = {
                'original_id': message_id,
                'original_stream': full_stream_name,
                'failure_reason': error_reason,
                'retry_count': str(retry_count),
                'timestamp': datetime.utcnow().isoformat()
            }

            # Include event data if provided
            if event_data:
                dlq_data['data'] = json.dumps(event_data)

            # Add to DLQ with a bounded length (GDPR retention control —
            # poison events are the ones most likely to sit indefinitely
            # in an unbounded log, so the DLQ must never be exempt).
            dlq_id = await self._redis.xadd(
                dlq_name,
                dlq_data,
                maxlen=self.dlq_maxlen,
                approximate=True
            )

            logger.warning(
                f"Event {message_id} moved to DLQ: {error_reason} "
                f"(retries: {retry_count})"
            )

            return dlq_id is not None

        except Exception as e:
            logger.error(f"Failed to move event to DLQ: {e}")
            return False

    async def create_consumer_group(
        self,
        stream_name: str,
        group_name: str,
        start_id: str = '0'
    ) -> bool:
        """
        Create a consumer group for a stream.

        Args:
            stream_name: Stream name
            group_name: Consumer group name
            start_id: Starting message ID ('0' = from beginning, '$' = new only)

        Returns:
            True if created or already exists

        Example:
            success = await pipeline.create_consumer_group(
                'events:process',
                'router-group'
            )
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            await self._redis.xgroup_create(
                full_stream_name,
                group_name,
                id=start_id,
                mkstream=True  # Create stream if it doesn't exist
            )
            logger.info(
                f"Created consumer group '{group_name}' for stream '{stream_name}'"
            )
            return True

        except redis.ResponseError as e:
            if 'BUSYGROUP' in str(e):
                # Group already exists
                logger.debug(
                    f"Consumer group '{group_name}' already exists for '{stream_name}'"
                )
                return True
            logger.error(f"Failed to create consumer group: {e}")
            return False

        except Exception as e:
            logger.error(f"Failed to create consumer group: {e}")
            return False

    async def get_pending_events(
        self,
        stream_name: str,
        consumer_group: str,
        consumer_name: Optional[str] = None,
        count: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get pending events (read but not acknowledged).

        Args:
            stream_name: Stream name
            consumer_group: Consumer group name
            consumer_name: Specific consumer (None = all consumers)
            count: Maximum number of pending events to return

        Returns:
            List of pending event info with message_id, consumer, idle_time, delivery_count

        Example:
            pending = await pipeline.get_pending_events(
                'events:process',
                'router-group'
            )
            for event in pending:
                print(f"Pending: {event['message_id']}, retries: {event['delivery_count']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)

        try:
            # Get pending messages
            pending = await self._redis.xpending_range(
                full_stream_name,
                consumer_group,
                '-',
                '+',
                count=count,
                consumername=consumer_name
            )

            result = []
            for pending_msg in pending:
                result.append({
                    'message_id': pending_msg['message_id'],
                    'consumer': pending_msg['consumer'],
                    'idle_time_ms': pending_msg['time_since_delivered'],
                    'delivery_count': pending_msg['times_delivered']
                })

            return result

        except Exception as e:
            logger.error(f"Failed to get pending events: {e}")
            return []

    async def get_stream_info(self, stream_name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a stream.

        Args:
            stream_name: Stream name

        Returns:
            Stream info with length, first_entry, last_entry, consumer_groups, etc.

        Example:
            info = await pipeline.get_stream_info('events:process')
            print(f"Stream length: {info['length']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return None

        full_stream_name = self._make_stream_name(stream_name)

        try:
            info = await self._redis.xinfo_stream(full_stream_name)

            # Convert to more readable format
            result = {
                'length': info.get('length', 0),
                'radix_tree_keys': info.get('radix-tree-keys', 0),
                'radix_tree_nodes': info.get('radix-tree-nodes', 0),
                'groups': info.get('groups', 0),
                'last_generated_id': info.get('last-generated-id'),
                'first_entry': info.get('first-entry'),
                'last_entry': info.get('last-entry')
            }

            return result

        except redis.ResponseError as e:
            if 'no such key' in str(e).lower():
                logger.debug(f"Stream '{stream_name}' does not exist yet")
                return {'length': 0, 'exists': False}
            logger.error(f"Failed to get stream info: {e}")
            return None

        except Exception as e:
            logger.error(f"Failed to get stream info: {e}")
            return None

    async def get_dlq_events(
        self,
        stream_name: str,
        count: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get events from dead letter queue.

        Args:
            stream_name: Original stream name
            count: Maximum number of DLQ events to return

        Returns:
            List of DLQ events with id, data, original_id, failure_reason, etc.

        Example:
            dlq_events = await pipeline.get_dlq_events('events:process')
            for event in dlq_events:
                print(f"Failed: {event['failure_reason']}")
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return []

        full_stream_name = self._make_stream_name(stream_name)
        dlq_name = self._make_dlq_name(full_stream_name)

        try:
            messages = await self._redis.xrange(dlq_name, count=count)

            result = []
            for message_id, message_data in messages:
                event = {
                    'id': message_id,
                    'original_id': message_data.get('original_id'),
                    'original_stream': message_data.get('original_stream'),
                    'failure_reason': message_data.get('failure_reason'),
                    'retry_count': int(message_data.get('retry_count', 0)),
                    'timestamp': message_data.get('timestamp')
                }

                # Include data if present
                if 'data' in message_data:
                    try:
                        event['data'] = json.loads(message_data['data'])
                    except Exception:
                        event['data'] = message_data['data']

                result.append(event)

            return result

        except Exception as e:
            logger.error(f"Failed to get DLQ events: {e}")
            return []

    async def trim_stream(
        self,
        stream_name: str,
        max_len: int = 1000,
        approximate: bool = True
    ) -> bool:
        """
        Trim a stream to a maximum length.

        Args:
            stream_name: Stream name to trim
            max_len: Maximum number of events to keep
            approximate: Allow approximate trimming (more efficient)

        Returns:
            True if trimmed successfully
        """
        if not self._connected:
            logger.error("Not connected to stream pipeline")
            return False

        full_stream_name = self._make_stream_name(stream_name)

        try:
            await self._redis.xtrim(
                full_stream_name,
                maxlen=max_len,
                approximate=approximate
            )
            logger.info(f"Trimmed stream '{stream_name}' to max {max_len} events")
            return True

        except Exception as e:
            logger.error(f"Failed to trim stream: {e}")
            return False


def create_stream_pipeline(
    redis_url: str,
    stream_prefix: str = "waddlebot:stream",
    dlq_prefix: str = "waddlebot:dlq",
    max_retries: Optional[int] = None,
    batch_size: Optional[int] = None,
    block_ms: Optional[int] = None,
    enabled: Optional[bool] = None,
    dlq_maxlen: Optional[int] = None
) -> StreamPipeline:
    """
    Factory function to create a stream pipeline.

    Args:
        redis_url: Redis connection URL
        stream_prefix: Prefix for stream names
        dlq_prefix: Prefix for DLQ names
        max_retries: Maximum retry attempts
        batch_size: Events per batch
        block_ms: Block time in ms
        enabled: Enable pipeline
        dlq_maxlen: Maximum DLQ length before trimming (default: 10000)

    Returns:
        Configured StreamPipeline instance

    Example:
        pipeline = create_stream_pipeline(
            redis_url='redis://localhost:6379',
            enabled=True
        )
        await pipeline.connect()
    """
    return StreamPipeline(
        redis_url=redis_url,
        stream_prefix=stream_prefix,
        dlq_prefix=dlq_prefix,
        max_retries=max_retries,
        batch_size=batch_size,
        block_ms=block_ms,
        enabled=enabled,
        dlq_maxlen=dlq_maxlen
    )
