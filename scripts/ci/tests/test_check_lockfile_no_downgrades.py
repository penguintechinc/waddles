"""Unit tests for scripts/ci/check-lockfile-no-downgrades.py.

Fixture repos (good + bad) exercise the merge-base diff + version-change
detection end to end against a real git history -- see gh-425 for the
failure class this guards (a silent, unacknowledged version change buried
in a large lockfile diff).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parent.parent / "check-lockfile-no-downgrades.py"


def _load_module() -> Any:
    """Loads the hyphenated CI script as an importable module for unit tests."""
    spec = importlib.util.spec_from_file_location("check_lockfile_no_downgrades", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()

CARGO_LOCK_V1 = """
[[package]]
name = "anyhow"
version = "1.0.104"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "serde"
version = "1.0.229"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""

CARGO_LOCK_DOWNGRADE = CARGO_LOCK_V1.replace('version = "1.0.104"', 'version = "1.0.99"')
CARGO_LOCK_ADDITION_ONLY = CARGO_LOCK_V1 + """
[[package]]
name = "thiserror"
version = "2.0.20"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "core").mkdir()
    (repo / "core" / "Cargo.lock").write_text(CARGO_LOCK_V1)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(repo: Path, merge_base: str, pr_body_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable, str(MODULE_PATH),
        "--merge-base", merge_base,
        "--head", "HEAD",
        "--repo-root", str(repo),
    ]
    if pr_body_file:
        args += ["--pr-body-file", str(pr_body_file)]
    return subprocess.run(args, capture_output=True, text=True, check=False)


class TestAdditionOnlyPasses:
    """A brand-new package added to the lockfile is always fine."""

    def test_addition_only_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "core" / "Cargo.lock").write_text(CARGO_LOCK_ADDITION_ONLY)
        _git(repo, "commit", "-aq", "-m", "add thiserror")
        result = _run(repo, base)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "additions only" in result.stdout or "No package version changes" in result.stdout


class TestUnacknowledgedDowngradeFails:
    """An existing package's version changing with no bump note fails."""

    def test_downgrade_without_note_fails(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "core" / "Cargo.lock").write_text(CARGO_LOCK_DOWNGRADE)
        _git(repo, "commit", "-aq", "-m", "unrelated change")
        result = _run(repo, base)
        assert result.returncode == 1
        assert "anyhow" in result.stderr
        assert "1.0.104" in result.stderr
        assert "1.0.99" in result.stderr


class TestAcknowledgedBumpPasses:
    """A matching `lockfile-bump:` line in the PR body allows the change."""

    def test_acknowledged_bump_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "core" / "Cargo.lock").write_text(CARGO_LOCK_DOWNGRADE)
        _git(repo, "commit", "-aq", "-m", "unrelated change")
        pr_body = tmp_path / "pr-body.txt"
        pr_body.write_text("lockfile-bump: anyhow 1.0.104->1.0.99 reverting a regression\n")
        result = _run(repo, base, pr_body_file=pr_body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "allowed" in result.stdout


class TestAcknowledgedViaCommitMessage:
    """A matching `lockfile-bump:` line in a commit message also allows it."""

    def test_acknowledged_via_commit_message_passes(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "core" / "Cargo.lock").write_text(CARGO_LOCK_DOWNGRADE)
        _git(repo, "commit", "-aq", "-m", "fix: revert anyhow\n\nlockfile-bump: anyhow 1.0.104->1.0.99 CVE revert")
        result = _run(repo, base)
        assert result.returncode == 0, result.stdout + result.stderr


class TestZeroDenominator:
    """No lockfiles anywhere in the repo tree is a hard failure, never a silent pass."""

    def test_no_lockfiles_anywhere_fails(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("hello\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        result = _run(repo, base)
        assert result.returncode == 1
        assert "0 lockfiles found" in result.stderr


class TestDiffVersionsUnit:
    """Unit-level: diff_versions distinguishes additions from real changes."""

    def test_addition_is_not_a_change(self) -> None:
        old = {"serde": ["1.0.229"]}
        new = {"serde": ["1.0.229"], "thiserror": ["2.0.20"]}
        assert mod.diff_versions(old, new) == []

    def test_version_change_is_flagged(self) -> None:
        old = {"anyhow": ["1.0.104"]}
        new = {"anyhow": ["1.0.99"]}
        assert mod.diff_versions(old, new) == [("anyhow", "1.0.104", "1.0.99")]
