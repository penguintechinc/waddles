"""One-off remediation for privileged inbound role-sync bindings purged by migration 0104.

Before 0104 a Discord role could be bound to `community-admin` / `moderator`, so anyone with
Manage Roles in a paired guild may already have promoted themselves. 0104 deletes those bindings
and records each one in `role_sync_purged_bindings`. This module finds the members whose role
plausibly came from one of them -- an active member of that community, holding exactly the
purged binding's `community_role`, with a linked Discord identity (pre-0104 sync left no
ledger, so this is the closest available evidence) -- and:

- default (review): enqueues each into `role_sync_remediation_queue` (status 0 = pending) for an
  admin to review; nothing about the member changes.
- `apply=True`: demotes each still-matching member to the baseline role and marks the queue
  row resolved (status 1). `community-owner` is never touched.

Idempotent (unique `(purged_binding_id, hub_user_id)`); logs carry ids and counts only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.role_sync_service import _set_member_role
from services.schema import bind_auth_tables, bind_guild_pairing_tables

logger = logging.getLogger(__name__)

STATUS_PENDING = 0
STATUS_DEMOTED = 1
STATUS_DISMISSED = 2
_OWNER_ROLE = "community-owner"


@dataclass(slots=True)
class RemediationReport:
    """Denominators for one run -- zero bindings examined is reported, never hidden."""

    purged_bindings_examined: int = 0
    candidates: int = 0
    newly_queued: int = 0
    demoted: int = 0


def _candidates(dal: Any, purged: Any) -> list[Any]:
    """Active members of the purged binding's community holding its role, with a Discord link."""
    bind_auth_tables(dal)
    m = dal.community_members
    rows = dal(
        (m.community_id == purged.community_id)
        & (m.role == purged.community_role)
        & (m.is_active == True)  # noqa: E712 - pydal idiom
    ).select()
    ident = dal.hub_user_identities
    out = []
    for row in rows:
        try:
            hub_user_id = int(row.user_id)
        except (TypeError, ValueError):
            continue
        if row.role == _OWNER_ROLE:
            continue
        linked = dal((ident.hub_user_id == hub_user_id) & (ident.platform == "discord")).count()
        if linked:
            out.append(row)
    return out


def remediate_purged_bindings(
    dal: Any, *, apply: bool = False, baseline_role: str = "member"
) -> RemediationReport:
    """Queue (default) or demote (`apply`) members promoted by a purged privileged binding."""
    bind_guild_pairing_tables(dal)
    report = RemediationReport()
    q = dal.role_sync_remediation_queue
    for purged in dal(dal.role_sync_purged_bindings.id > 0).select():
        report.purged_bindings_examined += 1
        for member in _candidates(dal, purged):
            report.candidates += 1
            hub_user_id = int(member.user_id)
            try:
                existing = (
                    dal((q.purged_binding_id == purged.id) & (q.hub_user_id == hub_user_id))
                    .select()
                    .first()
                )
                if existing is None:
                    row_id = q.insert(
                        purged_binding_id=purged.id,
                        community_id=purged.community_id,
                        hub_user_id=hub_user_id,
                        role_at_review=member.role,
                        status=STATUS_PENDING,
                        created_at=datetime.now(UTC),
                    )
                    report.newly_queued += 1
                    existing = dal(q.id == row_id).select().first()
                if apply and existing.status == STATUS_PENDING:
                    _set_member_role(dal, member, int(purged.community_id), baseline_role)
                    dal(q.id == existing.id).update(
                        status=STATUS_DEMOTED, resolved_at=datetime.now(UTC)
                    )
                    report.demoted += 1
                dal.commit()
            except Exception:
                dal.rollback()
                raise
    logger.info(
        "role_sync.remediation purged_bindings=%d candidates=%d queued=%d demoted=%d apply=%s",
        report.purged_bindings_examined,
        report.candidates,
        report.newly_queued,
        report.demoted,
        apply,
    )
    return report
