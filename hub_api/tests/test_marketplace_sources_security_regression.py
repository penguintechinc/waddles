"""Consolidated security-regression suite for marketplace sources (Phase 1, Task 11).

One named place that pins every security-critical finding to the test(s) that enforce it, so a
deleted/renamed/skipped guard turns THIS suite red (`make test-marketplace-security` runs it plus
every referenced file, in normal CI -- no cluster). Assertions already owned by a per-task file are
referenced, not duplicated. This file ADDS:

* the registry check (each referenced test exists; denominators printed/asserted), and
* the E2E-fixture cross-check: every hostile repo the flag-on E2E clones (built by
  `scripts/marketplace_fixture_git_server.py`) is pushed through the REAL scanner here, so a
  fixture that silently stops being hostile fails in CI rather than as a false-green E2E.

Cluster-only regressions (real egress-proxy denial, direct-git-egress-denied under the CNP,
real sandbox Job no-egress, DNS rebind through the real proxy, real executor signature
verification) live in `tests/api/test_marketplace_sources_e2e.py` -- they CANNOT be proven here.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from services import marketplace_source_scanner as sc
from tests.malicious_source_fixtures import make_source

TESTS = Path(__file__).resolve().parent
SCRIPT = TESTS.parents[1] / "scripts" / "marketplace_fixture_git_server.py"


@dataclass(slots=True, frozen=True)
class Regression:
    """One security finding and the existing tests that enforce it (`file::test_name`)."""

    finding: str
    nodes: tuple[str, ...]


REGRESSIONS: tuple[Regression, ...] = (
    # regression: generic /bundle_approvals approve route skipped every marketplace gate for
    # source_id rows (namespace, disabled, platform re-sign, human installer, audit).
    Regression(
        "approval-bypass",
        (
            "test_marketplace_source_ingest.py::test_generic_route_runs_marketplace_gates_for_sourced_version",
            "test_marketplace_source_ingest.py::test_generic_route_refuses_disabled_source_version",
            "test_source_namespace_gate.py::test_install_path_invokes_namespace_gate",
            "test_source_namespace_gate.py::test_install_authority_matrix",
        ),
    ),
    # regression: a fail-loud audit write raising must not skip bundle deactivation (fail-open
    # drift: disabled source's bundles stayed live).
    Regression(
        "fail-open-drift",
        ("test_marketplace_source_service.py::test_disable_and_deactivate_survives_audit_failure",),
    ),
    # regression: TOCTOU -- a disable landing after approve's status re-read must not install;
    # the lock must be a real `SELECT ... FOR UPDATE` on the source row.
    Regression(
        "toctou-row-lock",
        (
            "test_marketplace_source_ingest.py::test_disable_between_status_read_and_install_refused",
            "test_marketplace_source_ingest.py::test_source_lock_statement_is_for_update",
            "test_marketplace_source_ingest.py::test_source_disabled_mid_ingest_creates_no_version",
        ),
    ),
    # regression: namespace shadow -- external sources must never publish waddles.core.* or
    # another source's src- prefix; unknown source fails closed.
    Regression(
        "namespace-shadow",
        (
            "test_source_namespace_gate.py::test_external_rejects_core_cross_source_and_escape",
            "test_source_namespace_gate.py::test_external_rejects_bare_prefix",
            "test_source_namespace_gate.py::test_unknown_source_fails_closed",
            "test_marketplace_source_scanner.py::test_core_namespace_claim_rejected",
            "test_marketplace_source_scanner.py::test_cross_source_claim_rejected",
        ),
    ),
    # regression: denylist is deny-by-default -- unsupported manifests, off-registry sources,
    # symlinked manifests and config files are rejected, not skipped.
    Regression(
        "denylist-deny-by-default",
        (
            "test_marketplace_source_scanner.py::test_denylist_unsupported_manifest_rejected",
            "test_marketplace_source_scanner.py::test_denylist_non_registry_sources_rejected",
            "test_marketplace_source_scanner.py::test_denylist_symlinked_manifest_rejected",
            "test_marketplace_source_scanner.py::test_denylisted_dependency_rejected",
            "test_marketplace_source_scanner.py::test_denylist_npm_alias_and_lock_name",
        ),
    ),
    # regression: attestation replay -- a publisher signature is bound to the assigned src- app_id,
    # version and digest; replay across sources/names/versions or tampering is rejected.
    Regression(
        "attestation-replay",
        (
            "test_marketplace_publisher_attestation.py::test_replay_across_sources_rejected",
            "test_marketplace_publisher_attestation.py::test_signature_bound_to_app_id_and_version",
            "test_marketplace_publisher_attestation.py::test_wrong_publisher_key_rejected",
            "test_marketplace_publisher_attestation.py::test_tampered_artifact_rejected",
            "test_marketplace_publisher_attestation.py::test_keyless_source_rejected",
        ),
    ),
    # regression: SSRF/scheme -- loopback/metadata/RFC1918 hosts and non-https schemes rejected at
    # the scanner AND at the API add route.
    Regression(
        "ssrf-and-scheme",
        (
            "test_marketplace_source_scanner.py::test_ssrf_and_scheme_rejected",
            "test_v1_marketplace_sources_blueprint.py::test_invalid_or_ssrf_git_url_rejected",
            "test_marketplace_source_scanner.py::test_no_proxy_in_prod_mode_fails_closed",
        ),
    ),
    # regression: gates on the add/approve-update API (ack, tier, builtin immutability).
    Regression(
        "api-gates",
        (
            "test_v1_marketplace_sources_blueprint.py::test_tier_matrix",
            "test_v1_marketplace_sources_blueprint.py::test_sandbox_without_valid_ack_rejected_422",
            "test_v1_marketplace_sources_blueprint.py::test_is_builtin_not_settable",
            "test_v1_marketplace_sources_blueprint.py::test_builtin_cannot_be_deleted",
            "test_v1_marketplace_sources_blueprint.py::test_approve_update_advances_pin_only_when_pending",
        ),
    ),
)


def _referenced_files() -> set[str]:
    return {n.split("::")[0] for r in REGRESSIONS for n in r.nodes}


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
    }


def test_registry_has_the_expected_denominator() -> None:
    """Zero/short registry is a failure, never a pass (a gate must prove it ran)."""
    findings = {r.finding for r in REGRESSIONS}
    assert findings == {
        "approval-bypass", "fail-open-drift", "toctou-row-lock", "namespace-shadow",
        "denylist-deny-by-default", "attestation-replay", "ssrf-and-scheme", "api-gates",
    }  # fmt: skip
    assert sum(len(r.nodes) for r in REGRESSIONS) >= 30
    assert all(r.nodes for r in REGRESSIONS)


@pytest.mark.parametrize("fname", sorted(_referenced_files()))
def test_every_referenced_test_exists(fname: str) -> None:
    """A renamed/deleted enforcing test must fail here, not silently drop the regression."""
    path = TESTS / fname
    assert path.is_file(), f"{fname} missing"
    defined = _defined_tests(path)
    missing = [
        n for r in REGRESSIONS for n in r.nodes if n.startswith(fname + "::")
        and n.split("::")[1] not in defined
    ]  # fmt: skip
    assert not missing, f"enforcing tests vanished: {missing}"


def test_no_referenced_test_is_unconditionally_skipped() -> None:
    """A skip/xfail marker on an enforcing test would silently disable the regression."""
    bad: list[str] = []
    wanted = {n.split("::")[1]: n.split("::")[0] for r in REGRESSIONS for n in r.nodes}
    for fname in _referenced_files():
        tree = ast.parse((TESTS / fname).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and wanted.get(node.name) == fname
            ):
                for dec in node.decorator_list:
                    if "skip" in ast.unparse(dec) or "xfail" in ast.unparse(dec):
                        bad.append(f"{fname}::{node.name}")
    assert not bad, bad


# --- E2E fixture cross-check: every hostile repo is rejected by the REAL scanner ----------------


def _load_fixture_server() -> Any:
    mod = sys.modules.get("mkt_fixture_git")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("mkt_fixture_git", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mkt_fixture_git"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory: pytest.TempPathFactory) -> Any:
    fx = _load_fixture_server()
    store = fx.FixtureStore(
        tmp_path_factory.mktemp("hostile"),
        base_url="https://fixture.test",
        wasm=fx.PLACEHOLDER_WASM,
    )
    store.build_all()
    return store


@pytest.fixture
def _transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(sc.TEST_TRANSPORT_ENV, "1")

    async def _ok(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(sc, "validate_outbound_url", _ok)
    yield
    sc._test_remote = None


# regression: each hostile fixture the flag-on E2E uses must be a REAL rejection at the scanner.
WHOLE_SOURCE = {
    "hostile-symlink-escape": "symlink_escape",
    "hostile-symlink-abs": "symlink_absolute",
    "hostile-zip-bomb": "archive_bomb",
    "hostile-deep-path": "too_deep",
}
PER_BUNDLE = {
    "hostile-core-shadow": "namespace_violation",
    "hostile-other-prefix": "namespace_violation",
    "hostile-denylisted-dep": "denylisted_dependency",
}


@pytest.mark.usefixtures("_transport")
@pytest.mark.parametrize(("repo", "reason"), sorted(WHOLE_SOURCE.items()))
async def test_hostile_fixture_rejects_whole_source(
    fixture_store: Any, repo: str, reason: str, tmp_path: Path
) -> None:
    sc.enable_test_transport(f"file://{fixture_store.repos[repo].bare}")
    with pytest.raises(sc.ScanRejectedError) as ei:
        await sc.scan_source(make_source(fixture_store.head(repo)), workdir=tmp_path)
    assert ei.value.reason == reason


@pytest.mark.usefixtures("_transport")
@pytest.mark.parametrize(("repo", "reason"), sorted(PER_BUNDLE.items()))
async def test_hostile_fixture_rejects_the_bundle(
    fixture_store: Any, repo: str, reason: str, tmp_path: Path
) -> None:
    sc.enable_test_transport(f"file://{fixture_store.repos[repo].bare}")
    result = await sc.scan_source(make_source(fixture_store.head(repo)), workdir=tmp_path)
    assert result.examined == 1 and not result.bundles
    assert [r.reason for r in result.rejected] == [reason]


@pytest.mark.usefixtures("_transport")
async def test_valid_prebuilt_fixture_is_accepted_by_the_scanner(
    fixture_store: Any, tmp_path: Path
) -> None:
    """The control: the non-hostile fixture scans clean, so the rejections above are meaningful."""
    sc.enable_test_transport(f"file://{fixture_store.repos['prebuilt'].bare}")
    result = await sc.scan_source(make_source(fixture_store.head("prebuilt")), workdir=tmp_path)
    assert result.examined == 1 and len(result.bundles) == 1 and not result.rejected
