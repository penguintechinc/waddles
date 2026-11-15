"""Scheduled-bundle listing: which active bundles the host must tick, per (tenant, community, app).

svc-ingest owns the clock (`core/svc_ingest/src/connector/bundle_schedule.rs`); this module is its
source of truth. A row is returned only when ALL hold:

* the bundle has an `app_active_versions` pointer for the scope (tenant-wide = community 0), a
  current, non-revoked global install and tenant availability (never schedule a pulled bundle);
* its stored manifest declares a valid `schedule` and a `hub`/`bundle.reconcile` consume rule on
  its own `app_id_slug` source id (`scheduled_consume_rule_ok`);
* the matching `app_source_bindings` row `(hub, app_id_slug(app_id))` exists -- without it
  svc-process has no consumer group on that stream and the tick would be published into the void.

A candidate failing any manifest/binding check is EXCLUDED, logged at ERROR and counted on
`bundle_schedules_excluded_total{reason}` -- never silently dropped, never allowed to poison the
rest of the listing. Ids only, no PII.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry import metrics
from sqlalchemy import and_, select

from services.bundle_manifest_v2 import (
    SCHEDULE_PLATFORM,
    ConsumeRule,
    ManifestV2Error,
    app_id_slug,
    parse_schedule,
    scheduled_consume_rule_ok,
)

logger = logging.getLogger(__name__)

#: `app_active_versions.community_id` sentinel for a tenant-wide activation.
TENANT_WIDE_COMMUNITY_SENTINEL: Final = 0

_REASON_MANIFEST_MISSING: Final = "manifest_missing"
_REASON_SCHEDULE_INVALID: Final = "schedule_invalid"
_REASON_CONSUME_RULE: Final = "consume_rule_missing"
_REASON_BINDING: Final = "binding_missing"

_meter = metrics.get_meter("waddles.hub_api.bundle_schedules")
_excluded_counter = _meter.create_counter(
    "bundle_schedules_excluded_total",
    description="Scheduled-bundle candidates excluded from the listing, by reason.",
)
_listed_counter = _meter.create_counter(
    "bundle_schedules_listed_total", description="Scheduled bundle targets returned."
)
_requests_histogram = _meter.create_histogram(
    "bundle_schedules_candidates", description="Scheduled candidates examined per listing."
)


@dataclass(slots=True, frozen=True)
class ScheduleTarget:
    """One scheduled `(tenant, community, app)` with its resolved cadence."""

    tenant_id: int
    community_id: int
    tenant_slug: str
    community_name: str | None
    app_id: str
    interval_s: int
    jitter: float


@dataclass(slots=True, frozen=True)
class ScheduleListing:
    """Targets plus how many scheduled candidates were examined and excluded (the denominator)."""

    targets: tuple[ScheduleTarget, ...]
    examined: int
    excluded: int


def _exclude(reason: str, *, tenant_id: int, community_id: int, app_id: str) -> None:
    """Count and log (ERROR, ids only) one excluded candidate."""
    _excluded_counter.add(1, {"reason": reason})
    logger.error(
        "bundle schedule candidate excluded",
        extra={
            "reason": reason,
            "tenant_id": tenant_id,
            "community_id": community_id,
            "app_id": app_id,
            "op": "bundle_schedules",
        },
    )


def _rules(raw: dict[str, Any]) -> tuple[ConsumeRule, ...]:
    """The stored (already validated) manifest's process-stage `consumes` rules."""
    stage = (raw.get("stages") or {}).get("process") or {}
    return tuple(
        ConsumeRule(
            platform=str(rule["platform"]),
            source_id=rule.get("source_id"),
            event_types=tuple(rule.get("event_types") or ()),
            filters=dict(rule.get("filters") or {}),
        )
        for rule in stage.get("consumes") or ()
        if isinstance(rule, dict) and "platform" in rule
    )


async def list_schedule_targets(
    install_dal: Any,
    *,
    tenant_id: int | None = None,
    community_id: int | None = None,
    app_id: str | None = None,
) -> ScheduleListing:
    """List every active, scheduled bundle scope, optionally filtered by tenant/community/app.

    Filters are applied in SQL (parameterised); the caller has already validated their shape.
    Tenant-wide activations carry `community_id == 0` and `community_name is None`.
    """
    meta = install_dal.metadata.tables
    active = meta["app_active_versions"]
    versions = meta["app_versions"]
    uploads = meta["app_version_uploads"]
    tenants = meta["tenants"]
    communities = meta["communities"]
    installs = meta["app_global_installs"]
    availability = meta["bundle_tenant_availability"]
    bindings = meta["app_source_bindings"]

    stmt = (
        select(
            active.c.tenant_id,
            active.c.community_id,
            active.c.app_id,
            versions.c.version,
            tenants.c.slug.label("tenant_slug"),
            communities.c.name.label("community_name"),
        )
        .select_from(
            active.join(versions, versions.c.id == active.c.version_id)
            .join(tenants, tenants.c.id == active.c.tenant_id)
            .join(
                installs,
                and_(
                    installs.c.app_id == active.c.app_id,
                    installs.c.version == versions.c.version,
                    installs.c.superseded_by.is_(None),
                    installs.c.revoked_at.is_(None),
                ),
            )
            .join(
                availability,
                and_(
                    availability.c.tenant_id == active.c.tenant_id,
                    availability.c.app_id == active.c.app_id,
                    availability.c.available.is_(True),
                ),
            )
            .outerjoin(
                communities,
                and_(
                    communities.c.id == active.c.community_id,
                    communities.c.tenant_id == active.c.tenant_id,
                ),
            )
        )
        .order_by(active.c.tenant_id, active.c.community_id, active.c.app_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(active.c.tenant_id == tenant_id)
    if community_id is not None:
        stmt = stmt.where(active.c.community_id == community_id)
    if app_id is not None:
        stmt = stmt.where(active.c.app_id == app_id)

    targets: list[ScheduleTarget] = []
    examined = 0
    excluded = 0
    async with install_dal.engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
        for row in rows:
            manifest = (
                await conn.execute(
                    select(uploads.c.manifest_json)
                    .where((uploads.c.app_id == row.app_id) & (uploads.c.version == row.version))
                    .order_by(uploads.c.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not isinstance(manifest, dict):
                examined += 1
                excluded += 1
                _exclude(
                    _REASON_MANIFEST_MISSING,
                    tenant_id=row.tenant_id,
                    community_id=row.community_id,
                    app_id=row.app_id,
                )
                continue
            if manifest.get("schedule") is None:
                continue
            examined += 1
            tid, cid, aid = int(row.tenant_id), int(row.community_id), str(row.app_id)
            try:
                schedule = parse_schedule(manifest.get("schedule"))
            except ManifestV2Error:
                excluded += 1
                _exclude(_REASON_SCHEDULE_INVALID, tenant_id=tid, community_id=cid, app_id=aid)
                continue
            if schedule is None or not scheduled_consume_rule_ok(row.app_id, _rules(manifest)):
                excluded += 1
                _exclude(_REASON_CONSUME_RULE, tenant_id=tid, community_id=cid, app_id=aid)
                continue
            bound = (
                await conn.execute(
                    select(bindings.c.app_id).where(
                        (bindings.c.tenant_id == row.tenant_id)
                        & (bindings.c.community_id == row.community_id)
                        & (bindings.c.app_id == row.app_id)
                        & (bindings.c.platform == SCHEDULE_PLATFORM)
                        & (bindings.c.source_id == app_id_slug(row.app_id))
                    )
                )
            ).first()
            if bound is None:
                excluded += 1
                _exclude(_REASON_BINDING, tenant_id=tid, community_id=cid, app_id=aid)
                continue
            targets.append(
                ScheduleTarget(
                    tenant_id=tid,
                    community_id=cid,
                    tenant_slug=str(row.tenant_slug),
                    community_name=(
                        None if row.community_name is None else str(row.community_name)
                    ),
                    app_id=aid,
                    interval_s=schedule.interval_s,
                    jitter=schedule.jitter,
                )
            )
    _listed_counter.add(len(targets))
    _requests_histogram.record(examined)
    logger.debug(
        "bundle schedules listed",
        extra={"examined": examined, "listed": len(targets), "excluded": excluded},
    )
    return ScheduleListing(targets=tuple(targets), examined=examined, excluded=excluded)
