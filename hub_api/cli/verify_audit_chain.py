"""Verify the tamper-evident audit chains straight from the database (cron / CI / on-call).

Run as ``python3 -m cli.verify_audit_chain`` from hub-api's ``/app`` WORKDIR (same convention as
``cli.seed_core_bundles``) with the usual ``DATABASE_URL`` / ``DB_*`` environment. It recomputes
every chain from genesis (or just the chains named with ``--chain``) and exits non-zero on any
inconsistency, so a Kubernetes ``CronJob`` or a CI step can page on tampering without anyone
calling the HTTP API. No feature-flag/entitlement check applies: a verifier must be able to run
against whatever is in the table.

Exit codes (a check that cannot fail is not a check -- zero chains examined is a failure):

* ``0`` -- at least one chain examined and every one intact;
* ``1`` -- at least one chain is BROKEN (tampering, or a pinned head no longer matches);
* ``2`` -- nothing was examined (no chains / all empty) and ``--allow-empty`` was not given, or
  the arguments were invalid;
* ``3`` -- the check itself could not run (database unreachable, ``audit_events`` missing).

``--expect CHAIN=SEQ:HASH`` pins a chain's head (from ``GET /head`` or an earlier export) so a
truncated tail is detected too; the chain alone cannot see that. Output is one line per chain plus
a totals line with the denominators; it contains chain ids, sequence numbers and hashes only.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from flask_core.db_errors import describe_db_error, format_sanitized_traceback

from config import HubAPIConfig
from services.audit_chain import ChainStatus
from services.audit_service import AuditError, AuditService, VerificationReport
from services.bundle_install_dal import build_install_dal

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_NOTHING_EXAMINED = 2
EXIT_CANNOT_RUN = 3

_EXPECT_RE = re.compile(
    r"^(?P<chain>[A-Za-z0-9:_.\-]{1,64})=(?P<seq>[1-9][0-9]*):(?P<hash>[0-9a-f]{64})$"
)


@dataclass(slots=True, frozen=True)
class Pin:
    """An externally pinned chain head."""

    seq: int
    record_hash: str


@dataclass(slots=True, frozen=True)
class RunSummary:
    """Totals for one verifier run -- the denominators that make 'clean' mean something."""

    chains: int
    intact: int
    broken: int
    empty: int
    records: int

    def exit_code(self, *, allow_empty: bool) -> int:
        """Map the totals to the documented exit code."""
        if self.broken:
            return EXIT_BROKEN
        if self.intact == 0 and not allow_empty:
            return EXIT_NOTHING_EXAMINED
        return EXIT_OK


def parse_pins(values: Sequence[str]) -> dict[str, Pin]:
    """Parse repeated ``--expect CHAIN=SEQ:HASH`` arguments; raises ``ValueError`` if malformed."""
    pins: dict[str, Pin] = {}
    for value in values:
        match = _EXPECT_RE.match(value)
        if match is None:
            raise ValueError(f"--expect must look like CHAIN=SEQ:HASH (got {value!r})")
        pins[match["chain"]] = Pin(seq=int(match["seq"]), record_hash=match["hash"])
    return pins


def _line(report: VerificationReport) -> str:
    """One human/grep-friendly line per chain (no user data: ids, counts, hashes)."""
    result = report.verification
    base = (
        f"chain={report.chain_id} status={result.status.value} examined={result.examined} "
        f"complete={str(report.complete).lower()}"
    )
    if result.status is ChainStatus.BROKEN and result.break_ is not None:
        return f"{base} first_bad_seq={result.break_.seq} reason={result.break_.reason.value}"
    if result.head_seq is not None:
        return f"{base} head_seq={result.head_seq} head_hash={result.head_hash}"
    return base


async def verify_chains(
    service: AuditService,
    chain_ids: Sequence[str] | None,
    pins: dict[str, Pin],
    out: list[str],
) -> RunSummary:
    """Verify ``chain_ids`` (default: every chain with records); report lines go to ``out``."""
    targets = list(chain_ids) if chain_ids else await service.chain_ids()
    intact = broken = empty = records = 0
    for chain_id in targets:
        pin = pins.get(chain_id)
        report = await service.verify(
            chain_id,
            expected_head_seq=pin.seq if pin else None,
            expected_head_hash=pin.record_hash if pin else None,
            max_records=2**31,
        )
        out.append(_line(report))
        records += report.verification.examined
        status = report.verification.status
        if status is ChainStatus.INTACT:
            intact += 1
        elif status is ChainStatus.BROKEN:
            broken += 1
        else:
            empty += 1
    return RunSummary(
        chains=len(targets), intact=intact, broken=broken, empty=empty, records=records
    )


async def _run(args: argparse.Namespace) -> int:
    """Open the database, verify, print, and return the exit code."""
    try:
        pins = parse_pins(args.expect)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_NOTHING_EXAMINED
    config = HubAPIConfig.from_env()
    dal = None
    try:
        dal = await build_install_dal(config.database_url, pool_size=2)
        service = AuditService(dal)
        lines: list[str] = []
        summary = await verify_chains(service, args.chain, pins, lines)
    except AuditError as exc:
        sys.stderr.write(f"error: audit verification could not run: {exc}\n")
        return EXIT_CANNOT_RUN
    except Exception as exc:
        # Value-free: a driver message can embed bound values, so only the type + the frames.
        sys.stderr.write(f"error: audit verification could not run: {describe_db_error(exc)}\n")
        sys.stderr.write(format_sanitized_traceback(exc) + "\n")
        return EXIT_CANNOT_RUN
    finally:
        if dal is not None:
            await dal.close()
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.write(
        f"TOTAL chains={summary.chains} intact={summary.intact} broken={summary.broken} "
        f"empty={summary.empty} records_examined={summary.records}\n"
    )
    code = summary.exit_code(allow_empty=args.allow_empty)
    if code == EXIT_NOTHING_EXAMINED:
        sys.stderr.write("error: no audit records were examined; a zero-denominator check fails\n")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--chain", action="append", default=[], help="verify only this chain id")
    parser.add_argument(
        "--expect", action="append", default=[], help="pin a head: CHAIN=SEQ:HASH (repeatable)"
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="exit 0 when there are no records at all (deployments with no entitled tenant)",
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - script-execution guard, never hit under pytest
    sys.exit(main())
