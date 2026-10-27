#!/usr/bin/env python3
"""Build every `language: python` entry in `bundles/core-bundles.yaml` into a WASI 0.2
component via componentize-py, driven ENTIRELY by the catalog -- no per-bundle code.

Run from the repo root inside `bundles/Dockerfile.core-bundles`'s python-bundles-builder
stage (wit/, sdk/waddle-sdk/src, bundles/python, and bundles/core-bundles.yaml already
COPY'd in). Adding a new first-party Python bundle therefore needs ONLY a new
`bundles/core-bundles.yaml` entry (with its own `source_dir`) plus the bundle's own
`bundles/python/<name>/src` + `hub-manifest.yaml` -- never a Dockerfile edit, which is the
whole point: a hand-edited per-bundle Dockerfile stanza was the recurring PR-conflict
surface this script replaces.

For each catalog entry this produces, under `dist/` (relative to CWD):
  - `<artifact_path>`   -- the compiled .wasm component (componentize-py output)
  - `<manifest_path>`   -- a copy of `bundles/<source_dir>/hub-manifest.yaml`

`bundles/Dockerfile.core-bundles`'s final stage then does ONE
`COPY --from=python-bundles-builder /repo/dist/. /core-bundles/` instead of a COPY line
per bundle -- same destination filenames as before, just produced generically.

Every Python bundle shares the same SDK entry point (`waddle_sdk._component_entry`,
PR #382) and the same `-d wit/waddle-bundle -w stage` WIT world -- these are fixed
conventions, not catalog fields, because they have never varied across a single bundle to
date; `source_dir` IS a catalog field (added alongside this script) because the bundle
directory name is not safely derivable from `app_id`/`artifact_path` for every future
bundle.

Fail-loud throughout (general.md/critical-rules.md Fail-Loud Code Paths): a missing
`source_dir`, a missing source/manifest directory, a componentize-py failure, or zero
python bundles found in the catalog all raise and exit non-zero -- never a silent skip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# pyyaml ships no py.typed marker/stubs -- matches hub_api/cli/seed_core_bundles.py's own
# unresolved `import yaml` mypy gap (types-PyYAML is not pinned anywhere in this repo); not a
# relaxation introduced here, the same ecosystem gap every yaml-importing module in this repo
# already carries.
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "bundles" / "core-bundles.yaml"
SDK_SRC = REPO_ROOT / "sdk" / "waddle-sdk" / "src"
WIT_WORLD_DIR = REPO_ROOT / "wit" / "waddle-bundle"
WIT_WORLD_NAME = "stage"
# Every first-party Python bundle points componentize-py at this same shared SDK entry
# module (PR #382, fix/python-sdk-two-export-entry) -- a fixed convention, not a per-bundle
# catalog field, because it has never varied.
COMPONENT_ENTRY_MODULE = "waddle_sdk._component_entry"
DIST_DIR = REPO_ROOT / "dist"


class BuildError(RuntimeError):
    """A catalog or build-step problem that must fail the Dockerfile RUN, never be skipped."""


def _load_python_bundles() -> list[dict[str, Any]]:
    """Return every `bundles:` catalog row with `language: python`, in file order."""
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = raw.get("bundles") or []
    python_entries = [item for item in entries if item.get("language") == "python"]
    if not python_entries:
        # Verification Integrity (critical-rules.md): a zero-denominator result must fail
        # loudly, never report a quiet no-op success.
        raise BuildError(
            f"no language: python entries found in {CATALOG_PATH} -- "
            "expected at least one (e.g. waddles.core.example.pyping)"
        )
    return python_entries


def _build_one(entry: dict[str, Any]) -> str:
    """Componentize one catalog entry into `dist/<artifact_path>` + `dist/<manifest_path>`.

    Returns the entry's `app_id` for the caller's summary line.
    """
    raw_fields = {
        "app_id": entry.get("app_id"),
        "source_dir": entry.get("source_dir"),
        "manifest_path": entry.get("manifest_path"),
        "artifact_path": entry.get("artifact_path"),
    }
    missing = [field for field, value in raw_fields.items() if not value]
    if missing:
        raise BuildError(
            f"catalog entry {entry!r} is missing required field(s): {', '.join(missing)}"
        )
    # Narrowed to `str` for mypy --strict: the `missing` check above already proved every
    # value is truthy, but mypy can't infer that through a dict comprehension -- isinstance
    # makes the narrowing explicit (and doubles as a real schema-type check: a non-string
    # value here is as invalid as a missing one).
    fields: dict[str, str] = {}
    for field, value in raw_fields.items():
        if not isinstance(value, str):
            raise BuildError(
                f"catalog entry {entry!r} field {field!r} must be a string, got {type(value)!r}"
            )
        fields[field] = value
    app_id = fields["app_id"]
    source_dir = fields["source_dir"]
    manifest_path = fields["manifest_path"]
    artifact_path = fields["artifact_path"]

    bundle_dir = REPO_ROOT / "bundles" / source_dir
    bundle_src = bundle_dir / "src"
    bundle_manifest = bundle_dir / "hub-manifest.yaml"

    if not bundle_src.is_dir():
        raise BuildError(
            f"{app_id}: source_dir {source_dir!r} has no src/ directory at {bundle_src} "
            "-- COPY bundles/python bundles/python in the builder stage must include it"
        )
    if not bundle_manifest.is_file():
        raise BuildError(f"{app_id}: missing manifest file {bundle_manifest}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_wasm = DIST_DIR / artifact_path

    cmd = [
        "componentize-py",
        "-d",
        str(WIT_WORLD_DIR.relative_to(REPO_ROOT)),
        "-w",
        WIT_WORLD_NAME,
        "componentize",
        "-p",
        str(SDK_SRC.relative_to(REPO_ROOT)),
        "-p",
        str(bundle_src.relative_to(REPO_ROOT)),
        COMPONENT_ENTRY_MODULE,
        "-o",
        str(out_wasm.relative_to(REPO_ROOT)),
    ]
    print(f"build_python_bundles: {app_id} -> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)

    shutil.copyfile(bundle_manifest, DIST_DIR / manifest_path)
    return app_id


def main() -> int:
    entries = _load_python_bundles()
    built: list[str] = []
    for entry in entries:
        built.append(_build_one(entry))
    print(
        f"build_python_bundles: built {len(built)} python bundle(s): {', '.join(built)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print(f"build_python_bundles: FAIL -- {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(
            f"build_python_bundles: FAIL -- {exc.cmd[0]} exited {exc.returncode}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(exc.returncode or 1)
