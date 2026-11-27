#!/usr/bin/env python3
"""regression: fresh install migrate ran pre-install before postgres (alpha 2026-10-01)

Two assertions against the rendered chart:

1. No DB-dependent Job (reads DATABASE_URL, or mounts the dedicated
   db-migrate-secret) carries a "pre-install" Helm hook phase. In-chart
   Postgres (templates/infrastructure/postgres.yaml) is a plain/regular
   resource -- Helm only creates it AFTER the entire pre-install hook phase
   finishes -- so a DB-dependent pre-install Job always races a Postgres that
   doesn't exist yet on a true fresh install ("Database not ready after 60s",
   confirmed on alpha 2026-10-01).
2. `db-migrate` (templates/migrations-job.yaml) carries EXACTLY "pre-upgrade"
   -- no more, no less (fix/db-migrate-fresh-install-ordering, 2026-11-27;
   supersedes the 2026-10-01 "post-install,pre-upgrade" decision). "pre-upgrade"
   is required (existing releases advance the schema before any new pod rolls);
   "pre-install" must NEVER be present (Postgres does not exist yet); and
   "post-install" must NEVER be present: hub-api `/ready` waits for the alembic
   head revision, so a post-install migrate deadlocks `helm upgrade --install
   --wait` on every fresh install.
3. The fresh-install schema comes from hub-api's `db-migrate` initContainer
   (templates/_db_migrate.tpl, the full migrations image -- never
   create_all()+stamp) -- it must be rendered, or a fresh install never migrates.

Fails loudly (never masked) if zero Jobs are examined -- a scanner pointed at
a moved/renamed chart path reporting "0 violations" is not a passing gate,
it's a broken one (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "k8s" / "helm" / "waddlebot"


def _render(chart_dir: Path) -> list[dict]:
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
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _needs_db(doc: dict) -> bool:
    # Only what the Job's POD consumes counts. Metadata is excluded on purpose: the provision-secrets
    # hook's `waddles.penguintech.io/provisions-secrets` annotation NAMES db-migrate-secret because it
    # CREATES it (it never connects to the DB), which a whole-document scan misreads as a dependency.
    text = json.dumps(((doc.get("spec") or {}).get("template")) or doc)
    return any(
        marker in text
        for marker in ("DATABASE_URL", "POSTGRES_PASSWORD", "db-migrate-secret")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--chart-dir",
        type=Path,
        default=CHART_DIR,
        help="chart to render with its values-alpha.yaml (default: waddlebot)",
    )
    docs = _render(parser.parse_args(argv).chart_dir)
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]

    if not jobs:
        print(
            "FAIL: examined 0 Jobs in rendered chart output -- scanner is broken or chart path moved",
            file=sys.stderr,
        )
        return 1

    print(f"Examined {len(jobs)} Job(s) in rendered chart output.")

    violations = []
    migrate_job = None
    for job in jobs:
        name = job.get("metadata", {}).get("name", "<unknown>")
        hook = job.get("metadata", {}).get("annotations", {}).get("helm.sh/hook", "")
        if _needs_db(job) and "pre-install" in hook.split(","):
            violations.append(f"{name} (hook={hook!r}) carries pre-install")
        if (
            job.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
            == "db-migrate"
        ):
            migrate_job = (name, hook)

    if migrate_job is None:
        print(
            "FAIL: db-migrate Job not found in rendered chart output", file=sys.stderr
        )
        return 1

    name, hook = migrate_job
    phases = set(hook.split(","))
    if phases != {"pre-upgrade"}:
        violations.append(
            f"{name} (hook={hook!r}) must carry exactly 'pre-upgrade', got {sorted(phases)}"
        )

    hub_api = [
        doc
        for doc in docs
        if doc.get("kind") == "Deployment"
        and doc.get("metadata", {}).get("name", "").endswith("hub-api-v3")
    ]
    if len(hub_api) != 1:
        violations.append(f"expected exactly one hub-api Deployment, found {len(hub_api)}")
    else:
        inits = hub_api[0]["spec"]["template"]["spec"].get("initContainers") or []
        if "db-migrate" not in [c.get("name") for c in inits]:
            violations.append(
                "hub-api has no db-migrate initContainer -- a fresh install would never migrate"
            )

    if violations:
        print(f"FAIL: {len(violations)} hook-phase violation(s):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(
        "PASS: no DB-dependent Job carries pre-install, db-migrate Job is exactly pre-upgrade, "
        "and hub-api runs the db-migrate initContainer."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
