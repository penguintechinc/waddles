"""svc-process's real poll -> pull -> transform -> enqueue loop.

Mirrors `core/svc_ingest/runner.py`'s shape exactly, one stage over: RPOPs
each active bundle's `:process` Valkey key, runs the bundle's real
`transform()` entrypoint, and LPUSHes the result onto that bundle's
`:action` key as a JSON `StageEnvelope` -- the task's explicit requirement
("enqueue to `waddles:t:{tenant}:c:{community}:app:{app_id}:action`").
Separated from `app.py` for direct unit-testability, same rationale as
svc-ingest's own `runner.py`.

Wire contract (frozen, `flask_core.stream_pipeline`): the `:process` key
carries `json.dumps(StageEnvelope.to_dict())` strings; this runner reads
one with `StageEnvelope.from_dict(json.loads(raw))`, hands the carried
`PlatformEvent` to the bundle's `transform(event) -> PlatformEvent | None`
entrypoint, and writes the result back as a new `StageEnvelope` (`stage=
"action"`) onto the `:action` key the same way. Malformed input raises
`EnvelopeError` (a `ValueError` subclass) from `from_dict` -- caught here
per-event so one bad message never kills the poll loop.

A transform may return `None` to mean "no reply" -- e.g. a chat bot bundle
that only responds to commands/keywords and must not echo every message
back to the channel. `None` is logged (`process.no_reply`) and the event is
simply dropped -- nothing is enqueued to the `:action` key for it.

Every `transform_fn` call is wrapped in `flask_core.bundle_context()`
(tenant/community/app_id from the envelope just popped) -- `transform`'s
own frozen signature carries only the bare `PlatformEvent`, not the
envelope, so a stateful bundle reaches its tenant/community scope via
`flask_core.get_bundle_context()` from inside its own body instead (see
docs/APP_BUNDLE_AUTHORING.md, 'Accessing the database / shared state').

Cross-app routing (gh #298, `flask_core.PROCESS_TARGET_APP_ID_KEY`): a
transform's returned event may carry a reserved payload key requesting its
result be enqueued onto a DIFFERENT app's `:action` key than the
originating bundle's own (e.g. `bot_process` delegating `!forum` to the
community-forums feature bundle, whose action handler actually persists
the post). This runner pops that key back out of the payload -- it never
leaks into the enqueued event's real data -- and, when present, computes
the destination `:action` key from `target_app_id` instead of `bundle.
app_id`. `tenant`/`community` are unaffected either way: they still come
solely from `envelope_in` (itself sourced from `get_bundle_context()`
upstream), never from event payload -- `target_app_id` changes the
destination QUEUE KEY only, not the tenancy scope.

Board-demo live activity feed: after a successful (non-raising) transform,
`_emit_activity()` writes one best-effort `live_activity_events` row (inbound
message + the bot's reply, or `None` for no-reply) via `services.
activity_feed.record_activity`, so the live WebUI feed can show it. This is
pure telemetry, never load-bearing -- any failure (no DAL bound, DB error,
bad data) is caught broadly and logged; the pipeline still enqueues the
reply (if any) to the `:action` key and returns normally either way.

Content-moderation gate (P1, docs/plans/2026-09-08-content-moderation-
design.md): `services.moderation_gate.run_moderation_gate` runs inside the
same `bundle_context()` block, BEFORE `transform_fn` -- a mandatory gate,
not a bundle, so no community can individually opt out short of the
master PostHog flag. P1 is observe-safe: on a classifier match it logs and
applies a reputation hit (`core/reputation_module`'s already-fixed gh #299
`ReputationService.adjust()`), never blocks or alters the message -- every
one of its own failure modes (flag check, DB read, classifier, reputation
write) is caught internally and never propagates here, so it can never be
the reason a message fails to reach `transform_fn`.

Community resolution (gh #311): a tenant-wide (`community=None`) envelope
no longer maps unconditionally to `Config.DEMO_ACTIVITY_COMMUNITY_ID` --
`services.community_resolver.resolve_community` is consulted first (per-
user override -> channel primary -> demo shim -> none), controlled by
`Config.COMMUNITY_RESOLUTION_ENABLED` (default on; `false` restores the
prior unconditional shim with no lookup at all). An envelope that already
carries a real community is never looked up. Landing on the demo-shim
source logs one WARN per process lifetime (`_warn_demo_shim_once`), not
per event.

Moderation enforcement routing (gh-304 P4, final wiring step): right after
`run_moderation_gate` runs (still inside the same `bundle_context()`
block), `_maybe_route_moderation_enforcement` checks `event_in.payload`
for the gate's own `moderation_enforcement` stamp (mutated in place by
`services.moderation_gate._emit_enforcement_if_filter_on`) and, if
present, LPUSHes a SEPARATE action-stage envelope directly onto
`waddles.community.moderation.default`'s own `:action` key -- reading the
stamp off `event_in` rather than depending on whatever event `transform_fn`
happens to return (most bundles build a fresh outgoing event, silently
dropping any stamp that only rode on the inbound one). The synthetic
event's payload carries the enforcement dict plus a fixed set of identity
fields (never the original message text); normal processing of the
inbound event is otherwise unaffected. Feature-gated (`waddles.moderation.
enforce`, default ON); never raises into `_transform_and_enqueue`.

Ordinary-activity reputation accrual (gh #310): after a successful
(non-raising) transform of an INBOUND `event_type == "message"` event,
`services.activity_accrual.record_activity` (imported here as
`accrue_activity` -- `services.activity_feed.record_activity`, the board-
demo telemetry writer above, already owns the bare name) is awaited
best-effort to credit the acting user's community reputation --
`command_usage` for a bot-prefixed (`!...`) message, `chat_message`
otherwise. Runs whether or not `transform_fn` produced a reply: ordinary
chatter with no bot reply is exactly the activity this hook credits.
Skipped (never guessed) when the resolved community or the platform user
id can't be determined, and for any non-"message" `event_type` (Twitch
EventSub follow/subscribe/raid and similar system events are out of scope
for this positive-accrual path). Never raises into the caller.

Activation gate (P4, live-dispatch activation unification): right after
`community_for_context` is resolved and BEFORE `bundle_context()`/
`transform_fn` run, `services.activation_gate.is_app_activated` checks this
envelope's resolved community against `app_activations` for `bundle.
app_id` -- the webui's activation toggle (gh #586) was already the real
on/off switch for the LOADING side (`hub_api`'s distribution endpoint
already filters a community-scoped poll by `app_activations.enabled`); this
is the missing per-EVENT equivalent, so disabling a bundle now stops live
dispatch immediately rather than waiting on the next poll. Fails open (never
blocks) for a tenant-wide envelope, an app_id with no `app_activations` row
at all (most bundles today, including `bot_process` itself -- see that
module's own docstring and `services/activation_gate.py`'s rationale), or a
DB failure; only an EXISTING row with `enabled=False` actually blocks.
Not-activated is a graceful skip (DEBUG log, `return 0`), never an error.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from flask_core import (
    PROCESS_TARGET_APP_ID_KEY,
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    get_bundle_dal,
)
from flask_core.feature_flags import feature_enabled
from flask_core.stage_runner import (
    BundleDistribution,
    BundlePoller,
    EntrypointLoadError,
    load_entrypoint,
)
from flask_core.stream_pipeline import bundle_stream_key

from config import Config
from services.activation_gate import is_app_activated
from services.activity_accrual import ActivityAccrualResult
from services.activity_accrual import record_activity as accrue_activity
from services.activity_feed import record_activity
from services.community_resolver import resolve_community
from services.live_status import LIVE_STATUS_EVENT_TYPES, record_live_event
from services.moderation_gate import run_moderation_gate
from services.raid_shoutout import RAID_EVENT_TYPE, SHOUTOUT_APP_ID, maybe_auto_shoutout

logger = logging.getLogger(__name__)

#: PostHog flag gating the raid auto-shoutout hook entirely (gh #316) --
#: same flag key `builtin_handlers/social_shoutout_process.py`'s own `_FEATURE_FLAG`
#: uses for the manual `!so`/`!vso` path (not imported -- that module is a
#: process-stage bundle, this is the runner itself). Default ON: disabling
#: this flag skips the hook (and its DB/Redis round trip) entirely, whereas
#: a community's own `shoutout_config.auto_shoutout_mode == 'disabled'`
#: (checked inside `maybe_auto_shoutout`) only disables that one community.
_RAID_SHOUTOUT_FEATURE_FLAG = "waddles.bot.shoutout"

#: PostHog flag gating the live ON/OFF status hook (gh #287 S10) --
#: default ON, same rationale as `_RAID_SHOUTOUT_FEATURE_FLAG`: OFF skips
#: the hook (and its `coordination` DB write) entirely, before
#: `services.live_status.record_live_event` is even called.
_LIVE_STATUS_FEATURE_FLAG = "waddles.streaming.live_status"

#: gh-304 P4 wiring: PostHog flag gating THIS runner-level routing hook (get
#: an already-stamped `moderation_enforcement` payload onto its own action
#: envelope) -- independent of `services.moderation_gate`'s own
#: `_MODERATION_FLAG_KEY` master switch (that flag gates whether the STAMP
#: is ever produced at all; this one gates whether an already-produced
#: stamp is ACTED on). Default ON, mirroring `_RAID_SHOUTOUT_FEATURE_FLAG`'s
#: own default-on convention for a runner-level hook layered on top of an
#: upstream opt-in.
_MODERATION_ENFORCE_FEATURE_FLAG = "waddles.moderation.enforce"

#: `app_catalog.app_id` this hook routes a stamped enforcement event to --
#: MUST stay in sync with `services.moderation_gate._MODERATION_ENFORCE_
#: APP_ID` (that module's own identical, module-private constant;
#: duplicated here rather than imported since it is genuinely private
#: there and this task's scope is `runner.py`/`test_runner.py` only).
_MODERATION_ENFORCE_APP_ID = "waddles.community.moderation.default"

#: Identity payload keys copied verbatim from the inbound event onto the
#: synthetic enforcement envelope's own event payload -- exactly the field
#: names `core/svc_action/builtin_handlers/moderation_enforce_action.py` reads to
#: resolve its target user/channel/guild/broadcaster (`_resolve_target_
#: user_id`, `_enforce_discord`, `_enforce_twitch`), plus `message_id`/
#: `room_id` for audit/future use. Deliberately excludes `text` -- the
#: whole point of this synthetic envelope is NOT re-transmitting the
#: original message body to the enforcement action.
_ENFORCEMENT_IDENTITY_PAYLOAD_KEYS: tuple[str, ...] = (
    "author_id",
    "user_id",
    "channel_id",
    "guild_id",
    "channel_name",
    "broadcaster_id",
    "room_id",
    "message_id",
)

_DEMO_SHIM_WARNED = False


def _warn_demo_shim_once(community_id: int | None) -> None:
    """WARN, once per process (not per event), that resolution fell through to the demo shim.

    `resolve_community` itself only WARNs on a genuine lookup FAILURE
    (rate-limited per `(platform, entity)`); landing on `source=
    "demo_shim"` is not a failure, it's this runner's own alpha-only
    fallback -- worth one visible WARN per process lifetime so an operator
    notices the shim is still load-bearing, without spamming one line per
    tenant-wide message.
    """
    global _DEMO_SHIM_WARNED
    if not _DEMO_SHIM_WARNED:
        _DEMO_SHIM_WARNED = True
        logger.warning("runner.demo_shim_in_use community=%s", community_id)


def reset_demo_shim_warned_for_tests() -> None:
    """Clear the once-per-process demo-shim WARN latch. Test isolation only."""
    global _DEMO_SHIM_WARNED
    _DEMO_SHIM_WARNED = False


def _resolve_platform_user_id(event: PlatformEvent) -> str | None:
    """The platform-native user id for community resolution/activity accrual, or `None`.

    Same convention `services/moderation_gate.py::_resolve_platform_user_id`
    already uses (not imported -- that helper is private to its own
    module): prefers `payload['author_id']`, falls back to `event.actor`
    (a display name), `None` only when neither is available.
    """
    author_id = event.payload.get("author_id")
    if isinstance(author_id, str) and author_id:
        return author_id
    if event.actor:
        # `flask_core` ships no py.typed marker (`follow_imports = "skip"`
        # override in pyproject.toml) -- `event.actor`'s real `str | None`
        # annotation is invisible to mypy here, `cast` restores it. Same
        # boundary `services/moderation_gate.py::_resolve_platform_user_id`
        # already crosses identically.
        return cast(str, event.actor)
    return None


def _resolve_platform_entity_id(event: PlatformEvent) -> str | None:
    """The platform-native channel/server id for community resolution, or `None`.

    Same `channel_id or channel_name` normalization `_emit_activity()`
    (below) and `builtin_handlers/community_context_process.py` already use, to
    reconcile Discord's `channel_id` against Twitch's `channel_name` into
    one platform-entity identifier.
    """
    raw = event.payload.get("channel_id") or event.payload.get("channel_name")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _community_id_or_none(community: str | None) -> int | None:
    """Best-effort `int(community)` for `feature_enabled()`'s own `int | None` param, or `None`.

    Same conversion `builtin_handlers/social_shoutout_process.py::_community_id`
    performs locally for the identical `feature_enabled()` call shape --
    replicated here rather than imported (that module is a process-stage
    bundle, this is the runner itself).
    """
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


class ProcessRunner:
    """One poll+drain cycle per call to `run_once()`; `run_forever()` loops it in production."""

    def __init__(self, *, poller: BundlePoller, redis_client: Any, tenant_slug: str) -> None:
        """Build a runner bound to one `BundlePoller`, one Valkey client, and one tenant scope."""
        self._poller = poller
        self._redis = redis_client
        self._tenant_slug = tenant_slug
        self._running = False

    def stop(self) -> None:
        """Signal `run_forever()` to exit after its current iteration."""
        self._running = False

    async def run_forever(self) -> None:
        """Production loop: poll, drain every active bundle's process queue, sleep, repeat."""
        self._running = True
        while self._running:
            await self.run_once()
            import asyncio

            await asyncio.sleep(self._poller.next_delay_s)

    async def run_once(self) -> int:
        """One poll+drain cycle; returns total events transformed+enqueued. Never raises."""
        bundles = await self._poller.poll_once()
        total = 0
        for bundle in bundles:
            total += await self._process_bundle(bundle)
        return total

    async def _process_bundle(self, bundle: BundleDistribution) -> int:
        if bundle.entrypoint is None:
            logger.info("process.no_entrypoint app_id=%s -- skipping", bundle.app_id)
            return 0

        try:
            transform_fn = load_entrypoint(bundle.entrypoint)
        except EntrypointLoadError as exc:
            logger.error(
                "process.entrypoint_load_failed app_id=%s entrypoint=%s error=%s",
                bundle.app_id,
                bundle.entrypoint,
                exc,
            )
            return 0

        community_str: str | None = (
            str(bundle.community_id) if bundle.community_id is not None else None
        )
        process_key = bundle_stream_key(self._tenant_slug, community_str, bundle.app_id, "process")
        action_key = bundle_stream_key(self._tenant_slug, community_str, bundle.app_id, "action")

        count = 0
        while True:
            raw = await self._redis.rpop(process_key)
            if raw is None:
                break
            count += await self._transform_and_enqueue(
                raw,
                transform_fn,
                bundle=bundle,
                action_key=action_key,
                community_str=community_str,
            )
        return count

    async def _transform_and_enqueue(
        self,
        raw: Any,
        transform_fn: Callable[..., Awaitable[Any]],
        *,
        bundle: BundleDistribution,
        action_key: str,
        community_str: str | None,
    ) -> int:
        """Parse the incoming `StageEnvelope`, transform its event, LPUSH the result envelope."""
        try:
            envelope_in = StageEnvelope.from_dict(json.loads(raw))
        except (TypeError, ValueError) as exc:
            # ValueError also covers EnvelopeError (StageEnvelope.from_dict's
            # own error type is a ValueError subclass) and json.JSONDecodeError.
            logger.error("process.bad_envelope app_id=%s error=%s", bundle.app_id, exc)
            return 0

        event_in: PlatformEvent = envelope_in.event

        # Community resolution (gh #311): the pipeline runs tenant-wide
        # (`community=None`) today, but the command-router feature bundles
        # (`bot_process` -> social_quote/social_alias/community_polls/
        # community_announcements/community_forums) reject state-changing
        # ops without a community scope. An envelope that already carries a
        # real community is never overridden -- resolution only runs for a
        # tenant-wide envelope, via `resolve_community`'s per-user ->
        # per-channel -> demo-shim order. `Config.
        # COMMUNITY_RESOLUTION_ENABLED=false` restores the prior
        # unconditional demo-shim mapping with no lookup at all (an
        # operational escape hatch, not expected in normal operation).
        community_for_context: str | None
        if envelope_in.community is not None:
            community_for_context = envelope_in.community
        elif Config.COMMUNITY_RESOLUTION_ENABLED:
            resolved = await resolve_community(
                platform=event_in.platform,
                platform_user_id=_resolve_platform_user_id(event_in),
                platform_entity_id=_resolve_platform_entity_id(event_in),
                demo_default=Config.DEMO_ACTIVITY_COMMUNITY_ID,
            )
            logger.debug(
                "runner.community_resolved source=%s community=%s",
                resolved.source,
                resolved.community_id,
            )
            if resolved.source == "demo_shim":
                _warn_demo_shim_once(resolved.community_id)
            community_for_context = (
                str(resolved.community_id) if resolved.community_id is not None else None
            )
        else:
            community_for_context = str(Config.DEMO_ACTIVITY_COMMUNITY_ID)

        # Activation gate (P4, live-dispatch activation unification): skip
        # dispatch entirely for a bundle the webui's activation toggle (#586)
        # has disabled in this envelope's resolved community -- fails open
        # (never blocks) for a tenant-wide envelope, an unonboarded app_id
        # with no `app_activations` row at all, or a DB hiccup; see
        # `services/activation_gate.py`'s own module docstring for the full
        # rationale and why that is safe for every currently-working bundle.
        # Deliberately checked BEFORE `bundle_context()`/`transform_fn` --
        # moderation gate, activity emit/accrual, and the `:action` LPUSH
        # must never run for a command this community turned off.
        if not await is_app_activated(community=community_for_context, app_id=bundle.app_id):
            logger.debug(
                "process.not_activated app_id=%s community=%s -- skipping dispatch",
                bundle.app_id,
                community_for_context,
            )
            return 0

        try:
            with bundle_context(
                tenant=envelope_in.tenant,
                community=community_for_context,
                app_id=envelope_in.app_id,
            ):
                await run_moderation_gate(event_in, redis_client=self._redis)
                await self._maybe_route_moderation_enforcement(
                    envelope_in,
                    event_in,
                    community_str=community_str,
                    community_for_context=community_for_context,
                    bundle=bundle,
                )
                event_out: PlatformEvent | None = await transform_fn(event_in)
        except Exception as exc:  # noqa: BLE001 - one bad event must never kill the loop
            logger.error("process.transform_failed app_id=%s error=%s", bundle.app_id, exc)
            return 0

        # Cross-app routing (gh #298): pull the reserved routing key back out
        # of the payload before it goes anywhere else -- it must never reach
        # the activity feed or an action-stage bundle as real event data.
        target_app_id: str | None = None
        if event_out is not None and PROCESS_TARGET_APP_ID_KEY in event_out.payload:
            raw_target = event_out.payload[PROCESS_TARGET_APP_ID_KEY]
            if isinstance(raw_target, str) and raw_target:
                target_app_id = raw_target
            event_out = dataclasses.replace(
                event_out,
                payload={
                    k: v for k, v in event_out.payload.items() if k != PROCESS_TARGET_APP_ID_KEY
                },
            )

        await self._emit_activity(envelope_in, event_in, event_out)
        await self._accrue_activity(envelope_in, event_in, community_for_context, bundle)
        await self._maybe_shoutout_raid(
            envelope_in,
            event_in,
            community_str=community_str,
            community_for_context=community_for_context,
            bundle=bundle,
        )
        await self._maybe_live_status(
            envelope_in,
            event_in,
            community_for_context=community_for_context,
            bundle=bundle,
        )

        if event_out is None:
            logger.info("process.no_reply app_id=%s", bundle.app_id)
            return 0

        envelope_out = StageEnvelope(
            tenant=envelope_in.tenant,
            # Carry the RESOLVED community (real activation, or the demo-shim
            # fallback above) onto the action-stage envelope -- not
            # envelope_in.community, which is still None for a tenant-wide
            # activation. Action bundles run outside bundle_context() (they
            # only see the envelope they're handed), so this is the only
            # channel a resolved community reaches them through. Always
            # sourced from pipeline context/the shim, never from event
            # payload -- same tenancy invariant as target_app_id above.
            community=community_for_context,
            app_id=envelope_in.app_id,
            stage="action",
            event=event_out,
            ts=datetime.now(UTC).isoformat(),
            target_app_id=target_app_id,
        )
        destination_key = (
            bundle_stream_key(self._tenant_slug, community_str, target_app_id, "action")
            if target_app_id is not None
            else action_key
        )
        await self._redis.lpush(destination_key, json.dumps(envelope_out.to_dict()))
        return 1

    async def _emit_activity(
        self,
        envelope_in: StageEnvelope,
        event_in: PlatformEvent,
        event_out: PlatformEvent | None,
    ) -> None:
        """Best-effort write of one `live_activity_events` row for the live WebUI feed.

        FAIL-SAFE (demo-critical): wraps the entire emit in `except
        Exception` -- no DAL bound (`get_bundle_dal()`'s `BundleRuntimeError`),
        a DB error, or bad data must never break the pipeline or the reply.
        On any failure this logs and returns; the caller's subsequent LPUSH
        and normal return are unaffected either way. This is pure telemetry,
        never load-bearing.
        """
        try:
            dal = get_bundle_dal()
            community_id = (
                int(envelope_in.community)
                if envelope_in.community
                else Config.DEMO_ACTIVITY_COMMUNITY_ID
            )
            await record_activity(
                dal,
                community_id=community_id,
                platform=event_in.platform,
                actor=event_in.actor,
                message_in=event_in.payload.get("text"),
                reply_out=event_out.payload.get("text") if event_out is not None else None,
                channel_id=event_in.payload.get("channel_id")
                or event_in.payload.get("channel_name"),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort telemetry, must never break the pipeline
            logger.warning(
                "process.activity_emit_failed app_id=%s error=%s", envelope_in.app_id, exc
            )

    async def _accrue_activity(
        self,
        envelope_in: StageEnvelope,
        event_in: PlatformEvent,
        community_for_context: str | None,
        bundle: BundleDistribution,
    ) -> None:
        """Best-effort ordinary-activity reputation accrual hook (gh #310).

        Fires once per successfully-transformed INBOUND platform event --
        `event_in.event_type == "message"` only (a Twitch EventSub follow/
        subscribe/raid or any other non-chat system event is out of scope
        for this positive-accrual path, same as `services.activity_accrual`'s
        own `_SUPPORTED_EVENT_TYPES`). Runs regardless of whether
        `transform_fn` produced a reply (`event_out`) -- ordinary chatter
        that gets no bot reply is exactly the activity this hook exists to
        credit; only a `command_usage` (bot-prefixed) message is expected to
        usually also enqueue a reply. Skipped, never guessed, when the
        community or the platform user id can't be resolved.

        Awaited directly, not backgrounded: no fire-and-forget/background-
        task pattern exists elsewhere in this runner for a per-event side
        call (`_emit_activity` above is the closest precedent, and it is
        awaited too) -- `record_activity()` already short-circuits cheaply
        on a claimed cooldown before it would ever reach the reputation
        service's HTTP call (`reputation_gate_client.py`'s own 5s
        `httpx.AsyncClient` timeout). Never raises into the caller --
        `record_activity()`'s own contract already never raises; this wraps
        it anyway as defense in depth (mirrors `_emit_activity`'s FAIL-SAFE
        wrapping above) so a broken test double or future refactor can't
        turn best-effort telemetry into a pipeline-breaking bug.
        """
        if event_in.event_type != "message":
            logger.debug(
                "process.activity_accrual_skipped app_id=%s reason=non_message_event_type "
                "event_type=%s",
                bundle.app_id,
                event_in.event_type,
            )
            return
        if community_for_context is None:
            logger.debug(
                "process.activity_accrual_skipped app_id=%s reason=no_community", bundle.app_id
            )
            return
        platform_user_id = _resolve_platform_user_id(event_in)
        if platform_user_id is None:
            logger.debug(
                "process.activity_accrual_skipped app_id=%s reason=no_platform_user_id",
                bundle.app_id,
            )
            return

        text = event_in.payload.get("text")
        accrual_event_type = (
            "command_usage"
            if isinstance(text, str) and text.strip().startswith("!")
            else "chat_message"
        )

        try:
            result: ActivityAccrualResult = await accrue_activity(
                tenant=envelope_in.tenant,
                community=community_for_context,
                platform=event_in.platform,
                platform_user_id=platform_user_id,
                event_type=accrual_event_type,
                # No true per-message id exists on the frozen `StageEnvelope`/
                # `PlatformEvent` contract -- `ts` is the closest available
                # correlation value; `record_activity()` only carries this
                # into `metadata['event_id']` for audit/debugging, never as
                # an idempotency key (the per-user cooldown is that guard).
                event_id=envelope_in.ts,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort accrual, must never break the pipeline
            logger.warning("process.activity_accrual_failed app_id=%s error=%s", bundle.app_id, exc)
            return

        logger.debug(
            "process.activity_accrual_result app_id=%s applied=%s event_type=%s reason=%s",
            bundle.app_id,
            result.applied,
            result.event_type,
            result.reason,
        )

    async def _maybe_route_moderation_enforcement(
        self,
        envelope_in: StageEnvelope,
        event_in: PlatformEvent,
        *,
        community_str: str | None,
        community_for_context: str | None,
        bundle: BundleDistribution,
    ) -> None:
        """gh-304 P4 wiring: route a gate-stamped enforcement onto its own action envelope.

        `services.moderation_gate._emit_enforcement_if_filter_on` stamps
        `event_in.payload["moderation_enforcement"]` IN PLACE on the same
        `PlatformEvent` this method receives (see that module's docstring)
        -- but the stamp only survives on the event a bundle's
        `transform_fn` actually RETURNS, and most bundles (`bot_process.
        transform` included) build a fresh outgoing event rather than
        echoing the inbound one back, silently dropping it. This hook
        reads the stamp directly off `event_in` right after the gate runs
        -- never the bundle's own return value -- and, on a match, LPUSHes
        a SEPARATE action-stage `StageEnvelope` straight onto the
        moderation-enforce app's own `:action` key, same direct-build-and-
        enqueue mechanism `_maybe_shoutout_raid` below already uses for
        raid auto-shoutout. Normal processing (the original event still
        flowing to `transform_fn`) is entirely unaffected either way -- a
        pure side-additive enqueue, never a replacement.

        The synthetic event's payload carries ONLY the enforcement dict
        plus the identity fields `moderation_enforce_action.py::enforce()`
        itself reads (`_ENFORCEMENT_IDENTITY_PAYLOAD_KEYS`) -- never
        `text`, so the original message body never reaches the enforcement
        action. `platform`/`actor` ride on the synthetic `PlatformEvent`'s
        own top-level fields, matching how `enforce()` reads them.

        Feature-gated (`_MODERATION_ENFORCE_FEATURE_FLAG`, default ON)
        after the cheap stamp-presence check. Never raises: the whole body
        below the presence check is wrapped, so a Valkey hiccup or a
        malformed stamp can never turn a successful classification into a
        dropped/failed transform -- the surrounding `_transform_and_
        enqueue` caller's own broad `except Exception` (which WOULD drop
        the whole message) must never see an exception from here.
        """
        enforcement = event_in.payload.get("moderation_enforcement")
        if not isinstance(enforcement, dict):
            return

        try:
            enabled = await feature_enabled(
                _MODERATION_ENFORCE_FEATURE_FLAG,
                tenant=envelope_in.tenant,
                community=_community_id_or_none(community_for_context),
                default=True,
            )
            if not enabled:
                logger.debug("process.moderation_enforce_flag_disabled app_id=%s", bundle.app_id)
                return

            enforcement_payload: dict[str, Any] = {"moderation_enforcement": enforcement}
            for key in _ENFORCEMENT_IDENTITY_PAYLOAD_KEYS:
                value = event_in.payload.get(key)
                if value is not None:
                    enforcement_payload[key] = value

            enforcement_event = PlatformEvent(
                platform=event_in.platform,
                event_type=event_in.event_type,
                actor=event_in.actor,
                payload=enforcement_payload,
                occurred_at=datetime.now(UTC).isoformat(),
            )
            envelope_out = StageEnvelope(
                tenant=envelope_in.tenant,
                community=community_for_context,
                app_id=_MODERATION_ENFORCE_APP_ID,
                stage="action",
                event=enforcement_event,
                ts=datetime.now(UTC).isoformat(),
            )
            destination_key = bundle_stream_key(
                self._tenant_slug, community_str, _MODERATION_ENFORCE_APP_ID, "action"
            )
            await self._redis.lpush(destination_key, json.dumps(envelope_out.to_dict()))
            logger.debug(
                "runner.enforcement_routed category=%s app=%s",
                enforcement.get("category"),
                _MODERATION_ENFORCE_APP_ID,
            )
        except Exception as exc:  # noqa: BLE001 - routing must never break the main path
            logger.warning(
                "process.moderation_enforce_routing_failed app_id=%s error=%s",
                bundle.app_id,
                exc,
            )

    async def _maybe_shoutout_raid(
        self,
        envelope_in: StageEnvelope,
        event_in: PlatformEvent,
        *,
        community_str: str | None,
        community_for_context: str | None,
        bundle: BundleDistribution,
    ) -> None:
        """Best-effort raid auto-shoutout hook (gh #316) -- alongside the chat-command path.

        Fires for every inbound `channel.raid` event, independent of
        `transform_fn`'s own result: `builtin_handlers.bot_process.transform()`
        (this pipeline's chat-command router) returns `None` for any event
        with no `payload['text']` key, so a raid event never reaches the
        chat-command path at all -- this hook is the ONLY consumer that
        acts on a raid. The raid event's own normal flow (activity feed,
        reputation accrual, and whatever `event_out` `transform_fn` did
        produce) is entirely unaffected either way -- this is a pure
        side-additive enqueue, never a replacement, and never reorders the
        existing moderation-gate/`transform_fn`/accrual calls above it.

        On a `True` decision (`services.raid_shoutout.maybe_auto_shoutout`),
        builds and LPUSHes a NEW action-stage `StageEnvelope` directly onto
        `SHOUTOUT_APP_ID`'s own `:action` key -- same destination-key
        mechanism (`bundle_stream_key(tenant, community_str, app_id,
        "action")`) `_transform_and_enqueue` already uses for
        `PROCESS_TARGET_APP_ID_KEY` cross-app routing above, except this
        envelope is built here directly rather than returned from
        `transform_fn` (a raid event has no chat-command reply to
        piggyback on). `envelope.community` carries the RESOLVED
        `community_for_context` (required by `twitch_shoutout_action.
        shoutout()`, which raises on a `None` community) while the
        destination KEY is computed from the bundle-level `community_str`
        -- same split `_transform_and_enqueue`'s own envelope_out/
        destination_key already use, see that method's docstring.

        Feature-gated (`_RAID_SHOUTOUT_FEATURE_FLAG`, default ON) -- OFF
        skips the hook (and its DB/Redis round trip) entirely, before
        `maybe_auto_shoutout` is even called. Never raises: both the
        decision call and the LPUSH are wrapped, so a Valkey hiccup or an
        unexpected error here can never break the rest of the pipeline.
        """
        if event_in.event_type != RAID_EVENT_TYPE:
            return

        enabled = await feature_enabled(
            _RAID_SHOUTOUT_FEATURE_FLAG,
            tenant=envelope_in.tenant,
            community=_community_id_or_none(community_for_context),
            default=True,
        )
        if not enabled:
            logger.debug("process.raid_shoutout_flag_disabled app_id=%s", bundle.app_id)
            return

        try:
            decision = await maybe_auto_shoutout(event_in, community=community_for_context)
            if not decision.emit:
                logger.debug(
                    "process.raid_shoutout_skipped app_id=%s reason=%s",
                    bundle.app_id,
                    decision.reason,
                )
                return

            shoutout_event = dataclasses.replace(
                event_in,
                payload={
                    **event_in.payload,
                    "subcommand": "shoutout",
                    "kind": decision.kind,
                    "target": decision.target,
                },
            )
            envelope_out = StageEnvelope(
                tenant=envelope_in.tenant,
                community=community_for_context,
                app_id=SHOUTOUT_APP_ID,
                stage="action",
                event=shoutout_event,
                ts=datetime.now(UTC).isoformat(),
            )
            destination_key = bundle_stream_key(
                self._tenant_slug, community_str, SHOUTOUT_APP_ID, "action"
            )
            await self._redis.lpush(destination_key, json.dumps(envelope_out.to_dict()))
            logger.info(
                "process.raid_shoutout_enqueued app_id=%s target=%s kind=%s",
                bundle.app_id,
                decision.target,
                decision.kind,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort hook, must never break the pipeline
            logger.warning("process.raid_shoutout_failed app_id=%s error=%s", bundle.app_id, exc)

    async def _maybe_live_status(
        self,
        envelope_in: StageEnvelope,
        event_in: PlatformEvent,
        *,
        community_for_context: str | None,
        bundle: BundleDistribution,
    ) -> None:
        """Best-effort live ON/OFF status hook (gh #287 S10) -- alongside the raid-shoutout hook.

        Fires for every inbound `stream.online`/`stream.offline` event,
        independent of `transform_fn`'s own result -- same "side-additive,
        never a replacement" posture as `_maybe_shoutout_raid` above.
        Delegates the actual `coordination` table upsert to
        `services.live_status.record_live_event`, which never raises on
        its own; this method's own try/except is a second, defense-in-
        depth layer (same double-wrap `_maybe_shoutout_raid` uses around
        `maybe_auto_shoutout`).

        Feature-gated (`_LIVE_STATUS_FEATURE_FLAG`, default ON) -- OFF
        skips the hook (and its DB round trip) entirely, before
        `record_live_event` is even called.
        """
        if event_in.event_type not in LIVE_STATUS_EVENT_TYPES:
            return

        enabled = await feature_enabled(
            _LIVE_STATUS_FEATURE_FLAG,
            tenant=envelope_in.tenant,
            community=_community_id_or_none(community_for_context),
            default=True,
        )
        if not enabled:
            logger.debug("process.live_status_flag_disabled app_id=%s", bundle.app_id)
            return

        try:
            result = await record_live_event(event_in, community=community_for_context)
            if not result.recorded:
                logger.debug(
                    "process.live_status_skipped app_id=%s reason=%s", bundle.app_id, result.reason
                )
        except Exception as exc:  # noqa: BLE001 - best-effort hook, must never break the pipeline
            logger.warning("process.live_status_failed app_id=%s error=%s", bundle.app_id, exc)
