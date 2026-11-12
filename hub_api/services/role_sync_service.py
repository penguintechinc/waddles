"""Role-sync worker (Unit F) -- bidirectional Twitch<->Discord reconcile engine.

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

**Privilege hardening (migration 0104).** Inbound bindings may only target `vip`/`member`
(service 422 + DB CHECK) so a Discord Manage-Roles holder cannot self-promote. Grants only ever
RAISE a member (`_ROLE_RANK`) and never overwrite a role an admin set; every grant is recorded in
`role_sync_community_grants` and the opt-in demote pass revokes only those (restoring
`prior_role`). The Twitch -> Discord pass ledgers its Discord role adds in
`role_sync_discord_grants` and removes only those. Twitch moderators propagate only when the
pairing sets `propagate_moderators` (default off). The `waddles.role_sync` flag remains the
kill switch for every path.

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
`e2s` cadence). **Discord gateway push (inbound) is now wired** in
`services/role_sync_inbound_service.py` (Discord service bundle >=1.0.3 forwards
GUILD_MEMBER_UPDATE / GUILD_ROLE_*; origin-marker echo suppression via `sync_scope`).
**Follow-up:** an EventSub-pushed Twitch fast path; this worker's periodic pass
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

import asyncio
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
from services.guild_pairing import INBOUND_COMMUNITY_ROLES, list_bindings
from services.schema import bind_auth_tables, bind_guild_pairing_tables
from services.sync_worker_dal import open_worker_dal

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
#: Upper bound on one `Retry-After` sleep so a hostile/buggy header can't stall the CronJob.
_MAX_RETRY_AFTER_SECONDS = 10.0

#: Opt-in for demote-on-role-remove (Discord -> platform): comma-separated
#: `guild_tenant_pairings.id` list. Unset/empty (the default) = grant-only, exactly the
#: pre-existing behaviour -- no deploy demotes anyone until a pairing is listed here.
DEMOTE_PAIRING_IDS_ENV = "ROLE_SYNC_DEMOTE_PAIRING_IDS"
#: Role a demoted member falls back to (never null). Default `community-member`.
DEMOTE_BASELINE_ROLE_ENV = "ROLE_SYNC_DEMOTE_BASELINE_ROLE"
_DEFAULT_DEMOTE_BASELINE_ROLE = "community-member"
#: Circuit breaker: one pass may demote at most this many members unless that is also
#: <= `_DEMOTE_MAX_FRACTION` of the linked holders of sync-managed roles.
_DEMOTE_ABS_FLOOR = 3
_DEMOTE_MAX_FRACTION = 0.5
_OWNER_ROLE = "community-owner"


def _demote_enabled_for(pairing_id: int) -> bool:
    """True only when `pairing_id` is explicitly listed in `ROLE_SYNC_DEMOTE_PAIRING_IDS`."""
    raw = os.environ.get(DEMOTE_PAIRING_IDS_ENV, "")
    return str(pairing_id) in {tok.strip() for tok in raw.split(",") if tok.strip()}


def _demote_baseline_role() -> str:
    """Configured demote target (never empty)."""
    return os.environ.get(DEMOTE_BASELINE_ROLE_ENV, "").strip() or _DEFAULT_DEMOTE_BASELINE_ROLE


#: `guild_tenant_pairings.direction` values that run `reconcile_pairing()` (Twitch ->
#: Discord, subscriber_tier/moderator bindings).
_TWITCH_TO_DISCORD_DIRECTIONS = frozenset({"twitch_to_discord", "bidirectional"})
#: `guild_tenant_pairings.direction` values that run `reconcile_pairing_discord_to_
#: platform()` (Discord -> this platform's community role, community_role bindings).
_DISCORD_TO_PLATFORM_DIRECTIONS = frozenset({"discord_to_twitch", "bidirectional"})

#: PostHog flag gating this entire engine -- defaulted OFF until validated (critical-rules.md).
FEATURE_ROLE_SYNC = "waddles.role_sync"

_meter = get_meter()
_roles_added_counter = _meter.create_counter(
    "waddles_role_sync_roles_added_total", description="Discord roles added by role-sync"
)
_roles_removed_counter = _meter.create_counter(
    "waddles_role_sync_roles_removed_total", description="Discord roles removed by role-sync"
)
_community_roles_applied_counter = _meter.create_counter(
    "waddles_role_sync_community_roles_applied_total",
    description="hub-platform community_members.role changes applied by role-sync "
    "(Discord -> platform direction)",
)
_unverified_pairings_counter = _meter.create_counter(
    "waddles_role_sync_unverified_pairings_total",
    description="reconcile passes over grandfathered pre-0104 pairings with no ownership proof",
)
_sync_errors_counter = _meter.create_counter(
    "waddles_role_sync_errors_total", description="role-sync pairing failures, fail-closed"
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


@dataclass(slots=True, frozen=True)
class GuildMemberSnapshot:
    """Member read + whether pagination provably reached the end (`complete`)."""

    members: list[DiscordGuildMember]
    complete: bool


@dataclass(slots=True, frozen=True)
class DiscordGuildRoleInfo:
    """One `GET /guilds/{id}/roles` row reduced to what the mirror's privilege re-check needs."""

    role_id: str
    permissions: int
    managed: bool


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
        self,
        http_client: httpx.AsyncClient,
        *,
        bot_token: str,
        api_base: str = DISCORD_API_BASE,
        rate_limit_retries: int = 0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind to a shared `httpx.AsyncClient` + this pairing's tenant's resolved bot token.

        `rate_limit_retries` > 0 makes a 429 wait `Retry-After` (capped) and retry that many
        times before surfacing the usual `DiscordSyncError`; 0 (default) keeps fail-fast.
        """
        self._http = http_client
        self._bot_token = bot_token
        self._api_base = api_base
        self._rate_limit_retries = rate_limit_retries
        self._sleep = sleep

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue one request, honouring Discord's 429 `Retry-After` up to the retry budget."""
        attempt = 0
        while True:
            response = await self._http.request(method, url, **kwargs)
            if response.status_code != 429 or attempt >= self._rate_limit_retries:
                return response
            attempt += 1
            try:
                wait = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                wait = 1.0
            await self._sleep(min(max(wait, 0.0), _MAX_RETRY_AFTER_SECONDS))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._bot_token}"}

    async def get_member_role_ids(self, *, guild_id: str, user_id: str) -> set[str] | None:
        """`GET /guilds/{guild_id}/members/{user_id}` -- `None` if not a guild member (404)."""
        try:
            response = await self._send(
                "GET",
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
            response = await self._send(
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

    async def get_guild_roles(self, *, guild_id: str) -> list[DiscordGuildRoleInfo]:
        """`GET /guilds/{guild_id}/roles` -- id/permissions/managed per role."""
        try:
            response = await self._send(
                "GET",
                f"{self._api_base}/guilds/{guild_id}/roles",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordSyncError(f"discord guild role list failed: {exc}") from exc
        _classify_discord(response, action="guild role list")
        try:
            return [
                DiscordGuildRoleInfo(
                    role_id=str(r["id"]),
                    permissions=int(r.get("permissions", 0) or 0),
                    managed=bool(r.get("managed", False)),
                )
                for r in response.json() or []
            ]
        except (ValueError, TypeError, KeyError) as exc:
            raise DiscordSyncError("discord returned a malformed roles payload") from exc

    async def list_guild_members(self, *, guild_id: str) -> list[DiscordGuildMember]:
        """Members only -- grant-only callers; see `list_guild_members_checked` for completeness."""
        return (await self.list_guild_members_checked(guild_id=guild_id)).members

    async def list_guild_members_checked(self, *, guild_id: str) -> GuildMemberSnapshot:
        """`GET /guilds/{guild_id}/members`, paginated by snowflake `after` cursor (max 1000/page).

        Returns the members PLUS an explicit `complete` flag: True only when the
        cursor loop ended on a short/empty page (Discord has no more rows). Hitting
        `_MAX_PAGES`, an unusable cursor, or a row without a user id => `complete=False`
        (UNKNOWN), which the demote pass treats as "demote nobody". HTTP/transport
        errors raise `DiscordSyncError` as before.
        """
        members: list[DiscordGuildMember] = []
        after: str | None = None
        complete = False
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {"limit": str(_DISCORD_MEMBER_PAGE_SIZE)}
            if after:
                params["after"] = after
            try:
                response = await self._send(
                    "GET",
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
                complete = True
                break
            malformed = False
            for row in page:
                user = row.get("user") or {}
                user_id = user.get("id")
                if not user_id:
                    malformed = True
                    continue
                members.append(
                    DiscordGuildMember(
                        user_id=str(user_id),
                        role_ids=frozenset(str(r) for r in row.get("roles") or []),
                    )
                )
            if malformed:
                break  # a row we could not read => state unknown
            if len(page) < _DISCORD_MEMBER_PAGE_SIZE:
                complete = True
                break
            after = str(page[-1].get("user", {}).get("id") or "")
            if not after:
                break
        return GuildMemberSnapshot(members=members, complete=complete)


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
    community_roles_demoted: int = 0


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
    mirror_mappings_examined: int = 0
    mirror_mappings_failed: int = 0
    mirror_roles_added: int = 0
    mirror_roles_removed: int = 0
    mirror_skipped_not_member: int = 0


def _list_enabled_pairings(dal: Any) -> list[Any]:
    """Every `guild_tenant_pairings` row with `sync_enabled=True`, any direction.

    Direction filtering happens in the caller (not this query) so a
    skipped-direction pairing is still counted, not silently invisible.
    """
    bind_guild_pairing_tables(dal)
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
    minimal sqlite schema (see `conftest.py::guild_pairing_db`) actually has
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


#: Grant-only ordering. A sync grant may only RAISE a member along this ladder; it never lowers
#: or sideways-overwrites. Baseline roles (rank 0) are the only roles sync may replace without
#: having granted them itself.
_ROLE_RANK: dict[str, int] = {
    "member": 0,
    "community-member": 0,
    "vip": 1,
    "moderator": 2,
    "community-admin": 3,
    _OWNER_ROLE: 4,
}
_BASELINE_ROLES = frozenset({"member", "community-member"})


def _active_member(dal: Any, community_id: int, hub_user_id: int) -> Any | None:
    """The active `community_members` row for this hub user, or None."""
    bind_auth_tables(dal)
    return (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal idiom
        )
        .select()
        .first()
    )


def _community_grant(dal: Any, community_id: int, hub_user_id: int) -> Any | None:
    """This member's `role_sync_community_grants` ledger row, or None."""
    bind_guild_pairing_tables(dal)
    g = dal.role_sync_community_grants
    return dal((g.community_id == community_id) & (g.hub_user_id == hub_user_id)).select().first()


def _set_member_role(dal: Any, member: Any, community_id: int, role: str) -> None:
    """Write `role` (+ its `community_role_id`) and reset the claims cache; caller commits."""
    role_row = (
        dal((dal.community_roles.community_id == community_id) & (dal.community_roles.name == role))
        .select()
        .first()
    )
    dal(dal.community_members.id == member.id).update(
        role=role,
        community_role_id=int(role_row.id) if role_row is not None else None,
        claims_cache=None,
        updated_at=datetime.now(UTC),
    )


def _apply_community_role(
    dal: Any,
    *,
    community_id: int,
    hub_user_id: int,
    desired_role: str,
    pairing_id: int,
) -> str:
    """Grant-only: raise an active member to `desired_role` and record it in the grant ledger.

    Never lowers a role and never overwrites one an admin set: a member whose current role is
    above baseline is only touched when the ledger shows role-sync itself granted that role.
    Privileged targets are refused even if a binding somehow names one (DB CHECK is the other
    guard). Returns `"applied"` / `"unchanged"` / `"not_a_member"` / `"owner_protected"` /
    `"privileged_refused"` / `"admin_set_untouched"` / `"no_downgrade"`.
    """
    member = _active_member(dal, community_id, hub_user_id)
    if member is None:
        return "not_a_member"
    current = str(member.role)
    if current == _OWNER_ROLE:
        return "owner_protected"
    if desired_role not in INBOUND_COMMUNITY_ROLES:
        logger.error(
            "role_sync.privileged_target_refused community_id=%s pairing_id=%s",
            community_id,
            pairing_id,
        )
        return "privileged_refused"
    if current == desired_role:
        return "unchanged"
    grant = _community_grant(dal, community_id, hub_user_id)
    sync_owned = grant is not None and grant.granted_role == current
    if current not in _BASELINE_ROLES and not sync_owned:
        return "admin_set_untouched"
    if _ROLE_RANK.get(desired_role, 0) <= _ROLE_RANK.get(current, 0):
        return "no_downgrade"

    prior = grant.prior_role if sync_owned and grant is not None else current
    g = dal.role_sync_community_grants
    try:
        _set_member_role(dal, member, community_id, desired_role)
        if grant is not None:
            dal(g.id == grant.id).update(
                pairing_id=pairing_id,
                granted_role=desired_role,
                prior_role=prior,
                granted_at=datetime.now(UTC),
            )
        else:
            g.insert(
                pairing_id=pairing_id,
                community_id=community_id,
                hub_user_id=hub_user_id,
                granted_role=desired_role,
                prior_role=prior,
                granted_at=datetime.now(UTC),
            )
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return "applied"


def _revoke_community_grant(
    dal: Any, *, community_id: int, hub_user_id: int, pairing_id: int, baseline_role: str
) -> str:
    """Undo ONE role-sync grant: restore `prior_role` (else baseline) and drop the ledger row.

    Only a grant this pairing recorded, still matching the member's current role, is undone; a
    role an admin changed since is left alone (the stale ledger row is dropped).
    Returns `"applied"` / `"not_owned"` / `"not_a_member"`.
    """
    grant = _community_grant(dal, community_id, hub_user_id)
    if grant is None or int(grant.pairing_id) != pairing_id:
        return "not_owned"
    g = dal.role_sync_community_grants
    member = _active_member(dal, community_id, hub_user_id)
    try:
        if member is None or str(member.role) != grant.granted_role:
            dal(g.id == grant.id).delete()
            dal.commit()
            return "not_a_member" if member is None else "not_owned"
        restore = grant.prior_role or baseline_role
        if _ROLE_RANK.get(restore, 0) > _ROLE_RANK.get(grant.granted_role, 0):
            restore = baseline_role  # defensive: a revoke never raises anyone
        _set_member_role(dal, member, community_id, restore)
        dal(g.id == grant.id).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return "applied"


def _run_demote_pass(
    dal: Any,
    *,
    community_id: int,
    pairing_id: int,
    candidates: list[int],
    managed_roles: frozenset[str],
    baseline_role: str,
    member_count: int,
    member_read_complete: bool,
    linked_managed_holders: int,
) -> int:
    """Guarded revoke of THIS pairing's own grants for members who lost every mapped role.

    Aborts (revokes nothing) when the member read is empty or not provably complete, the baseline
    role is missing, or the circuit breaker trips. Only ledger-recorded grants are revoked
    (`_revoke_community_grant`); roles set by an admin are never touched.
    """
    if not candidates:
        return 0
    if member_count == 0 or not member_read_complete:
        logger.warning(
            "role_sync.demote_aborted_unknown_member_read pairing_id=%s members=%d complete=%s",
            pairing_id,
            member_count,
            member_read_complete,
        )
        return 0
    bind_auth_tables(dal)
    has_baseline = (
        dal(
            (dal.community_roles.community_id == community_id)
            & (dal.community_roles.name == baseline_role)
        )
        .select()
        .first()
    )
    if has_baseline is None:
        logger.warning(
            "role_sync.demote_aborted_baseline_missing pairing_id=%s baseline=%s",
            pairing_id,
            baseline_role,
        )
        return 0
    would_demote = []
    for uid in candidates:
        grant = _community_grant(dal, community_id, uid)
        if (
            grant is not None
            and int(grant.pairing_id) == pairing_id
            and grant.granted_role in managed_roles
            and _current_role(dal, community_id, uid) == grant.granted_role
        ):
            would_demote.append(uid)
    population = len(would_demote) + linked_managed_holders
    if (
        len(would_demote) > _DEMOTE_ABS_FLOOR
        and len(would_demote) > population * _DEMOTE_MAX_FRACTION
    ):
        logger.error(
            "role_sync.demote_aborted_circuit_breaker pairing_id=%s would_demote=%d population=%d",
            pairing_id,
            len(would_demote),
            population,
        )
        return 0
    demoted = 0
    for uid in would_demote:
        if (
            _revoke_community_grant(
                dal,
                community_id=community_id,
                hub_user_id=uid,
                pairing_id=pairing_id,
                baseline_role=baseline_role,
            )
            == "applied"
        ):
            demoted += 1
    logger.info(
        "role_sync.demote_pass pairing_id=%s community_id=%s demoted=%d",
        pairing_id,
        community_id,
        demoted,
    )
    return demoted


def _current_role(dal: Any, community_id: int, hub_user_id: int) -> str | None:
    """Active member's current `community_members.role`, or None."""
    bind_auth_tables(dal)
    row = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal idiom
        )
        .select()
        .first()
    )
    return None if row is None else str(row.role)


def _warn_if_grandfathered(pairing: Any) -> None:
    """WARN + count a pre-0104 pairing that never passed the guild-ownership proof (still runs)."""
    if getattr(pairing, "ownership_verified_at", None) is None:
        logger.warning(
            "role_sync.pairing_ownership_unverified pairing_id=%s -- grandfathered",
            pairing.id,
        )
        _unverified_pairings_counter.add(1)


def _discord_grants(dal: Any, pairing_id: int) -> dict[str, set[str]]:
    """`{discord_user_id: {role_id}}` this pairing's Twitch -> Discord pass has granted."""
    bind_guild_pairing_tables(dal)
    g = dal.role_sync_discord_grants
    out: dict[str, set[str]] = {}
    for row in dal(g.pairing_id == pairing_id).select():
        out.setdefault(str(row.platform_user_id), set()).add(str(row.discord_role_id))
    return out


def _record_discord_grant(dal: Any, pairing_id: int, role_id: str, user_id: str) -> None:
    """Ledger a Discord role this pass added (idempotent)."""
    g = dal.role_sync_discord_grants
    q = (
        (g.pairing_id == pairing_id)
        & (g.discord_role_id == role_id)
        & (g.platform_user_id == user_id)
    )
    try:
        if dal(q).count() == 0:
            g.insert(
                pairing_id=pairing_id,
                discord_role_id=role_id,
                platform="discord",
                platform_user_id=user_id,
                granted_at=datetime.now(UTC),
            )
        dal.commit()
    except Exception:
        dal.rollback()
        raise


def _drop_discord_grant(dal: Any, pairing_id: int, role_id: str, user_id: str) -> None:
    """Forget a ledgered Discord grant once removed (or already gone)."""
    g = dal.role_sync_discord_grants
    try:
        dal(
            (g.pairing_id == pairing_id)
            & (g.discord_role_id == role_id)
            & (g.platform_user_id == user_id)
        ).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise


async def _flag_enabled(tenant_slug: str) -> bool:
    """`feature_enabled(FEATURE_ROLE_SYNC, tenant=...)`, defaulted OFF.

    `feature_enabled` itself already degrades to a cached/default value on
    a PostHog/license-server outage (never raises) -- this wrapper exists
    only so a test environment without `flask_core` installed (see this
    module's `ImportError` guard above) treats the flag as OFF rather than
    crashing on import.
    """
    if feature_enabled is None:  # pragma: no cover -- only in a flask_core-less environment
        return False
    return bool(await feature_enabled(FEATURE_ROLE_SYNC, tenant=tenant_slug))


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
        # Moderator propagation is OPT-IN per pairing (one platform's moderator must not affect
        # the community by default); Twitch mods are not even fetched unless enabled.
        propagate_mods = bool(getattr(pairing, "propagate_moderators", False))
        mods: set[str] = (
            await twitch_client.list_moderators(
                broadcaster_id=broadcaster_id, user_token=user_token, client_id=client_id
            )
            if propagate_mods
            else set()
        )

        bindings = list_bindings(dal, community_id, pairing_id)
        tier_role: dict[int, str] = {
            b.subscriber_tier: b.discord_role_id
            for b in bindings
            if b.sync_scope == "subscriber_tier" and b.subscriber_tier is not None
        }
        mod_role = (
            next((b.discord_role_id for b in bindings if b.sync_scope == "moderator"), None)
            if propagate_mods
            else None
        )
        managed_role_ids = set(tier_role.values()) | ({mod_role} if mod_role else set())

        if not managed_role_ids:
            return PairingSyncResult(pairing_id, community_id, 0, 0, 0, None)

        discord_client = make_discord_client(bot_token)
        # Re-check privilege at run time: a bound role may have gained admin perms since binding.
        get_roles = getattr(discord_client, "get_guild_roles", None)
        if get_roles is not None:
            from services.discord_guild_roles import is_privileged_role  # noqa: PLC0415 - cycle

            privileged_ids = {
                r.role_id
                for r in await get_roles(guild_id=guild_id)
                if is_privileged_role(r.permissions, r.managed)
            }
            refused = managed_role_ids & privileged_ids
            if refused:
                logger.error(
                    "role_sync.privileged_binding_skipped pairing_id=%s count=%d",
                    pairing_id,
                    len(refused),
                )
                managed_role_ids -= refused
                tier_role = {t: r for t, r in tier_role.items() if r not in refused}
                if mod_role in refused:
                    mod_role = None

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

        # Removal is ledger-only: a role is removed solely when THIS pairing granted it, so a
        # role an admin assigned by hand in Discord is never taken away.
        ledger = _discord_grants(dal, pairing_id)
        roles_added = 0
        roles_removed = 0
        for discord_user_id in set(desired_by_discord_user) | set(ledger):
            desired_roles = desired_by_discord_user.get(discord_user_id, set())
            current = await discord_client.get_member_role_ids(
                guild_id=guild_id, user_id=discord_user_id
            )
            if current is None:
                continue  # not a guild member -- nothing to sync for them this pass
            owned = ledger.get(discord_user_id, set())
            for role_id in desired_roles - current:
                if await discord_client.add_role(
                    guild_id=guild_id, user_id=discord_user_id, role_id=role_id
                ):
                    roles_added += 1
                    _record_discord_grant(dal, pairing_id, role_id, discord_user_id)
            for role_id in owned - desired_roles:
                if role_id in current and await discord_client.remove_role(
                    guild_id=guild_id, user_id=discord_user_id, role_id=role_id
                ):
                    roles_removed += 1
                _drop_discord_grant(dal, pairing_id, role_id, discord_user_id)

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
        demote_on = _demote_enabled_for(pairing_id)
        member_read_complete = False
        checked = getattr(discord_client, "list_guild_members_checked", None)
        if demote_on and checked is not None:
            snapshot = await checked(guild_id=guild_id)
            members = snapshot.members
            member_read_complete = bool(snapshot.complete)
        else:
            members = await discord_client.list_guild_members(guild_id=guild_id)
        role_priority = _community_role_priorities(dal, community_id)
        demote_candidates: list[int] = []
        linked_managed_holders = 0
        managed_roles = frozenset(role_by_discord_role_id.values())

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
                # Guard 1: this member WAS returned by Discord and holds none of the mapped
                # roles. Only collected (never applied here) -- guards 2/3 run after the loop.
                if demote_on:
                    absent_user = _find_linked_hub_user_by_discord(dal, member.user_id)
                    if absent_user is not None:
                        demote_candidates.append(absent_user)
                continue

            hub_user_id = _find_linked_hub_user_by_discord(dal, member.user_id)
            if hub_user_id is None:
                skipped_unlinked += 1
                continue

            linked_managed_holders += 1
            outcome = _apply_community_role(
                dal,
                community_id=community_id,
                hub_user_id=hub_user_id,
                desired_role=desired_role,
                pairing_id=pairing_id,
            )
            if outcome == "applied":
                applied += 1
            elif outcome == "not_a_member":
                skipped_not_member += 1
            elif outcome == "owner_protected":
                skipped_owner_protected += 1
            # "unchanged" -- already correct, not counted as a skip or an error.

        demoted = 0
        if demote_on:
            demoted = _run_demote_pass(
                dal,
                community_id=community_id,
                pairing_id=pairing_id,
                candidates=demote_candidates,
                managed_roles=managed_roles,
                baseline_role=_demote_baseline_role(),
                member_count=len(members),
                member_read_complete=member_read_complete,
                linked_managed_holders=linked_managed_holders,
            )

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
            demoted,
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
            return HttpDiscordRoleTargetClient(
                bound_http_client, bot_token=bot_token, rate_limit_retries=2
            )

    summary = ReconcileSummary()
    try:
        pairings = _list_enabled_pairings(dal)
        summary.pairings_examined = len(pairings)
        for pairing in pairings:
            _warn_if_grandfathered(pairing)
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
            async with bundle_span("role_sync.pairing", pairing_id=int(pairing.id)):
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

        # Cross-guild Discord role mirror (migration 0099): independent of pairing direction.
        # Imported lazily -- the mirror module imports this one.
        from services.discord_role_mirror import reconcile_role_mirrors

        try:
            mirror = await reconcile_role_mirrors(
                dal,
                credential_resolver=credential_resolver,
                make_discord_client=make_discord_client,
            )
        except Exception as exc:  # noqa: BLE001 - mirror failure must not mask the pairing summary
            logger.error("role_mirror.batch_failed error_type=%s", type(exc).__name__)
            _sync_errors_counter.add(1)
            summary.mirror_mappings_failed += 1
        else:
            summary.mirror_mappings_examined = mirror.mappings_examined
            summary.mirror_mappings_failed = mirror.mappings_failed
            summary.mirror_roles_added = mirror.roles_added
            summary.mirror_roles_removed = mirror.roles_removed
            summary.mirror_skipped_not_member = mirror.skipped_not_member
        return summary
    finally:
        if owns_http_client:
            await bound_http_client.aclose()


async def main() -> int:
    """CronJob entrypoint: one reconcile pass, denominators printed, never a silent zero.

    The engine queries the RAW pydal DAL (`async_dal.dal`) and token lookup takes the
    flask_core `AsyncDAL` itself (`get_decrypted_tokens` calls `.dal` + `.select_async`) --
    exactly the objects hub-api builds in `app.py::startup`. Neither is a penguin-dal
    `AsyncDB`: that has no `.dal`, and reading one raises `TableNotFoundError: Table 'dal'
    not found` (see `services/sync_worker_dal.py`).
    """
    from services.community_connections import SYSTEM, get_decrypted_tokens

    with open_worker_dal(os.environ["DATABASE_URL"]) as async_dal:

        async def _get_broadcaster_user_token(community_id: int) -> str | None:
            # CronJob iterating communities: no request tenant -> explicit SYSTEM scope (tenant is
            # resolved from the community row and passed to SQL; SQL itself never accepts NULL).
            tokens = await get_decrypted_tokens(async_dal, community_id, "twitch", tenant_id=SYSTEM)
            return tokens.access_token if tokens else None

        summary = await run_role_sync_reconcile_batch(
            async_dal.dal, get_broadcaster_user_token=_get_broadcaster_user_token
        )
    print(
        f"role_sync: pairings_examined={summary.pairings_examined} "
        f"pairings_synced={summary.pairings_synced} "
        f"pairings_failed={summary.pairings_failed} "
        f"pairings_skipped_wrong_direction={summary.pairings_skipped_wrong_direction} "
        f"roles_added={summary.roles_added} roles_removed={summary.roles_removed} "
        f"community_roles_applied={summary.community_roles_applied} "
        f"mirror_mappings={summary.mirror_mappings_examined} "
        f"mirror_failed={summary.mirror_mappings_failed} "
        f"mirror_added={summary.mirror_roles_added} "
        f"mirror_removed={summary.mirror_roles_removed} "
        f"mirror_skipped_not_member={summary.mirror_skipped_not_member}"
    )
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
