"""Raid auto-shoutout decision -- should an inbound Twitch raid trigger the shoutout App?

gh #316's auto-shoutout half: `core/svc_ingest/bundles/twitch_eventsub_ingest.py`
already normalizes a Twitch EventSub `channel.raid` notification into a
`PlatformEvent` (`event.payload['user_login']`/`['user_id']` is the
RAIDING channel -- the `from_broadcaster_*` fields in Twitch's own
EventSub payload, per `core/svc_ingest/eventsub.py::build_raw_event`;
`event.payload['metadata']['viewers']` is the raid's viewer count). This
module decides, from the target community's own `shoutout_config` row
(migration `046_add_remaining_admin_tables.sql`, the same table
`hub_api/services/bot_shoutout.py` serves the admin UI from), whether that
raid should auto-trigger a shoutout -- `runner.py::_maybe_shoutout_raid`
is the only caller, enqueuing a fresh action-stage envelope for
`waddles.bot.shoutout.default` when `.emit` is `True`. Never touches the
shoutout SEND path itself (`core/svc_action/bundles/
twitch_shoutout_action.py` owns that, unchanged by this module).

Async, Redis-first, `flask_core.AsyncDAL.execute()` (`$1`/`$2`...
placeholders, dict-like `Row` results) -- same construction as
`services/community_context_store.py`/`services/command_alias_store.py`.
The community's `shoutout_config` row is cached 60s in Redis (positive AND
negative, `_HIT_PREFIX`/`_MISS_VALUE`) exactly like `command_alias_store.
resolve_alias`'s own cache shape -- this sits in the runner's hot per-raid
path, so a sustained stream of raids for the same community must not hit
Postgres more than once per TTL window. `list_only` mode's `shoutout_creators`
membership check is NOT cached (one raid per community is rare enough that
the extra round trip is cheap, and caching a boolean per-target would need
its own key space for no real benefit here).

Rules (real schema, migration 046 -- no `auto_shoutout_enabled` column
exists; `auto_shoutout_mode == 'disabled'` IS the enable/disable gate):
  - `auto_shoutout_mode == 'disabled'` OR `trigger_raid_host` is `False`
    -> never emit (the config default is a genuinely disabled state, not
    an oversight -- a community must opt in).
  - `'list_only'` -> emit only if the raider's login is an active row in
    `shoutout_creators` for this community (`platform` + case-insensitive
    `platform_username` match).
  - `'all_creators'` -> always emit.
  - `'role_based'` -> treated as `'all_creators'` FOR NOW -- a raid event
    carries no badge/role data (same documented gap `bundles/
    social_shoutout_process.py`'s own module docstring calls out for
    `vip`/`subscriber`), so there is nothing to evaluate a role against
    yet. Revisit once role data reaches the pipeline.
  - No minimum-viewers threshold is enforced -- `shoutout_config` (migration
    046) has no such column as of this writing; add one there first if this
    becomes a real requirement.
  - `kind`: `'video'` if the config's `vso_enabled` is `True`, else `'text'`
    -- `vso_enabled` is literally "is video shoutout capability on for this
    community", the closest existing column to a clip/video flag.

Never raises -- a config/creator-list DB failure, a Redis failure, or any
other unexpected error all degrade to `emit=False` (WARN-logged, rate-
limited per `community_id`, exactly like `command_alias_store.
_warn_rate_limited`) rather than blocking or crashing the runner's poll
loop. DEBUG-logged at every decision point per Observability's "overlog at
DEBUG".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import redis.asyncio as redis_asyncio
from flask_core import PlatformEvent

from config import Config

logger = logging.getLogger(__name__)


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls."""

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


#: The only `PlatformEvent.event_type` this module ever acts on --
#: matches `core/svc_ingest/bundles/twitch_eventsub_ingest.py`'s own
#: `KNOWN_EVENT_TYPES` entry for raids.
RAID_EVENT_TYPE = "channel.raid"

#: `app_catalog.app_id` a `True` decision is enqueued to -- same constant
#: value as `bundles/social_shoutout_process.py`'s own `_SHOUTOUT_APP_ID`
#: (not imported -- that module is a process-stage bundle, this is a
#: plain service; both target the same App Bundle SDK default App).
SHOUTOUT_APP_ID = "waddles.bot.shoutout.default"

#: Auto-shoutout modes that always emit (subject only to the
#: disabled/trigger_raid_host gate above) -- `role_based` is folded in
#: here for now, see module docstring.
_ALWAYS_EMIT_MODES = frozenset({"all_creators", "role_based"})

_DISABLED_MODE = "disabled"
_LIST_ONLY_MODE = "list_only"

#: Config-row cache TTL -- matches `command_alias_store._POSITIVE_TTL_S`'s
#: order of magnitude but shorter, since a `shoutout_config` change (e.g.
#: an operator flipping `auto_shoutout_mode` mid-stream) should take effect
#: quickly and this module has no `invalidate_*` write-side counterpart to
#: bust the cache early.
_CONFIG_CACHE_TTL_S = 60

#: Cache-value prefix marking a positive hit (JSON-encoded config follows);
#: mirrors `command_alias_store._HIT_PREFIX`/`_MISS_VALUE`.
_HIT_PREFIX = "1:"
_MISS_VALUE = "0"

_SHOUTOUT_CONFIG_SQL = (
    "SELECT auto_shoutout_mode, trigger_raid_host, vso_enabled "
    "FROM shoutout_config WHERE community_id = $1 LIMIT 1"
)

_LIST_MEMBERSHIP_SQL = (
    "SELECT 1 FROM shoutout_creators "
    "WHERE community_id = $1 AND platform = $2 AND LOWER(platform_username) = LOWER($3) "
    "LIMIT 1"
)

_redis_lock = threading.Lock()
_redis_client: Any | None = None


def _get_redis_client() -> Any:
    """Lazily construct (once, process-wide) the Valkey client this module reads/writes.

    Same construction/rationale as `services/community_context_store.py`'s
    own `_get_redis_client` -- a second connection is intentional, this
    module has no side channel to receive the runner's own client.
    """
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = redis_asyncio.from_url(
                Config.VALKEY_URL, encoding="utf-8", decode_responses=True
            )
        return _redis_client


def reset_redis_client_for_tests() -> None:
    """Clear the cached singleton Redis client. Test isolation only."""
    global _redis_client
    _redis_client = None


def _resolve_dal(dal: _ExecutableDal | None) -> _ExecutableDal:
    """Return `dal` if given, else the process-wide DAL bound via `flask_core.set_bundle_dal()`."""
    if dal is not None:
        return dal
    from flask_core import get_bundle_dal

    # `flask_core` ships no py.typed marker (`follow_imports = "skip"` in
    # pyproject.toml) -- see `community_context_store._resolve_dal`'s
    # identical cast for the same boundary.
    return cast("_ExecutableDal", get_bundle_dal())


def _resolve_redis(redis_client: Any | None) -> Any:
    """Return `redis_client` if given, else this module's own lazily-built singleton."""
    if redis_client is not None:
        return redis_client
    return _get_redis_client()


_warn_lock = threading.Lock()
_last_warned_at: dict[int, float] = {}
_WARN_INTERVAL_S = 30.0


def _warn_rate_limited(community_id: int, exc: Exception) -> None:
    """Log a lookup failure -- WARN at most once per interval per `community_id`.

    Mirrors `command_alias_store._warn_rate_limited`/`community_resolver.
    _warn_rate_limited` -- a sustained DB/Redis outage must not flood the
    WARN log once per raid.
    """
    now = time.monotonic()
    should_warn = False
    with _warn_lock:
        last = _last_warned_at.get(community_id)
        if last is None or (now - last) >= _WARN_INTERVAL_S:
            _last_warned_at[community_id] = now
            should_warn = True
    if should_warn:
        logger.warning("raid_shoutout.lookup_failed community_id=%s error=%s", community_id, exc)
    else:
        logger.debug("raid_shoutout.lookup_failed community_id=%s error=%r", community_id, exc)


def reset_warn_rate_limit_for_tests() -> None:
    """Clear the per-`community_id` last-WARN-at tracker. Test isolation only."""
    with _warn_lock:
        _last_warned_at.clear()


def _config_cache_key(community_id: int) -> str:
    """Build the Redis key for one community's cached `shoutout_config` row."""
    return f"raidso:cfg:{community_id}"


@dataclass(slots=True, frozen=True)
class ShoutoutDecision:
    """The outcome of `maybe_auto_shoutout` -- whether/how to shout out a raider.

    `kind`/`target` are populated whenever a `target` could be extracted
    from the event, even on a `emit=False` decision (useful for logging);
    only `emit=True` decisions are ever acted on by the caller.
    """

    emit: bool
    kind: str | None
    target: str | None
    reason: str


@dataclass(slots=True, frozen=True)
class _RaidShoutoutConfig:
    """The subset of one `shoutout_config` row this module's decision needs."""

    auto_shoutout_mode: str
    trigger_raid_host: bool
    vso_enabled: bool


#: Used for both a missing `shoutout_config` row and any lookup failure --
#: identical to the column defaults migration 046 itself ships
#: (`auto_shoutout_mode` defaults to `'disabled'`), so an unprovisioned
#: community behaves exactly like an explicitly-disabled one.
_DEFAULT_CONFIG = _RaidShoutoutConfig(
    auto_shoutout_mode=_DISABLED_MODE, trigger_raid_host=True, vso_enabled=True
)


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)`, or `None` if absent/unparseable."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


def _extract_raider_login(event: PlatformEvent) -> str | None:
    """The raiding channel's login -- `payload['user_login']` first, per module docstring.

    Falls back to `payload['user_id']` then `event.actor` (all three are
    populated from the same `from_broadcaster_*` EventSub fields by
    `eventsub.py::build_raw_event`/`twitch_eventsub_ingest.py::normalize`).
    Normalized (leading `@` stripped, lowercased) -- same convention
    `bundles/social_shoutout_process.py::_normalize_login` uses for the
    manual `!so`/`!vso` path.
    """
    raw = event.payload.get("user_login") or event.payload.get("user_id") or event.actor
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lstrip("@").lower()


async def _load_config(
    community_id: int, *, dal: _ExecutableDal, redis_client: Any
) -> _RaidShoutoutConfig:
    """Redis-first `shoutout_config` read; caches both a hit and a miss for `_CONFIG_CACHE_TTL_S`.

    TTL applies to both outcomes -- see module docstring.
    """
    key = _config_cache_key(community_id)
    cached = await redis_client.get(key)
    if cached is not None:
        if cached == _MISS_VALUE:
            return _DEFAULT_CONFIG
        fields = json.loads(cached[len(_HIT_PREFIX) :])  # noqa: E203 - ruff-format's slice spacing
        return _RaidShoutoutConfig(**fields)

    rows = await dal.execute(_SHOUTOUT_CONFIG_SQL, [community_id])
    if not rows:
        await redis_client.set(key, _MISS_VALUE, ex=_CONFIG_CACHE_TTL_S)
        return _DEFAULT_CONFIG

    row = rows[0]
    config = _RaidShoutoutConfig(
        auto_shoutout_mode=str(row["auto_shoutout_mode"] or _DISABLED_MODE),
        trigger_raid_host=bool(row["trigger_raid_host"]),
        vso_enabled=bool(row["vso_enabled"]),
    )
    payload = json.dumps(
        {
            "auto_shoutout_mode": config.auto_shoutout_mode,
            "trigger_raid_host": config.trigger_raid_host,
            "vso_enabled": config.vso_enabled,
        }
    )
    await redis_client.set(key, f"{_HIT_PREFIX}{payload}", ex=_CONFIG_CACHE_TTL_S)
    return config


async def _is_listed_creator(
    community_id: int, *, platform: str, target_login: str, dal: _ExecutableDal
) -> bool:
    """`True` if `target_login` is an active `shoutout_creators` row for this community."""
    rows = await dal.execute(_LIST_MEMBERSHIP_SQL, [community_id, platform, target_login])
    return bool(rows)


async def maybe_auto_shoutout(
    event: PlatformEvent,
    *,
    community: str | None,
    dal: _ExecutableDal | None = None,
    redis_client: Any | None = None,
) -> ShoutoutDecision:
    """Decide whether an inbound `channel.raid` event should auto-trigger a shoutout.

    Args:
        event: The normalized raid `PlatformEvent`. Any `event_type` other
            than `RAID_EVENT_TYPE` short-circuits to `emit=False` before
            any DB/Redis call.
        community: The pipeline's resolved community id (a numeric string)
            for this event -- `core/svc_process/runner.py`'s own
            `community_for_context`. `None` (no resolved community) also
            short-circuits before any DB/Redis call.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.
        redis_client: Test-only override; defaults to this module's own
            singleton client.

    Returns:
        A `ShoutoutDecision`. Never raises -- see module docstring.
    """
    if event.event_type != RAID_EVENT_TYPE:
        return ShoutoutDecision(emit=False, kind=None, target=None, reason="not_a_raid_event")

    community_id = _community_id(community)
    if community_id is None:
        logger.debug("raid_shoutout.no_community")
        return ShoutoutDecision(emit=False, kind=None, target=None, reason="no_community")

    target = _extract_raider_login(event)
    if target is None:
        logger.debug("raid_shoutout.no_target community_id=%s", community_id)
        return ShoutoutDecision(emit=False, kind=None, target=None, reason="no_target")

    try:
        active_dal = _resolve_dal(dal)
        cache = _resolve_redis(redis_client)
        config = await _load_config(community_id, dal=active_dal, redis_client=cache)

        if config.auto_shoutout_mode == _DISABLED_MODE or not config.trigger_raid_host:
            logger.debug(
                "raid_shoutout.suppressed community_id=%s mode=%s trigger_raid_host=%s",
                community_id,
                config.auto_shoutout_mode,
                config.trigger_raid_host,
            )
            return ShoutoutDecision(emit=False, kind=None, target=target, reason="disabled")

        if config.auto_shoutout_mode == _LIST_ONLY_MODE:
            listed = await _is_listed_creator(
                community_id, platform=event.platform, target_login=target, dal=active_dal
            )
            if not listed:
                logger.debug(
                    "raid_shoutout.not_in_list community_id=%s target=%s", community_id, target
                )
                return ShoutoutDecision(emit=False, kind=None, target=target, reason="not_in_list")
        elif config.auto_shoutout_mode not in _ALWAYS_EMIT_MODES:
            logger.debug(
                "raid_shoutout.unknown_mode community_id=%s mode=%s",
                community_id,
                config.auto_shoutout_mode,
            )
            return ShoutoutDecision(emit=False, kind=None, target=target, reason="unknown_mode")

        kind = "video" if config.vso_enabled else "text"
        logger.debug(
            "raid_shoutout.emit community_id=%s target=%s kind=%s mode=%s",
            community_id,
            target,
            kind,
            config.auto_shoutout_mode,
        )
        return ShoutoutDecision(emit=True, kind=kind, target=target, reason="ok")
    except Exception as exc:  # noqa: BLE001 -- must never break the runner's poll loop, see docstring
        _warn_rate_limited(community_id, exc)
        return ShoutoutDecision(emit=False, kind=None, target=target, reason="lookup_failed")
