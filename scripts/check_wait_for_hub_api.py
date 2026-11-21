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
  - The auto-provision-keys and db-migrate Jobs -- db-migrate IS the thing
    hub-api's own `/ready` waits ON (hub_api/bootstrap.py observes
    `alembic_version`, never creates/alters it), so waiting on hub-api here
    would be circular; auto-provision-keys is a pre-install/pre-upgrade hook
    that always runs before hub-api's Deployment exists. (db-migrate itself
    is now also post-install, per the 2026-10-01 user decision that the
    schema always comes from this Job -- but it is still exempt from this
    initContainer for the same circularity reason, not because of hook
    timing.)

  - Pre-hub-api hooks (PRE_HUB_API_HOOKS below: provision-secrets,
    postgres-handover) -- exempt ONLY while the rendered Job still carries a
    `helm.sh/hook` annotation that includes `pre-install`. Drop the hook
    annotation (turning it into an ordinary workload) and the exemption stops
    applying, so the Job is checked like every other.

Every exemption carries its reason in the table it lives in; adding one is a
reviewed code change here, never a label/annotation a template can set itself.

Fails loudly (never masked) if zero candidate workloads are examined -- a
scanner pointed at a moved/renamed chart path reporting "0 violations" is not
a passing gate, it's a broken one (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "k8s" / "helm" / "waddlebot"

#: component label -> reason. Exempt unconditionally.
EXEMPT_COMPONENTS: dict[str, str] = {
    "hub-api": "cannot wait on itself",
    "db-migrate": "hub-api's /ready waits ON the schema this Job creates -- circular",
    "auto-provision-keys": "pre-install/pre-upgrade hook: creates keys before hub-api exists",
}
#: component label -> reason. Exempt ONLY while the rendered Job is a Helm
#: `pre-install` hook: Helm runs every pre-install hook to completion before it
#: creates any ordinary resource, so hub-api's Deployment does not exist yet and
#: waiting on it would deadlock the install.
PRE_HUB_API_HOOKS: dict[str, str] = {
    # files/provision-secrets.sh creates every internally generated Secret
    # (DB role passwords, root KEK, HMAC rings) that hub-api itself mounts.
    "provision-secrets": "pre-install hook: creates the Secrets hub-api needs to start",
    # Scales the legacy Deployment/postgres to 0 before the Postgres
    # StatefulSet (which hub-api depends on) starts on the same PVC.
    "postgres-handover": "pre-install hook: hands Postgres PGDATA over before hub-api's database exists",
}
EXEMPT_PATH_PREFIX = "waddlebot/templates/infrastructure/"

_SOURCE_RE = re.compile(r"^# Source: (.+)$", re.MULTILINE)


def _render(chart_dir: Path) -> list[tuple[str, dict]]:
    """Returns (source_template_path, parsed_doc) pairs, parsed via Helm's own
    `# Source: <path>` marker that precedes every rendered document."""
    result = subprocess.run(
        [
            "helm",
            "template",
            "waddlebot",
            str(chart_dir),
            "-f",
            str(chart_dir / "values-alpha.yaml"),
            "--kube-version",
            "1.30.0",
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


def _is_pre_install_hook(doc: dict) -> bool:
    annotations = doc.get("metadata", {}).get("annotations", {}) or {}
    phases = str(annotations.get("helm.sh/hook", ""))
    return "pre-install" in {p.strip() for p in phases.split(",")}


def exemption_reason(source: str, doc: dict) -> str | None:
    """Why this workload may skip wait-for-hub-api, or None if it must wait.

    Pure function over one rendered document, so the exemption rules are unit
    tested without a helm render."""
    labels = doc.get("metadata", {}).get("labels", {}) or {}
    component = labels.get("app.kubernetes.io/component", "")
    if component in EXEMPT_COMPONENTS:
        return EXEMPT_COMPONENTS[component]
    if _is_infra(source):
        return "infrastructure hub-api depends on"
    if (
        component in PRE_HUB_API_HOOKS
        and doc.get("kind") == "Job"
        and _is_pre_install_hook(doc)
    ):
        return PRE_HUB_API_HOOKS[component]
    return None


def find_violations(pairs: list[tuple[str, dict]]) -> tuple[int, int, list[str]]:
    """Returns (examined, exempted, violations) over rendered (source, doc) pairs."""
    examined = 0
    exempted = 0
    violations = []
    for source, doc in pairs:
        if doc.get("kind") not in {"Deployment", "Job"}:
            continue
        if exemption_reason(source, doc) is not None:
            exempted += 1
            continue
        examined += 1
        if not _has_wait_container(doc):
            name = doc.get("metadata", {}).get("name", "<unknown>")
            violations.append(f"{doc.get('kind')}/{name}")
    return examined, exempted, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--chart-dir",
        type=Path,
        default=CHART_DIR,
        help="chart to render with its values-alpha.yaml (default: waddlebot)",
    )
    args = parser.parse_args(argv)

    examined, exempted, violations = find_violations(_render(args.chart_dir))

    if not examined:
        print(
            "FAIL: examined 0 non-hub-api/non-infra Deployment/Job workloads -- "
            "scanner is broken or chart path moved",
            file=sys.stderr,
        )
        return 1

    print(f"Examined {examined} non-hub-api/non-infra workload(s); {exempted} exempt.")

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
