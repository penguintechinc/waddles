"""Self-test: ci-gate tiering (scripts/ci/ci-gate-tier.py) never weakens main.

Proves: main examines EVERY check (snapshot below = the full set); release examines exactly
the fast+coverage list; missing required checks fail; empty set stays empty (zero examined
is handled as a failure by ci-gate itself).
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

P = pathlib.Path(__file__).resolve().parents[1] / "ci-gate-tier.py"
spec = importlib.util.spec_from_file_location("tier", P)
tier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tier)

FAST = [
    "Detect changed areas",
    "Gate integrity (lint/security gates can fail)",
    "No stubs / no silent fallbacks (check-no-stubs)",
    "Python unit tests",
    "Alembic migration chain (real Postgres)",
    "Alembic single-head invariant (static, no DB)",
    "OpenAPI spec drift (openapi/v1.yaml matches generated)",
    "Helm chart lint + hook dependency ordering",
    "Dockerfile.rust path-dependency coverage",
    "Lockfile additions-only (no silent version changes)",
    "Security & Code Quality",
    "Node audit (hub-webui)",
    "Python security checks",
    "zizmor + actionlint",
    "build svc-action",
    "build svc-ingest",
    "fmt + clippy + deny + coverage / Lint and Test",
    "golangci-lint + govulncheck (Go 1.26)",
]
MAIN_ONLY = [
    "Determine Version and Tags",
    "Detect Changed Modules",
    "Build hub Container / Build & Scan (PR)",
    "Build hub Container / Merge Multi-Arch Manifest",
    "Build identity-core Container / Build (linux/amd64)",
    "Build router (linux/arm64) [PR]",
    "Merge hub Manifest",
    "Trivy Image Scan (gated)",
    "Supply-chain self-test",
    "Supply chain hub",
    "Security Scan",
    "Notify Build Status",
    "CodeQL Analysis",
    "kind install/upgrade",
    "kind upgrade-path (merge-base -> PR)",
]


def mk(names, conclusion="success"):
    return [{"name": n, "status": "completed", "conclusion": conclusion} for n in names]


def test_main_tier_requires_every_check():
    allc = mk(FAST + MAIN_ONLY)
    assert tier.filt("main", allc) == allc  # nothing dropped: full required list


def test_release_tier_is_exactly_fast_list():
    got = [c["name"] for c in tier.filt("release", mk(FAST + MAIN_ONLY))]
    assert got == FAST


def test_failing_main_only_check_fatal_on_main_ignored_on_release():
    allc = mk(FAST) + mk(["CodeQL Analysis"], "failure")
    assert any(c["conclusion"] == "failure" for c in tier.filt("main", allc))
    assert not any(c["conclusion"] == "failure" for c in tier.filt("release", allc))


def test_failing_fast_check_stays_fatal_on_release():
    allc = mk(FAST[1:]) + mk([FAST[0]], "failure")
    assert any(c["conclusion"] == "failure" for c in tier.filt("release", allc))


def test_missing_required_check_fails_both_tiers():
    for t in ("release", "main"):
        assert tier.missing(t, mk(FAST)) == []
        short = mk([n for n in FAST if n != "Security & Code Quality"])
        assert tier.missing(t, short) == ["Security & Code Quality"]
        assert len(tier.missing(t, [])) == len(tier.required_present(t)) > 0


def test_required_present_are_all_in_release_set():
    assert set(tier.required_present("release")) <= set(FAST)
    assert set(tier.required_present("main")) >= set(tier.required_present("release"))


def test_tier_for_base():
    assert tier.tier_for_base("release/v3.0.X") == "release"
    assert tier.tier_for_base("refs/heads/release/v3.0.X") == "release"
    for b in ("main", "refs/heads/main", "", "feature/x"):
        assert tier.tier_for_base(b) == "main"  # fail closed


def test_cli_roundtrip_and_missing_exit_code():
    r = subprocess.run([sys.executable, str(P), "filter", "--tier", "release"],
                       input=json.dumps(mk(FAST + MAIN_ONLY)), capture_output=True, text=True, check=True)
    assert [c["name"] for c in json.loads(r.stdout)] == FAST
    r = subprocess.run([sys.executable, str(P), "missing", "--tier", "main"],
                       input="[]", capture_output=True, text=True)
    assert r.returncode == 1 and r.stdout.strip()
