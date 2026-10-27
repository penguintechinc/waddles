"""P1+P4 inbound content-moderation gate -- reputation always, enforcement opt-in.

Per docs/plans/2026-09-08-content-moderation-design.md SS2/SS4: a mandatory
stage-runner gate `runner.py::_transform_and_enqueue` calls BEFORE any
process-stage bundle's own `transform()` sees an inbound chat message --
never a bundle itself, so it can never be individually disabled by a
community's own app activation state (only the master flag below controls
it). Every match applies a reputation hit (P1, unconditional). gh-304
(P4) additionally emits enforcement (timeout+warn) onto the SAME event's
payload -- `moderation_enforce_action.py`'s own `enforce()` contract --
ONLY when the community opted the matched category INTO enforcement (not
just classification): `f"{category}_filter"` present in this community's
`enabled_categories` set (the only per-community config surface
`community_moderation_config`/`services/moderation_config.py` currently
expose -- a flat `set[str]`, so the enforcement opt-in is a second,
distinctly-suffixed string in that same set rather than a separate DB
column). The message always continues to `transform_fn` regardless of
outcome, and this function itself never raises into the runner (every
external call -- flag check, DB read, classifier, reputation write -- is
caught and logged, never fatal).

Emitting `moderation_enforcement` + `PROCESS_TARGET_APP_ID_KEY` onto
`event.payload` here mutates the SAME `PlatformEvent` object `runner.py`
passes into `transform_fn` right after this call returns (`payload` is a
plain mutable `dict`, `PlatformEvent` carries no `slots`/`frozen`
guard against it) -- the gate has no `transform_fn`-style return value of
its own to stamp the routing key onto (unlike `social_music_process.py`'s
own `PROCESS_TARGET_APP_ID_KEY` usage, which returns a fresh event). Actually
getting this stamped payload all the way to an enqueued `:action` envelope
for `waddles.community.moderation.default` depends on the specific
process-stage bundle's own `transform_fn` preserving/forwarding these
payload keys into whatever `PlatformEvent` it returns -- that per-bundle
wiring is tracked separately (gh-304, other chunks), never this module's
job to guarantee.

Master switch: `_MODERATION_FLAG_KEY`, a PostHog flag via `flask_core.
feature_flags.feature_enabled` (default OFF). OFF -> total no-op, not even
a DB read for enabled categories.

Fan-out dedupe: the SAME raw platform message is normalized independently
by every active app's own ingest-stage bundle (each app gets its own
`:ingest`/`:process`/`:action` Valkey key trio, `flask_core.stream_pipeline
.bundle_stream_key`) -- so `_transform_and_enqueue` (and this gate, called
from inside it) legitimately runs once per (bundle, message), not once per
message. Without a dedupe guard, a community running N active process
bundles would take N reputation hits for one flagged message.
`_claim_dedupe_slot` uses a short-TTL Valkey `SET NX` keyed off a hash of
the message's own content (not a bundle/app id) so only the first bundle
to observe a given raw message actually classifies/adjusts; every other
fanned-out copy of the same message sees the key already claimed and skips
straight through, unaltered, to its own `transform_fn` as normal.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from flask_core import (
    PROCESS_TARGET_APP_ID_KEY,
    BundleRuntimeError,
    get_bundle_context,
    get_bundle_dal,
)
from flask_core.feature_flags import feature_enabled
from moderation_module import Classification, ClassificationProvider, LocalOllamaClassifier

from services.moderation_config import get_enabled_categories, get_tenant_id
from services.reputation_gate_client import ReputationAdjuster, get_reputation_service

if TYPE_CHECKING:
    from flask_core import PlatformEvent
    from flask_core.bundle_runtime import BundleContext

logger = logging.getLogger(__name__)

#: `waddles.<module>.<feature>` per the repo's flag-key convention
#: (`libs/core_platform_module/features.py`'s own table). New flag,
#: default OFF -- see `flask_core.feature_flags.feature_enabled`'s own
#: `default=False` degrade-safe contract.
_MODERATION_FLAG_KEY = "waddles.community.content_moderation"

#: `PlatformEvent.event_type` values every ingest bundle uses for a chat
#: message (`discord_ingest.py`/`twitch_ingest.py`/`echo_ingest.py`'s own
#: `event_type="message"`) -- anything else (follow/sub/raid/...) is never
#: a moderation candidate.
_CHAT_EVENT_TYPES = frozenset({"message"})

#: P1 default reputation weight for a moderation match: reuse `WeightManager
#: .CommunityWeights.warn`'s existing -25.0 bucket (community rep AND, for
#: an already hub-linked platform user, global score -- `ReputationService
#: .adjust()` updates both from one call) rather than inventing a new,
#: unconfigurable weight bucket inside `reputation_module` (out of scope --
#: only `core/svc_process` is being touched here). Per-category weights are
#: an explicit later phase (design SS10 Q1); `amount_multiplier=1.0` keeps
#: this call's `score_change` exactly equal to the community's own `warn`
#: weight (customizable per-community today via `community_reputation_config
#: .warn` for premium communities -- a incidental but real benefit of
#: reusing the existing bucket instead of a gate-local constant).
_REPUTATION_HIT_EVENT_TYPE = "warn"
_REPUTATION_HIT_MULTIPLIER = 1.0

#: gh-304 (P4) enforcement opt-in convention: a community enables
#: enforcement for a category by ALSO including `f"{category}_filter"`
#: (alongside the bare category itself) in its `enabled_categories` set --
#: `community_moderation_config`/`moderation_config.py` expose no separate
#: boolean column for this today (see module docstring), so the flag rides
#: in the same flat `set[str]` the classifier itself consumes.
_ENFORCEMENT_FILTER_SUFFIX = "_filter"

#: gh-304 default timeout applied to every enforced category alike --
#: no per-category override exists yet (design SS10 Q1/Q2, open question);
#: `moderation_enforce_action.py`'s own contract treats `timeout_s <= 0` as
#: "warn only," so this constant must stay positive.
_ENFORCEMENT_TIMEOUT_S = 600

#: gh-304 default warn message -- no per-community/per-category template
#: config exists yet (same SS10 open-question set as the timeout above).
#: Deliberately generic: never includes the matched message's own text.
_ENFORCEMENT_WARN_TEXT_TEMPLATE = (
    "Your message was removed for {category}. Please keep it friendly."
)

#: `app_catalog.app_id` for the enforcement action bundle
#: (`moderation_enforce_action.py:enforce`), seeded by migration
#: `0016_moderation_enforce_app`. `PROCESS_TARGET_APP_ID_KEY` cross-app
#: routing convention: `flask_core.stream_pipeline`'s own docstring,
#: `builtin_handlers/social_music_process.py`'s existing usage.
_MODERATION_ENFORCE_APP_ID = "waddles.community.moderation.default"

_DEDUPE_TTL_SECONDS = 30

_classifier_lock = threading.Lock()
_classifier: ClassificationProvider | None = None


def _get_default_classifier() -> ClassificationProvider:
    """Lazily construct (once) the process-wide `LocalOllamaClassifier` (env-configured)."""
    global _classifier
    with _classifier_lock:
        if _classifier is None:
            _classifier = LocalOllamaClassifier()
        return _classifier


def reset_classifier_for_tests() -> None:
    """Clear the cached default classifier -- test isolation only."""
    global _classifier
    _classifier = None


def _mask_actor(actor: str | None) -> str:
    """Mask a platform display name/id for logging -- `security.md` SanitizedLogger convention."""
    if not actor:
        return "<unknown>"
    if len(actor) <= 2:
        return actor[0] + "*"
    return actor[:2] + "*" * (len(actor) - 2)


def _resolve_platform_user_id(event: PlatformEvent) -> str | None:
    """The platform-native id to charge the reputation hit against, or `None` if unresolvable.

    Prefers `payload["author_id"]` (the native platform user id, same field
    `community_reputation_process.py`/`social_welcome_process.py` use),
    falling back to `event.actor` (a display name) -- matches this
    codebase's existing convention. `None` means the gate cannot identify
    who to charge; the reputation hit is skipped (never guessed).
    """
    author_id = event.payload.get("author_id")
    if isinstance(author_id, str) and author_id:
        return author_id
    if event.actor:
        # `flask_core` ships no py.typed marker (`follow_imports = "skip"`
        # override in pyproject.toml) -- `event.actor`'s real `str | None`
        # annotation is invisible to mypy here, `cast` restores it.
        return cast(str, event.actor)
    return None


async def _claim_dedupe_slot(redis_client: Any, ctx: BundleContext, event: PlatformEvent) -> bool:
    """Atomically claim the dedupe slot for this message; `True` if this call won the race.

    Content-hash based (not a bundle/app id) so every fanned-out copy of
    the SAME raw message computes the SAME key. A `redis_client` failure
    degrades to "proceed" (`True`) -- losing the dedupe guarantee on a
    Valkey outage is preferable to silently dropping moderation coverage
    entirely.
    """
    basis = "|".join(
        [
            ctx.tenant,
            ctx.community or "",
            event.platform,
            event.actor or "",
            str(event.payload.get("channel_id", "")),
            str(event.payload.get("text", "")),
        ]
    )
    digest = hashlib.sha256(basis.encode()).hexdigest()[:32]
    key = f"waddles:mod:dedupe:{digest}"
    try:
        claimed = await redis_client.set(key, "1", nx=True, ex=_DEDUPE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - dedupe is best-effort, never fatal
        logger.warning("moderation_gate.dedupe_check_failed error=%s", exc)
        return True
    return bool(claimed)


async def run_moderation_gate(
    event: PlatformEvent,
    *,
    redis_client: Any,
    feature_enabled_fn: Callable[..., Awaitable[bool]] = feature_enabled,
    get_enabled_categories_fn: Callable[[int], Awaitable[set[str]]] | None = None,
    get_tenant_id_fn: Callable[[str], Awaitable[int | None]] | None = None,
    classifier: ClassificationProvider | None = None,
    reputation_service: ReputationAdjuster | None = None,
) -> None:
    """Classify one inbound chat message and apply a reputation hit on a match. Never raises.

    Must be called from inside the runner's own `flask_core.bundle_context()`
    block (reads `get_bundle_context()` for tenant/community -- never from
    `event.payload`, security.md Tenant Isolation). No-op, in increasing
    cost order: non-chat event -> no bundle context / no community -> flag
    OFF -> no enabled categories for this community (classifier never
    called) -> dedupe slot already claimed by a fanned-out sibling copy ->
    classifier returns no match. On a match: logs (category/confidence/
    severity/masked actor) and calls `reputation_service.adjust()`; the
    caller's `transform_fn` always still runs afterward regardless of any
    of the above.
    """
    if event.event_type not in _CHAT_EVENT_TYPES:
        return

    try:
        ctx = get_bundle_context()
    except BundleRuntimeError:
        return
    if ctx.community is None:
        return
    try:
        community_id = int(ctx.community)
    except (TypeError, ValueError):
        return

    try:
        enabled = await feature_enabled_fn(
            _MODERATION_FLAG_KEY, tenant=ctx.tenant, community=community_id, default=False
        )
    except Exception as exc:  # noqa: BLE001 - flag check must never break the pipeline
        logger.warning("moderation_gate.flag_check_failed error=%s", exc)
        return
    if not enabled:
        return

    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return

    # `get_bundle_dal()` is resolved lazily, inside each default closure --
    # never called up front -- so a caller supplying all three DB-backed
    # overrides (every unit test in `test_moderation_gate.py`) never
    # requires a DAL to be bound at all. A real "no DAL bound"
    # `BundleRuntimeError` from the REAL default path is still caught by
    # the broad `except Exception` around each call site below, same as
    # any other config-read failure.
    fetch_categories = get_enabled_categories_fn or (
        lambda cid: get_enabled_categories(get_bundle_dal(), cid)
    )
    try:
        enabled_categories = await fetch_categories(community_id)
    except Exception as exc:  # noqa: BLE001 - config read must never break the pipeline
        logger.warning("moderation_gate.config_read_failed error=%s", exc)
        return
    if not enabled_categories:
        return

    if not await _claim_dedupe_slot(redis_client, ctx, event):
        return

    fetch_tenant_id = get_tenant_id_fn or (lambda slug: get_tenant_id(get_bundle_dal(), slug))
    try:
        tenant_id = await fetch_tenant_id(ctx.tenant)
    except Exception as exc:  # noqa: BLE001 - log-correlation only, never fatal
        logger.warning("moderation_gate.tenant_id_lookup_failed error=%s", exc)
        tenant_id = None

    active_classifier = classifier or _get_default_classifier()
    try:
        match: Classification | None = await active_classifier.classify(
            text,
            enabled_categories,
            tenant_id=tenant_id if tenant_id is not None else 0,
            community_id=community_id,
        )
    except Exception as exc:  # noqa: BLE001 - classifier must never break the pipeline
        logger.warning("moderation_gate.classify_failed error=%s", exc)
        return
    if match is None:
        return

    logger.info(
        "moderation_gate.match category=%s confidence=%.3f severity=%s "
        "tenant=%s community=%s actor=%s",
        match.category,
        match.confidence,
        match.severity,
        ctx.tenant,
        community_id,
        _mask_actor(event.actor),
    )

    platform_user_id = _resolve_platform_user_id(event)
    if platform_user_id is None:
        logger.warning(
            "moderation_gate.no_platform_user_id category=%s -- reputation hit skipped",
            match.category,
        )
        return

    try:
        active_reputation_service = reputation_service or get_reputation_service()
        await active_reputation_service.adjust(
            community_id=community_id,
            user_id=None,
            event_type=_REPUTATION_HIT_EVENT_TYPE,
            platform=event.platform,
            platform_user_id=platform_user_id,
            metadata={
                "moderation_category": match.category,
                "confidence": match.confidence,
                "severity": match.severity,
            },
            reason=f"content moderation match: {match.category}",
            amount_multiplier=_REPUTATION_HIT_MULTIPLIER,
        )
    except Exception as exc:  # noqa: BLE001 - reputation write must never break the pipeline
        logger.error("moderation_gate.reputation_adjust_failed error=%s", exc)

    _emit_enforcement_if_filter_on(event, match, enabled_categories)


def _emit_enforcement_if_filter_on(
    event: PlatformEvent, match: Classification, enabled_categories: set[str]
) -> None:
    """Stamp `moderation_enforcement` + `PROCESS_TARGET_APP_ID_KEY` iff the category's filter is on.

    Enforcement opt-in convention (see module docstring): `f"{category}
    _filter"` present in this community's OWN `enabled_categories` set --
    the community must have BOTH `category` (to reach the classifier at
    all) AND `f"{category}_filter"` (to also enforce) enabled; classifying
    without the `_filter` suffix present means reputation-only, matching
    design SS2's "reputation hit is standalone; community may also enable
    timeout/warn for the category." Mutates `event.payload` in place --
    see module docstring for why this is a mutation, not a return value.
    Never raises: this runs after the reputation call inside
    `run_moderation_gate`'s own no-raise contract, and a malformed/missing
    config value here must never turn into a dispatch failure.
    """
    filter_key = f"{match.category}{_ENFORCEMENT_FILTER_SUFFIX}"
    if filter_key not in enabled_categories:
        logger.debug("moderation.enforcement_skipped reason=filter_off")
        return

    timeout_s = _ENFORCEMENT_TIMEOUT_S
    warn_text = _ENFORCEMENT_WARN_TEXT_TEMPLATE.format(category=match.category)
    event.payload["moderation_enforcement"] = {
        "category": match.category,
        "score": match.confidence,
        "timeout_s": timeout_s,
        "warn_text": warn_text,
        "action": "timeout+warn",
    }
    event.payload[PROCESS_TARGET_APP_ID_KEY] = _MODERATION_ENFORCE_APP_ID
    logger.debug(
        "moderation.enforcement_emitted category=%s timeout_s=%s",
        match.category,
        timeout_s,
    )
