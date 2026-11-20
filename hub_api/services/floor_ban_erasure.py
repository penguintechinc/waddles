"""GDPR Art. 17 erasure of the automated hard-ban outbox (`bundle_reputation_floor_bans`).

The outbox is the audit trail of enforced automated bans AND the durable queue of bans not yet
delivered, so erasure cannot simply DELETE the subject's rows. The only write path hub-api has to
the table is the SECURITY DEFINER function `reputation_floor_ban_erase_subject` (alembic 0114);
hub-api holds no DELETE/UPDATE on the table. Per `(tenant, platform, subject token)` the function:

* deletes refused rows (status 6 -- the floor was never breached, nothing was ever enforced);
* pseudonymises done rows (status 2 -- a sender-confirmed ban): `user_uuid` is replaced by a one-way
  derivative of the keyed suppression code, so the row no longer names the person; never deleted;
* leaves pending rows (1/3/4/5/7) untouched -- erasure never cancels an unsent ban.

`ban_suppression.collect_active_restrictions` has already turned every done/pending row into an
`AUTO_BAN` suppression restriction, written in the same transaction, so the erased user still cannot
re-join. A done row whose subject has no suppression code (no linked identity on that platform)
fails the erasure loudly (rolled back) rather than leaving the identifier or deleting the evidence.

Synchronous like `data_privacy_service._sync_erase()` (one pool-thread transaction); never commits.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any

from services import ban_suppression
from services.ban_suppression import ActiveRestriction, FloorBanRow
from services.sql_util import execute_positional
from services.suppression_keys import SuppressionKeySource, current_code

logger = logging.getLogger(__name__)

#: `deletion_scope` keys this step reports (counts of rows, never identifiers).
COUNT_DELETED = "bundle_reputation_floor_bans"
COUNT_PSEUDONYMISED = "bundle_reputation_floor_bans__scrubbed"
COUNT_PENDING = "bundle_reputation_floor_bans__pending_kept"

_ERASE_SQL = (
    "SELECT r_deleted, r_pseudonymised, r_pending FROM "
    "reputation_floor_ban_erase_subject($1::uuid, $2::bigint, $3::text, $4::bytea)"
)

#: `(token, tenant_id, platform, code_or_None) -> (deleted, pseudonymised, pending_left)`.
EraseCall = Callable[[str, int, str, "bytes | None"], tuple[int, int, int]]


class FloorBanErasureError(RuntimeError):
    """The floor-ban rows could not be erased safely; the whole erasure must roll back."""


def _postgres_call(dal: Any) -> EraseCall:
    """The production call: the SECURITY DEFINER function over the erasure transaction's DAL."""

    def call(token: str, tenant_id: int, platform: str, code: bytes | None) -> tuple[int, int, int]:
        rows = execute_positional(dal, _ERASE_SQL, placeholders=[token, tenant_id, platform, code])
        if not rows:
            raise FloorBanErasureError("reputation_floor_ban_erase_subject returned no result")
        deleted, pseudonymised, pending = rows[0]
        return int(deleted), int(pseudonymised), int(pending)

    return call


def _code_for(
    restrictions: Collection[ActiveRestriction],
    keys: SuppressionKeySource,
    *,
    tenant_id: int,
    platform: str,
) -> bytes | None:
    """The keyed suppression code (32 bytes) of the subject's identity on `(tenant, platform)`.

    The first matching restriction in a stable order supplies it, so repeated calls agree. None
    when the subject has no coded identity there (the SQL function then refuses to touch done rows).
    """
    matching = sorted(
        (r for r in restrictions if r.tenant_id == tenant_id and r.platform == platform),
        key=lambda r: r.platform_user_id,
    )
    if not matching:
        return None
    code = current_code(keys, tenant_id, platform, matching[0].platform_user_id)
    return bytes.fromhex(code.hmac)


def erase_floor_ban_rows(
    dal: Any,
    *,
    user_uuids: Collection[str],
    restrictions: Collection[ActiveRestriction],
    keys: SuppressionKeySource | None,
    call: EraseCall | None = None,
) -> dict[str, int]:
    """Erase the subject's floor-ban rows; returns non-zero counts keyed for `deletion_scope`.

    `call` is a test seam (the SQL function needs Postgres); production leaves it unset.
    A deployment without the table has nothing to erase and reports no counts.
    """
    rows: list[FloorBanRow] = ban_suppression.floor_ban_rows(dal, user_uuids)
    if not rows:
        return {}
    erase = call or _postgres_call(dal)
    deleted = pseudonymised = pending = 0
    groups = sorted({(r.tenant_id, r.platform, r.user_uuid) for r in rows})
    for tenant_id, platform, token in groups:
        has_done = any(
            r.status == 2 and (r.tenant_id, r.platform, r.user_uuid) == (tenant_id, platform, token)
            for r in rows
        )
        code = None
        if has_done:
            if keys is None:
                raise FloorBanErasureError(
                    "suppression keys are required to pseudonymise a confirmed automated ban"
                )
            code = _code_for(restrictions, keys, tenant_id=tenant_id, platform=platform)
        d, p, left = erase(token, tenant_id, platform, code)
        deleted += d
        pseudonymised += p
        pending += left
    logger.info(
        "dsar.floor_ban_rows_erased",
        extra={"deleted": deleted, "pseudonymised": pseudonymised, "pending_kept": pending},
    )
    return {
        key: value
        for key, value in (
            (COUNT_DELETED, deleted),
            (COUNT_PSEUDONYMISED, pseudonymised),
            (COUNT_PENDING, pending),
        )
        if value
    }
