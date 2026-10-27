"""Per-user community context store -- backs the `!cc` context-switch handler (gh #311).

Two independent reads this module serves:

- `list_channel_communities`: every community with an **approved**
  `community_servers` link to a given `(platform, platform_entity_id)`
  channel/server/workspace -- the set a user is allowed to switch into,
  primary-linked community first, matching `communities.name` (this schema
  has no `communities.slug` column -- the legacy `processing/router_module/
  services/context_service.py::get_available_communities` selected one
  anyway, a latent bug never hit in practice; this module selects `name`
  only).
- `get_context`/`set_context`/`clear_context`: the per-user override table
  (`user_platform_context`, migration `054_user_platform_context.sql`),
  Redis-cached with a 24h TTL exactly like the legacy `ContextService`
  it replaces -- `get_context` checks Redis first, falls back to the table
  on a miss/expiry, and re-warms Redis on a table hit so a cold cache
  self-heals on the next read instead of hammering Postgres every call.

Async throughout -- matches `services/moderation_config.py`'s DAL access
model (`flask_core.AsyncDAL.execute()`, `$1`/`$2`... placeholders, dict-like
`Row` results) and this stage runner's single-consumer poll loop
(`runner.py`, `BundleContext`'s own docstring: one envelope processed at a
time, never concurrent `asyncio.gather` fan-out).

These functions do not catch DB/Redis errors -- a genuine failure (bad
connection, FK violation on an unapproved `community_id`, etc.) propagates
to the caller. `services/community_resolver.py` is the layer responsible
for degrading gracefully on top of this store (never raises, falls through
to the next resolution source) -- mirrors this codebase's existing split
between `moderation_config.py` (raises) and `moderation_gate.py` (catches),
not a new convention.

Every public function takes an optional `dal`/`redis_client` override
(keyword-only, defaulting to `None`) purely for test injection -- the `!cc`
handler calls every function with ONLY the documented required keyword
arguments; the defaults resolve to `flask_core.get_bundle_dal()` and this
module's own lazily-constructed process-wide Redis client
(`config.Config.VALKEY_URL`, same construction `app.py` uses for the
runner's own client) respectively.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol, cast

import redis.asyncio as redis_asyncio

from config import Config

logger = logging.getLogger(__name__)


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls."""

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


#: Default TTL for both `set_context`'s own default and `get_context`'s
#: cache-rewarm-on-table-hit path -- 24h, matching the legacy
#: `ContextService.USER_CTX_TTL` this module replaces.
DEFAULT_CONTEXT_TTL_S = 86_400

_LIST_CHANNEL_COMMUNITIES_SQL = (
    "SELECT c.id AS id, c.name AS name, cs.is_primary AS is_primary "
    "FROM community_servers cs "
    "JOIN communities c ON c.id = cs.community_id "
    "WHERE cs.platform = $1 AND cs.platform_server_id = $2 AND cs.status = 'approved' "
    "ORDER BY cs.is_primary DESC, c.name ASC"
)

_GET_CONTEXT_SQL = (
    "SELECT community_id FROM user_platform_context "
    "WHERE platform = $1 AND platform_user_id = $2 AND platform_entity_id = $3 "
    "LIMIT 1"
)

_SET_CONTEXT_SQL = (
    "INSERT INTO user_platform_context "
    "(platform, platform_user_id, platform_entity_id, community_id, updated_at) "
    "VALUES ($1, $2, $3, $4, NOW()) "
    "ON CONFLICT (platform, platform_user_id, platform_entity_id) "
    "DO UPDATE SET community_id = EXCLUDED.community_id, updated_at = NOW()"
)

_CLEAR_CONTEXT_SQL = (
    "DELETE FROM user_platform_context "
    "WHERE platform = $1 AND platform_user_id = $2 AND platform_entity_id = $3"
)

_redis_lock = threading.Lock()
_redis_client: Any | None = None


def _get_redis_client() -> Any:
    """Lazily construct (once, process-wide) the Valkey client this module reads/writes.

    Same construction as `app.py`'s own runner-wide client
    (`redis.asyncio.from_url(Config.VALKEY_URL, encoding="utf-8",
    decode_responses=True)`) -- a second connection is intentional rather
    than threading the runner's existing client through: this module has no
    parameter to receive one (the `!cc` handler's frozen `transform(event)`
    entrypoint carries no side channel for it, same reason
    `flask_core.bundle_runtime` exists at all), so it owns its own client
    the same way `services/moderation_gate.py`'s `_get_default_classifier()`
    and `services/reputation_gate_client.py`'s `get_reputation_service()`
    each lazily own their own singleton.
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


def _context_cache_key(*, platform: str, platform_user_id: str, platform_entity_id: str) -> str:
    """Build the Redis key for one user's per-channel context override.

    `ctx:{platform}:{platform_entity_id}:{platform_user_id}` -- entity
    before user, per this module's own frozen interface (note this is the
    REVERSE field order from the legacy `ContextService._user_ctx_key`'s
    `ctx:{platform}:{user_id}:{entity_id}`; the two caches are not
    compatible and are not meant to be -- this module owns its own key
    space).
    """
    return f"ctx:{platform}:{platform_entity_id}:{platform_user_id}"


def _resolve_dal(dal: _ExecutableDal | None) -> _ExecutableDal:
    """Return `dal` if given, else the process-wide DAL bound via `flask_core.set_bundle_dal()`."""
    if dal is not None:
        return dal
    from flask_core import get_bundle_dal

    # `flask_core` ships no py.typed marker (`follow_imports = "skip"` override
    # in pyproject.toml) -- `get_bundle_dal()`'s real `AsyncDAL` return type is
    # invisible to mypy here, `cast` restores it (same pattern
    # `services/moderation_gate.py::_resolve_platform_user_id` uses for the
    # identical boundary).
    return cast("_ExecutableDal", get_bundle_dal())


def _resolve_redis(redis_client: Any | None) -> Any:
    """Return `redis_client` if given, else this module's own lazily-built singleton."""
    if redis_client is not None:
        return redis_client
    return _get_redis_client()


@dataclass(slots=True)
class ChannelCommunity:
    """One community with an approved `community_servers` link to a channel/server."""

    id: int
    name: str
    is_primary: bool


async def list_channel_communities(
    *,
    platform: str,
    platform_entity_id: str,
    dal: _ExecutableDal | None = None,
) -> list[ChannelCommunity]:
    """Return every APPROVED community linked to this channel/server, primary first.

    This is the security gate for context switching -- `!cc` (and
    `set_context`, indirectly, via the handler's own validation) only ever
    offers/accepts a `community_id` drawn from this list. Ordered
    `is_primary DESC, name ASC` so the channel's default community always
    sorts first, remaining choices alphabetical.

    Args:
        platform: Platform slug (e.g. `"discord"`, `"twitch"`).
        platform_entity_id: The channel/server/workspace id, matching
            `community_servers.platform_server_id`.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.

    Returns:
        Zero or more `ChannelCommunity` rows -- empty if no approved link
        exists for this channel/server at all.
    """
    active_dal = _resolve_dal(dal)
    rows = await active_dal.execute(_LIST_CHANNEL_COMMUNITIES_SQL, [platform, platform_entity_id])
    return [
        ChannelCommunity(
            id=int(row["id"]), name=str(row["name"]), is_primary=bool(row["is_primary"])
        )
        for row in rows
    ]


async def get_context(
    *,
    platform: str,
    platform_user_id: str,
    platform_entity_id: str,
    dal: _ExecutableDal | None = None,
    redis_client: Any | None = None,
) -> int | None:
    """Return this user's per-channel community override, or `None` if none/expired.

    Redis first (`_context_cache_key`); on a miss (cache never set, or the
    24h TTL expired) falls back to the `user_platform_context` table, which
    has no TTL of its own -- it is the source of truth. A table hit
    re-warms Redis (`DEFAULT_CONTEXT_TTL_S`) so the cache self-heals on the
    next read instead of hitting Postgres on every call after an expiry.
    A miss at both layers returns `None` -- the caller
    (`services/community_resolver.py`) treats that identically to "never
    set an override", falling through to the channel's primary community.

    Args:
        platform: Platform slug.
        platform_user_id: The platform-native user id.
        platform_entity_id: The channel/server/workspace id.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.
        redis_client: Test-only override; defaults to this module's own
            singleton client.

    Returns:
        The overridden `community_id`, or `None`.
    """
    cache = _resolve_redis(redis_client)
    key = _context_cache_key(
        platform=platform, platform_user_id=platform_user_id, platform_entity_id=platform_entity_id
    )
    cached = await cache.get(key)
    if cached is not None:
        return int(cached)

    active_dal = _resolve_dal(dal)
    rows = await active_dal.execute(
        _GET_CONTEXT_SQL, [platform, platform_user_id, platform_entity_id]
    )
    if not rows:
        return None

    community_id = int(rows[0]["community_id"])
    await cache.set(key, str(community_id), ex=DEFAULT_CONTEXT_TTL_S)
    return community_id


async def set_context(
    *,
    platform: str,
    platform_user_id: str,
    platform_entity_id: str,
    community_id: int,
    ttl_s: int = DEFAULT_CONTEXT_TTL_S,
    dal: _ExecutableDal | None = None,
    redis_client: Any | None = None,
) -> None:
    """Upsert this user's per-channel community override, table then cache.

    No validation against `list_channel_communities` happens here -- the
    caller (the `!cc` handler) is expected to have already confirmed
    `community_id` is one of this channel's approved links before calling;
    an invalid `community_id` still raises (FK violation on
    `user_platform_context.community_id REFERENCES communities(id)`) rather
    than failing silently.

    Args:
        platform: Platform slug.
        platform_user_id: The platform-native user id.
        platform_entity_id: The channel/server/workspace id.
        community_id: The community to switch this user's context to.
        ttl_s: Redis cache TTL in seconds; defaults to `DEFAULT_CONTEXT_TTL_S`
            (24h).
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.
        redis_client: Test-only override; defaults to this module's own
            singleton client.
    """
    active_dal = _resolve_dal(dal)
    await active_dal.execute(
        _SET_CONTEXT_SQL, [platform, platform_user_id, platform_entity_id, community_id]
    )

    cache = _resolve_redis(redis_client)
    key = _context_cache_key(
        platform=platform, platform_user_id=platform_user_id, platform_entity_id=platform_entity_id
    )
    await cache.set(key, str(community_id), ex=ttl_s)


async def clear_context(
    *,
    platform: str,
    platform_user_id: str,
    platform_entity_id: str,
    dal: _ExecutableDal | None = None,
    redis_client: Any | None = None,
) -> None:
    """Remove this user's per-channel community override, table then cache.

    Args:
        platform: Platform slug.
        platform_user_id: The platform-native user id.
        platform_entity_id: The channel/server/workspace id.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.
        redis_client: Test-only override; defaults to this module's own
            singleton client.
    """
    active_dal = _resolve_dal(dal)
    await active_dal.execute(_CLEAR_CONTEXT_SQL, [platform, platform_user_id, platform_entity_id])

    cache = _resolve_redis(redis_client)
    key = _context_cache_key(
        platform=platform, platform_user_id=platform_user_id, platform_entity_id=platform_entity_id
    )
    await cache.delete(key)
