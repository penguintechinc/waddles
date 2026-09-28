"""Internal, service-only batched display-name resolution.

The PII boundary's one egress path for `core/svc_action`'s chat-egress
detokenizer (spec
`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
S10.1/S10.4).

Backs `blueprints/v1.internal_users.py`'s
`POST /api/v1/internal/users/display-names`. `svc_action` sends a batch of
`{user:<uuid>}` tokens it cannot itself classify -- some are real,
tenant-linked `hub_users` identities, some are PR #429's (`feature/ingest-
pii-tokenization`) deterministic ephemeral pseudonyms for an unknown/
unlinked platform account. This module resolves both against their own
tenant-scoped store and simply omits a UUID it cannot place in either --
never an error, never a raw UUID, never cross-tenant data (`rules/
critical-rules.md` PII Tokenization / `rules/security.md` Tenant
Isolation).

**Schema note (real users):** `hub_users.id` is today an integer `SERIAL`
primary key with no UUID identity column, so the "real linked user" path
below is written against a `hub_users.uuid` column that does not exist on
this branch yet -- feature-checked (`"uuid" in dal.hub_users.fields`) so it
is a documented no-op until that column lands, rather than a hard failure.
`community_members.user_id` (the column `bundle_active_set::identity
::resolve_linked_user_id` reads on the svc_action side) is itself a
stringified `hub_users.id` today, not a UUID -- confirmed against
`hub_api/services/admin_service.py` etc's own `str(user_id)` comparisons --
so no `{user:<uuid>}` token round-trips through this "linked" path in
practice until a dedicated migration adds a real UUID identity to
`hub_users`. That migration is out of scope for this landing; this module
is written so wiring it up later is additive (drop the feature check),
not a rewrite.

**Schema note (ephemeral pseudonyms):** `ephemeral_identities(tenant_id,
pseudonym, platform, platform_user_id, handle, last_seen, expires_at)` is
being added by PR #429 (`feature/ingest-pii-tokenization`) and is not yet
present on this branch -- feature-checked (`"ephemeral_identities" in
dal.tables`) for the same reason. **This PR must merge after #429** so the
table exists in production before this code path is exercised for real;
until then it is a safe, tolerant no-op (fail-safe-empty, same posture as
every other unresolvable UUID).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Hard cap on one request's `user_uuids` batch -- matches
#: `blueprints/v1/internal_users.py`'s own input validation; kept here too
#: so a direct service-layer caller (tests) can't bypass it.
MAX_USER_UUIDS = 100


def _valid_uuids(raw: list[str]) -> list[str]:
    """Filters `raw` down to syntactically valid UUID strings, de-duplicated.

    A forged/malformed entry is silently dropped rather than raising --
    the caller already validated `raw` is a list of at most
    `MAX_USER_UUIDS` strings; a single bad entry must not fail the whole
    batch for the UUIDs that ARE valid.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        try:
            normalized = str(UUID(entry))
        except (ValueError, AttributeError, TypeError):
            continue
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


async def _resolve_linked_users(dal: Any, *, tenant_id: int, uuids: list[str]) -> dict[str, str]:
    """Real, tenant-linked `hub_users` display names.

    Tolerant no-op until `hub_users.uuid` exists (see this module's docstring).
    """
    if not uuids or "uuid" not in dal.hub_users.fields:
        return {}

    # Tenant-scope via the same community-membership hop `flask_core.
    # tenancy.tenant_scoped` uses for any table with no direct `tenant_id`
    # column: a hub_users row is "in" this tenant if it is a member of at
    # least one of this tenant's communities. `community_members.user_id`
    # is a VARCHAR holding `str(hub_users.id)` (see this module's
    # docstring), so the join back to `hub_users.id` (INTEGER) compares
    # `hub_users.id` cast to its string form -- pydal's `.astype("string")`
    # emits `CAST(... AS TEXT)` on every backend this repo supports
    # (Postgres/sqlite), matching the string this VARCHAR column holds.
    tenant_community_ids = dal(dal.communities.tenant_id == tenant_id)._select(dal.communities.id)
    member_user_ids = dal(dal.community_members.community_id.belongs(tenant_community_ids))._select(
        dal.community_members.user_id, distinct=True
    )

    rows = dal(
        (dal.hub_users.uuid.belongs(uuids))
        & (dal.hub_users.id.astype("string").belongs(member_user_ids))
    ).select(dal.hub_users.uuid, dal.hub_users.display_name, dal.hub_users.username)
    return {
        str(row.uuid): (row.display_name or row.username or "")
        for row in rows
        if row.display_name or row.username
    }


async def _resolve_ephemeral_pseudonyms(
    dal: Any, *, tenant_id: int, uuids: list[str]
) -> dict[str, str]:
    """`ephemeral_identities` display names (the `handle` column).

    Tolerant no-op until PR #429's table lands (see this module's docstring).
    """
    if not uuids or "ephemeral_identities" not in dal.tables:
        return {}

    now = datetime.now(UTC)
    table = dal.ephemeral_identities
    query = (
        (table.pseudonym.belongs(uuids))
        & (table.tenant_id == tenant_id)
        & ((table.expires_at == None) | (table.expires_at > now))  # noqa: E711
    )
    rows = dal(query).select(table.pseudonym, table.handle)
    return {str(row.pseudonym): row.handle for row in rows if row.handle}


async def resolve_display_names(
    dal: Any, *, tenant_id: int, user_uuids: list[str]
) -> dict[str, str]:
    """Batched `{uuid: display_name}` lookup, strictly scoped to `tenant_id`.

    Never raises for an individual unresolvable UUID -- a cross-tenant,
    erased, unknown, or malformed entry is simply absent from the returned
    mapping (`egress_detokenizer::NameResolver`'s own fail-safe-empty
    contract on the `svc_action` side). Logs only counts (never the
    UUIDs/names themselves) for audit purposes.
    """
    uuids = _valid_uuids(user_uuids)
    linked = await _resolve_linked_users(dal, tenant_id=tenant_id, uuids=uuids)
    ephemeral = await _resolve_ephemeral_pseudonyms(dal, tenant_id=tenant_id, uuids=uuids)
    resolved = {**linked, **ephemeral}

    logger.info(
        "internal display-name resolution",
        extra={
            "event_type": "AUDIT",
            "action": "resolve_display_names",
            "tenant_id": tenant_id,
            "requested_count": len(user_uuids),
            "resolved_count": len(resolved),
        },
    )
    return resolved
