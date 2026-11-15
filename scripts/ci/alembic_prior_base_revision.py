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

A PR that adds a merge revision (base already has two heads, the PR joins them)
enters history at several parents: stdout is then those parents, sorted and
comma-joined. Several independent new heads stay a real error.

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
MERGE_DOWN_REVISION_RE = re.compile(
    r"^down_revision\s*=\s*[(\[]([^)\]]*)[)\]]", re.MULTILINE
)
QUOTED_ID_RE = re.compile(r'["\']([^"\']+)["\']')

# A revision's parent(s): None (root), one id, or a tuple of ids (a merge revision).
Down = str | tuple[str, ...] | None


def parse_revision(source: str) -> tuple[str, Down]:
    """Extract `(revision, down_revision)` from one migration file's source text.

    `down_revision` is a single id for a normal revision, a tuple of ids for a
    merge revision (`down_revision = ("a", "b")`), or None for a root.
    """
    rev_match = REVISION_RE.search(source)
    if not rev_match:
        raise ValueError("no `revision = ...` assignment found")
    down_match = DOWN_REVISION_RE.search(source)
    if down_match:
        return rev_match.group(1), down_match.group(1)
    merge_match = MERGE_DOWN_REVISION_RE.search(source)
    if merge_match:
        parents = tuple(QUOTED_ID_RE.findall(merge_match.group(1)))
        if not parents:
            raise ValueError("`down_revision` tuple holds no quoted revision ids")
        return rev_match.group(1), parents if len(parents) > 1 else parents[0]
    return rev_match.group(1), None


def _parents(down: Down) -> tuple[str, ...]:
    """Normalise a `down_revision` value to a (possibly empty) tuple of parent ids."""
    if down is None:
        return ()
    if isinstance(down, str):
        return (down,)
    return tuple(down)


def revisions_at(ref: str) -> dict[str, Down]:
    """`revision_id -> down_revision_id` for every `alembic/versions/*.py` file at `ref`."""
    listing = subprocess.run(  # noqa: S603 -- fixed argv, no shell, CI-only
        ["git", "ls-tree", "-r", "--name-only", ref, "--", "alembic/versions"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    result: dict[str, Down] = {}
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
    base_revisions: dict[str, Down], head_revisions: dict[str, Down]
) -> tuple[int, str]:
    """Compute the prior-base revision(s) from two `revision -> down_revision` maps.

    Pure (no subprocess/git) so it can be unit tested directly against
    constructed revision maps. Returns `(exit_code, output)` per the module
    docstring's exit-code contract: `(0, <revision>)`, `(2, <diagnostic>)` for
    "no new revisions" (benign), or `(1, <diagnostic>)` for a real error.

    Normally the new revisions stack linearly on ONE existing revision. When a
    PR adds a merge revision (e.g. joining a base that already has two heads),
    the new revisions enter pre-existing history at several parents: the
    output is then the sorted, comma-joined parents, accepted only if the new
    revisions still converge on exactly one new head (anything else is a real,
    still-fatal branching error).
    """
    new_ids = set(head_revisions) - set(base_revisions)
    if not new_ids:
        return 2, (
            "no new alembic/versions/*.py revisions between base and head "
            "-- nothing for this gate to verify"
        )

    entry_points = {
        parent
        for rid in new_ids
        for parent in _parents(head_revisions[rid])
        if parent not in new_ids
    }
    new_heads = new_ids - {p for rid in new_ids for p in _parents(head_revisions[rid])}
    has_merge = any(len(_parents(head_revisions[rid])) > 1 for rid in new_ids)
    ok_multi = has_merge and len(new_heads) == 1
    if not entry_points or (len(entry_points) != 1 and not ok_multi) or len(new_heads) != 1:
        return 1, (
            f"expected a single linear stack (or one merge) entering pre-existing "
            f"history, found {len(entry_points)} entry point(s) "
            f"{sorted(entry_points)} and {len(new_heads)} new head(s) "
            f"{sorted(new_heads)} -- new revisions {sorted(new_ids)} do not form "
            f"a single linear stack"
        )

    return 0, ",".join(sorted(entry_points))


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
