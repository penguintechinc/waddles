"""Platform identity -> stable UUID resolution (PII boundary, issues #429 / #427).

Raw platform identifiers and handles exist only inside hub-api; callers
outside it (svc-ingest/svc-process/svc-action, bundles) get UUIDs only.
The resolution logic itself lives in the ``resolve_identity_uuid()``
Postgres function (alembic 0045) so the membership trigger, the backfill
and this service share one implementation. Every failure raises -- there
is no default/fallback UUID.

Round 2 adds the two lookups that complete the boundary:

- :func:`resolve_target` -- a raw chat reference (``@bob``, ``bob`` or a
  Discord ``<@123>`` mention) to a UUID, tenant-scoped, matching handles
  only against hub-api-owned PII columns. Not-found and ambiguous matches
  raise (a secret must never go to a guessed recipient).
- :func:`resolve_display_names` -- UUIDs back to display names, for the
  egress detokenizer only. Misses are reported as unresolved, never
  fabricated; names are sanitized and tenant-scoped.

Security hardening (identity review) adds:

- :func:`authorize_tenant` -- the tenant a gRPC caller may act on comes from its
  VALIDATED token claim, never from the request body alone: a tenant-bound token
  is confined to its own tenant (mismatch and unknown tenants both raise
  :class:`TenantAccessDeniedError`, no existence oracle); only the operator-plane
  ``system`` tenant may name another tenant, and every such use is counted.
- :func:`erase_pseudonym_handles` -- the GDPR erasure path for the one raw handle
  store (``ephemeral_pseudonyms.handle``).
- :func:`collect_uuid_unavailability` / :func:`run_uuid_availability_monitor` -- the
  metric for community members whose ``user_uuid`` could not be derived, so a
  trigger-side fail-closed NULL is observable rather than only a ``RAISE WARNING``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flask_core.service_jwt import SYSTEM_TENANT
from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("hub_api.identity")
_meter = metrics.get_meter("hub_api.identity")
_resolve_counter = _meter.create_counter(
    "hub_api.identity.resolutions", description="Identity resolutions by outcome"
)
_resolve_latency = _meter.create_histogram(
    "hub_api.identity.resolve_duration_ms", unit="ms", description="Batch resolve latency"
)
_batch_size = _meter.create_histogram(
    "hub_api.identity.batch_size", description="Items per mint/resolve batch"
)

_op_counter = _meter.create_counter(
    "hub_api.identity.operations", description="Handle/display-name lookups by op and outcome"
)
_op_latency = _meter.create_histogram(
    "hub_api.identity.operation_duration_ms", unit="ms", description="Lookup latency by op"
)
_display_lookups = _meter.create_counter(
    "hub_api.identity.display_name_lookups", description="Display-name lookups by hit/miss"
)
_tenant_authz = _meter.create_counter(
    "hub_api.identity.tenant_authorizations",
    description="Tenant-scope decisions on internal identity RPCs by op and result",
)
_erasures = _meter.create_counter(
    "hub_api.identity.pseudonym_erasures", description="GDPR pseudonym erasures by mode"
)

#: Why a community member's ``user_uuid`` is NULL ("unavailable"), as written by the
#: membership trigger (alembic 0048). Kept in step with the table's CHECK constraint.
UNAVAILABLE_REASONS = ("uuid_collision", "dangling_user_id", "unresolvable", "no_tenant")
_unavailable_counts: dict[str, int] = {}


def _observe_unavailable(_options: CallbackOptions) -> list[Observation]:
    """Gauge callback: members currently without a derivable ``user_uuid``, per reason."""
    return [
        Observation(_unavailable_counts.get(reason, 0), {"reason": reason})
        for reason in UNAVAILABLE_REASONS
    ]


_meter.create_observable_gauge(
    "hub_api.identity.members_uuid_unavailable",
    callbacks=[_observe_unavailable],
    description="Community members whose user_uuid could not be derived, by reason",
)

MAX_BATCH = 100
MAX_TARGET_LEN = 255
MAX_DISPLAY_NAME_LEN = 64
_PLATFORM_RE = re.compile(r"^[a-z0-9_-]{1,50}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_NON_USER_AT_REFS = frozenset({"@everyone", "@here"})
#: Bidi marks/overrides/isolates: invisible characters a display name could use to
#: spoof or reorder the message it is substituted into (dropped outright).
_UNSAFE_NAME_CHARS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


class IdentityResolutionError(Exception):
    """Base for resolution failures; never swallowed into a default UUID."""


class IdentityValidationError(IdentityResolutionError):
    """Caller input failed validation (maps to INVALID_ARGUMENT)."""


class TenantNotFoundError(IdentityResolutionError):
    """Tenant id/slug did not resolve (maps to NOT_FOUND)."""


class HandleNotFoundError(IdentityResolutionError):
    """No identity in the tenant matches the handle (maps to NOT_FOUND)."""


class AmbiguousHandleError(IdentityResolutionError):
    """The handle matches more than one identity -- never guessed (FAILED_PRECONDITION)."""


class TenantAccessDeniedError(IdentityResolutionError):
    """The caller's token is not entitled to the requested tenant (maps to PERMISSION_DENIED)."""


@dataclass(slots=True, frozen=True)
class IdentityRequest:
    """One (tenant, platform, platform_user_id) tuple to resolve."""

    tenant_id: str
    platform: str
    platform_user_id: str
    handle: str = ""


@dataclass(slots=True, frozen=True)
class ResolvedIdentity:
    """Resolved identity: only the platform id echoed back plus its UUID."""

    platform_user_id: str
    uuid: uuid.UUID


def _validate_tenant_ref(tenant_id: str) -> None:
    """Reject an empty/oversized/control-char tenant reference."""
    if not tenant_id or len(tenant_id) > 255 or _CONTROL_RE.search(tenant_id):
        raise IdentityValidationError("tenant_id invalid")


def _validate(item: IdentityRequest) -> None:
    """Reject malformed input at the boundary before touching the DB."""
    _validate_tenant_ref(item.tenant_id)
    if not _PLATFORM_RE.match(item.platform):
        raise IdentityValidationError("platform invalid")
    if (
        not item.platform_user_id
        or len(item.platform_user_id) > 255
        or _CONTROL_RE.search(item.platform_user_id)
    ):
        raise IdentityValidationError("platform_user_id invalid")
    if len(item.handle) > 255 or _CONTROL_RE.search(item.handle):
        raise IdentityValidationError("handle invalid")


async def _tenant_pk(async_dal: Any, tenant_ref: str) -> int:
    """Resolve a numeric id or slug to tenants.id; unknown tenant fails loud."""
    rows = await async_dal.executesql_async(
        "SELECT id FROM tenants WHERE id::text = %s OR slug = %s "
        "ORDER BY (id::text = %s) DESC LIMIT 1",
        [tenant_ref, tenant_ref, tenant_ref],
    )
    if not rows:
        raise TenantNotFoundError("tenant not found")
    return int(rows[0][0])


async def resolve_identities(
    async_dal: Any, items: Sequence[IdentityRequest]
) -> list[ResolvedIdentity]:
    """Resolve each tuple to its stable UUID (linked hub user, else pseudonym).

    Batch is bounded (<=100), validated, de-duplicated per (tenant, platform,
    platform_user_id); response order follows first occurrence. Any failure
    raises -- partial results are never returned.
    """
    if not items or len(items) > MAX_BATCH:
        raise IdentityValidationError(f"batch must contain 1..{MAX_BATCH} items")
    for item in items:
        _validate(item)
    started = time.monotonic()
    with _tracer.start_as_current_span("identity.resolve") as span:
        span.set_attribute("identity.batch_size", len(items))
        _batch_size.record(len(items))
        try:
            tenant_cache: dict[str, int] = {}
            seen: dict[tuple[str, str, str], ResolvedIdentity] = {}
            for item in items:
                if item.tenant_id not in tenant_cache:
                    tenant_cache[item.tenant_id] = await _tenant_pk(async_dal, item.tenant_id)
                key = (str(tenant_cache[item.tenant_id]), item.platform, item.platform_user_id)
                if key in seen:
                    continue
                rows = await async_dal.executesql_async(
                    "SELECT resolve_identity_uuid(%s, %s, %s, NULL, %s)",
                    [
                        tenant_cache[item.tenant_id],
                        item.platform,
                        item.platform_user_id,
                        item.handle or None,
                    ],
                )
                if not rows or rows[0][0] is None:
                    raise IdentityResolutionError("resolution returned no uuid")
                seen[key] = ResolvedIdentity(item.platform_user_id, uuid.UUID(str(rows[0][0])))
        except IdentityResolutionError as exc:
            _resolve_counter.add(len(items), {"outcome": type(exc).__name__})
            logger.warning(
                "identity resolution rejected",
                extra={"action": "identity_resolve", "result": type(exc).__name__},
            )
            raise
        except Exception as exc:
            _resolve_counter.add(len(items), {"outcome": "error"})
            logger.error(
                "identity resolution failed",
                extra={"action": "identity_resolve", "result": type(exc).__name__},
            )
            raise IdentityResolutionError("resolution failed") from exc
        _resolve_counter.add(len(items), {"outcome": "ok"})
        _resolve_latency.record((time.monotonic() - started) * 1000.0)
        logger.debug(
            "identity resolved",
            extra={"action": "identity_resolve", "count": len(seen), "result": "ok"},
        )
        return list(seen.values())


async def resolve_identity(
    async_dal: Any, tenant_id: str, platform: str, platform_user_id: str
) -> uuid.UUID:
    """Resolve one platform identity to its UUID (the index secret/lastseen key on)."""
    out = await resolve_identities(
        async_dal, [IdentityRequest(tenant_id, platform, platform_user_id)]
    )
    return out[0].uuid


@dataclass(slots=True, frozen=True)
class ParsedTarget:
    """A validated chat reference: a platform user id (mention) or a handle to look up."""

    kind: Literal["mention", "handle"]
    value: str


@dataclass(slots=True, frozen=True)
class ResolvedTarget:
    """Outcome of :func:`resolve_target`: the UUID and how it matched (never the handle)."""

    uuid: uuid.UUID
    kind: Literal["mention", "handle"]


@dataclass(slots=True, frozen=True)
class ResolvedDisplayName:
    """One resolved display name; ``uuid`` is the token exactly as the caller sent it."""

    uuid: str
    display_name: str
    is_hub_user: bool


@dataclass(slots=True, frozen=True)
class DisplayNameResult:
    """Batch outcome: resolved names plus tokens that resolved to nothing (never an error)."""

    names: tuple[ResolvedDisplayName, ...]
    unresolved: tuple[str, ...]


#: Candidate UUIDs for a handle inside one tenant (at most 2 are needed: 0 = not found,
#: 1 = resolved, 2 = ambiguous). Two PII-bearing sources, both hub-api-owned:
#: the pseudonym store's mint-time handle (re-pointed at the hub user once the platform
#: account is linked -- the same precedence ``resolve_identity_uuid()`` applies, so a
#: stale pseudonym can never collide with its own linked identity) and the linked
#: ``hub_user_identities.platform_username``, restricted to identities that are members
#: of one of the tenant's communities. Matching is case-insensitive and exact.
_HANDLE_CANDIDATES_SQL = """
SELECT DISTINCT cand.uuid::text FROM (
    SELECT COALESCE(hu.uuid, ep.pseudonym) AS uuid
      FROM ephemeral_pseudonyms ep
      LEFT JOIN hub_user_identities hui
        ON hui.platform = ep.platform AND hui.platform_user_id = ep.platform_user_id
      LEFT JOIN hub_users hu ON hu.id = hui.hub_user_id
     WHERE ep.tenant_id = %s AND ep.platform = %s AND lower(ep.handle) = lower(%s)
    UNION
    SELECT hu.uuid
      FROM hub_user_identities hui
      JOIN hub_users hu ON hu.id = hui.hub_user_id
     WHERE hui.platform = %s AND lower(hui.platform_username) = lower(%s)
       AND EXISTS (
            SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
             WHERE c.tenant_id = %s
               AND ((cm.platform = hui.platform AND cm.platform_user_id = hui.platform_user_id)
                    OR cm.user_id = hu.id::text))
) cand
LIMIT 2
"""

#: Hub-user display names, only for users that are members of the tenant (a UUID from
#: another tenant resolves to nothing -- no cross-tenant existence oracle). Preference:
#: the hub profile name, then the tenant's most recent member display name, then the
#: first linked platform username. Never username/email.
_HUB_NAMES_SQL = """
SELECT hu.uuid::text,
       COALESCE(
           NULLIF(btrim(hu.display_name), ''),
           (SELECT NULLIF(btrim(cm.display_name), '')
              FROM community_members cm JOIN communities c ON c.id = cm.community_id
             WHERE c.tenant_id = %s
               AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text)
               AND NULLIF(btrim(cm.display_name), '') IS NOT NULL
             ORDER BY cm.id DESC LIMIT 1),
           (SELECT NULLIF(btrim(hui.platform_username), '')
              FROM hub_user_identities hui
             WHERE hui.hub_user_id = hu.id
               AND NULLIF(btrim(hui.platform_username), '') IS NOT NULL
             ORDER BY hui.id LIMIT 1)
       )
  FROM hub_users hu
 WHERE hu.uuid = ANY(%s::uuid[])
   AND EXISTS (
        SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
         WHERE c.tenant_id = %s AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text))
"""

_PSEUDONYM_NAMES_SQL = """
SELECT ep.pseudonym::text, ep.handle
  FROM ephemeral_pseudonyms ep
 WHERE ep.tenant_id = %s AND ep.pseudonym = ANY(%s::uuid[])
"""


def parse_target(platform: str, raw: str) -> ParsedTarget:
    """Validate a raw chat reference and classify it as a mention or a handle.

    Accepts ``@bob`` / ``bob`` and, on Discord, ``<@123>`` / ``<@!123>``. Anything
    that is not a plain user reference (role/channel/emoji mentions, ``@everyone``,
    ``@here``, control characters, oversize input) raises
    :class:`IdentityValidationError` -- it is never coerced into a lookup.
    """
    if not _PLATFORM_RE.match(platform):
        raise IdentityValidationError("platform invalid")
    target = raw.strip() if isinstance(raw, str) else ""
    if not target or len(target) > MAX_TARGET_LEN or _CONTROL_RE.search(target):
        raise IdentityValidationError("target invalid")
    if target.lower() in _NON_USER_AT_REFS:
        raise IdentityValidationError("target is not a user reference")
    if target.startswith("<") and target.endswith(">"):
        mention = _DISCORD_MENTION_RE.match(target) if platform == "discord" else None
        if mention is None:
            raise IdentityValidationError("target is not a user reference")
        return ParsedTarget("mention", mention.group(1))
    handle = (target[1:] if target.startswith("@") else target).strip()
    if not handle or handle.startswith("@") or "<" in handle or ">" in handle:
        raise IdentityValidationError("target invalid")
    return ParsedTarget("handle", handle)


async def _observed[T](op: str, size: int, work: Callable[[], Awaitable[T]]) -> T:
    """Run ``work`` under a span with outcome/latency metrics and PII-free logging.

    Domain errors are re-raised unchanged (logged at WARNING by class name only);
    anything else is wrapped in :class:`IdentityResolutionError` with the driver
    error chained -- the caller never sees a default value.
    """
    started = time.monotonic()
    with _tracer.start_as_current_span(f"identity.{op}") as span:
        span.set_attribute("identity.batch_size", size)
        _batch_size.record(size, {"op": op})
        try:
            result = await work()
        except IdentityResolutionError as exc:
            outcome = type(exc).__name__
            _op_counter.add(1, {"op": op, "outcome": outcome})
            logger.warning(
                "identity operation rejected",
                extra={"action": f"identity_{op}", "result": outcome},
            )
            raise
        except Exception as exc:
            _op_counter.add(1, {"op": op, "outcome": "error"})
            logger.error(
                "identity operation failed",
                extra={"action": f"identity_{op}", "result": type(exc).__name__},
            )
            raise IdentityResolutionError("resolution failed") from exc
        _op_counter.add(1, {"op": op, "outcome": "ok"})
        _op_latency.record((time.monotonic() - started) * 1000.0, {"op": op})
        return result


async def resolve_target(
    async_dal: Any, tenant_id: str, platform: str, target: str
) -> ResolvedTarget:
    """Resolve a raw chat reference to a UUID entirely inside the PII boundary.

    A Discord mention carries the stable platform user id, so it resolves through
    ``resolve_identity_uuid()`` (linked hub user, else get-or-create pseudonym). A
    handle is matched case-insensitively within the tenant against hub-api-owned PII
    columns: exactly one distinct identity resolves, none raises
    :class:`HandleNotFoundError`, several raise :class:`AmbiguousHandleError` -- a
    secret or lookup must never land on a guessed identity. The handle is never
    persisted, echoed or logged.
    """
    _validate_tenant_ref(tenant_id)
    parsed = parse_target(platform, target)

    async def work() -> ResolvedTarget:
        tenant_pk = await _tenant_pk(async_dal, tenant_id)
        if parsed.kind == "mention":
            rows = await async_dal.executesql_async(
                "SELECT resolve_identity_uuid(%s, %s, %s, NULL, NULL)",
                [tenant_pk, platform, parsed.value],
            )
            if not rows or rows[0][0] is None:
                raise IdentityResolutionError("resolution returned no uuid")
            return ResolvedTarget(uuid.UUID(str(rows[0][0])), "mention")
        rows = await async_dal.executesql_async(
            _HANDLE_CANDIDATES_SQL,
            [tenant_pk, platform, parsed.value, platform, parsed.value, tenant_pk],
        )
        candidates = sorted({str(r[0]) for r in rows or []})
        if not candidates:
            raise HandleNotFoundError("handle not found")
        if len(candidates) > 1:
            raise AmbiguousHandleError("handle ambiguous")
        return ResolvedTarget(uuid.UUID(candidates[0]), "handle")

    resolved = await _observed("resolve_target", 1, work)
    logger.debug(
        "identity target resolved",
        extra={"action": "identity_resolve_target", "kind": resolved.kind, "result": "ok"},
    )
    return resolved


def _clean_display_name(raw: Any) -> str:
    """Make a stored name safe to substitute into outbound text; ``""`` if nothing remains.

    Drops bidi/format spoofing characters, turns other control characters and line
    separators into spaces, collapses whitespace and caps the length. Per-sink escaping
    (Discord markdown, IRC, HTML) stays the egress layer's job.
    """
    if not isinstance(raw, str):
        return ""
    chars = [
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in raw
        if ch not in _UNSAFE_NAME_CHARS
    ]
    return " ".join("".join(chars).split())[:MAX_DISPLAY_NAME_LEN].strip()


async def resolve_display_names(
    async_dal: Any, tenant_id: str, tokens: Sequence[str]
) -> DisplayNameResult:
    """Resolve up to 100 UUIDs (hub users or pseudonyms) to display names, tenant-scoped.

    For the egress detokenizer only -- the one place a name legitimately leaves the
    boundary. A token that is malformed, unknown, in another tenant, or has no usable
    name is reported in ``unresolved`` (fail-safe-empty); it is never an error and never
    a fabricated name. Duplicates collapse to their first occurrence and results keep
    request order, with each token echoed exactly as sent so the caller can key on it.
    """
    _validate_tenant_ref(tenant_id)
    if not tokens or len(tokens) > MAX_BATCH:
        raise IdentityValidationError(f"batch must contain 1..{MAX_BATCH} items")

    async def work() -> DisplayNameResult:
        ordered: list[tuple[str, str]] = []  # (token as sent, canonical uuid or "")
        seen: set[str] = set()
        for token in tokens:
            try:
                key = str(uuid.UUID(token))
            except (ValueError, TypeError, AttributeError) as exc:
                # Garbled/stale token: reported as unresolved, never an error (the value
                # itself is caller input and stays out of the log).
                logger.debug(
                    "display-name token is not a uuid",
                    extra={
                        "action": "identity_resolve_display_names",
                        "result": type(exc).__name__,
                    },
                )
                ordered.append((str(token)[:64], ""))
                continue
            if key not in seen:
                seen.add(key)
                ordered.append((token, key))
        keys = [key for _, key in ordered if key]
        found: dict[str, tuple[str, bool]] = {}
        if keys:
            tenant_pk = await _tenant_pk(async_dal, tenant_id)
            hub_rows = await async_dal.executesql_async(
                _HUB_NAMES_SQL, [tenant_pk, keys, tenant_pk]
            )
            for row in hub_rows or []:
                name = _clean_display_name(row[1])
                if name:
                    found[str(row[0])] = (name, True)
            pseudonym_rows = await async_dal.executesql_async(
                _PSEUDONYM_NAMES_SQL, [tenant_pk, keys]
            )
            for row in pseudonym_rows or []:
                name = _clean_display_name(row[1])
                if name:
                    found.setdefault(str(row[0]), (name, False))
        names = tuple(
            ResolvedDisplayName(token, *found[key]) for token, key in ordered if key in found
        )
        unresolved = tuple(token for token, key in ordered if key not in found)
        _display_lookups.add(len(names), {"result": "hit"})
        _display_lookups.add(len(unresolved), {"result": "miss"})
        return DisplayNameResult(names, unresolved)

    result = await _observed("resolve_display_names", len(tokens), work)
    logger.debug(
        "display names resolved",
        extra={
            "action": "identity_resolve_display_names",
            "hits": len(result.names),
            "misses": len(result.unresolved),
            "result": "ok",
        },
    )
    return result


async def authorize_tenant(
    async_dal: Any, caller_tenant: str, requested_ref: str, *, op: str, caller: str = ""
) -> str:
    """Return the tenant reference a caller may act on, from its VALIDATED token claim.

    ``caller_tenant`` is the ``tenant`` claim of an already-verified machine JWT (never
    request input). A tenant-bound token is confined to that tenant: an empty
    ``requested_ref`` defaults to it, a matching one (id or slug) is accepted, and any
    other tenant -- existing or not -- raises :class:`TenantAccessDeniedError` with the
    same constant message (no tenant-existence oracle). The operator-plane ``system``
    tenant may name any tenant but must name one explicitly; each such use is counted
    (``result=system``) so cross-tenant operator access is observable. An absent claim
    is denied. The requested reference is never logged (caller input).
    """
    claim = caller_tenant.strip() if isinstance(caller_tenant, str) else ""

    async def work() -> str:
        if not claim:
            _tenant_authz.add(1, {"op": op, "result": "no_claim"})
            raise TenantAccessDeniedError("tenant access denied")
        if claim == SYSTEM_TENANT:
            _validate_tenant_ref(requested_ref)
            _tenant_authz.add(1, {"op": op, "result": "system"})
            logger.debug(
                "operator-plane tenant access",
                extra={"action": "identity_tenant_authz", "op": op, "caller": caller[:128]},
            )
            return requested_ref
        if requested_ref:
            _validate_tenant_ref(requested_ref)
        try:
            bound_pk = await _tenant_pk(async_dal, claim)
            requested_pk = await _tenant_pk(async_dal, requested_ref) if requested_ref else bound_pk
        except TenantNotFoundError as exc:
            _tenant_authz.add(1, {"op": op, "result": "denied"})
            logger.warning(
                "tenant-bound token named an unknown tenant",
                extra={"action": "identity_tenant_authz", "op": op, "caller": caller[:128]},
            )
            raise TenantAccessDeniedError("tenant access denied") from exc
        if requested_pk != bound_pk:
            _tenant_authz.add(1, {"op": op, "result": "denied"})
            logger.warning(
                "tenant-bound token denied cross-tenant request",
                extra={"action": "identity_tenant_authz", "op": op, "caller": caller[:128]},
            )
            raise TenantAccessDeniedError("tenant access denied")
        _tenant_authz.add(1, {"op": op, "result": "allowed"})
        return str(bound_pk)

    return await _observed("authorize_tenant", 1, work)


async def erase_pseudonym_handles(
    async_dal: Any,
    platform: str,
    platform_user_id: str,
    *,
    tenant_id: str | None = None,
    delete_mapping: bool = False,
) -> int:
    """GDPR erasure for ``ephemeral_pseudonyms.handle`` -- the one raw-handle store.

    Wipes the stored handle of every pseudonym minted for the platform account (all
    tenants when ``tenant_id`` is ``None``). The pseudonym UUID is kept by default so
    data already keyed on it stays consistent -- with the handle gone it names no one
    and the display-name / handle-lookup paths report it unresolved. ``delete_mapping``
    additionally removes the (platform, platform_user_id) -> pseudonym row, severing
    the account from the UUID for good (a later mint starts a fresh pseudonym). Returns
    the number of rows affected; the action is recorded in ``identity_resolution_events``
    (no PII). Operator/DSAR path only -- deliberately not exposed on the gRPC surface.
    """
    if not _PLATFORM_RE.match(platform):
        raise IdentityValidationError("platform invalid")
    if not platform_user_id or len(platform_user_id) > 255 or _CONTROL_RE.search(platform_user_id):
        raise IdentityValidationError("platform_user_id invalid")

    async def work() -> int:
        tenant_pk = await _tenant_pk(async_dal, tenant_id) if tenant_id else None
        rows = await async_dal.executesql_async(
            "SELECT erase_ephemeral_pseudonym_handles(%s, %s, %s, %s)",
            [platform, platform_user_id, tenant_pk, delete_mapping],
        )
        if not rows or rows[0][0] is None:
            raise IdentityResolutionError("erasure returned no count")
        return int(rows[0][0])

    affected = await _observed("erase_pseudonym_handles", 1, work)
    mode = "delete_mapping" if delete_mapping else "handle_only"
    _erasures.add(affected, {"mode": mode})
    logger.info(
        "pseudonym erasure applied",
        extra={
            "action": "identity_erase_pseudonym",
            "mode": mode,
            "affected": affected,
            "result": "ok",
        },
    )
    return affected


async def collect_uuid_unavailability(async_dal: Any) -> dict[str, int]:
    """Count community members whose ``user_uuid`` is NULL, by reason, and publish the gauge.

    The membership trigger fails closed (NULL) when a uuid cannot be derived or collides
    inside a community, recording the reason on the row; this surfaces those rows as the
    ``hub_api.identity.members_uuid_unavailable`` gauge and a WARNING when the picture
    changes, so "unavailable" is a visible state rather than a silent one.
    """
    rows = await async_dal.executesql_async(
        "SELECT user_uuid_unavailable_reason, count(*) FROM community_members "
        "WHERE user_uuid IS NULL AND user_uuid_unavailable_reason IS NOT NULL GROUP BY 1"
    )
    counts = {str(r[0]): int(r[1]) for r in rows or []}
    changed = counts != {k: v for k, v in _unavailable_counts.items() if v}
    _unavailable_counts.clear()
    _unavailable_counts.update(counts)
    if counts and changed:
        logger.warning(
            "community members have no derivable user_uuid (fail-closed: unavailable)",
            extra={"action": "identity_uuid_unavailable", "counts": counts},
        )
    else:
        logger.debug(
            "user_uuid availability collected",
            extra={"action": "identity_uuid_unavailable", "counts": counts},
        )
    return counts


async def run_uuid_availability_monitor(
    async_dal: Any,
    interval_seconds: float = 60.0,
    *,
    is_ready: Callable[[], bool] | None = None,
) -> None:
    """Refresh :func:`collect_uuid_unavailability` forever; a failed pass never stops the loop.

    Started once from hub-api startup. ``is_ready`` (schema bootstrap finished) gates each
    pass so a fresh/behind schema is not logged as a failure. A collection error is logged
    with its traceback and retried on the next tick -- telemetry failure is never a request
    failure.
    """
    while True:
        try:
            if is_ready is None or is_ready():
                await collect_uuid_unavailability(async_dal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "user_uuid availability collection failed",
                extra={"action": "identity_uuid_unavailable", "result": "error"},
            )
        await asyncio.sleep(interval_seconds)
