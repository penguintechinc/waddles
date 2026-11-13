"""Operator CLI: review / revert roles granted by privileged role-sync bindings purged in 0104.

Run as `python3 -m cli.role_sync_remediate [--apply] [--baseline-role member]` from hub_api with
the usual `DATABASE_URL` env (`make role-sync-remediate [APPLY=1]`). Default only enqueues the
affected members into `role_sync_remediation_queue` for admin review; `--apply` demotes them to
the baseline role. Prints the counts; exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from services.role_sync_remediation import remediate_purged_bindings
from services.sync_worker_dal import open_worker_dal


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, run the remediation once, print the denominators."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="demote (default: queue for review)")
    parser.add_argument("--baseline-role", default="member")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        sys.stderr.write("DATABASE_URL is required\n")
        return 1
    with open_worker_dal(database_url) as adal:
        report = remediate_purged_bindings(
            adal.dal, apply=args.apply, baseline_role=args.baseline_role
        )
    sys.stdout.write(
        f"purged_bindings_examined={report.purged_bindings_examined} "
        f"candidates={report.candidates} newly_queued={report.newly_queued} "
        f"demoted={report.demoted} apply={args.apply}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
