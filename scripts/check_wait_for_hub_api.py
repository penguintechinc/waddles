#!/usr/bin/env python3
"""regression: non-hub-api workloads must wait for hub-api before starting (alpha 2026-10-01)

Asserts every non-hub-api, non-infrastructure Deployment/Job in
k8s/helm/waddlebot carries the `wait-for-hub-api` initContainer
(waddlebot.waitForHubApiInitContainer, templates/_helpers.tpl). hub-api is now
the one place that creates/validates the schema at startup
(hub_api/bootstrap.py); every other workload depends on it being there and
stable first, same as it previously depended on a pre-install migrate Job
that no longer runs on install.

Exemptions (the only ones that may legitimately skip this initContainer):
  - hub-api itself (templates/hub-api.yaml) -- cannot wait on itself.
  - Infrastructure (Postgres/Valkey/SeaweedFS, templates/infrastructure/*) --
    hub-api depends on these, not the other way around.
  - The auto-provision-keys and db-migrate Jobs -- both run as Helm hooks
    strictly BEFORE hub-api's Deployment is even created (pre-install /
    pre-upgrade), so hub-api cannot be "online" yet when they run; waiting on
    it would deadlock the release.

Fails loudly (never masked) if zero candidate workloads are examined -- a
scanner pointed at a moved/renamed chart path reporting "0 violations" is not
a passing gate, it's a broken one (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "k8s" / "helm" / "waddlebot"

EXEMPT_COMPONENTS = {"hub-api", "db-migrate", "auto-provision-keys"}
#: Everything under templates/infrastructure/ is a dependency hub-api itself
#: needs (Postgres/Valkey/SeaweedFS) or a credential-less network primitive
#: (egress-proxy) with no schema dependency -- never a consumer waiting ON
#: hub-api. Matched by source template path, not a label/name heuristic,
#: so a new file dropped in that directory is exempt by construction.
EXEMPT_PATH_PREFIX = "waddlebot/templates/infrastructure/"

_SOURCE_RE = re.compile(r"^# Source: (.+)$", re.MULTILINE)


def _render() -> list[tuple[str, dict]]:
    """Returns (source_template_path, parsed_doc) pairs, parsed via Helm's own
    `# Source: <path>` marker that precedes every rendered document."""
    result = subprocess.run(
        [
            "helm", "template", "waddlebot", str(CHART_DIR),
            "-f", str(CHART_DIR / "values-alpha.yaml"),
            "--kube-version", "1.30.0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pairs = []
    for chunk in result.stdout.split("\n---\n"):
        match = _SOURCE_RE.search(chunk)
        source = match.group(1) if match else "<unknown>"
        doc = yaml.safe_load(chunk)
        if doc:
            pairs.append((source, doc))
    return pairs


def _is_infra(source: str) -> bool:
    return source.startswith(EXEMPT_PATH_PREFIX)


def _has_wait_container(doc: dict) -> bool:
    spec = doc.get("spec", {}).get("template", {}).get("spec", {}) or {}
    init_containers = spec.get("initContainers") or []
    return any(c.get("name") == "wait-for-hub-api" for c in init_containers)


def main() -> int:
    pairs = _render()
    candidates = []
    for source, doc in pairs:
        if doc.get("kind") not in {"Deployment", "Job"}:
            continue
        labels = doc.get("metadata", {}).get("labels", {}) or {}
        component = labels.get("app.kubernetes.io/component", "")
        if component in EXEMPT_COMPONENTS:
            continue
        if _is_infra(source):
            continue
        candidates.append(doc)

    if not candidates:
        print(
            "FAIL: examined 0 non-hub-api/non-infra Deployment/Job workloads -- "
            "scanner is broken or chart path moved",
            file=sys.stderr,
        )
        return 1

    print(f"Examined {len(candidates)} non-hub-api/non-infra workload(s).")

    violations = []
    for doc in candidates:
        if not _has_wait_container(doc):
            name = doc.get("metadata", {}).get("name", "<unknown>")
            kind = doc.get("kind")
            violations.append(f"{kind}/{name}")

    if violations:
        print(
            f"FAIL: {len(violations)} workload(s) missing the wait-for-hub-api initContainer:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("PASS: every non-hub-api/non-infra workload waits for hub-api.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
