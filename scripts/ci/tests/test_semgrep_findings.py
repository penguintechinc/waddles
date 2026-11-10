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
