"""Ban-evasion suppression: keep a keyed code for an erased BANNED user, nothing for anyone else.

GDPR Art. 17 erasure (`services/data_privacy_service.py`) used to keep every erased
user's reputation and platform-identity link. That is replaced by *suppression only*:

* **No active restriction** -> everything is deleted, reputation included. No tombstone,
  no retained score. A benign user who returns gets a fresh start.
* **Active restriction (a ban)** -> exactly one `suppression_records` row per
  (tenant, platform identity) is written in the SAME transaction, THEN all PII and
  reputation is deleted exactly as above. The row holds a per-tenant keyed one-way code
  (`services/suppression_keys.py`), a closed restriction-type enum and an expiry -- no
  person identifier, no offence detail (Art. 9/10).
* **On return** the same code is recomputed from the incoming platform id; a non-expired
  match re-applies the restriction (`reapply_for_hub_user`, `check_and_ban_membership`).
* **Per tenant only.** Every query here is `tenant_id`-scoped and the tenant key is
  derived per tenant, so a code written for tenant A can never match in tenant B
  (Waddles is a processor per tenant; a cross-tenant list would make it a controller).

Lawful basis is Art. 6(1)(f) legitimate interest -- NOT an Art. 17(3) exemption. The
pseudonymised code is still personal data: it is deleted at expiry
(`purge_expired`) or when a moderator lifts the restriction (`lift_suppression`), and the
data subject keeps the right to object / human review. Counsel to verify; see
`docs/compliance/suppression-LIA.md`.

**What counts as an active restriction.** Waddles persists exactly one such state: a
`community_members` row whose `role` is `banned` (written by the reputation policy
enforcer's auto-ban). Platform-side timeouts / mutes (`moderation_enforce_action`) are
enforced on the platform and never stored here, so there is nothing to carry. A ban of
a hub-linked member suppresses every identity linked to that hub user; a ban of a
platform-keyed row (no hub link) suppresses only that platform account.

**Fail-loud, key only when needed.** Recording needs the master key; a missing key
raises (and rolls the whole erasure back) rather than erasing without suppressing. A
lookup first asks whether the tenant has any live row at all -- if not, no key is touched
and nothing can fail; if it does and the key is missing, the check raises (membership
creation fails closed) instead of silently letting a banned user in.

Nothing here logs, traces or labels a platform id or a code: only tenant id, platform
name, restriction enum and counts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, TypeVar

from opentelemetry import trace

from services.bundle_telemetry import get_meter, get_tracer
from services.schema import bind_erasure_reputation_tables, bind_suppression_tables
from services.suppression_keys import (
    PlatformKekKeySource,
    SuppressionInputError,
    SuppressionKeySource,
    current_code,
    lookup_codes,
    normalize_identity,
    validate_tenant_id,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: `community_members.role` value the reputation policy enforcer writes on an auto-ban.
BANNED_ROLE: Final = "banned"

#: Optional ceiling (days) on how long ANY suppression row lives, applied when a row is written.
#: Unset = a ban with no expiry is kept until lifted (the design default); set it to bound
#: retention for storage-limitation (Art. 5(1)(e)) -- see the LIA.
ENV_MAX_RETENTION_DAYS: Final = "WADDLES_SUPPRESSION_MAX_RETENTION_DAYS"
_MAX_RETENTION_CEILING_DAYS: Final = 3650

#: How often the in-process loop deletes expired rows (lookups ignore expired rows regardless).
DEFAULT_PURGE_INTERVAL_SECONDS: Final = 3600.0

Identity = tuple[str, str]


class RestrictionReason(StrEnum):
    """The closed vocabulary of restriction types -- mirrors the `reason_enum` CHECK constraint.

    No free text and no offence detail: widening this set is a migration (a reviewed change).
    """

    BAN = "ban"
    AUTO_BAN = "auto_ban"


class SuppressionConfigError(RuntimeError):
    """A suppression setting is malformed. Fails loudly; never falls back to a default."""


@dataclass(slots=True, frozen=True)
class ActiveRestriction:
    """One active restriction found at erasure time; held in memory only, never persisted raw."""

    tenant_id: int
    platform: str
    platform_user_id: str = field(repr=False)
    reason: RestrictionReason
    expires_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class SuppressionMatch:
    """A non-expired suppression row matched an incoming identity."""

    tenant_id: int
    reason: RestrictionReason
    expires_at: datetime | None


@dataclass(slots=True, frozen=True)
class _Instruments:
    """The OTel instruments suppression reports through (created lazily, once)."""

    events: Any
    check_ms: Any
    purged: Any


_INSTRUMENTS: _Instruments | None = None


def _instruments() -> _Instruments:
    """Return the suppression counters/histogram, creating them on first use.

    Lazy so a deployment's / test's MeterProvider is the one they bind to. Labels are
    `op` / `result` / `reason` only -- never a tenant-scoped id, platform id or code (PII).
    """
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        meter = get_meter()
        _INSTRUMENTS = _Instruments(
            events=meter.create_counter(
                "waddles_hub_suppression_events_total",
                description="ban-suppression operations, by op and result",
            ),
            check_ms=meter.create_histogram(
                "waddles_hub_suppression_check_duration_ms",
                unit="ms",
                description="wall time of one return-time suppression check",
            ),
            purged=meter.create_counter(
                "waddles_hub_suppression_purged_total",
                description="expired suppression rows deleted",
            ),
        )
    return _INSTRUMENTS


def _emit(op: str, result: str, *, reason: str | None = None, started: float | None = None) -> None:
    """Emit one event (and the check latency); telemetry failure is logged, never raised."""
    try:
        instruments = _instruments()
        labels = {"op": op, "result": result}
        if reason is not None:
            labels["reason"] = reason
        instruments.events.add(1, labels)
        if started is not None:
            instruments.check_ms.record((time.perf_counter() - started) * 1000.0, {"op": op})
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        logger.exception("suppression.metrics_failed")


def max_retention_days(environ: Mapping[str, str] | None = None) -> int | None:
    """Parse the optional retention ceiling; unset -> None (until lifted), malformed -> raise."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_MAX_RETENTION_DAYS, "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError as exc:
        raise SuppressionConfigError(f"{ENV_MAX_RETENTION_DAYS} must be an integer") from exc
    if not 1 <= days <= _MAX_RETENTION_CEILING_DAYS:
        raise SuppressionConfigError(
            f"{ENV_MAX_RETENTION_DAYS} must be between 1 and {_MAX_RETENTION_CEILING_DAYS}"
        )
    return days


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp (sqlite / `timestamp`) as UTC so comparisons never mix kinds."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    """The later of two expiries, where NULL (until lifted) outranks every date."""
    if a is None or b is None:
        return None
    return a if _as_utc(a) >= _as_utc(b) else b


def _stronger(a: RestrictionReason, b: RestrictionReason) -> RestrictionReason:
    """A human-issued BAN outranks an AUTO_BAN when two sources describe the same identity."""
    return RestrictionReason.BAN if RestrictionReason.BAN in (a, b) else a


def _is_banned_role(role: Any) -> bool:
    """True for the persisted ban state (`community_members.role == 'banned'`)."""
    return isinstance(role, str) and role.strip().lower() == BANNED_ROLE


def _bind(dal: Any) -> None:
    """Idempotently bind every table this module touches (no-op when already bound)."""
    bind_suppression_tables(dal)
    bind_erasure_reputation_tables(dal)


def identities_of(dal: Any, hub_user_id: int) -> list[Identity]:
    """The subject's linked `(platform, platform_user_id)` pairs AS STORED, de-duplicated.

    Raw (not normalised) on purpose: they are matched by equality against other tables'
    stored values (erasure deletes, member-row arms). Normalisation happens at the
    suppression boundary (`normalize_identity` inside every code computation).
    """
    rows = dal(dal.hub_user_identities.hub_user_id == hub_user_id).select(
        dal.hub_user_identities.platform, dal.hub_user_identities.platform_user_id
    )
    pairs: list[Identity] = []
    for row in rows:
        if not row.platform or not row.platform_user_id:
            continue
        pair = (str(row.platform), str(row.platform_user_id))
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def subject_member_rows(
    dal: Any, *, hub_user_id: int, identities: Collection[Identity]
) -> list[Any]:
    """Every `community_members` row that belongs to the subject, in every community.

    Two arms, same as Art. 17 erasure has always matched: hub-linked rows
    (`user_id == str(hub_user_id)`) and rows keyed only by platform identity (blank `user_id`)
    whose `(platform, platform_user_id)` is one of the subject's linked identities. A row
    linked to a DIFFERENT hub user is never returned, even if it names the same platform id.
    """
    members = dal.community_members
    fields = (
        members.id,
        members.community_id,
        members.user_id,
        members.platform,
        members.platform_user_id,
        members.role,
        members.is_active,
        members.user_uuid,
    )
    seen: dict[int, Any] = {}
    for row in dal(members.user_id == str(hub_user_id)).select(*fields):
        seen[int(row.id)] = row
    for platform, platform_user_id in identities:
        keyed = dal(
            (members.platform == platform)
            & (members.platform_user_id == platform_user_id)
            & ((members.user_id == None) | (members.user_id == ""))  # noqa: E711 - pydal query
        ).select(*fields)
        for row in keyed:
            seen[int(row.id)] = row
    return list(seen.values())


def tenants_of(dal: Any, community_ids: Iterable[int]) -> dict[int, int]:
    """Map `community_id -> tenant_id`; a community with no tenant is omitted (and logged)."""
    ids = sorted({int(c) for c in community_ids if c is not None})
    if not ids:
        return {}
    rows = dal(dal.communities.id.belongs(ids)).select(
        dal.communities.id, dal.communities.tenant_id
    )
    mapping = {int(r.id): int(r.tenant_id) for r in rows if r.tenant_id is not None}
    missing = len(ids) - len(mapping)
    if missing:
        logger.warning(
            "suppression.community_without_tenant",
            extra={"action": "suppression_scope", "result": "skipped", "communities": missing},
        )
    return mapping


def _was_auto_banned(
    dal: Any, *, community_id: int, hub_user_id: int, pairs: Collection[Identity]
) -> bool:
    """True if the reputation log recorded an `auto_ban` for the subject in this community."""
    events = dal.reputation_events
    who = events.hub_user_id == hub_user_id
    for platform, platform_user_id in pairs:
        who = who | ((events.platform == platform) & (events.platform_user_id == platform_user_id))
    query = (events.community_id == community_id) & (events.event_type == "auto_ban") & who
    return bool(dal(query).count())


def collect_active_restrictions(
    dal: Any,
    *,
    hub_user_id: int,
    identities: Collection[Identity],
    members: Iterable[Any],
) -> list[ActiveRestriction]:
    """Find the subject's active bans BEFORE anything is deleted.

    One entry per distinct normalised `(tenant, platform, platform_user_id)`: `BAN` outranks
    `AUTO_BAN` and an indefinite restriction outranks a dated one when several rows describe
    the same identity. Holds raw platform ids in memory only -- they are turned into keyed
    codes by :func:`record_suppressions` and never persisted or logged. An identity that
    cannot be normalised cannot be coded and is skipped with a (value-free) WARNING.
    """
    _bind(dal)
    banned = [m for m in members if _is_banned_role(m.role)]
    if not banned:
        return []
    tenant_by_community = tenants_of(dal, (m.community_id for m in banned))
    merged: dict[tuple[int, str, str], ActiveRestriction] = {}
    for member in banned:
        tenant_id = tenant_by_community.get(int(member.community_id))
        if tenant_id is None:
            continue
        hub_linked = str(member.user_id or "") == str(hub_user_id)
        pairs: list[Identity] = list(identities) if hub_linked else []
        if member.platform and member.platform_user_id:
            own = (str(member.platform), str(member.platform_user_id))
            if own not in pairs:
                pairs.append(own)
        if not pairs:
            continue
        reason = (
            RestrictionReason.AUTO_BAN
            if _was_auto_banned(
                dal,
                community_id=int(member.community_id),
                hub_user_id=hub_user_id,
                pairs=pairs,
            )
            else RestrictionReason.BAN
        )
        for raw_platform, raw_platform_user_id in pairs:
            try:
                platform, platform_user_id = normalize_identity(raw_platform, raw_platform_user_id)
            except SuppressionInputError:
                logger.warning(
                    "suppression.identity_skipped",
                    extra={"action": "suppression_identity", "result": "invalid"},
                )
                continue
            key = (tenant_id, platform, platform_user_id)
            previous = merged.get(key)
            merged[key] = ActiveRestriction(
                tenant_id=tenant_id,
                platform=platform,
                platform_user_id=platform_user_id,
                reason=reason if previous is None else _stronger(previous.reason, reason),
                expires_at=None,
            )
    return list(merged.values())


def record_suppressions(
    dal: Any,
    restrictions: Collection[ActiveRestriction],
    *,
    keys: SuppressionKeySource,
    now: datetime,
    retention_days: int | None = None,
) -> int:
    """Write one `suppression_records` row per restriction (upsert); returns rows written.

    Runs INSIDE the caller's transaction and never commits or rolls back itself. The row
    stores only the keyed code, the platform name, the key version, the enum and the expiry
    (capped by `retention_days` when set). An existing row for the same code is extended,
    never duplicated (the table is UNIQUE on `(tenant_id, platform, key_ver, hmac)`).

    Raises:
        SuppressionKeyError: the master key is missing/malformed -- the caller must abort,
            not erase without suppressing.
    """
    if not restrictions:
        return 0
    _bind(dal)
    table = dal.suppression_records
    ceiling = None if retention_days is None else now + timedelta(days=retention_days)
    written = 0
    for restriction in restrictions:
        code = current_code(
            keys, restriction.tenant_id, restriction.platform, restriction.platform_user_id
        )
        expires_at = restriction.expires_at
        if ceiling is not None:
            expires_at = ceiling if expires_at is None else min(expires_at, ceiling, key=_as_utc)
        existing = dal(
            (table.tenant_id == restriction.tenant_id)
            & (table.platform == restriction.platform)
            & (table.key_ver == code.key_ver)
            & (table.hmac == code.hmac)
        ).select(table.id, table.expires_at, table.reason_enum)
        if existing:
            row = existing.first()
            dal(table.id == row.id).update(
                expires_at=_later(row.expires_at, expires_at),
                reason_enum=_stronger(RestrictionReason(row.reason_enum), restriction.reason).value,
            )
        else:
            table.insert(
                tenant_id=restriction.tenant_id,
                platform=restriction.platform,
                key_ver=code.key_ver,
                hmac=code.hmac,
                reason_enum=restriction.reason.value,
                expires_at=expires_at,
                created_at=now,
            )
        written += 1
    logger.debug(
        "suppression.recorded",
        extra={"action": "suppression_record", "result": "ok", "rows": written},
    )
    return written


def _live(table: Any, tenant_id: int, platform: str, now: datetime) -> Any:
    """Query: this tenant's non-expired rows for `platform`."""
    return (
        (table.tenant_id == tenant_id)
        & (table.platform == platform)
        & ((table.expires_at == None) | (table.expires_at > now))  # noqa: E711 - pydal query
    )


def find_suppression(
    dal: Any,
    *,
    tenant_id: int,
    platform: str,
    platform_user_id: str,
    keys: SuppressionKeySource,
    now: datetime,
) -> SuppressionMatch | None:
    """Return the non-expired restriction matching this identity in THIS tenant, else None.

    Cheap path first: if the tenant has no live row for the platform, no key is touched and
    nothing can fail. Otherwise the keyed codes (current + previous master key) are computed
    and matched on `(tenant_id, platform, key_ver, hmac)`; an expired row never matches
    whether or not the purge job has deleted it yet.

    Raises:
        SuppressionKeyError: the tenant has live rows but the master key is unavailable --
            the caller must fail closed.
        SuppressionInputError: invalid tenant id / platform / platform user id.
    """
    tenant_id = validate_tenant_id(tenant_id)
    plat, puid = normalize_identity(platform, platform_user_id)
    _bind(dal)
    table = dal.suppression_records
    live = _live(table, tenant_id, plat, now)
    if not dal(live).count():
        return None
    codes = lookup_codes(keys, tenant_id, plat, puid)
    wanted = {(c.key_ver, c.hmac) for c in codes}
    rows = dal(live & table.hmac.belongs([c.hmac for c in codes])).select(
        table.key_ver, table.hmac, table.reason_enum, table.expires_at
    )
    hits = [r for r in rows if (r.key_ver, r.hmac) in wanted]
    if not hits:
        return None
    reason = RestrictionReason(hits[0].reason_enum)
    expires_at = hits[0].expires_at
    for extra in hits[1:]:
        reason = _stronger(reason, RestrictionReason(extra.reason_enum))
        expires_at = _later(expires_at, extra.expires_at)
    return SuppressionMatch(tenant_id=tenant_id, reason=reason, expires_at=expires_at)


def check_identities(
    dal: Any,
    *,
    tenant_id: int,
    identities: Iterable[Identity],
    keys: SuppressionKeySource,
    now: datetime,
) -> SuppressionMatch | None:
    """First suppression match among a person's identities, within one tenant."""
    for platform, platform_user_id in identities:
        try:
            match = find_suppression(
                dal,
                tenant_id=tenant_id,
                platform=platform,
                platform_user_id=platform_user_id,
                keys=keys,
                now=now,
            )
        except SuppressionInputError:
            # A stored identity that cannot be normalised could never have been recorded either
            # (the record path skips it identically), so it cannot match; say so, value-free.
            logger.warning(
                "suppression.identity_skipped",
                extra={"action": "suppression_identity", "result": "invalid"},
            )
            continue
        if match is not None:
            return match
    return None


def lift_suppression(
    dal: Any,
    *,
    tenant_id: int,
    platform: str,
    platform_user_id: str,
    keys: SuppressionKeySource,
) -> int:
    """Delete this identity's suppression row(s) in this tenant (restriction lifted / objected).

    Returns rows deleted (0 if there was none). Deletes expired rows too. Runs inside the
    caller's transaction. A missing key with rows present raises -- a lift must not report
    success while leaving the row behind.
    """
    tenant_id = validate_tenant_id(tenant_id)
    plat, puid = normalize_identity(platform, platform_user_id)
    _bind(dal)
    table = dal.suppression_records
    if not dal((table.tenant_id == tenant_id) & (table.platform == plat)).count():
        return 0
    deleted = 0
    for code in lookup_codes(keys, tenant_id, plat, puid):
        deleted += (
            dal(
                (table.tenant_id == tenant_id)
                & (table.platform == plat)
                & (table.key_ver == code.key_ver)
                & (table.hmac == code.hmac)
            ).delete()
            or 0
        )
    return int(deleted)


def purge_expired(dal: Any, *, now: datetime) -> int:
    """Delete every row whose `expires_at` has passed; returns rows deleted. No key needed."""
    _bind(dal)
    table = dal.suppression_records
    return int(
        dal((table.expires_at != None) & (table.expires_at <= now)).delete() or 0  # noqa: E711
    )


def ban_membership(dal: Any, *, community_id: int, hub_user_id: int, now: datetime) -> None:
    """Upsert the hub user's membership in `community_id` as banned + inactive.

    This re-applies the restriction in the same form the reputation policy enforcer writes
    an auto-ban, so every existing reader (member lists, authz) treats it as one. Runs
    inside the caller's transaction.
    """
    members = dal.community_members
    existing = dal(
        (members.community_id == community_id) & (members.user_id == str(hub_user_id))
    ).select(members.id)
    if existing:
        dal(members.id == existing.first().id).update(
            role=BANNED_ROLE, is_active=False, updated_at=now
        )
    else:
        members.insert(
            community_id=community_id,
            user_id=str(hub_user_id),
            role=BANNED_ROLE,
            is_active=False,
            joined_at=now,
            updated_at=now,
        )


def reapply_for_hub_user(
    dal: Any, *, hub_user_id: int, keys: SuppressionKeySource, now: datetime
) -> int:
    """Re-apply any matching suppression to the hub user's existing, non-banned memberships.

    For every tenant where the user has a membership, each linked identity is checked
    against THAT tenant's list only; a match flips that tenant's membership rows to banned.
    Called after an identity is linked (a banned person may join first and link the banned
    account later). Returns membership rows banned. Runs inside the caller's transaction.
    """
    _bind(dal)
    identities = identities_of(dal, hub_user_id)
    if not identities:
        return 0
    members = dal.community_members
    rows = dal(
        (members.user_id == str(hub_user_id))
        & ((members.role == None) | (members.role != BANNED_ROLE))  # noqa: E711
    ).select(members.id, members.community_id)
    if not rows:
        return 0
    tenant_by_community = tenants_of(dal, (r.community_id for r in rows))
    banned = 0
    for tenant_id in sorted(set(tenant_by_community.values())):
        match = check_identities(
            dal, tenant_id=tenant_id, identities=identities, keys=keys, now=now
        )
        if match is None:
            continue
        ids = [int(r.id) for r in rows if tenant_by_community.get(int(r.community_id)) == tenant_id]
        banned += (
            dal(members.id.belongs(ids)).update(role=BANNED_ROLE, is_active=False, updated_at=now)
            or 0
        )
        logger.info(
            "suppression.membership_reapplied",
            extra={
                "action": "suppression_reapply",
                "result": "banned",
                "tenant_id": tenant_id,
                "reason": match.reason.value,
                "rows": len(ids),
            },
        )
    return int(banned)


async def run_in_transaction(async_dal: Any, dal: Any, work: Callable[[], T]) -> T:
    """Run `work` as ONE executor job: same pool thread, one commit, rollback on any failure.

    Mirrors `data_privacy_service.anonymize_user_data`: `AsyncDAL`'s per-call helpers each
    commit on their own, so a multi-statement check-then-write must be a single job.
    """
    loop = asyncio.get_running_loop()

    def _job() -> T:
        try:
            result = work()
            dal.commit()
            return result
        except Exception:
            dal.rollback()
            raise

    return await loop.run_in_executor(async_dal.executor, _job)


async def check_and_ban_membership(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    hub_user_id: int,
    keys: SuppressionKeySource | None = None,
) -> SuppressionMatch | None:
    """Pre-creation gate: if the hub user is suppressed in this community's tenant, ban them there.

    Called by every hub-api path that is about to create a membership for a hub user. On a
    match the membership is written banned + inactive (the restriction is re-applied) and
    the match is returned so the caller can refuse; on no match nothing is written. Fails
    closed: a key / database error propagates and the caller must not create the membership.
    """
    source = keys or PlatformKekKeySource()
    started = time.perf_counter()
    with get_tracer().start_as_current_span(
        "hub.suppression.check", record_exception=False, set_status_on_exception=False
    ) as span:

        def _work() -> SuppressionMatch | None:
            _bind(dal)
            now = datetime.now(UTC)
            tenant_by_community = tenants_of(dal, [community_id])
            tenant_id = tenant_by_community.get(community_id)
            if tenant_id is None:
                return None
            identities = identities_of(dal, hub_user_id)
            match = check_identities(
                dal, tenant_id=tenant_id, identities=identities, keys=source, now=now
            )
            if match is not None:
                ban_membership(dal, community_id=community_id, hub_user_id=hub_user_id, now=now)
            return match

        try:
            match = await run_in_transaction(async_dal, dal, _work)
        except Exception as exc:
            span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
            _emit("check", "error", started=started)
            logger.exception(
                "suppression.check_failed",
                extra={
                    "action": "suppression_check",
                    "result": "error",
                    "community_id": community_id,
                },
            )
            raise
        span.set_attribute("matched", match is not None)
    _emit(
        "check",
        "matched" if match is not None else "clear",
        reason=None if match is None else match.reason.value,
        started=started,
    )
    return match


async def reapply_for_hub_user_async(
    async_dal: Any, dal: Any, *, hub_user_id: int, keys: SuppressionKeySource | None = None
) -> int:
    """Async wrapper over :func:`reapply_for_hub_user` (one transaction); returns rows banned."""
    source = keys or PlatformKekKeySource()
    started = time.perf_counter()
    try:
        banned = await run_in_transaction(
            async_dal,
            dal,
            lambda: reapply_for_hub_user(
                dal, hub_user_id=hub_user_id, keys=source, now=datetime.now(UTC)
            ),
        )
    except Exception:
        _emit("reapply", "error", started=started)
        logger.exception(
            "suppression.reapply_failed",
            extra={"action": "suppression_reapply", "result": "error", "hub_user_id": hub_user_id},
        )
        raise
    _emit("reapply", "banned" if banned else "clear", started=started)
    return banned


async def lift_suppression_async(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    platform: str,
    platform_user_id: str,
    keys: SuppressionKeySource | None = None,
) -> int:
    """Async wrapper over :func:`lift_suppression` (one transaction); returns rows deleted."""
    source = keys or PlatformKekKeySource()
    deleted = await run_in_transaction(
        async_dal,
        dal,
        lambda: lift_suppression(
            dal,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=platform_user_id,
            keys=source,
        ),
    )
    _emit("lift", "deleted" if deleted else "none")
    logger.info(
        "suppression.lifted",
        extra={
            "action": "suppression_lift",
            "result": "ok",
            "tenant_id": tenant_id,
            "rows": deleted,
        },
    )
    return deleted


async def purge_expired_async(async_dal: Any, dal: Any) -> int:
    """Async wrapper over :func:`purge_expired` (one transaction); returns rows deleted."""
    deleted = await run_in_transaction(
        async_dal, dal, lambda: purge_expired(dal, now=datetime.now(UTC))
    )
    if deleted:
        _instruments().purged.add(deleted)
        logger.info(
            "suppression.purged",
            extra={"action": "suppression_purge", "result": "ok", "rows": deleted},
        )
    return deleted


async def run_purge_loop(
    async_dal: Any,
    dal: Any,
    interval_seconds: float = DEFAULT_PURGE_INTERVAL_SECONDS,
    *,
    is_ready: Callable[[], bool] | None = None,
) -> None:
    """Delete expired suppression rows forever; a failed pass is logged and retried next tick.

    Started once from hub-api startup. `is_ready` (schema bootstrap finished) gates each pass
    so a fresh/behind schema is not logged as a failure. Safe in every replica: the delete is
    idempotent. Lookups already ignore expired rows, so a late pass never changes a decision.
    """
    while True:
        try:
            if is_ready is None or is_ready():
                await purge_expired_async(async_dal, dal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "suppression.purge_failed",
                extra={"action": "suppression_purge", "result": "error"},
            )
        await asyncio.sleep(interval_seconds)
