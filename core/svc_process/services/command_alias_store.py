"""Per-community command alias lookups -- backs `bundles.bot_process`'s alias-expansion hook.

`command_aliases` (migration `013_schema_optimizations.sql`) maps a
community-scoped `alias` word to a free-text `target_command` expansion
(e.g. `sr` -> `songrequest`, or `giveaway` -> `announce giveaway starts
now`) -- `bot_process.transform()` looks up any `!<word>` that is not
already a built-in or loaded feature command, rewrites the message with
the stored expansion, and re-parses once (see that module's own docstring
for the no-recursion guarantee). The `!alias` management bundle (a
separate bundle, out of scope here) is the surface writing to this same
table; it is expected to call `invalidate_alias` after every add/update/
delete so a just-changed alias is never served stale for the remainder of
its cache TTL.

Async, Redis-first -- same construction as this module's sibling
`services/community_context_store.py`: `flask_core.AsyncDAL.execute()`
with `$1`/`$2`... placeholders, dict-like `Row` results, an injectable
`dal`/`redis_client` (test-only, keyword-only) defaulting to
`flask_core.get_bundle_dal()`/this module's own lazily-built singleton
Redis client.

Unlike `community_context_store`, `resolve_alias` never raises -- a
genuine DB/Redis failure degrades to "no alias found" (`None`), WARN-
logged and rate-limited per `(community_id, alias)` pair exactly like
`services/community_resolver.py`'s own external-call guard. This store
sits directly in `bot_process`'s hot per-message path (every unrecognized
command word triggers a lookup), so a failure here must never block the
bot's own commands or keyword responder.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import redis.asyncio as redis_asyncio

from config import Config

logger = logging.getLogger(__name__)


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls."""

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


#: Positive-hit cache TTL -- an active alias resolves for 5 minutes before
#: this store re-checks Postgres, bounding staleness after a write that
#: (for any reason) missed `invalidate_alias`.
_POSITIVE_TTL_S = 300

#: Negative-cache TTL -- "no active alias for this word" is cached for 1
#: minute, short enough that a just-added alias becomes visible quickly
#: even without `invalidate_alias` being called, long enough to absorb
#: repeated unknown-command lookups (bot_process's hot path -- any
#: unrecognized word triggers one) without hammering Postgres.
_NEGATIVE_TTL_S = 60

#: Cache-value prefix marking a positive hit -- the raw `target_command`
#: follows. Distinguishes a real (if ever empty) `target_command` from the
#: negative-cache marker below; `target_command` is DB `NOT NULL` so should
#: never actually be empty in practice, but this scheme doesn't rely on
#: that.
_HIT_PREFIX = "1:"

#: Cache value marking a negative-cache (miss) entry.
_MISS_VALUE = "0"

_SELECT_ALIAS_SQL = (
    "SELECT alias, target_command FROM command_aliases "
    "WHERE community_id = $1 AND alias = $2 AND deleted_at IS NULL "
    "LIMIT 1"
)

_LIST_ALIASES_SQL = (
    "SELECT alias, target_command FROM command_aliases "
    "WHERE community_id = $1 AND deleted_at IS NULL "
    "ORDER BY alias ASC"
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


def _cache_key(*, community_id: int, alias: str) -> str:
    """Build the Redis key for one community's alias lookup: `cmdalias:{community_id}:{alias}`."""
    return f"cmdalias:{community_id}:{alias}"


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
_last_warned_at: dict[tuple[int, str], float] = {}
_WARN_INTERVAL_S = 30.0


def _warn_rate_limited(community_id: int, alias: str, exc: Exception) -> None:
    """Log a `resolve_alias` failure -- WARN at most once per interval per `(community_id, alias)`.

    Every failure is logged (DEBUG at minimum, per Observability's "overlog
    at DEBUG"); only the WARN surface is throttled, mirroring
    `services/community_resolver.py::_warn_rate_limited` -- a sustained
    DB/Redis outage must not flood the WARN log once per chat message.
    """
    key = (community_id, alias)
    now = time.monotonic()
    should_warn = False
    with _warn_lock:
        last = _last_warned_at.get(key)
        if last is None or (now - last) >= _WARN_INTERVAL_S:
            _last_warned_at[key] = now
            should_warn = True
    if should_warn:
        logger.warning(
            "command_alias_store.resolve_failed community_id=%s alias=%s error=%s",
            community_id,
            alias,
            exc,
        )
    else:
        logger.debug(
            "command_alias_store.resolve_failed community_id=%s alias=%s error=%r",
            community_id,
            alias,
            exc,
        )


def reset_warn_rate_limit_for_tests() -> None:
    """Clear the per-`(community_id, alias)` last-WARN-at tracker. Test isolation only."""
    with _warn_lock:
        _last_warned_at.clear()


@dataclass(slots=True)
class CommandAlias:
    """One active `command_aliases` row -- the word a user typed and its stored expansion."""

    alias: str
    target_command: str
    community_id: int


async def resolve_alias(
    *,
    community_id: int | None,
    alias: str,
    dal: _ExecutableDal | None = None,
    redis_client: Any | None = None,
) -> CommandAlias | None:
    """Return the active alias for `(community_id, alias)`, or `None` if none exists.

    Redis-first (`cmdalias:{community_id}:{alias}`): a hit returns
    immediately without touching Postgres; a miss falls back to the table
    and caches the outcome either way (`_POSITIVE_TTL_S` on a row found,
    `_NEGATIVE_TTL_S` on none) so a sustained stream of unknown-command
    lookups for the same word never hits the DB more than once per TTL
    window. A Redis/DB failure at any point never raises -- it is WARN-
    logged (rate-limited, see `_warn_rate_limited`) and treated identically
    to "no alias", since this sits in `bot_process`'s hot per-message path.

    `community_id=None` short-circuits to `None` before any cache/DB call
    -- an alias with no community to scope it can never match
    `command_aliases.community_id`'s `NOT NULL` column, so there is
    nothing to look up.

    Args:
        community_id: The community to scope the lookup to, or `None` to
            skip the lookup entirely (e.g. a tenant-wide envelope with no
            resolved community).
        alias: The command word (already lowercased by the caller) to
            resolve.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.
        redis_client: Test-only override; defaults to this module's own
            singleton client.

    Returns:
        A `CommandAlias`, or `None` if no active alias matches or the
        lookup failed.
    """
    if community_id is None:
        return None

    try:
        cache = _resolve_redis(redis_client)
        key = _cache_key(community_id=community_id, alias=alias)
        cached = await cache.get(key)
        if cached is not None:
            if cached == _MISS_VALUE:
                return None
            return CommandAlias(
                alias=alias,
                target_command=cached[len(_HIT_PREFIX) :],  # noqa: E203 - ruff-format's slice spacing
                community_id=community_id,
            )

        active_dal = _resolve_dal(dal)
        rows = await active_dal.execute(_SELECT_ALIAS_SQL, [community_id, alias])
        if not rows:
            await cache.set(key, _MISS_VALUE, ex=_NEGATIVE_TTL_S)
            return None

        target_command = str(rows[0]["target_command"])
        await cache.set(key, f"{_HIT_PREFIX}{target_command}", ex=_POSITIVE_TTL_S)
        return CommandAlias(alias=alias, target_command=target_command, community_id=community_id)
    except Exception as exc:  # noqa: BLE001 -- must never break the bot's hot path, see docstring
        _warn_rate_limited(community_id, alias, exc)
        return None


async def invalidate_alias(
    *, community_id: int, alias: str, redis_client: Any | None = None
) -> None:
    """Delete the cached lookup for `(community_id, alias)`.

    Called by the alias-management bundle immediately after an
    add/update/delete so a just-changed alias is never served from a stale
    cache entry for the remainder of its TTL.

    Args:
        community_id: The community the alias belongs to.
        alias: The alias word.
        redis_client: Test-only override; defaults to this module's own
            singleton client.
    """
    cache = _resolve_redis(redis_client)
    key = _cache_key(community_id=community_id, alias=alias)
    await cache.delete(key)


async def list_aliases(
    *, community_id: int, dal: _ExecutableDal | None = None
) -> list[CommandAlias]:
    """Return every active alias for `community_id`, alphabetical by alias word.

    Args:
        community_id: The community to list aliases for.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.

    Returns:
        Zero or more `CommandAlias` rows.
    """
    active_dal = _resolve_dal(dal)
    rows = await active_dal.execute(_LIST_ALIASES_SQL, [community_id])
    return [
        CommandAlias(
            alias=str(row["alias"]),
            target_command=str(row["target_command"]),
            community_id=community_id,
        )
        for row in rows
    ]
