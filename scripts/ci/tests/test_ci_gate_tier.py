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
    "Rust bundle-executor e2e (flag-on command, main tier)",
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
        assert tier.missing(t, mk(FAST + (tier.MAIN_EXTRA_PRESENT if t == "main" else []))) == []
        extra = tier.MAIN_EXTRA_PRESENT if t == "main" else []
        short = mk([n for n in FAST if n != "Security & Code Quality"] + extra)
        assert tier.missing(t, short) == ["Security & Code Quality"]
        assert len(tier.missing(t, [])) == len(tier.required_present(t)) > 0


def test_required_present_are_all_in_release_set():
    assert set(tier.required_present("release")) <= set(FAST)
    assert set(tier.required_present("main")) >= set(tier.required_present("release"))


def test_bundle_executor_e2e_required_on_main_only():
    name = "Rust bundle-executor e2e (flag-on command, main tier)"
    assert name in tier.required_present("main")
    assert name not in tier.required_present("release")
    assert tier.missing("main", mk(FAST)) == [name]
    assert tier.missing("release", mk(FAST)) == []
    assert tier.is_main_only(name)


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


# ---- path gating (release PRs only) ------------------------------------------
DET = "Detect changed areas"


def mk_mixed(ran, skipped, detector="success"):
    """Build a check set: `ran` succeed, `skipped` are detector-skipped, one detector check."""
    out = mk(ran) + [{"name": n, "status": "completed", "conclusion": "skipped"} for n in skipped]
    return out + [{"name": DET, "status": "completed", "conclusion": detector}]


RUST_JOBS = ["build svc-action", "build svc-ingest", "fmt + clippy + deny + coverage / Lint and Test"]
PY_JOBS = ["Python unit tests", "Python security checks"]


def gate_ok(tier_name, checks):
    """Mirror ci-gate's verdict: nothing missing, nothing failed, skips trusted, something ran."""
    ex = tier.filt(tier_name, checks)
    if not ex or tier.missing(tier_name, checks):
        return False
    if any(c["conclusion"] in ("failure", "cancelled", "timed_out", "action_required") for c in ex):
        return False
    r = tier.skip_report(tier_name, ex)
    return r["ran"] > 0 and not r["skipped_untrusted"]


REQ = [n for n in tier.required_present("release") if n != DET]


def test_python_only_diff_rust_skipped_gate_passes():
    c = mk_mixed(REQ + PY_JOBS, RUST_JOBS)
    assert gate_ok("release", c)
    assert tier.skip_report("release", c)["skipped_not_affected"] == len(RUST_JOBS)


def test_detector_missing_makes_skips_untrusted_and_missing_fails():
    c = mk(REQ + PY_JOBS) + [{"name": n, "status": "completed", "conclusion": "skipped"} for n in RUST_JOBS]
    assert not gate_ok("release", c)
    assert tier.missing("release", c) == [DET]


def test_detector_failed_or_cancelled_fails_gate():
    for bad in ("failure", "cancelled"):
        assert not gate_ok("release", mk_mixed(REQ + PY_JOBS, RUST_JOBS, detector=bad))


def test_required_job_failed_or_cancelled_still_fails_on_release():
    for bad in ("failure", "cancelled"):
        c = mk_mixed(REQ[1:] + PY_JOBS, RUST_JOBS) + mk([REQ[0]], bad)
        assert not gate_ok("release", c)


def test_required_job_missing_fails_even_with_trusted_detector():
    c = mk_mixed([n for n in REQ if n != "Security & Code Quality"] + PY_JOBS, RUST_JOBS)
    assert not gate_ok("release", c)


def test_zero_ran_or_zero_examined_fails():
    only_skips = [{"name": n, "status": "completed", "conclusion": "skipped"} for n in RUST_JOBS]
    assert tier.skip_report("release", only_skips)["ran"] == 0
    assert not gate_ok("release", [])
    r = subprocess.run([sys.executable, str(P), "skips", "--tier", "release"],
                       input="[]", capture_output=True, text=True)
    assert r.returncode == 1


def test_cargo_lock_diff_all_rust_jobs_ran_gate_passes():
    # Detector turned every rust job on -> they ran (success), nothing skipped.
    assert gate_ok("release", mk_mixed(REQ + RUST_JOBS + PY_JOBS, []))


def test_workflow_diff_everything_ran_gate_passes():
    assert gate_ok("release", mk_mixed(REQ + RUST_JOBS + PY_JOBS, []))


def test_failing_rust_job_on_cargo_lock_diff_fails():
    c = mk_mixed(REQ + PY_JOBS, []) + mk(RUST_JOBS[:1], "failure")
    assert not gate_ok("release", c)


def test_main_pr_every_check_required_and_skip_never_attributed_to_detector():
    c = mk_mixed(REQ + RUST_JOBS + PY_JOBS, []) + mk(MAIN_ONLY)
    assert tier.filt("main", c) == c  # everything examined on main
    # A skip on main is never counted as "not affected" path-gating.
    s = mk_mixed(REQ + PY_JOBS, RUST_JOBS)
    assert tier.skip_report("main", tier.filt("main", s))["skipped_not_affected"] == 0
    # A failing main-only check is fatal on main.
    assert not gate_ok("main", c + mk(["CodeQL Analysis"], "failure"))
    # Detector dropped: on main, missing = detector + every main-extra check not supplied.
    assert tier.missing("main", mk(FAST[1:])) == [DET] + tier.MAIN_EXTRA_PRESENT
    assert tier.missing("main", mk(FAST[1:] + tier.MAIN_EXTRA_PRESENT)) == [DET]


def test_bundle_executor_e2e_required_on_main_not_release():
    e2e = "Rust bundle-executor e2e (flag-on command, main tier)"
    assert e2e in tier.MAIN_EXTRA_PRESENT
    assert e2e in tier.required_present("main")
    assert e2e not in tier.required_present("release")
    assert e2e in tier.missing("main", mk(FAST))
    assert e2e not in tier.missing("release", mk(FAST))
