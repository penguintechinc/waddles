"""Self-tests for scripts/ci/check-builtin-handler-inventory.py (gh #840 PR4).

A gate that cannot fail is not a gate (critical-rules.md Verification
Integrity): every rule is proven against a PLANTED violation in a throwaway
tree (exit non-zero + the finding named), the safe shape is proven NOT to
fire, and the real repo tree is run end to end with a non-zero denominator.
Driven through the CLI (`--root`), exactly as the workflow invokes it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CI_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CI_DIR.parent.parent
SCRIPT = CI_DIR / "check-builtin-handler-inventory.py"

RUST_SRC = "pub fn normalize_widget() {}\n"
BUNDLE_MANIFEST = 'command_prefix: ["!widget"]\n'

COVERED = """\
handlers:
  - handler: core/svc_ingest/builtin_handlers/widget_ingest.py
    status: covered
    parity: unverified
    targets:
      - {kind: rust-path, path: core/svc_ingest/src/normalize.rs, contains: "pub fn normalize_widget"}
"""


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the gate CLI against `root` and capture output."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def make_tree(root: Path, map_yaml: str, *, handlers: tuple[str, ...] = ("widget_ingest",)) -> None:
    """Build a minimal repo: handler modules, a Rust target, a bundle manifest and the map."""
    for name in handlers:
        stage = name.rsplit("_", 1)[1]
        path = root / "core" / f"svc_{stage}" / "builtin_handlers" / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("async def normalize(raw):\n    return raw\n", encoding="utf-8")
    rust = root / "core/svc_ingest/src/normalize.rs"
    rust.parent.mkdir(parents=True, exist_ok=True)
    rust.write_text(RUST_SRC, encoding="utf-8")
    manifest = root / "bundles/python/widget/hub-manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(BUNDLE_MANIFEST, encoding="utf-8")
    ci_map = root / ".ci/builtin-handler-map.yaml"
    ci_map.parent.mkdir(parents=True, exist_ok=True)
    ci_map.write_text(textwrap.dedent(map_yaml), encoding="utf-8")


def test_fully_covered_tree_passes_and_prints_denominator(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "handlers=1 covered=1" in result.stdout


def test_unmapped_handler_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED, handlers=("widget_ingest", "gadget_ingest"))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "gadget_ingest.py: live Python handler has NO Rust-path/bundle mapping" in result.stderr


def test_covered_target_missing_file_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED.replace("normalize.rs", "nope.rs"))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "file does not exist" in result.stderr


def test_covered_evidence_not_found_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED.replace("normalize_widget", "normalize_missing"))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "not found -- mapping is stale or wrong" in result.stderr


def test_covered_without_evidence_regex_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED.replace(', contains: "pub fn normalize_widget"', ""))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "`contains` evidence regex is required" in result.stderr


def test_verified_parity_needs_real_corpus_vectors(tmp_path: Path) -> None:
    verified = COVERED.replace(
        "parity: unverified", 'parity: verified\n    corpus: ["widget/*.json"]'
    )
    make_tree(tmp_path, verified)
    result = run(tmp_path)
    assert result.returncode == 1
    assert "requires `corpus` globs matching real" in result.stderr
    vector = tmp_path / "tests/data_plane_parity/corpus/widget/a.json"
    vector.parent.mkdir(parents=True)
    vector.write_text("{}", encoding="utf-8")
    assert run(tmp_path).returncode == 0


def test_bundle_target_must_match_manifest_evidence(tmp_path: Path) -> None:
    bundle = """\
    handlers:
      - handler: core/svc_process/builtin_handlers/widget_process.py
        status: covered
        parity: unverified
        targets:
          - {kind: bundle, path: bundles/python/widget/hub-manifest.yaml, contains: '"!widget"'}
    """
    make_tree(tmp_path, bundle, handlers=("widget_process",))
    assert run(tmp_path).returncode == 0
    make_tree(tmp_path, bundle.replace('"!widget"', '"!nothere"'), handlers=("widget_process",))
    assert run(tmp_path).returncode == 1


PENDING = """\
handlers:
  - handler: core/svc_ingest/builtin_handlers/widget_ingest.py
    status: pending
    gap: "no Rust path yet"
    tracking: "#840"
"""


def test_pending_allowed_while_handler_exists_but_strict_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, PENDING)
    assert run(tmp_path).returncode == 0
    strict = run(tmp_path, "--strict")
    assert strict.returncode == 1
    assert "--strict forbids pending" in strict.stderr


def test_pending_without_gap_or_tracking_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, PENDING.replace('    tracking: "#840"\n', ""))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "pending entries need both `gap` and `tracking`" in result.stderr


def test_removing_a_pending_handler_fails_the_platform_drop_guard(tmp_path: Path) -> None:
    """The core purpose: delete the Python handler before its replacement landed -> FATAL."""
    make_tree(tmp_path, PENDING)
    (tmp_path / "core/svc_ingest/builtin_handlers/widget_ingest.py").unlink()
    result = run(tmp_path)
    assert result.returncode == 1
    assert "REMOVED while still `pending`" in result.stderr


def test_stale_covered_entry_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED, handlers=("other_ingest",))
    result = run(tmp_path)
    assert result.returncode == 1
    assert "stale entry" in result.stderr


def test_dangling_stage_entrypoint_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED)
    manifest = tmp_path / "core/svc_ingest/builtin_handlers/widget_ingest.py"
    manifest.write_text(
        'async def normalize(raw):\n    return raw\n\nM = {"entrypoint": "builtin_handlers.widget_ingest:missing_fn"}\n'
        'N = {"entrypoint": "builtin_handlers.ghost_ingest:normalize"}\n',
        encoding="utf-8",
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "widget_ingest:missing_fn -- function not defined" in result.stderr
    assert "ghost_ingest:normalize -- no such handler module" in result.stderr


def test_prose_mention_of_an_entrypoint_is_not_a_declaration(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED)
    handler = tmp_path / "core/svc_ingest/builtin_handlers/widget_ingest.py"
    handler.write_text(
        "'''see builtin_handlers.ghost_ingest:norm\nalize'''\nasync def normalize(raw):\n    return raw\n",
        encoding="utf-8",
    )
    assert run(tmp_path).returncode == 0


def test_zero_handlers_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / ".ci").mkdir()
    (tmp_path / ".ci/builtin-handler-map.yaml").write_text("handlers: []\n", encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "zero handler modules discovered" in result.stderr


def test_duplicate_and_bad_status_fail(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED + COVERED.split("handlers:\n")[1])
    assert "duplicate map entry" in run(tmp_path).stderr
    make_tree(tmp_path, COVERED.replace("status: covered", "status: maybe"))
    assert "status must be one of" in run(tmp_path).stderr


def test_missing_map_file_fails(tmp_path: Path) -> None:
    make_tree(tmp_path, COVERED)
    (tmp_path / ".ci/builtin-handler-map.yaml").unlink()
    result = run(tmp_path)
    assert result.returncode == 1
    assert "mapping file missing" in result.stderr


@pytest.mark.parametrize("strict", [False])
def test_real_repo_tree_passes_with_nonzero_denominator(strict: bool) -> None:
    result = run(REPO_ROOT)
    assert result.returncode == 0, result.stderr
    handlers = int(result.stdout.split("handlers=")[1].split()[0])
    assert handlers >= 40, f"only {handlers} handlers discovered in the real tree"
