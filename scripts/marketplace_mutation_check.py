#!/usr/bin/env python3
"""Mutation check for the marketplace-sources security gates (plan Task 11 Step 2).

For each gate (SHA pin / namespace / ack / tier / attestation) a registered, exact-text mutation
disables the gate in source; the matching tests MUST then fail. A mutation that is not applicable
(text not found exactly once) or that SURVIVES (tests still pass) is a hard failure -- a gate whose
test cannot fail is not a gate.

Modes::

    unit     (no cluster, runs in CI/locally) apply -> run the gate's hub_api tests -> expect RED
             -> revert (always, in `finally`); also requires the UNMUTATED baseline to be GREEN.
    apply    GATE   apply the mutation and stop (11C: then rebuild/deploy hub-api to the cluster).
    revert   GATE   `git checkout --` the mutated files (11C: after the E2E assertion).
    e2e-k    GATE   print the pytest -k expression selecting the gate's cluster test (11C:
                    `make test-marketplace-e2e PYTEST_K="$(... e2e-k GATE)"` must FAIL while mutated).

Safety: refuses to touch a file with uncommitted changes (revert is `git checkout -- <file>`).
Needs HUB_PYTHON (a python3 with hub_api test deps); defaults to the current interpreter.
"""

# ruff: noqa: E501, S607  (exact-text mutation anchors cannot be wrapped)
from __future__ import annotations

import argparse
import os
import re
import subprocess  # noqa: S404
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HUB = REPO / "hub_api"


@dataclass(slots=True, frozen=True)
class Edit:
    """One exact-text replacement (must match exactly once)."""

    path: str
    old: str
    new: str


@dataclass(slots=True, frozen=True)
class Mutation:
    """A gate, the edits that disable it, and the tests that must then fail."""

    gate: str
    edits: tuple[Edit, ...]
    unit_tests: tuple[str, ...]
    e2e_k: str


SCANNER = "hub_api/services/marketplace_source_scanner.py"
MUTATIONS: dict[str, Mutation] = {
    "sha-pin": Mutation(
        "sha-pin",
        (
            Edit(
                SCANNER,
                '"origin", pinned, watch=root)',
                '"origin", "main", watch=root)',
            ),
            Edit(SCANNER, "    if resolved != pinned:\n", "    if False:\n"),
            Edit(SCANNER, "    if head != pinned:\n", "    if False:\n"),
        ),
        ("tests/test_marketplace_source_scanner.py::test_sha_mismatch_aborts",),
        "sha_pin",
    ),
    "namespace": Mutation(
        "namespace",
        (
            Edit(
                "hub_api/services/vendor_bundle_authz.py",
                "    if source is not None and source.is_builtin:\n        if app_id.startswith(CORE_NAMESPACE_PREFIX) and _remainder_ok(",  # noqa: E501
                "    return\n    if source is not None and source.is_builtin:\n        if app_id.startswith(CORE_NAMESPACE_PREFIX) and _remainder_ok(",  # noqa: E501
            ),
        ),
        (
            "tests/test_source_namespace_gate.py::test_external_rejects_bare_prefix",
            "tests/test_marketplace_source_scanner.py::test_core_namespace_claim_rejected",
        ),
        "namespace",
    ),
    "ack": Mutation(
        "ack",
        (
            Edit(
                "hub_api/blueprints/v1/marketplace_sources.py",
                "        if mode == BuildMode.SANDBOX:\n            if data.sandbox_trust_ack != WARNING_HASH:\n",
                "        if mode == BuildMode.SANDBOX:\n            if False:\n",
            ),
        ),
        (
            "tests/test_v1_marketplace_sources_blueprint.py::test_sandbox_without_valid_ack_rejected_422",
        ),
        "ack",
    ),
    "tier": Mutation(
        "tier",
        (
            Edit(
                "hub_api/blueprints/v1/marketplace_sources.py",
                "    required = TIER_ENTERPRISE if mode == BuildMode.SANDBOX else TIER_PROFESSIONAL\n",
                "    return None\n    required = TIER_ENTERPRISE if mode == BuildMode.SANDBOX else TIER_PROFESSIONAL\n",
            ),
        ),
        ("tests/test_v1_marketplace_sources_blueprint.py::test_tier_matrix",),
        "tier",
    ),
    "attestation": Mutation(
        "attestation",
        (
            Edit(
                "hub_api/services/marketplace_publisher_attestation.py",
                "        key.verify(sig, payload)\n    except InvalidSignature as exc:",
                "        pass\n    except InvalidSignature as exc:",
            ),
        ),
        (
            "tests/test_marketplace_publisher_attestation.py::test_wrong_publisher_key_rejected",
            "tests/test_marketplace_publisher_attestation.py::test_replay_across_sources_rejected",
        ),
        "attestation",
    ),
}


class MutationError(Exception):
    """A precondition failed (dirty file, text not found exactly once, ...)."""


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def apply(m: Mutation) -> None:
    """Apply `m`'s edits; every file must be clean and every `old` must occur exactly once."""
    texts: dict[str, str] = {}
    for e in m.edits:
        if _git("status", "--porcelain", "--", e.path).strip():
            raise MutationError(f"{e.path} has uncommitted changes; refusing to mutate")
        texts.setdefault(e.path, (REPO / e.path).read_text())
    for e in m.edits:
        if texts[e.path].count(e.old) != 1:
            raise MutationError(f"{m.gate}: mutation text not found exactly once in {e.path}")
        texts[e.path] = texts[e.path].replace(e.old, e.new)
    for path, text in texts.items():
        (REPO / path).write_text(text)


def revert(m: Mutation) -> None:
    """Restore the mutated files from git."""
    _git("checkout", "--", *sorted({e.path for e in m.edits}))


def run_tests(nodes: tuple[str, ...]) -> tuple[int, int, int]:
    """Run pytest on `nodes` (cwd hub_api); returns (returncode, passed, failed)."""
    py = os.environ.get("HUB_PYTHON", sys.executable)
    proc = subprocess.run(  # noqa: S603
        [py, "-m", "pytest", *nodes, "--no-cov", "-q", "-p", "no:cacheprovider", "-W", "ignore"],
        cwd=HUB, capture_output=True, text=True, check=False,
    )  # fmt: skip

    def count(word: str) -> int:
        hit = re.search(rf"(\d+) {word}", proc.stdout)
        return int(hit.group(1)) if hit else 0

    return proc.returncode, count("passed"), count("failed")


def unit_check(gates: list[str]) -> int:
    """Baseline must be green; each mutation must turn its tests red. Returns process exit code."""
    rc = 0
    for g in gates:
        m = MUTATIONS[g]
        code, passed, failed = run_tests(m.unit_tests)
        if code != 0 or passed < len(m.unit_tests):
            print(
                f"FAIL {g}: unmutated baseline not green (rc={code} passed={passed} failed={failed})"
            )  # noqa: T201
            rc = 1
            continue
        apply(m)
        try:
            code, mpassed, mfailed = run_tests(m.unit_tests)
        finally:
            revert(m)
        killed = code != 0 and mfailed >= 1
        print(
            f"{'KILLED  ' if killed else 'SURVIVED'} {g}: baseline passed={passed}; mutated passed={mpassed} failed={mfailed}"  # noqa: E501
        )  # noqa: T201
        rc |= 0 if killed else 1
    if not gates:
        print("FAIL: zero gates examined")  # noqa: T201
        return 1
    print(f"examined {len(gates)} gate(s)")  # noqa: T201
    return rc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("mode", choices=["unit", "apply", "revert", "e2e-k", "list"])
    p.add_argument("gate", nargs="?", choices=[*MUTATIONS, "all"], default="all")
    args = p.parse_args(argv)
    gates = list(MUTATIONS) if args.gate == "all" else [args.gate]
    try:
        if args.mode == "list":
            for g in MUTATIONS:
                print(g)  # noqa: T201
            return 0
        if args.mode == "unit":
            return unit_check(gates)
        if args.gate == "all":
            raise MutationError(f"{args.mode} needs a single GATE")
        m = MUTATIONS[args.gate]
        if args.mode == "apply":
            apply(m)
        elif args.mode == "revert":
            revert(m)
        else:
            print(m.e2e_k)  # noqa: T201
    except (MutationError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
