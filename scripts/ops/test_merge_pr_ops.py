"""Tests for merge-release-into-prs.sh and cleanup-merged-pr.sh against throwaway git repos.

A bare repo stands in for origin and a PATH-stubbed `gh` serves canned `gh pr view` JSON per PR,
so every case runs offline and asserts on real git state (pushed refs, removed worktrees/branches).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MERGE = HERE / "merge-release-into-prs.sh"
CLEANUP = HERE / "cleanup-merged-pr.sh"
BASE = "release/v9.9.X"

GH_STUB = """#!/usr/bin/env bash
# gh stub: logs argv; `repo view` -> acme/widgets; `pr view N` -> $GH_FIXTURES/pr-N.json (+ --jq/-q).
echo "$*" >> "$GH_FIXTURES/calls"
if [ "$1" = repo ] && [ "$2" = view ]; then echo acme/widgets; exit 0; fi
if [ "$1" = pr ] && [ "$2" = view ]; then
  f="$GH_FIXTURES/pr-$3.json"; [ -f "$f" ] || { echo "no fixture $f" >&2; exit 1; }
  jqx=""; prev=""
  for a in "$@"; do case "$prev" in --jq|-q) jqx="$a" ;; esac; prev="$a"; done
  if [ -n "$jqx" ]; then jq -r "$jqx" "$f"; else cat "$f"; fi
  exit 0
fi
echo "gh stub: unexpected call: $*" >&2; exit 99
"""


def git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    """Run git in cwd with the isolated test environment and return stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    """Build origin (bare) + a clone with a release base, and an isolated env with the gh stub on PATH."""
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "gh").write_text(GH_STUB)
    (stub_bin / "gh").chmod(0o755)
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GH_FIXTURES": str(fixtures),
        "TMPDIR": str(tmp_path),
        "GIT_SIGN": "0",
        "LC_ALL": "C",
    }
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], env=env, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], env=env, check=True)
    (work / "a.txt").write_text("base\n")
    git(work, "add", "a.txt", env=env)
    git(work, "commit", "-q", "-m", "init", env=env)
    git(work, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}", env=env)
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main", env=env)
    return {"tmp": tmp_path, "env": env, "work": work, "origin": origin, "fx": fixtures}


def branch(
    w: dict, name: str, fname: str, content: str, start: str = f"origin/{BASE}"
) -> str:
    """Create branch `name` from `start` with one commit writing fname, push it, return its sha."""
    env, work = w["env"], w["work"]
    git(work, "fetch", "-q", "origin", env=env)
    git(work, "checkout", "-q", "-B", name, start, env=env)
    (work / fname).write_text(content)
    git(work, "add", fname, env=env)
    git(work, "commit", "-q", "-m", f"{name}: {fname}", env=env)
    git(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}", env=env)
    sha = git(work, "rev-parse", "HEAD", env=env)
    git(work, "checkout", "-q", "--detach", env=env)
    return sha


def advance_base(w: dict, fname: str, content: str) -> str:
    """Push a new commit onto the release base; returns the new base sha."""
    return branch(w, BASE, fname, content)


def pr(
    w: dict,
    n: int,
    head: str,
    base: str = BASE,
    state: str = "OPEN",
    cross: bool = False,
) -> None:
    """Write the gh fixture for PR n."""
    (w["fx"] / f"pr-{n}.json").write_text(
        json.dumps(
            {
                "headRefName": head,
                "baseRefName": base,
                "state": state,
                "isCrossRepository": cross,
            }
        )
    )


def run(w: dict, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    """Run an ops script from the clone with the isolated env (+ extra env)."""
    env = {**w["env"], **extra}
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=w["work"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def remote_sha(w: dict, ref: str) -> str:
    """Return the sha of refs/heads/<ref> on origin, or '' when absent."""
    out = subprocess.run(
        ["git", "ls-remote", str(w["origin"]), f"refs/heads/{ref}"],
        env=w["env"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()[0] if out.strip() else ""


# merge-release-into-prs.sh


def test_merge_clean_pushes_merge_commit_with_trailers(world):
    old = branch(world, "feature/a", "b.txt", "b\n")
    base_sha = advance_base(world, "c.txt", "c\n")
    pr(world, 11, "feature/a")
    res = run(
        world, MERGE, "11", COMMIT_TRAILERS="Co-Authored-By: Bot <bot@example.invalid>"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        "examined=1 merged=1 up_to_date=0 skipped=0 conflicts=0 failed=0" in res.stdout
    )
    new = remote_sha(world, "feature/a")
    assert new != old
    env, work = world["env"], world["work"]
    git(work, "fetch", "-q", "origin", env=env)
    parents = git(work, "log", "-1", "--format=%P", "origin/feature/a", env=env).split()
    assert parents == [old, base_sha]
    body = git(work, "log", "-1", "--format=%B", "origin/feature/a", env=env)
    assert f"Merge {BASE} into feature/a" in body and "Co-Authored-By: Bot" in body
    calls = (world["fx"] / "calls").read_text()
    assert "--force" not in calls


def test_merge_up_to_date_does_not_push(world):
    sha = branch(world, "feature/b", "b.txt", "b\n")
    pr(world, 12, "feature/b")
    res = run(world, MERGE, "12")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "up-to-date" in res.stdout and "up_to_date=1" in res.stdout
    assert remote_sha(world, "feature/b") == sha


def test_merge_conflict_aborts_and_fails(world):
    sha = branch(world, "feature/c", "a.txt", "head side\n")
    advance_base(world, "a.txt", "base side\n")
    pr(world, 13, "feature/c")
    res = run(world, MERGE, "13")
    assert res.returncode == 1
    assert (
        "#13 CONFLICT (feature/c): a.txt" in res.stdout and "conflicts=1" in res.stdout
    )
    assert remote_sha(world, "feature/c") == sha
    assert "merge-release-into-prs" not in git(
        world["work"], "worktree", "list", env=world["env"]
    )


def test_merge_dry_run_never_pushes(world):
    sha = branch(world, "feature/d", "d.txt", "d\n")
    advance_base(world, "e.txt", "e\n")
    pr(world, 14, "feature/d")
    res = run(world, MERGE, "--dry-run", "14")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DRY-RUN would push merge" in res.stdout and "dry_run=1" in res.stdout
    assert remote_sha(world, "feature/d") == sha


@pytest.mark.parametrize(
    ("head", "base", "msg"),
    [
        ("feature/e", "main", "REFUSE base 'main' is not release/*"),
        ("release/v1.0.X", BASE, "REFUSE protected head"),
    ],
)
def test_merge_refuses_main_base_and_protected_head(world, head, base, msg):
    pr(world, 15, head, base=base)
    res = run(world, MERGE, "15")
    assert res.returncode == 1
    assert msg in res.stdout and "failed=1" in res.stdout


def test_merge_base_pin_mismatch_refused(world):
    branch(world, "feature/f", "f.txt", "f\n")
    pr(world, 16, "feature/f")
    res = run(world, MERGE, "16", BASE="release/v0.0.X")
    assert res.returncode == 1 and "!= BASE" in res.stdout


def test_merge_skips_closed_and_cross_repo(world):
    pr(world, 17, "feature/g", state="MERGED")
    pr(world, 18, "feature/h", cross=True)
    res = run(world, MERGE, "17", "18")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "examined=2" in res.stdout and "skipped=2" in res.stdout


@pytest.mark.parametrize(
    ("args", "rc"), [((), 2), (("abc",), 2), (("--force", "1"), 2), (("--help",), 0)]
)
def test_merge_usage(world, args, rc):
    res = run(world, MERGE, *args)
    assert res.returncode == rc
    if rc == 0:
        assert "Usage: scripts/ops/merge-release-into-prs.sh" in res.stdout
    assert not (world["fx"] / "calls").exists()


# cleanup-merged-pr.sh


def add_worktree(w: dict, name: str) -> Path:
    """Check branch `name` out in a new worktree under tmp and return its path."""
    path = w["tmp"] / f"wt-{name.replace('/', '_')}"
    git(w["work"], "worktree", "add", "-q", str(path), name, env=w["env"])
    return path


def merge_into_base(w: dict, name: str) -> None:
    """Fast-forward the release base to include branch `name` (simulates the PR merge)."""
    env, work = w["env"], w["work"]
    git(work, "push", "-q", "origin", f"origin/{name}:refs/heads/{BASE}", env=env)
    git(work, "fetch", "-q", "origin", env=env)


def test_cleanup_merged_removes_worktree_remote_and_local(world):
    branch(world, "feature/m", "m.txt", "m\n")
    git(
        world["work"],
        "branch",
        "-q",
        "-f",
        "feature/m",
        "origin/feature/m",
        env=world["env"],
    )
    merge_into_base(world, "feature/m")
    wt = add_worktree(world, "feature/m")
    pr(world, 21, "feature/m", state="MERGED")
    res = run(world, CLEANUP, "21")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        "examined=1 worktrees_removed=1 remote_deleted=1 local_deleted=1 kept=0"
        in res.stdout
    )
    assert not wt.exists()
    assert remote_sha(world, "feature/m") == ""
    assert git(world["work"], "branch", "--list", "feature/m", env=world["env"]) == ""


def test_cleanup_dirty_worktree_kept(world):
    branch(world, "feature/n", "n.txt", "n\n")
    git(
        world["work"],
        "branch",
        "-q",
        "-f",
        "feature/n",
        "origin/feature/n",
        env=world["env"],
    )
    merge_into_base(world, "feature/n")
    wt = add_worktree(world, "feature/n")
    (wt / "scratch.txt").write_text("uncommitted\n")
    pr(world, 22, "feature/n", state="MERGED")
    res = run(world, CLEANUP, "22")
    assert res.returncode == 1
    assert "KEEP dirty worktree" in res.stdout
    assert wt.exists() and (wt / "scratch.txt").exists()
    assert git(world["work"], "branch", "--list", "feature/n", env=world["env"]) != ""


def test_cleanup_unmerged_remote_kept(world):
    branch(world, "feature/o", "o.txt", "o\n")
    merge_into_base(world, "feature/o")
    extra = branch(world, "feature/o", "o2.txt", "late\n", start="origin/feature/o")
    pr(world, 23, "feature/o", state="MERGED")
    res = run(world, CLEANUP, "23")
    assert res.returncode == 1
    assert f"KEEP remote feature/o not in {BASE}" in res.stdout
    assert remote_sha(world, "feature/o") == extra


def test_cleanup_open_pr_skipped(world):
    branch(world, "feature/p", "p.txt", "p\n")
    pr(world, 24, "feature/p", state="OPEN")
    res = run(world, CLEANUP, "24")
    assert res.returncode == 1 and "SKIP state=OPEN" in res.stdout
    assert remote_sha(world, "feature/p") != ""


def test_cleanup_dry_run_changes_nothing(world):
    sha = branch(world, "feature/q", "q.txt", "q\n")
    merge_into_base(world, "feature/q")
    wt = add_worktree(world, "feature/q")
    pr(world, 25, "feature/q", state="MERGED")
    res = run(world, CLEANUP, "--dry-run", "25")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (
        "DRY: git push -q origin --delete feature/q" in res.stdout
        and "dry_run=1" in res.stdout
    )
    assert wt.exists() and remote_sha(world, "feature/q") == sha


@pytest.mark.parametrize(("args", "rc"), [((), 2), (("x1",), 2), (("--help",), 0)])
def test_cleanup_usage(world, args, rc):
    res = run(world, CLEANUP, *args)
    assert res.returncode == rc
    assert not (world["fx"] / "calls").exists()
