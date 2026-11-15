"""Regression gate: every catalog row must have its manifest + artifact inputs in the seeder image.

The `waddlebot-core-bundle-seeder` image (`bundles/Dockerfile.core-bundles`) is the only place
`/core-bundles/<manifest_path>` and `/core-bundles/<artifact_path>` come from. A catalog row whose
Rust/C# bundle has no build stage or no `COPY` into `/core-bundles` passes every YAML-level check
and then dies on the alpha deploy with `FileNotFoundError: /core-bundles/<name>.manifest.yaml`
(`waddles.core.app.reputation-accrual` did exactly that). These tests statically cross-check the
catalog against the Dockerfile's build inputs; the denominators are asserted so an empty or
mis-rooted scan fails instead of passing vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_BUNDLES = _REPO / "bundles"
_CATALOG = _BUNDLES / "core-bundles.yaml"
_DOCKERFILE = _BUNDLES / "Dockerfile.core-bundles"

#: `COPY bundles/<src>/hub-manifest.yaml /core-bundles/<name>` -> {<name>: <src>}
_MANIFEST_COPY_RE = re.compile(
    r"^COPY\s+bundles/(?P<src>\S+?)/hub-manifest\.yaml\s+/core-bundles/(?P<dst>\S+)\s*$",
    re.MULTILINE,
)
#: `COPY --from=<stage> /repo/bundles/<src>/target/.../<wasm> /core-bundles/<name>`
#: -> {<name>: <src>}
_ARTIFACT_COPY_RE = re.compile(
    r"^COPY\s+--from=\S+\s+/repo/bundles/(?P<src>\S+?)/(?:target|bin)/\S+\s+/core-bundles/(?P<dst>\S+)\s*$",
    re.MULTILINE,
)


def _catalog_rows() -> list[dict]:
    """Every `bundles:` row of the catalog."""
    rows = yaml.safe_load(_CATALOG.read_text())["bundles"]
    assert len(rows) >= 100, f"only {len(rows)} catalog rows examined"
    return rows


def _non_python_rows() -> list[dict]:
    """Rows built by hand-written Dockerfile stanzas (python rows are catalog-driven)."""
    rows = [r for r in _catalog_rows() if r["language"] != "python"]
    assert len(rows) >= 1, "no non-python catalog rows examined"
    return rows


def test_every_non_python_row_has_manifest_copied_into_image() -> None:
    """Each rust/csharp row's `manifest_path` is COPY'd from its own `source_dir` hub manifest."""
    dockerfile = _DOCKERFILE.read_text()
    copies = {m["dst"]: m["src"] for m in _MANIFEST_COPY_RE.finditer(dockerfile)}
    assert len(copies) >= 1, "no manifest COPY lines parsed from the Dockerfile"

    problems = []
    for row in _non_python_rows():
        got = copies.get(row["manifest_path"])
        if got != row["source_dir"]:
            problems.append(
                f"{row['app_id']}: /core-bundles/{row['manifest_path']} is copied from "
                f"{got!r}, catalog source_dir is {row['source_dir']!r}"
            )
    assert not problems, "\n".join(problems)


def test_every_non_python_row_has_artifact_copied_into_image() -> None:
    """Each rust/csharp row's `artifact_path` is COPY'd out of a build stage (its `source_dir`)."""
    dockerfile = _DOCKERFILE.read_text()
    copies = {m["dst"]: m["src"] for m in _ARTIFACT_COPY_RE.finditer(dockerfile)}
    assert len(copies) >= 1, "no artifact COPY lines parsed from the Dockerfile"

    problems = []
    for row in _non_python_rows():
        got = copies.get(row["artifact_path"])
        if got != row["source_dir"]:
            problems.append(
                f"{row['app_id']}: /core-bundles/{row['artifact_path']} is copied from "
                f"{got!r}, catalog source_dir is {row['source_dir']!r}"
            )
    assert not problems, "\n".join(problems)


def test_every_non_python_row_source_is_copied_into_a_build_stage() -> None:
    """Each row's `source_dir` is copied into a builder stage; its hub manifest exists."""
    dockerfile = _DOCKERFILE.read_text()
    problems = []
    for row in _non_python_rows():
        src = row["source_dir"]
        if not (_BUNDLES / src / "hub-manifest.yaml").is_file():
            problems.append(f"{row['app_id']}: bundles/{src}/hub-manifest.yaml missing on disk")
        if not re.search(rf"^COPY\s+bundles/{re.escape(src)}\s", dockerfile, re.MULTILINE):
            problems.append(f"{row['app_id']}: no `COPY bundles/{src} ...` build stanza")
    assert not problems, "\n".join(problems)


def test_every_python_row_has_catalog_driven_inputs() -> None:
    """Python rows are built by build_python_bundles.py from `<source_dir>/{src,hub-manifest}`."""
    rows = [r for r in _catalog_rows() if r["language"] == "python"]
    assert len(rows) >= 1, "no python catalog rows examined"
    problems = []
    for row in rows:
        base = _BUNDLES / row["source_dir"]
        if not (base / "hub-manifest.yaml").is_file():
            problems.append(f"{row['app_id']}: {base}/hub-manifest.yaml missing")
        if not (base / "src").is_dir():
            problems.append(f"{row['app_id']}: {base}/src missing")
    assert not problems, "\n".join(problems)
    # The whole python tree + the bulk dist/ COPY are what make the above sufficient.
    dockerfile = _DOCKERFILE.read_text()
    assert "COPY bundles/python bundles/python" in dockerfile
    assert "COPY --from=python-bundles-builder /repo/dist/. /core-bundles/" in dockerfile
