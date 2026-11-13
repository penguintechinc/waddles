"""Exercise source recovery and the real frozen-baseline filter through their CLIs."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parents[1]
SOURCE = "def load():\n    try:\n        fetch()\n    except OSError:\n        return None\n"


def report(path="module.py", start=2, end=5, text="requires login"):
    return {"results": [{
        "check_id": "rules.python-silent-except-return-default",
        "path": path,
        "start": {"line": start},
        "end": {"line": end},
        "extra": {"lines": text},
    }]}


def convert(root, data):
    raw = root / "report.json"
    raw.write_text(json.dumps(data))
    return subprocess.run(
        [sys.executable, str(CI / "semgrep_findings.py"), str(raw), str(root)],
        text=True, capture_output=True, check=False,
    )


def test_redacted_and_normal_fingerprints_and_changed_source(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(SOURCE)
    matched = "\n".join(SOURCE.splitlines()[1:])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"entries": [{
        "rule_id": "python-silent-except-return-default",
        "path": "module.py",
        "line_hash": hashlib.sha256(matched.strip().encode()).hexdigest()[:16],
    }]}))
    allowlist = tmp_path / "allowlist.yml"
    allowlist.write_text("[]\n")
    normal = convert(tmp_path, report(text=matched))
    redacted = convert(tmp_path, report())
    assert normal.returncode == redacted.returncode == 0
    assert normal.stdout == redacted.stdout
    assert json.loads(redacted.stdout)[0]["text"] == matched

    def filtered(findings):
        return subprocess.run(
            [sys.executable, str(CI / "check_no_stubs_filter.py"),
             str(allowlist), str(baseline)],
            input=findings, text=True, capture_output=True, check=False,
        )

    accepted = filtered(redacted.stdout)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["silent_fallback"]["baselined"] == 1
    source.write_text(SOURCE.replace("return None", "return False"))
    changed = convert(tmp_path, report(text=matched))
    assert changed.returncode == 0
    rejected = filtered(changed.stdout)
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["silent_fallback"]["blocked"] == 1
    assert "return False" in rejected.stderr


@pytest.mark.parametrize("path,start,end", [
    ("module.py", 0, 2), ("module.py", 4, 3), ("module.py", 1, 6),
    ("module.py", "1", 2), ("module.py", True, 2),
    ("missing.py", 1, 1), ("../module.py", 1, 1), ("/etc/passwd", 1, 1),
    (".", 1, 1),
])
def test_invalid_source_and_ranges_fail(tmp_path, path, start, end):
    (tmp_path / "module.py").write_text(SOURCE)
    result = convert(tmp_path, report(path, start, end))
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr


def test_symlink_escape_fails(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(SOURCE)
    (root / "module.py").symlink_to(outside)
    result = convert(root, report())
    assert result.returncode != 0
    assert "outside repository" in result.stderr


def test_crlf_source_preserves_semgrep_line_semantics(tmp_path):
    (tmp_path / "module.py").write_bytes(SOURCE.replace("\n", "\r\n").encode())
    result = convert(tmp_path, report())
    assert result.returncode == 0
    assert json.loads(result.stdout)[0]["text"] == "\n".join(SOURCE.splitlines()[1:])


def test_semgrep_silent_fallback_rules_true_positives_and_negatives(tmp_path):
    """Verify the actual semgrep-silent-fallback.yml rules against a Rust + Python fixture."""
    import shutil

    if shutil.which("semgrep") is None:
        pytest.skip("semgrep binary not on PATH")

    rust_src = tmp_path / "sample.rs"
    rust_src.write_text(
        """\
pub fn silent_default(x: Result<i32, &str>) -> i32 {
    x.unwrap_or_default()
}

pub fn silent_closure(x: Result<i32, &str>) -> i32 {
    x.unwrap_or_else(|_| 0)
}

pub fn mutex_poison_recovery(m: &std::sync::Mutex<Vec<i32>>) -> Vec<i32> {
    m.lock().unwrap_or_else(|e| e.into_inner()).clone()
}

pub fn loud_panic(x: Result<i32, &str>) -> i32 {
    x.unwrap_or_else(|e| panic!("fatal: {e}"))
}

pub fn nosemgrep_annotated(x: Option<&str>) -> String {
    // nosemgrep: rust-silent-unwrap-or-default -- intentional empty sentinel
    x.unwrap_or_default().to_string()
}

#[cfg(test)]
mod tests {
    #[test]
    fn inside_tests_ignored() {
        let v: i32 = Err::<i32, &str>("err").unwrap_or_default();
        assert_eq!(v, 0);
    }
}
"""
    )

    test_only_rs = tmp_path / "foo_tests.rs"
    test_only_rs.write_text(
        """\
pub fn helper_in_tests_file(x: Result<i32, &str>) -> i32 {
    x.unwrap_or_default()
}
"""
    )

    py_src = tmp_path / "sample.py"
    py_src.write_text(
        """\
import logging
logger = logging.getLogger(__name__)

def silent_py():
    try:
        return int("bad")
    except ValueError:
        return None

def logged_py():
    try:
        return int("bad")
    except ValueError as exc:
        logger.warning("bad int: %s", exc)
        return None

def mapped_py():
    try:
        return int("bad")
    except ValueError as exc:
        return _err(exc)
"""
    )

    proc = subprocess.run(
        [
            "semgrep",
            "--config",
            str(CI / "semgrep-silent-fallback.yml"),
            "--json",
            "--metrics=off",
            "-q",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    results = json.loads(proc.stdout)["results"]
    found = {(Path(r["path"]).name, r["start"]["line"], r["check_id"].split(".")[-1]) for r in results}

    # True positives: silent_default (line 2), silent_closure (line 6), silent_py (line 5)
    assert ("sample.rs", 2, "rust-silent-unwrap-or-default") in found
    assert ("sample.rs", 6, "rust-silent-unwrap-or-default") in found
    assert ("sample.py", 5, "python-silent-except-return-default") in found
    assert ("sample.py", 5, "python-capability-silent-denial") in found

    # True negative: an exception mapped into the returned value (`return _err(exc)`) is surfaced.
    assert not any(f == "sample.py" and line == 18 for (f, line, _) in found), found

    # True negatives: mutex_poison_recovery, loud_panic, logged_closure, nosemgrep_annotated, mod tests, *_tests.rs, logged_py
    rust_lines = {line for (fname, line, _) in found if fname == "sample.rs"}
    assert rust_lines == {2, 6}
    assert not any(fname == "foo_tests.rs" for (fname, _, _) in found)
    py_lines = {line for (fname, line, _) in found if fname == "sample.py"}
    assert py_lines == {5}

