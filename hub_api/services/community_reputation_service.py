"""Community reputation *read* service -- score + tier visibility (gh-310).

Backs `blueprints/v1/community_reputation.py`'s two member-facing routes:
the caller's own `(community score, tenant score)` snapshot
(`GET .../reputation/me`) and a community's top-N reputation leaderboard
(`GET .../reputation/leaderboard`). Read-only -- SELECTs only, never a
write; reputation is written exclusively by
`core/reputation_module/services/reputation_service.py::adjust()` (the
`!rep`/`!reputation` write path) and the M3 Platform-admin group's
`services/admin_service.py::adjust_reputation()` (neither touched here).

Two tables, two different provenance:

- `community_members.reputation` -- bound via `services.schema.
  bind_auth_tables()` (the M1 binder every other service module touching
  this table already calls: `admin_service.py`, `community_loyalty.py`'s
  own test fixtures), reused here rather than a second local
  `define_table()` call, which pydal rejects outright for an
  already-bound table name.
- `reputation_tenant.score` -- migration `097_reputation_tenant_scope.sql`
  (re-scoped from the original, cross-tenant `reputation_global` added by
  `080_add_reputation_tables.sql` -- security.md Tenant Isolation: a
  reputation aggregate must never span tenants); no existing hub-api
  binder touches this table (`core/reputation_module` owns it), so
  `_ensure_reputation_tables()` below defines it locally, guarded the same
  `dal.tables` membership-check way `services/community_common.py::
  ensure_community_tables()` does. No auto `id` column -- `(tenant_id,
  hub_user_id)` is the real table's own composite PRIMARY KEY
  (`primarykey=["tenant_id", "hub_user_id"]`).

**Known DB-grants gap (flagged, not fixed here -- out of this module's
edit scope):** migration 097's `GRANT SELECT, INSERT, UPDATE ON
reputation_tenant` targets `mod_core_reputation` only; no migration
grants hub-api's own scoped DB role SELECT on this table. A production
Postgres deployment enforcing those per-service grants (security.md
Per-Service Database Accounts) will see this service 42501 on its first
`reputation_tenant` read until that grant is added -- needs a follow-up
migration, tracked separately. Sqlite-backed tests (no grants concept)
don't catch this.

**Tier table** -- `flask_core.reputation_tiers.REPUTATION_TIERS` /
`.reputation_tier()` is the single shared source for this FICO-style
(300-850) table, used identically by this module and by
`core/svc_process/bundles/community_reputation_process.py`'s `!rep` reply
(both processes already depend on `flask_core`; previously each kept a
hand-mirrored copy guarded only by a source-parsing drift test -- see
`core/reputation_module/config.py`'s `Config.REPUTATION_TIERS`, a
*different*, unrelated {min,max,label} table this module does not touch).
The `0 -> 300`, `1000 -> 850` rescale this table encodes (`new = 300 +
old * 0.55`) is historical context for gh-310's original 0-1000-scale
brief; see `flask_core/reputation_tiers.py` for the table itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask_core.reputation_tiers import REPUTATION_TIERS, reputation_tier
from pydal import Field

from services.schema import bind_auth_tables

__all__ = [
    "REPUTATION_TIERS",
    "reputation_tier",
    "MyReputationDTO",
    "LeaderboardEntryDTO",
    "get_my_reputation",
    "get_leaderboard",
]

#: FICO-style baseline every score defaults to when no row exists --
#: matches `reputation_tenant.score`'s own DB column default (migration
#: 097) and `community_members.reputation`'s (`bind_auth_tables()`).
_DEFAULT_SCORE = 600

#: Hard ceiling for `get_leaderboard()`'s `limit` -- matches
#: `services/pagination.py::DEFAULT_MAX_PAGE_SIZE` / `community_loyalty.
#: py::_MAX_LEADERBOARD_LIMIT`'s identical convention.
_MAX_LEADERBOARD_LIMIT = 100


@dataclass(slots=True, frozen=True)
class MyReputationDTO:
    """`GET .../reputation/me` response payload -- both scoring tiers, each with a label."""

    community_score: int
    community_tier: str
    tenant_score: int
    tenant_tier: str
    total_events: int
    last_event_at: str | None


@dataclass(slots=True, frozen=True)
class LeaderboardEntryDTO:
    """One `GET .../reputation/leaderboard` row -- display name only, no ids/emails."""

    display_name: str
    score: int
    tier: str


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _ensure_reputation_tables(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently bind `community_members` (reused) + `reputation_tenant` (new, local).

    `bind_auth_tables()` no-ops after its own first call on this `dal`
    instance (see that function's docstring) -- safe to call unconditionally
    every request, matching every other service module's `_ensure_tables()`
    convention. `migrate` defaults to `False` (production: schema owned by
    `config/postgres/migrations/097_reputation_tenant_scope.sql`, this
    process never runs DDL); tests pass `migrate=True` against a throwaway
    `sqlite:memory`/file DAL, same convention every `bind_*` function in
    `services/schema.py` follows.
    """
    bind_auth_tables(dal, migrate=migrate)
    if "reputation_tenant" not in dal.tables:
        dal.define_table(
            "reputation_tenant",
            Field("tenant_id", "integer", notnull=True),
            Field("hub_user_id", "integer", notnull=True),
            Field("score", "integer", default=_DEFAULT_SCORE),
            Field("total_events", "integer", default=0),
            Field("last_event_at", "datetime"),
            Field("created_at", "datetime"),
            Field("updated_at", "datetime"),
            primarykey=["tenant_id", "hub_user_id"],
            migrate=migrate,
        )


async def get_my_reputation(
    async_dal: Any, dal: Any, *, community_id: int, hub_user_id: int, tenant_id: int
) -> MyReputationDTO:
    """Caller's own community + tenant reputation, each defaulted to 600 (baseline) if unset.

    `tenant_id` MUST come from the caller's own validated `TenantContext`
    (`flask_core.tenancy.get_tenant_context`) -- never client-supplied
    (security.md Tenant Isolation). A member never linked to a
    `community_members` row yet (brand new) looks identical to "everyone
    starts at 600" -- never an error, same convention
    `community_reputation_process.py`'s `!rep` reply follows.
    """
    _ensure_reputation_tables(dal)

    member_rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
        )
    )
    member = member_rows.first() if member_rows else None
    community_score = (
        int(member.reputation)
        if member is not None and member.reputation is not None
        else _DEFAULT_SCORE
    )

    tenant_rows = await async_dal.select_async(
        dal(
            (dal.reputation_tenant.tenant_id == tenant_id)
            & (dal.reputation_tenant.hub_user_id == hub_user_id)
        )
    )
    tenant_row = tenant_rows.first() if tenant_rows else None
    tenant_score = (
        int(tenant_row.score)
        if tenant_row is not None and tenant_row.score is not None
        else _DEFAULT_SCORE
    )
    total_events = int(tenant_row.total_events) if tenant_row is not None else 0
    last_event_at = _iso(tenant_row.last_event_at) if tenant_row is not None else None

    return MyReputationDTO(
        community_score=community_score,
        community_tier=reputation_tier(community_score),
        tenant_score=tenant_score,
        tenant_tier=reputation_tier(tenant_score),
        total_events=total_events,
        last_event_at=last_event_at,
    )


async def get_leaderboard(
    async_dal: Any, dal: Any, *, community_id: int, limit: int = 10
) -> list[LeaderboardEntryDTO]:
    """Top `limit` community members by `reputation`, highest first, active members only.

    Display-name + score + tier only (security.md PII Tokenization / this
    feature's own "no ids/emails" brief) -- never `user_id`/
    `platform_user_id`.
    """
    _ensure_reputation_tables(dal)
    clamped_limit = max(1, min(int(limit), _MAX_LEADERBOARD_LIMIT))
    rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal idiom
        ),
        orderby=~dal.community_members.reputation,
        limitby=(0, clamped_limit),
    )
    entries = []
    for row in rows:
        score = int(row.reputation) if row.reputation is not None else _DEFAULT_SCORE
        entries.append(
            LeaderboardEntryDTO(
                display_name=row.display_name or "unknown",
                score=score,
                tier=reputation_tier(score),
            )
        )
    return entries
