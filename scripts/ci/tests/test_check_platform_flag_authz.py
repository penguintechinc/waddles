"""Self-tests for scripts/ci/check_platform_flag_authz.py (temp trees, no real bundles)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_platform_flag_authz", Path(__file__).resolve().parents[1] / "check_platform_flag_authz.py"
)
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_platform_flag_authz"] = gate
_SPEC.loader.exec_module(gate)

REF = "# caller-can PR (feature/sdk-caller-can) batch 1"
OFFENDER = "bundles/python/foo/src/app.py"
BAD_PY = 'def _is_privileged(p):\n    return p.get("is_mod") is True\n'


def _tree(tmp_path: Path, files: dict[str, str], allow: str = "") -> Path:
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    (tmp_path / "allow.txt").write_text(allow)
    return tmp_path


def _run(root: Path) -> int:
    return gate.run(root, root / "allow.txt")


def test_hit_fails(tmp_path, capsys):
    root = _tree(tmp_path, {OFFENDER: BAD_PY})
    assert _run(root) == 1
    assert "files scanned: 1, hits: 1, allowlisted files: 0" in capsys.readouterr().out


def test_pragma_passes(tmp_path):
    body = 'def badge(p):\n    return p.get("is_mod")  # display-only: platform-flag\n'
    assert _run(_tree(tmp_path, {OFFENDER: body})) == 0


def test_pragma_on_previous_line_passes(tmp_path):
    body = 'def badge(p):\n    # display-only: platform-flag\n    return p.get("is_broadcaster")\n'
    assert _run(_tree(tmp_path, {OFFENDER: body})) == 0


@pytest.mark.parametrize(
    "rel,body,pragma",
    [
        ("bundles/rust/x/src/lib.rs", 'let m = p.get("is_mod") == Some(&true);\n', "// display-only: platform-flag"),
        ("bundles/csharp/x/Logic.cs", "if (!Kit.IsModerator(e.PayloadJson)) return;\n", "// display-only: platform-flag"),
    ],
)
def test_non_python_hit_and_pragma(tmp_path, rel, body, pragma):
    assert _run(_tree(tmp_path / "a", {rel: body})) == 1
    assert _run(_tree(tmp_path / "b", {rel: f"{pragma}\n{body}"})) == 0


def test_comments_docstrings_and_tests_ignored(tmp_path):
    files = {
        OFFENDER: '"""Reads is_mod from the payload."""\n# is_mod here\nx = 1\n',
        "bundles/rust/x/src/lib.rs": '// is_mod\n/// is_broadcaster\nfn f() {}\n#[cfg(test)]\nmod t { fn g(){ let _ = "is_mod"; } }\n',
        "bundles/python/foo/tests/test_app.py": BAD_PY,
        "bundles/python/foo/src/test_helper.py": BAD_PY,
    }
    assert _run(_tree(tmp_path, files)) == 0


def test_allowlisted_passes(tmp_path, capsys):
    root = _tree(tmp_path, {OFFENDER: BAD_PY}, f"{OFFENDER}  {REF}\n")
    assert _run(root) == 0
    assert "files scanned: 1, hits: 1, allowlisted files: 1" in capsys.readouterr().out


def test_stale_allowlist_fails(tmp_path, capsys):
    clean = {OFFENDER: "x = 1\n"}
    root = _tree(tmp_path, clean, f"{OFFENDER}  {REF}\n")
    assert _run(root) == 1
    assert "stale allowlist entry" in capsys.readouterr().err


def test_missing_reference_fails(tmp_path, capsys):
    root = _tree(tmp_path, {OFFENDER: BAD_PY}, f"{OFFENDER}\n")
    assert _run(root) == 1
    assert "lacks" in capsys.readouterr().err


def test_wrong_reference_fails(tmp_path):
    root = _tree(tmp_path, {OFFENDER: BAD_PY}, f"{OFFENDER}  # some PR batch 1\n")
    assert _run(root) == 1


def test_zero_files_scanned_fails(tmp_path, capsys):
    root = _tree(tmp_path, {})
    assert _run(root) == 1
    assert "zero bundle source files scanned" in capsys.readouterr().err


def test_non_allowlisted_hit_alongside_allowlisted_fails(tmp_path):
    other = "bundles/python/bar/src/app.py"
    root = _tree(tmp_path, {OFFENDER: BAD_PY, other: BAD_PY}, f"{OFFENDER}  {REF}\n")
    assert _run(root) == 1
