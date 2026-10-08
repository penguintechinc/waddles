"""Bar Citizen role-sync worker (Unit F) -- bidirectional Twitch<->Discord reconcile engine.

**Two directions, each with its own single authoritative source -- never
the same role concept written both ways.** `guild_tenant_pairings.direction`
picks which of the two reconcile passes below run for a pairing
(`twitch_to_discord`, `discord_to_twitch`, or `bidirectional` -- both):

- **`twitch_to_discord`** (`reconcile_pairing()`, unchanged from the first
  cut of this worker): Twitch subscriber tiers T1/T2/T3 + moderators are
  authoritative, mirrored INTO Discord via `community_role_sync_bindings`
  rows with `sync_scope IN ('subscriber_tier', 'moderator')`.
- **`discord_to_twitch`** (`reconcile_pairing_discord_to_platform()`, new
  this PR -- the direction deferred at Unit F's first cut, see git
  history for the prior module docstring's "deliberately deferred"
  note). Despite the historical name (kept verbatim rather than
  renamed -- it is `guild_tenant_pairings.direction`'s literal CHECK
  constraint value, changing it is a breaking migration for zero
  benefit), this direction does NOT write to Twitch at all -- Twitch
  grants no API to assign a subscriber tier. Discord guild role
  membership is authoritative instead, driving the linked hub_user's
  **community** role/scope (`community_members.role`) via
  `community_role_sync_bindings` rows with `sync_scope = 'community_role'`
  (migration 0036) -- i.e. "Discord role -> this platform's own community
  authz", not "Discord role -> Twitch".

**Loop-prevention is structural, not stateful.** The blocker that
deferred this direction at Unit F's first cut was "never re-applying a
change this worker just made". Migration 0036 resolves it at the schema
level instead of a last-applied-state table: a `community_role_sync_
bindings` row's `sync_scope` fixes ONE authoritative write direction for
life (enforced at binding-creation time by `services/guild_pairing.py::
create_binding()` -- a `discord_role_id` already bound as `subscriber_
tier`/`moderator` under a pairing can never also be bound as
`community_role` there, and vice versa). `reconcile_pairing()` only ever
calls Discord's add/remove-role API; `reconcile_pairing_discord_to_
platform()` only ever reads Discord roles and writes `community_members`
-- disjoint write targets, so this worker's own write is never also
something it later reads back as a change to re-apply. No cycle exists to
prevent at runtime.

**Conflict precedence (one user, >1 mapped Discord role).** Resolved by
`community_roles.priority` (existing column, already the DB-side ordering
other callers use) -- highest priority held among the user's currently-
mapped Discord roles wins, deterministic tie-break on role name. See
`_resolve_desired_community_role()`.

**Grant-only, never auto-demotes.** If a previously-mapped user holds none
of a pairing's mapped Discord roles this pass, their community role is
left untouched -- a transient Discord API hiccup must never silently
revoke a human's existing community authz. Demotion remains an explicit
admin action (`services/admin_service.py::update_member_role()`). Roles
are security-sensitive (authz, not display) -- this worker is
conservative by design, matching `community-owner`'s own existing
never-touched protection (below) rather than requiring a second
protection category.

**`community-owner` is sacrosanct.** Never assignable via a
`community_role` binding (`services/guild_pairing.py`'s own
`VALID_COMMUNITY_ROLES` excludes it) and never overwritten by this
worker even if somehow already set -- mirrors `admin_service.
update_member_role()`'s own invariant verbatim.

**Role-sync never crosses tenants.** Both directions resolve every
mapping through `community_role_sync_bindings`/`guild_tenant_pairings`,
which are scoped by `community_id` alone; `_resolve_tenant_for_community()`
derives the owning tenant from that community row, never from caller
input -- a Discord guild paired with communities in two different tenants
(N:M, migration 0034) reconciles each pairing's tenant independently, and
a hub_user's community-role write is always scoped to the SAME
`community_id` the Discord role mapping came from.

**Trigger model: periodic reconcile, not event-driven.** Wiring Twitch
EventSub subscription/moderator-change events (or a Discord gateway
GUILD_MEMBER_UPDATE push) through the live ingest pipeline would touch
`core/svc_ingest`/`core/svc_action`'s Rust dispatch -- exactly the surface
PR #561 (tokenization) and the future Rust routing unit are also changing
concurrently. Rather than risk a collision there, this unit polls Helix/
Discord REST per enabled pairing on a fixed cadence via `main()` below,
run as a Kubernetes CronJob -- same "standalone process, own
`penguin-dal` connection" shape `usage_aggregator_service.py` already
uses, chosen over an in-process hub-api loop since Twitch's own rate
limits make a sub-minute cadence pointless here (unlike that module's
`e2s` cadence). **Follow-up (not in this PR):** an EventSub/gateway-pushed
fast path once the Rust dispatch work lands; this worker's periodic pass
remains the correctness backstop (drift reconciliation) even after that
lands -- reconciliation-by-polling is never fully replaced by push.

**Identity resolution.** `community_role_sync_bindings` maps a Twitch
concept (sub tier / moderator) or a hub-platform concept (community role)
to a Discord role ID; the actual Twitch user <-> Discord user <-> hub_user
link is `hub_user_identities` (one row per `(hub_user_id, platform)`). A
Twitch subscriber/moderator or Discord guild member with no linked
counterpart identity is skipped and counted, never an error. A linked
hub_user who is not yet an active `community_members` row for the target
community is also skipped and counted, never an error -- this worker
never creates community membership, only adjusts an existing member's
role.

**Fail-closed per pairing, per direction.** A credential/API failure for
one pairing's one direction is logged at ERROR and that direction's
reconcile is skipped; it never raises out of `run_role_sync_reconcile_
batch()` and never affects the other direction of the same pairing, any
other pairing, or any other tenant.

**No raw PII in logs.** Every log line below carries platform user IDs,
pairing/community/guild IDs, and counts only -- never a Twitch login or
Discord username.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydal import Field

from services.bundle_telemetry import bundle_span, get_meter
from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    TransportUnavailable,
)
from services.guild_pairing import list_bindings
from services.schema import bind_auth_tables, bind_bar_citizen_tables

try:
    from flask_core.feature_flags import feature_enabled
except ImportError:  # pragma: no cover -- exercised only outside the real flask_core install
    feature_enabled = None

logger = logging.getLogger(__name__)

TWITCH_API_BASE = "https://api.twitch.tv/helix"
DISCORD_API_BASE = "https://discord.com/api/v10"
_REQUEST_TIMEOUT_SECONDS = 10.0
#: Safety cap on Helix/Discord pagination per pairing per reconcile pass
#: (~2,000 rows at 100-1000/page).
_MAX_PAGES = 20
#: Discord's own documented max page size for `GET /guilds/{id}/members`.
_DISCORD_MEMBER_PAGE_SIZE = 1000

#: `guild_tenant_pairings.direction` values that run `reconcile_pairing()` (Twitch ->
#: Discord, subscriber_tier/moderator bindings).
_TWITCH_TO_DISCORD_DIRECTIONS = frozenset({"twitch_to_discord", "bidirectional"})
#: `guild_tenant_pairings.direction` values that run `reconcile_pairing_discord_to_
#: platform()` (Discord -> this platform's community role, community_role bindings).
_DISCORD_TO_PLATFORM_DIRECTIONS = frozenset({"discord_to_twitch", "bidirectional"})

#: PostHog flag gating this entire engine -- defaulted OFF until validated (critical-rules.md).
FEATURE_BAR_CITIZEN_ROLE_SYNC = "waddles.bar_citizen.role_sync"

_meter = get_meter()
_roles_added_counter = _meter.create_counter(
    "waddles_bar_citizen_roles_added_total", description="Discord roles added by role-sync"
)
_roles_removed_counter = _meter.create_counter(
    "waddles_bar_citizen_roles_removed_total", description="Discord roles removed by role-sync"
)
_community_roles_applied_counter = _meter.create_counter(
    "waddles_bar_citizen_community_roles_applied_total",
    description="hub-platform community_members.role changes applied by role-sync "
    "(Discord -> platform direction)",
)
_sync_errors_counter = _meter.create_counter(
    "waddles_bar_citizen_sync_errors_total", description="role-sync pairing failures, fail-closed"
)


class TwitchSyncError(Exception):
    """Raised for any Twitch Helix failure while resolving subs/mods for one pairing."""


class DiscordSyncError(Exception):
    """Raised for any Discord REST failure while reading/writing one member's roles."""


class TwitchRoleSourceClient(Protocol):
    """Twitch-side read-only data this engine needs. Real impl: `HttpTwitchRoleSourceClient`."""

    async def get_broadcaster_id(self, *, user_token: str, client_id: str) -> str:
        """Resolve the broadcaster's own Twitch user id from their user-scoped token."""
        ...

    async def list_subscriber_tiers(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> dict[str, int]:
        """Return `{twitch_user_id: tier}` (tier in 1/2/3) for every active subscriber."""
        ...

    async def list_moderators(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> set[str]:
        """Return the set of twitch_user_ids who are moderators."""
        ...


@dataclass(slots=True, frozen=True)
class DiscordGuildMember:
    """One `GET /guilds/{id}/members` row -- the Discord-side source for the platform direction."""

    user_id: str
    role_ids: frozenset[str]


class DiscordRoleTargetClient(Protocol):
    """Discord-side role read/write this engine needs. Real impl: `HttpDiscordRoleTargetClient`."""

    async def get_member_role_ids(self, *, guild_id: str, user_id: str) -> set[str] | None:
        """Return the member's current role IDs, or `None` if they aren't a guild member."""
        ...

    async def add_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        """`PUT` the role onto the member; return whether the call succeeded."""
        ...

    async def remove_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        """`DELETE` the role from the member; return whether the call succeeded."""
        ...

    async def list_guild_members(self, *, guild_id: str) -> list[DiscordGuildMember]:
        """Return every guild member's `(user_id, role_ids)` -- the platform direction's read."""
        ...


def _classify_twitch(response: httpx.Response, *, action: str) -> None:
    """Raise a SPECIFIC `TwitchSyncError` for a non-2xx response; return `None` on 2xx."""
    if response.status_code == 401:
        raise TwitchSyncError(f"twitch oauth token didn't work (401) during {action}")
    if response.status_code == 403:
        raise TwitchSyncError(f"twitch token lacks required scope (403) during {action}")
    if response.status_code == 429:
        raise TwitchSyncError(f"twitch api rate limited (429) during {action}")
    if response.status_code >= 400:
        raise TwitchSyncError(f"twitch api returned HTTP {response.status_code} during {action}")


class HttpTwitchRoleSourceClient:
    """Real Helix client for role-sync: broadcaster self-lookup + paginated subs/mods.

    Deliberately separate from `core/svc_action/services/twitch_helix.py`'s
    `TwitchHelixClient` -- that client only ever mints/uses an app
    (`client_credentials`) token, but `/subscriptions` and `/moderation/
    moderators` both require a *user* (broadcaster-scoped) token, which
    this engine resolves per-community via the injected
    `get_broadcaster_user_token` callable. Same Client-Id/Bearer header
    shape and HTTP error classification style as `twitch_helix.py`/
    `services/platform_moderation.py`, reused as a pattern (not imported)
    since the auth lane here is a user token, not an app token.
    """

    def __init__(self, http_client: httpx.AsyncClient, *, api_base: str = TWITCH_API_BASE) -> None:
        """Bind to a shared `httpx.AsyncClient`; `api_base` overridable for tests."""
        self._http = http_client
        self._api_base = api_base

    async def get_broadcaster_id(self, *, user_token: str, client_id: str) -> str:
        """`GET /users` with no `login` param -- resolves the token owner's own user id."""
        headers = {"Authorization": f"Bearer {user_token}", "Client-Id": client_id}
        try:
            response = await self._http.get(
                f"{self._api_base}/users", headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise TwitchSyncError(f"twitch self-lookup request failed: {exc}") from exc
        _classify_twitch(response, action="self-lookup")
        data = response.json().get("data") or []
        if not data:
            raise TwitchSyncError("twitch self-lookup returned no user")
        return str(data[0]["id"])

    async def _paginate(
        self, path: str, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {user_token}", "Client-Id": client_id}
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {"broadcaster_id": broadcaster_id, "first": "100"}
            if cursor:
                params["after"] = cursor
            try:
                response = await self._http.get(
                    f"{self._api_base}{path}",
                    headers=headers,
                    params=params,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                raise TwitchSyncError(f"twitch {path} request failed: {exc}") from exc
            _classify_twitch(response, action=path)
            body = response.json()
            items.extend(body.get("data") or [])
            cursor = (body.get("pagination") or {}).get("cursor") or None
            if not cursor:
                break
        return items

    async def list_subscriber_tiers(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> dict[str, int]:
        """`GET /subscriptions` -- maps Twitch's `"1000"/"2000"/"3000"` tier strings to 1/2/3."""
        rows = await self._paginate(
            "/subscriptions",
            broadcaster_id=broadcaster_id,
            user_token=user_token,
            client_id=client_id,
        )
        tiers: dict[str, int] = {}
        for row in rows:
            user_id = row.get("user_id")
            tier_raw = str(row.get("tier", ""))
            if not user_id or not tier_raw:
                continue
            try:
                tier = int(tier_raw) // 1000
            except ValueError:
                continue
            if tier in (1, 2, 3):
                tiers[str(user_id)] = tier
        return tiers

    async def list_moderators(
        self, *, broadcaster_id: str, user_token: str, client_id: str
    ) -> set[str]:
        """`GET /moderation/moderators`."""
        rows = await self._paginate(
            "/moderation/moderators",
            broadcaster_id=broadcaster_id,
            user_token=user_token,
            client_id=client_id,
        )
        return {str(row["user_id"]) for row in rows if row.get("user_id")}


def _classify_discord(response: httpx.Response, *, action: str) -> None:
    """Raise a SPECIFIC `DiscordSyncError` for a non-2xx/404 response; return `None` otherwise."""
    if response.status_code == 401:
        raise DiscordSyncError(f"discord bot token didn't work (401) during {action}")
    if response.status_code == 403:
        raise DiscordSyncError(f"discord bot lacks permission (403) during {action}")
    if response.status_code == 429:
        raise DiscordSyncError(f"discord api rate limited (429) during {action}")
    if response.status_code >= 400 and response.status_code != 404:
        raise DiscordSyncError(f"discord api returned HTTP {response.status_code} during {action}")


class HttpDiscordRoleTargetClient:
    """Real Discord REST client for role-sync.

    Same `PUT`/`DELETE .../guilds/{guild}/members/{user}/roles/{role}`
    shape `action/pushing/discord_action_module/services/discord_service.
    py::manage_role` already uses, reimplemented here (not imported) since
    that module lives in a separately-deployed service/container with its
    own `config.py`/pydal activity-log wiring that hub-api does not share.
    """

    def __init__(
        self, http_client: httpx.AsyncClient, *, bot_token: str, api_base: str = DISCORD_API_BASE
    ) -> None:
        """Bind to a shared `httpx.AsyncClient` + this pairing's tenant's resolved bot token."""
        self._http = http_client
        self._bot_token = bot_token
        self._api_base = api_base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._bot_token}"}

    async def get_member_role_ids(self, *, guild_id: str, user_id: str) -> set[str] | None:
        """`GET /guilds/{guild_id}/members/{user_id}` -- `None` if not a guild member (404)."""
        try:
            response = await self._http.get(
                f"{self._api_base}/guilds/{guild_id}/members/{user_id}",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordSyncError(f"discord member lookup failed: {exc}") from exc
        if response.status_code == 404:
            return None
        _classify_discord(response, action="member lookup")
        return {str(r) for r in response.json().get("roles") or []}

    async def _manage_role(self, *, guild_id: str, user_id: str, role_id: str, add: bool) -> bool:
        method = "PUT" if add else "DELETE"
        action = "role add" if add else "role remove"
        try:
            response = await self._http.request(
                method,
                f"{self._api_base}/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordSyncError(f"discord {action} failed: {exc}") from exc
        _classify_discord(response, action=action)
        return response.status_code in (200, 201, 204)

    async def add_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        """`PUT` the role onto the member."""
        return await self._manage_role(
            guild_id=guild_id, user_id=user_id, role_id=role_id, add=True
        )

    async def remove_role(self, *, guild_id: str, user_id: str, role_id: str) -> bool:
        """`DELETE` the role from the member."""
        return await self._manage_role(
            guild_id=guild_id, user_id=user_id, role_id=role_id, add=False
        )

    async def list_guild_members(self, *, guild_id: str) -> list[DiscordGuildMember]:
        """`GET /guilds/{guild_id}/members`, paginated by snowflake `after` cursor (max 1000/page).

        The discord_to_platform direction's one read of guild state --
        mirrors `_paginate()`'s cursor-loop shape on
        `HttpTwitchRoleSourceClient` (a different pagination style --
        Discord has no cursor token, just "page by the last-seen user id" --
        so not shared code, same `_MAX_PAGES` safety cap).
        """
        members: list[DiscordGuildMember] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {"limit": str(_DISCORD_MEMBER_PAGE_SIZE)}
            if after:
                params["after"] = after
            try:
                response = await self._http.get(
                    f"{self._api_base}/guilds/{guild_id}/members",
                    headers=self._headers(),
                    params=params,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                raise DiscordSyncError(f"discord guild member list failed: {exc}") from exc
            _classify_discord(response, action="guild member list")
            page = response.json() or []
            if not page:
                break
            for row in page:
                user = row.get("user") or {}
                user_id = user.get("id")
                if not user_id:
                    continue
                members.append(
                    DiscordGuildMember(
                        user_id=str(user_id),
                        role_ids=frozenset(str(r) for r in row.get("roles") or []),
                    )
                )
            if len(page) < _DISCORD_MEMBER_PAGE_SIZE:
                break
            after = str(page[-1].get("user", {}).get("id") or "")
            if not after:
                break
        return members


@dataclass(slots=True, frozen=True)
class PairingSyncResult:
    """Outcome of one `reconcile_pairing()` call -- counts only, no platform IDs logged twice."""

    pairing_id: int
    community_id: int
    roles_added: int
    roles_removed: int
    users_skipped_unlinked: int
    error: str | None


@dataclass(slots=True, frozen=True)
class PlatformSyncResult:
    """Outcome of one `reconcile_pairing_discord_to_platform()` call -- counts only."""

    pairing_id: int
    community_id: int
    community_roles_applied: int
    users_skipped_unlinked: int
    users_skipped_not_member: int
    users_skipped_owner_protected: int
    error: str | None


@dataclass(slots=True)
class ReconcileSummary:
    """Aggregate counters for one `run_role_sync_reconcile_batch()` pass -- the CLI's print line."""

    pairings_examined: int = 0
    pairings_synced: int = 0
    pairings_skipped_wrong_direction: int = 0
    pairings_failed: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    community_roles_applied: int = 0


def _list_enabled_pairings(dal: Any) -> list[Any]:
    """Every `guild_tenant_pairings` row with `sync_enabled=True`, any direction.

    Direction filtering happens in the caller (not this query) so a
    skipped-direction pairing is still counted, not silently invisible.
    """
    bind_bar_citizen_tables(dal)
    t = dal.guild_tenant_pairings
    return list(dal(t.sync_enabled == True).select())  # noqa: E712 - pydal idiom


def _bind_reference_tenants(dal: Any) -> None:
    """Idempotently ensure `dal.tenants` exists, same field set as `app.py::_bind_reference_tables`.

    In the Quart app this is always a no-op (the table is already bound at
    startup); the standalone CronJob entrypoint (`main()` below) has no
    such startup hook, so this engine binds it the same way, never via
    `services.schema.bind_tenant_tables()` -- that function's own
    `redefine=True` replaces `tenants`' field list wholesale, which would
    silently widen the Python-side Table object past what a test's
    minimal sqlite schema (see `conftest.py::bar_citizen_db`) actually has
    columns for.
    """
    if "tenants" in dal.tables:
        return
    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )


def _resolve_tenant_for_community(dal: Any, community_id: int) -> tuple[int, str, bool] | None:
    """`(tenant_id, tenant_slug, is_global)` for `community_id`, `None` if either row is missing."""
    bind_auth_tables(dal)
    _bind_reference_tenants(dal)
    community = dal(dal.communities.id == community_id).select().first()
    if community is None:
        return None
    tenant = dal(dal.tenants.id == community.tenant_id).select().first()
    if tenant is None:
        return None
    return int(tenant.id), str(tenant.slug), bool(tenant.is_global)


def _find_linked_discord_user(dal: Any, twitch_user_id: str) -> str | None:
    """Follow `hub_user_identities`: twitch_user_id -> hub_user_id -> discord_user_id.

    `None` if the Twitch account has no linked hub user, or that hub user
    has no linked Discord identity -- both are normal, uncounted-as-error
    states (a subscriber who never linked Discord), never a fail-closed path.
    """
    bind_auth_tables(dal)
    t = dal.hub_user_identities
    twitch_row = (
        dal((t.platform == "twitch") & (t.platform_user_id == twitch_user_id)).select().first()
    )
    if twitch_row is None:
        return None
    discord_row = (
        dal((t.platform == "discord") & (t.hub_user_id == twitch_row.hub_user_id)).select().first()
    )
    if discord_row is None:
        return None
    return str(discord_row.platform_user_id)


def _find_linked_hub_user_by_discord(dal: Any, discord_user_id: str) -> int | None:
    """Reverse of `_find_linked_discord_user()`: discord_user_id -> hub_user_id.

    `None` if this Discord account has no linked hub_user -- a guild
    member who never connected their platform account, a normal,
    uncounted-as-error state.
    """
    bind_auth_tables(dal)
    t = dal.hub_user_identities
    row = dal((t.platform == "discord") & (t.platform_user_id == discord_user_id)).select().first()
    return int(row.hub_user_id) if row is not None else None


def _resolve_desired_community_role(
    role_priority: dict[str, int], mapped_roles: set[str]
) -> str | None:
    """Highest-`community_roles.priority` role among `mapped_roles`; deterministic tie-break.

    `role_priority` unknown entries default to priority 0 (same default
    `community_roles.priority` itself uses) -- a `community_role` binding
    naming a role this community has never explicitly prioritized still
    resolves, just lowest-priority among whatever else is in play. Ties
    (equal priority) break on role name, descending, purely for
    determinism across reconcile passes -- never a semantic ranking.
    """
    if not mapped_roles:
        return None
    return max(mapped_roles, key=lambda role: (role_priority.get(role, 0), role))


def _community_role_priorities(dal: Any, community_id: int) -> dict[str, int]:
    """`{community_roles.name: priority}` for `community_id` -- the conflict-precedence table."""
    bind_auth_tables(dal)
    rows = dal(dal.community_roles.community_id == community_id).select()
    return {row.name: int(row.priority or 0) for row in rows}


def _apply_community_role(
    dal: Any, *, community_id: int, hub_user_id: int, desired_role: str
) -> str:
    """Idempotently set an existing active member's role, never touching `community-owner`.

    Returns one of `"applied"` / `"unchanged"` / `"not_a_member"` /
    `"owner_protected"` -- this worker never creates `community_members`
    rows (a Discord guild member who never joined this hub platform
    community is skipped, not auto-enrolled) and never downgrades
    `community-owner` (mirrors `admin_service.update_member_role()`'s own
    invariant). Mirrors that function's own `community_role_id` lookup +
    `claims_cache` reset tail, but via the sync `dal` this module already
    uses throughout (see module docstring / `guild_pairing.py`'s own
    "raw pydal, explicit commit" convention) rather than `admin_service`'s
    `async_dal`/`TenantContext`-gated surface, which assumes a human actor
    performing one promotion, not a system reconcile pass.
    """
    bind_auth_tables(dal)
    member = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal idiom
        )
        .select()
        .first()
    )
    if member is None:
        return "not_a_member"
    if member.role == "community-owner":
        return "owner_protected"
    if member.role == desired_role:
        return "unchanged"

    role_row = (
        dal(
            (dal.community_roles.community_id == community_id)
            & (dal.community_roles.name == desired_role)
        )
        .select()
        .first()
    )
    community_role_id = int(role_row.id) if role_row is not None else None

    dal(dal.community_members.id == member.id).update(
        role=desired_role,
        community_role_id=community_role_id,
        claims_cache=None,
        updated_at=datetime.now(UTC),
    )
    dal.commit()
    return "applied"


async def _flag_enabled(tenant_slug: str) -> bool:
    """`feature_enabled(FEATURE_BAR_CITIZEN_ROLE_SYNC, tenant=...)`, defaulted OFF.

    `feature_enabled` itself already degrades to a cached/default value on
    a PostHog/license-server outage (never raises) -- this wrapper exists
    only so a test environment without `flask_core` installed (see this
    module's `ImportError` guard above) treats the flag as OFF rather than
    crashing on import.
    """
    if feature_enabled is None:  # pragma: no cover -- only in a flask_core-less environment
        return False
    return bool(await feature_enabled(FEATURE_BAR_CITIZEN_ROLE_SYNC, tenant=tenant_slug))


async def reconcile_pairing(
    dal: Any,
    pairing: Any,
    *,
    get_broadcaster_user_token: Callable[[int], Awaitable[str | None]],
    credential_resolver: CredentialResolver,
    twitch_client: TwitchRoleSourceClient,
    make_discord_client: Callable[[str], DiscordRoleTargetClient],
) -> PairingSyncResult:
    """Reconcile ONE `guild_tenant_pairings` row's Twitch -> Discord direction.

    Runs for `direction in ("twitch_to_discord", "bidirectional")` --
    `discord_to_twitch`-only pairings are a no-op here (handled instead by
    `reconcile_pairing_discord_to_platform()`).

    Fail-closed: any credential/API/unexpected failure is caught here and
    returned as `PairingSyncResult.error` -- never raised to the caller
    (`run_role_sync_reconcile_batch`'s per-pairing isolation depends on
    this never propagating).
    """
    pairing_id = int(pairing.id)
    community_id = int(pairing.community_id)
    guild_id = str(pairing.discord_guild_id)

    if not pairing.sync_enabled or pairing.direction not in _TWITCH_TO_DISCORD_DIRECTIONS:
        return PairingSyncResult(pairing_id, community_id, 0, 0, 0, None)

    try:
        tenant = _resolve_tenant_for_community(dal, community_id)
        if tenant is None:
            raise TransportUnavailable(f"community {community_id} has no resolvable tenant")
        tenant_id, tenant_slug, is_global = tenant

        if not await _flag_enabled(tenant_slug):
            logger.info("role_sync.flag_disabled pairing_id=%s tenant=%s", pairing_id, tenant_slug)
            return PairingSyncResult(pairing_id, community_id, 0, 0, 0, None)

        twitch_creds = await credential_resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="twitch"
        )
        discord_creds = await credential_resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
        )
        client_id = str(twitch_creds.payload.get("client_id", ""))
        bot_token = str(discord_creds.payload.get("bot_token", ""))
        if not client_id or not bot_token:
            raise TransportUnavailable(
                f"tenant {tenant_id} has incomplete twitch/discord credentials for role-sync"
            )

        user_token = await get_broadcaster_user_token(community_id)
        if not user_token:
            raise TransportUnavailable(
                f"community {community_id} has no connected Twitch broadcaster token"
            )

        broadcaster_id = await twitch_client.get_broadcaster_id(
            user_token=user_token, client_id=client_id
        )
        tiers = await twitch_client.list_subscriber_tiers(
            broadcaster_id=broadcaster_id, user_token=user_token, client_id=client_id
        )
        mods = await twitch_client.list_moderators(
            broadcaster_id=broadcaster_id, user_token=user_token, client_id=client_id
        )

        bindings = list_bindings(dal, community_id, pairing_id)
        tier_role: dict[int, str] = {
            b.subscriber_tier: b.discord_role_id
            for b in bindings
            if b.sync_scope == "subscriber_tier" and b.subscriber_tier is not None
        }
        mod_role = next((b.discord_role_id for b in bindings if b.sync_scope == "moderator"), None)
        managed_role_ids = set(tier_role.values()) | ({mod_role} if mod_role else set())

        if not managed_role_ids:
            return PairingSyncResult(pairing_id, community_id, 0, 0, 0, None)

        discord_client = make_discord_client(bot_token)

        desired_by_discord_user: dict[str, set[str]] = {}
        skipped_unlinked = 0
        for twitch_user_id in set(tiers) | mods:
            discord_user_id = _find_linked_discord_user(dal, twitch_user_id)
            if discord_user_id is None:
                skipped_unlinked += 1
                continue
            desired = desired_by_discord_user.setdefault(discord_user_id, set())
            tier = tiers.get(twitch_user_id)
            if tier is not None and tier in tier_role:
                desired.add(tier_role[tier])
            if twitch_user_id in mods and mod_role:
                desired.add(mod_role)

        roles_added = 0
        roles_removed = 0
        for discord_user_id, desired_roles in desired_by_discord_user.items():
            current = await discord_client.get_member_role_ids(
                guild_id=guild_id, user_id=discord_user_id
            )
            if current is None:
                continue  # not a guild member -- nothing to sync for them this pass
            for role_id in desired_roles - current:
                if await discord_client.add_role(
                    guild_id=guild_id, user_id=discord_user_id, role_id=role_id
                ):
                    roles_added += 1
            for role_id in (current & managed_role_ids) - desired_roles:
                if await discord_client.remove_role(
                    guild_id=guild_id, user_id=discord_user_id, role_id=role_id
                ):
                    roles_removed += 1

        logger.info(
            "role_sync.pairing_synced pairing_id=%s community_id=%s guild_id=%s "
            "subs=%d mods=%d roles_added=%d roles_removed=%d skipped_unlinked=%d",
            pairing_id,
            community_id,
            guild_id,
            len(tiers),
            len(mods),
            roles_added,
            roles_removed,
            skipped_unlinked,
        )
        _roles_added_counter.add(roles_added)
        _roles_removed_counter.add(roles_removed)
        return PairingSyncResult(
            pairing_id, community_id, roles_added, roles_removed, skipped_unlinked, None
        )

    except (TransportUnavailable, TwitchSyncError, DiscordSyncError) as exc:
        logger.error(
            "role_sync.pairing_failed pairing_id=%s community_id=%s error_type=%s",
            pairing_id,
            community_id,
            type(exc).__name__,
        )
        _sync_errors_counter.add(1)
        return PairingSyncResult(pairing_id, community_id, 0, 0, 0, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - fail-closed: one pairing's bug must never crash the worker
        logger.error(
            "role_sync.pairing_failed_unexpected pairing_id=%s community_id=%s error_type=%s",
            pairing_id,
            community_id,
            type(exc).__name__,
        )
        _sync_errors_counter.add(1)
        return PairingSyncResult(pairing_id, community_id, 0, 0, 0, "unexpected_error")


async def reconcile_pairing_discord_to_platform(
    dal: Any,
    pairing: Any,
    *,
    credential_resolver: CredentialResolver,
    make_discord_client: Callable[[str], DiscordRoleTargetClient],
) -> PlatformSyncResult:
    """Reconcile ONE `guild_tenant_pairings` row's Discord -> platform direction.

    Runs for `direction in ("discord_to_twitch", "bidirectional")` --
    `twitch_to_discord`-only pairings are a no-op here (handled instead by
    `reconcile_pairing()`). Discord guild role membership is authoritative;
    this never calls Discord's add/remove-role API (see module docstring's
    structural loop-prevention argument) -- only `community_members.role`
    is ever written.

    Needs no Twitch credentials/broadcaster token at all (unlike
    `reconcile_pairing()`) -- a `bidirectional` pairing missing its Twitch
    broadcaster token still runs this direction independently; the two
    directions fail closed independently, each wrapped in its own
    try/except, same per-pairing isolation `run_role_sync_reconcile_batch`
    already guarantees across pairings.
    """
    pairing_id = int(pairing.id)
    community_id = int(pairing.community_id)
    guild_id = str(pairing.discord_guild_id)

    if not pairing.sync_enabled or pairing.direction not in _DISCORD_TO_PLATFORM_DIRECTIONS:
        return PlatformSyncResult(pairing_id, community_id, 0, 0, 0, 0, None)

    try:
        tenant = _resolve_tenant_for_community(dal, community_id)
        if tenant is None:
            raise TransportUnavailable(f"community {community_id} has no resolvable tenant")
        tenant_id, tenant_slug, is_global = tenant

        if not await _flag_enabled(tenant_slug):
            logger.info(
                "role_sync.platform_flag_disabled pairing_id=%s tenant=%s", pairing_id, tenant_slug
            )
            return PlatformSyncResult(pairing_id, community_id, 0, 0, 0, 0, None)

        discord_creds = await credential_resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
        )
        bot_token = str(discord_creds.payload.get("bot_token", ""))
        if not bot_token:
            raise TransportUnavailable(f"tenant {tenant_id} has no discord bot token for role-sync")

        bindings = list_bindings(dal, community_id, pairing_id)
        role_by_discord_role_id: dict[str, str] = {
            b.discord_role_id: b.community_role
            for b in bindings
            if b.sync_scope == "community_role" and b.community_role is not None
        }
        if not role_by_discord_role_id:
            return PlatformSyncResult(pairing_id, community_id, 0, 0, 0, 0, None)

        discord_client = make_discord_client(bot_token)
        members = await discord_client.list_guild_members(guild_id=guild_id)
        role_priority = _community_role_priorities(dal, community_id)

        applied = 0
        skipped_unlinked = 0
        skipped_not_member = 0
        skipped_owner_protected = 0
        for member in members:
            mapped_roles = {
                role_by_discord_role_id[role_id]
                for role_id in member.role_ids
                if role_id in role_by_discord_role_id
            }
            desired_role = _resolve_desired_community_role(role_priority, mapped_roles)
            if desired_role is None:
                continue  # no mapped Discord role held this pass -- grant-only, never demote

            hub_user_id = _find_linked_hub_user_by_discord(dal, member.user_id)
            if hub_user_id is None:
                skipped_unlinked += 1
                continue

            outcome = _apply_community_role(
                dal, community_id=community_id, hub_user_id=hub_user_id, desired_role=desired_role
            )
            if outcome == "applied":
                applied += 1
            elif outcome == "not_a_member":
                skipped_not_member += 1
            elif outcome == "owner_protected":
                skipped_owner_protected += 1
            # "unchanged" -- already correct, not counted as a skip or an error.

        logger.info(
            "role_sync.platform_pairing_synced pairing_id=%s community_id=%s guild_id=%s "
            "members=%d community_roles_applied=%d skipped_unlinked=%d skipped_not_member=%d "
            "skipped_owner_protected=%d",
            pairing_id,
            community_id,
            guild_id,
            len(members),
            applied,
            skipped_unlinked,
            skipped_not_member,
            skipped_owner_protected,
        )
        _community_roles_applied_counter.add(applied)
        return PlatformSyncResult(
            pairing_id,
            community_id,
            applied,
            skipped_unlinked,
            skipped_not_member,
            skipped_owner_protected,
            None,
        )

    except (TransportUnavailable, DiscordSyncError) as exc:
        logger.error(
            "role_sync.platform_pairing_failed pairing_id=%s community_id=%s error_type=%s",
            pairing_id,
            community_id,
            type(exc).__name__,
        )
        _sync_errors_counter.add(1)
        return PlatformSyncResult(pairing_id, community_id, 0, 0, 0, 0, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - fail-closed: one pairing's bug must never crash the worker
        logger.error(
            "role_sync.platform_pairing_failed_unexpected pairing_id=%s community_id=%s "
            "error_type=%s",
            pairing_id,
            community_id,
            type(exc).__name__,
        )
        _sync_errors_counter.add(1)
        return PlatformSyncResult(pairing_id, community_id, 0, 0, 0, 0, "unexpected_error")


async def run_role_sync_reconcile_batch(
    dal: Any,
    *,
    get_broadcaster_user_token: Callable[[int], Awaitable[str | None]],
    credential_resolver: CredentialResolver | None = None,
    twitch_client: TwitchRoleSourceClient | None = None,
    make_discord_client: Callable[[str], DiscordRoleTargetClient] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ReconcileSummary:
    """One full reconcile pass over every `sync_enabled` pairing (CronJob entrypoint body).

    `get_broadcaster_user_token` is the only required dependency -- it's
    the seam to the real `community_connections.get_decrypted_tokens`
    wiring (`main()` below), kept out of this function's defaults so unit
    tests never need a real `AsyncDB`/community-connections stack.
    """
    owns_http_client = http_client is None
    http_client = http_client or httpx.AsyncClient()
    credential_resolver = credential_resolver or DefaultCredentialResolver()
    twitch_client = twitch_client or HttpTwitchRoleSourceClient(http_client)
    bound_http_client = http_client

    if make_discord_client is None:

        def make_discord_client(bot_token: str) -> DiscordRoleTargetClient:
            return HttpDiscordRoleTargetClient(bound_http_client, bot_token=bot_token)

    summary = ReconcileSummary()
    try:
        pairings = _list_enabled_pairings(dal)
        summary.pairings_examined = len(pairings)
        for pairing in pairings:
            direction = pairing.direction
            if (
                direction not in _TWITCH_TO_DISCORD_DIRECTIONS
                and direction not in _DISCORD_TO_PLATFORM_DIRECTIONS
            ):
                # Defensive only -- `guild_tenant_pairings.direction` has a DB-level CHECK
                # constraint (migration 0034) covering exactly these three values; this
                # branch should be unreachable in production but is counted, never silently
                # dropped, if it is ever hit (e.g. a future direction value added to the DB
                # before this worker is updated to handle it).
                summary.pairings_skipped_wrong_direction += 1
                continue

            pairing_failed = False
            async with bundle_span("bar_citizen.role_sync.pairing", pairing_id=int(pairing.id)):
                if direction in _TWITCH_TO_DISCORD_DIRECTIONS:
                    twitch_result = await reconcile_pairing(
                        dal,
                        pairing,
                        get_broadcaster_user_token=get_broadcaster_user_token,
                        credential_resolver=credential_resolver,
                        twitch_client=twitch_client,
                        make_discord_client=make_discord_client,
                    )
                    if twitch_result.error:
                        pairing_failed = True
                    else:
                        summary.roles_added += twitch_result.roles_added
                        summary.roles_removed += twitch_result.roles_removed

                if direction in _DISCORD_TO_PLATFORM_DIRECTIONS:
                    platform_result = await reconcile_pairing_discord_to_platform(
                        dal,
                        pairing,
                        credential_resolver=credential_resolver,
                        make_discord_client=make_discord_client,
                    )
                    if platform_result.error:
                        pairing_failed = True
                    else:
                        summary.community_roles_applied += platform_result.community_roles_applied

            # A `bidirectional` pairing where one direction succeeds and the other fails
            # counts as failed, not synced -- `pairings_synced`/`pairings_failed` is a
            # per-pairing binary classification, not per-direction; each direction's own
            # role/community-role counters above are still credited independently of this
            # classification, so a partial success is never silently invisible.
            if pairing_failed:
                summary.pairings_failed += 1
            else:
                summary.pairings_synced += 1
        return summary
    finally:
        if owns_http_client:
            await bound_http_client.aclose()


async def _build_install_dal() -> Any:
    """Open this standalone CronJob process's own penguin-dal connection.

    Same DSN as the app, a separate pool -- mirrors `usage_aggregator_
    service.py::_build_install_dal`.
    """
    from services.bundle_install_dal import build_install_dal

    return await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)


async def main() -> int:
    """CronJob entrypoint: one reconcile pass, denominators printed, never a silent zero."""
    from services.community_connections import get_decrypted_tokens

    install_dal = await _build_install_dal()
    dal = install_dal.dal

    async def _get_broadcaster_user_token(community_id: int) -> str | None:
        tokens = await get_decrypted_tokens(install_dal, community_id, "twitch")
        return tokens.access_token if tokens else None

    summary = await run_role_sync_reconcile_batch(
        dal, get_broadcaster_user_token=_get_broadcaster_user_token
    )
    print(
        f"role_sync: pairings_examined={summary.pairings_examined} "
        f"pairings_synced={summary.pairings_synced} "
        f"pairings_failed={summary.pairings_failed} "
        f"pairings_skipped_wrong_direction={summary.pairings_skipped_wrong_direction} "
        f"roles_added={summary.roles_added} roles_removed={summary.roles_removed} "
        f"community_roles_applied={summary.community_roles_applied}"
    )
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
