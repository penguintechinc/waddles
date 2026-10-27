"""Marketplace source auto-update detection (Phase 1, Task 8; design 4.5).

A scheduled pass asks each external source's git host -- THROUGH the allowlisting git-proxy, via
a hardened `git ls-remote` (no clone, no build, no scan, no install) -- for the commit its
configured `ref` now points at. A commit different from the pin is recorded as
`pending_commit_sha` (status `NEEDS_APPROVAL`) and admins are notified via the audit chain. The pin
NEVER advances here: only the human `approve_update` (which, for sandbox sources, also needs a
fresh trust-ack) promotes pending -> pinned, so a hostile upstream push can never run unreviewed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from penguin_dal import AsyncDB

from services import bundle_audit
from services import marketplace_source_scanner as _scanner
from services.bundle_telemetry import bundle_span, get_meter
from services.errors import ApiError
from services.marketplace_source_guards import GuardLimits
from services.marketplace_source_scanner import (
    PROXY_ENV,
    ScanRejectedError,
    _git_argv,
    _git_env,
    _run_git,
)
from services.marketplace_source_service import (
    SourceRecord,
    SourceStatus,
    list_sources,
    record_pending_update,
)
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

AUDIT_ACTION = "marketplace_source_update_available"
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@+-]{0,254}$")
_SHA_LINE_RE = re.compile(r"^([0-9a-f]{40})\t(\S+)$")
_LS_REMOTE_TIMEOUT_S = 30.0
_CHECKED_STATUSES = (SourceStatus.ACTIVE, SourceStatus.NEEDS_APPROVAL)


class UpdateOutcome(StrEnum):
    """Result of checking one source for an upstream update."""

    UNCHANGED = "unchanged"
    PENDING = "pending"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(slots=True, frozen=True)
class RescanSummary:
    """Counts for one rescan pass; `examined` is the denominator (zero examined is not a pass)."""

    examined: int
    unchanged: int
    pending: int
    skipped: int
    errors: int


def _pick_sha(output: str, ref: str) -> str | None:
    """Choose the commit for `ref` from `ls-remote` output (peeled tag > branch > tag > HEAD)."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = _SHA_LINE_RE.match(line)
        if match:
            found[match.group(2)] = match.group(1)
    for name in (f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if name in found:
            return found[name]
    return None


async def resolve_latest_commit(source: SourceRecord) -> str:
    """Resolve the commit `source.ref` points at now, through the git-proxy, with no clone.

    Raises `ScanRejectedError` on a missing proxy (direct egress denied), a rejected URL/ref, a
    git failure/timeout, or an unresolvable ref.
    """
    if source.git_url is None or source.ref is None:
        raise ScanRejectedError("no_url", "source has no git_url/ref")
    if not _REF_RE.fullmatch(source.ref):
        raise ScanRejectedError("bad_ref", "ref contains unsupported characters")
    local = _scanner._test_remote is not None
    try:
        await validate_outbound_url(source.git_url, allowed_schemes=("https",))
    except ApiError as exc:
        raise ScanRejectedError("url_rejected", str(exc.message)) from exc
    proxy = os.environ.get(PROXY_ENV)
    if not proxy and not local:
        raise ScanRejectedError(
            "no_egress_proxy", f"{PROXY_ENV} is not configured (direct egress denied)"
        )
    remote = _scanner._test_remote if local and _scanner._test_remote else source.git_url
    limits = GuardLimits(clone_timeout_s=_LS_REMOTE_TIMEOUT_S)
    with tempfile.TemporaryDirectory(prefix="mktupd-") as tmp:
        root = Path(tmp)
        argv = _git_argv(
            "ls-remote", "--", remote, source.ref,
            f"refs/heads/{source.ref}", f"refs/tags/{source.ref}", f"refs/tags/{source.ref}^{{}}",
            proxy=None if local else proxy, local=local,
        )  # fmt: skip
        out = await _run_git(
            argv,
            cwd=root,
            env=_git_env(root, proxy=None if local else proxy),
            limits=limits,
            deadline=time.monotonic() + _LS_REMOTE_TIMEOUT_S,
        )
    sha = _pick_sha(out, source.ref)
    if sha is None:
        raise ScanRejectedError("ref_not_found", "ref did not resolve to a commit")
    return sha


async def check_source_for_update(dal: AsyncDB, source: SourceRecord) -> UpdateOutcome:
    """Check one source; record `pending_commit_sha` + notify admins when upstream moved.

    Skips built-in, un-pinned and non-ACTIVE/NEEDS_APPROVAL (incl. DISABLED) sources. Never
    advances the pin, scans, builds or installs. Per-source failures return `ERROR`, never raise.
    """
    if (
        source.is_builtin
        or source.pinned_commit_sha is None
        or (source.status not in _CHECKED_STATUSES)
    ):
        return UpdateOutcome.SKIPPED
    sid = str(source.public_id)
    try:
        async with bundle_span("marketplace.source.update_check", source_public_id=sid):
            latest = await resolve_latest_commit(source)
            if latest == source.pinned_commit_sha or latest == source.pending_commit_sha:
                return UpdateOutcome.UNCHANGED
            updated = await record_pending_update(dal, source.public_id, latest_sha=latest)
    except (ScanRejectedError, ApiError) as exc:
        logger.warning(
            "marketplace source update check failed",
            extra={"source_public_id": sid, "reason": getattr(exc, "reason", type(exc).__name__)},
        )
        return UpdateOutcome.ERROR
    if updated is None:  # raced (disabled/approved/already recorded): nothing changed
        return UpdateOutcome.UNCHANGED
    try:
        await bundle_audit.record(
            dal,
            actor_id=None,
            action=AUDIT_ACTION,
            target_type="marketplace_source",
            target_id=sid,
            details={
                "pending_commit_sha": latest,
                "pinned_commit_sha": source.pinned_commit_sha,
                "build_mode": int(source.build_mode),
            },
        )
    except Exception:
        logger.exception(
            "marketplace source update notification failed", extra={"source_public_id": sid}
        )
        return UpdateOutcome.ERROR
    logger.info(
        "marketplace source update pending approval",
        extra={"source_public_id": sid, "pending_commit_sha": latest},
    )
    return UpdateOutcome.PENDING


async def rescan_all(dal: AsyncDB) -> RescanSummary:
    """Check every external source once (failures isolated per source) and return the counts."""
    meter = get_meter()
    counter = meter.create_counter("marketplace.source.update_check.total")
    sources = await list_sources(dal)
    tally = dict.fromkeys(UpdateOutcome, 0)
    examined = 0
    for source in sources:
        outcome = await check_source_for_update(dal, source)
        tally[outcome] += 1
        counter.add(1, {"outcome": outcome.value})
        if outcome != UpdateOutcome.SKIPPED:
            examined += 1
        await asyncio.sleep(0)
    return RescanSummary(
        examined=examined,
        unchanged=tally[UpdateOutcome.UNCHANGED],
        pending=tally[UpdateOutcome.PENDING],
        skipped=tally[UpdateOutcome.SKIPPED],
        errors=tally[UpdateOutcome.ERROR],
    )
