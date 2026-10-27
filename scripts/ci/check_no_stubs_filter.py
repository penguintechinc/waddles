"""Filters raw stub-marker / silent-fallback findings against the repo's
tracked-deferral mechanisms for scripts/ci/check-no-stubs.sh.

Two independent mechanisms, by design (see .ci/stub-allowlist.yml and
.ci/silent-fallback-baseline.json headers for why they differ):

- Stub markers (TODO/FIXME/not-wired/not-implemented/etc.): matched against
  `.ci/stub-allowlist.yml` -- exact {path, pattern} entries, each requiring a
  non-empty `issue`. Deferral here is deliberate, one entry per reviewed file.
- Silent-fallback (semgrep) findings: matched against
  `.ci/silent-fallback-baseline.json` -- a frozen, line-hash-keyed snapshot
  (gh-605) of pre-existing debt too large to triage file-by-file in one pass;
  the gate fails only on findings NOT in that snapshot (ratchet, same
  pattern as scripts/lint.sh's .checks-baseline).

Reads a JSON array of findings on stdin:
  [{"kind": "stub", "path": ..., "line": int, "text": ...}, ...]
  [{"kind": "fallback", "rule_id": ..., "path": ..., "line": int, "text": ...}, ...]

Exit 0 and prints a summary JSON if every finding is covered and every
allowlist entry has a non-empty issue; exit 1 otherwise (summary still
printed, to stdout, so the caller can log it before failing).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

import yaml


def load_allowlist(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a top-level YAML list, got {type(data).__name__}")
    return data


def load_baseline(path: str) -> set[tuple[str, str, str]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        (e["rule_id"], e["path"], e["line_hash"])
        for e in data.get("entries", [])
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_no_stubs_filter.py <stub-allowlist.yml> <silent-fallback-baseline.json>", file=sys.stderr)
        return 2

    allowlist_path, baseline_path = sys.argv[1], sys.argv[2]
    findings = json.load(sys.stdin)

    allowlist_errors = []
    allowlist = load_allowlist(allowlist_path)
    for i, entry in enumerate(allowlist):
        issue = entry.get("issue")
        if not issue or not str(issue).strip():
            allowlist_errors.append(f"{allowlist_path} entry #{i} (path={entry.get('path')!r}): missing/empty 'issue'")
        for required in ("path", "pattern"):
            if not entry.get(required):
                allowlist_errors.append(f"{allowlist_path} entry #{i}: missing '{required}'")

    baseline_keys = load_baseline(baseline_path)

    stub_total = 0
    stub_allowlisted = 0
    stub_blocked = []

    fallback_total = 0
    fallback_baselined = 0
    fallback_blocked = []

    compiled_allowlist = []
    for entry in allowlist:
        try:
            compiled_allowlist.append((entry["path"], re.compile(entry["pattern"], re.IGNORECASE)))
        except re.error as e:
            allowlist_errors.append(f"{allowlist_path} path={entry.get('path')!r}: invalid pattern regex: {e}")

    for finding in findings:
        if finding["kind"] == "stub":
            stub_total += 1
            matched = any(
                finding["path"] == path and rx.search(finding["text"])
                for path, rx in compiled_allowlist
            )
            if matched:
                stub_allowlisted += 1
            else:
                stub_blocked.append(finding)
        else:
            fallback_total += 1
            line_hash = hashlib.sha256(finding["text"].strip().encode("utf-8")).hexdigest()[:16]
            key = (finding["rule_id"], finding["path"], line_hash)
            if key in baseline_keys:
                fallback_baselined += 1
            else:
                fallback_blocked.append(finding)

    summary = {
        "stub_markers": {
            "total": stub_total,
            "allowlisted": stub_allowlisted,
            "blocked": len(stub_blocked),
        },
        "silent_fallback": {
            "total": fallback_total,
            "baselined": fallback_baselined,
            "blocked": len(fallback_blocked),
        },
        "allowlist_errors": allowlist_errors,
    }
    print(json.dumps(summary, indent=2))

    if stub_blocked:
        print("\n=== BLOCKED stub-marker findings (not in .ci/stub-allowlist.yml) ===", file=sys.stderr)
        for f in stub_blocked:
            print(f"  {f['path']}:{f['line']}: {f['text'].strip()}", file=sys.stderr)

    if fallback_blocked:
        print("\n=== BLOCKED silent-fallback findings (new, not in .ci/silent-fallback-baseline.json) ===", file=sys.stderr)
        for f in fallback_blocked:
            print(f"  [{f['rule_id']}] {f['path']}:{f['line']}: {f['text'].strip()}", file=sys.stderr)

    if allowlist_errors:
        print("\n=== .ci/stub-allowlist.yml errors ===", file=sys.stderr)
        for e in allowlist_errors:
            print(f"  {e}", file=sys.stderr)

    if stub_blocked or fallback_blocked or allowlist_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
