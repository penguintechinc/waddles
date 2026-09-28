"""Permission/cooldown helpers and the `dispatch()` result shape shared by `!so`/`!vso`.

Extracted verbatim (no behavior change) from `app.py`'s original `!so`-only
implementation so `video_shoutout.py` (the `!vso` video-shoutout path added
alongside this module) can reuse the *exact same* `so_permission` tier
check the task's own requirement calls for ("permission checks matching
`!so`") without a circular import between `app` and `video_shoutout`
(`app` wires both commands' `transform`/`dispatch` together, so neither
sibling module can import the other at module scope).
"""

from __future__ import annotations

from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

#: `shoutout_config.so_permission`'s own column default (migration 046) -- used whenever the
#: table/row/connection isn't reachable, never raised or treated as a denial.
_DEFAULT_PERMISSION = "mod"
#: `shoutout_config.cooldown_minutes`'s own column default (migration 046). Exported
#: (un-prefixed) too -- `app.py`'s text-shoutout `dispatch()` reads it as its own payload
#: fallback default.
_DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_COOLDOWN_MINUTES = _DEFAULT_COOLDOWN_MINUTES

#: Permission levels this bundle can never evaluate for lack of badge data (`vip`/
#: `subscriber`) plus the always-open `everyone` -- all three are satisfied unconditionally.
_ALWAYS_ALLOWED_PERMISSIONS = frozenset({"everyone", "vip", "subscriber"})
#: `community_members.role` values satisfying `admin_only` -- owner/admin tiers only.
_ADMIN_ONLY_ROLES = frozenset({"owner", "admin", "community-owner", "community-admin"})
#: `community_members.role` values satisfying `mod` -- moderator or above.
_MOD_OR_ABOVE_ROLES = frozenset(
    {"owner", "admin", "moderator", "community-owner", "community-admin"}
)

_SHOUTOUT_CONFIG_SQL = (
    "SELECT so_permission, cooldown_minutes FROM shoutout_config WHERE community_id = $1 LIMIT 1"
)
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)


def community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the config/role lookups; unparseable/`None` -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


async def shoutout_permission_and_cooldown(
    dal: AsyncDB, community_id_: int | None
) -> tuple[str, int]:
    """Read `(so_permission, cooldown_minutes)` from `shoutout_config`; defaults on any miss.

    Degrades to `(_DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES)` on a missing community, a
    missing row, or any DB error -- never raises, never denies outright on an infrastructure
    problem.
    """
    if community_id_ is None:
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    try:
        rows = await dal.execute(_SHOUTOUT_CONFIG_SQL, [community_id_])
    except Exception:  # noqa: BLE001 -- must never block a shoutout, only degrade
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    if not rows:
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    row = rows[0]
    permission = str(row.get("so_permission") or _DEFAULT_PERMISSION)
    cooldown = row.get("cooldown_minutes")
    cooldown_minutes = int(cooldown) if cooldown is not None else _DEFAULT_COOLDOWN_MINUTES
    return permission, cooldown_minutes


async def caller_role(dal: AsyncDB, event: PlatformEvent, community_id_: int | None) -> str | None:
    """`community_members.role` for the caller, or `None` on any miss/error.

    Match by `(platform, platform_user_id)` first, else `display_name == event.actor`. Fails
    closed (`None`) on a missing community, any lookup error, or no matching row; never raises.
    """
    if community_id_ is None:
        return None

    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id_, event.platform, platform_user_id]
            )
            if rows:
                return str(rows[0]["role"]).lower()
        if event.actor:
            rows = await dal.execute(_ROLE_BY_DISPLAY_NAME_SQL, [community_id_, event.actor])
            if rows:
                return str(rows[0]["role"]).lower()
    except Exception:  # noqa: BLE001 -- permission check must fail closed, never crash
        return None

    return None


def permission_satisfied(permission: str, role: str | None) -> bool:
    """Evaluate `so_permission` against the caller's community role.

    `everyone`/`vip`/`subscriber` are always-satisfied; `admin_only` requires owner/admin;
    `mod` requires moderator or above. An unrecognized value falls back to the `mod` threshold.
    """
    normalized = permission.lower()
    if normalized in _ALWAYS_ALLOWED_PERMISSIONS:
        return True
    if normalized == "admin_only":
        return role in _ADMIN_ONLY_ROLES
    return role in _MOD_OR_ABOVE_ROLES


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result.

    See `bundles/python/pyping`'s identical class for why this exact duck-typed shape
    (`transport`, `detail`, `sub_type`, `http_status`) is what `waddle_sdk._component_entry.
    WitWorld.dispatch` reads off.
    """

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str, http_status: int | None = None) -> None:
        """Record which provider it relayed to, a short detail, and any HTTP status."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = http_status
