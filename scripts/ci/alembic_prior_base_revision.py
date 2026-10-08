#!/usr/bin/env python3
"""Compute the alembic revision a PR's new migration(s) stack on top of.

Used by `.github/workflows/pr-validation.yml`'s `alembic-migration-chain`
job: diffs revision IDs (not filenames, so a same-PR rename/renumber --
e.g. rebasing a stacked branch onto a freshly-merged predecessor -- never
confuses it) between `alembic/versions/*.py` at the PR base commit and the
current checkout, then walks the new revisions' own `down_revision` chain
to find the single entry point back into pre-existing history. That entry
point is exactly the revision `alembic downgrade` must target to fully
reverse everything this PR adds, which is what the CI gate needs to prove
the new migration(s) both apply and roll back cleanly.

Exit codes (the CI step branches on these -- see pr-validation.yml): `0`
with the revision on stdout (success); `1` with a diagnostic on stderr (a
real error -- the new revisions don't form a single linear stack, still
must fail the job); `2` with a diagnostic on stderr (no new revisions in
this PR -- e.g. a PR that only touches `alembic/tests/**` helpers -- an
intentional, non-error signal the CI step uses to skip the downgrade/
reapply round-trip rather than fail the gate for a no-op).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

REVISION_RE = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
DOWN_REVISION_RE = re.compile(r'^down_revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def parse_revision(source: str) -> tuple[str, str | None]:
    """Extract `(revision, down_revision)` from one migration file's source text."""
    rev_match = REVISION_RE.search(source)
    if not rev_match:
        raise ValueError("no `revision = ...` assignment found")
    down_match = DOWN_REVISION_RE.search(source)
    return rev_match.group(1), (down_match.group(1) if down_match else None)


def revisions_at(ref: str) -> dict[str, str | None]:
    """`revision_id -> down_revision_id` for every `alembic/versions/*.py` file at `ref`."""
    listing = subprocess.run(  # noqa: S603 -- fixed argv, no shell, CI-only
        ["git", "ls-tree", "-r", "--name-only", ref, "--", "alembic/versions"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    result: dict[str, str | None] = {}
    for path in listing:
        if not path.endswith(".py"):
            continue
        source = subprocess.run(  # noqa: S603 -- fixed argv, no shell, CI-only
            ["git", "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        rev, down = parse_revision(source)
        result[rev] = down
    return result


def resolve_prior_base_revision(
    base_revisions: dict[str, str | None], head_revisions: dict[str, str | None]
) -> tuple[int, str]:
    """Compute the prior-base revision from two `revision -> down_revision` maps.

    Pure (no subprocess/git) so it can be unit tested directly against
    constructed revision maps. Returns `(exit_code, output)` per the module
    docstring's exit-code contract: `(0, <revision>)`, `(2, <diagnostic>)` for
    "no new revisions" (benign), or `(1, <diagnostic>)` for a real error.
    """
    new_ids = set(head_revisions) - set(base_revisions)
    if not new_ids:
        return 2, (
            "no new alembic/versions/*.py revisions between base and head "
            "-- nothing for this gate to verify"
        )

    entry_points = {
        head_revisions[rid]
        for rid in new_ids
        if head_revisions[rid] is not None and head_revisions[rid] not in new_ids
    }
    if len(entry_points) != 1:
        return 1, (
            f"expected exactly one entry point into pre-existing history, found "
            f"{len(entry_points)}: {sorted(entry_points)} -- new revisions "
            f"{sorted(new_ids)} do not form a single linear stack"
        )

    return 0, next(iter(entry_points))


def main() -> int:
    """CLI entry point: print the prior base revision to stdout, or a diagnostic to stderr."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True, help="PR base commit (pre-PR tree)")
    parser.add_argument("--head-sha", default="HEAD", help="PR head commit (defaults to HEAD)")
    args = parser.parse_args()

    base_revisions = revisions_at(args.base_sha)
    head_revisions = revisions_at(args.head_sha)

    exit_code, output = resolve_prior_base_revision(base_revisions, head_revisions)
    print(output, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
