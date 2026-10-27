"""Unit tests for scripts/ci/check-dockerfile-rust-path-deps.py.

Fixture repos (good + bad) exercise the transitive path-dependency walk and
the COPY-coverage assertion end to end, without touching the real repo tree
-- see gh-425/gh-498/gh-465/gh-468/gh-469 for the failure class this guards.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parent.parent / "check-dockerfile-rust-path-deps.py"


def _load_module() -> Any:
    """Loads the hyphenated CI script as an importable module for unit tests."""
    spec = importlib.util.spec_from_file_location("check_dockerfile_rust_path_deps", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered to resolve types
    spec.loader.exec_module(module)
    return module


mod = _load_module()

CRATE_CARGO_TOML = """
[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[dependencies]
{deps}
"""

DOCKERFILE_GOOD = """\
FROM rust:1.97-slim-bookworm AS builder
WORKDIR /build
COPY svc_action/Cargo.toml svc_action/Cargo.lock ./svc_action/
COPY svc_action/src ./svc_action/src
COPY bundle_active_set ./bundle_active_set
COPY bundle_host_http ./bundle_host_http
COPY egress_assertion ./egress_assertion
WORKDIR /build/svc_action
RUN cargo build --release --locked
FROM debian:bookworm-slim AS runtime
COPY --from=builder /build/svc_action/target/release/svc-action /app/svc-action
"""

DOCKERFILE_BAD = """\
FROM rust:1.97-slim-bookworm AS builder
WORKDIR /build
COPY svc_action/Cargo.toml svc_action/Cargo.lock ./svc_action/
COPY svc_action/src ./svc_action/src
COPY bundle_active_set ./bundle_active_set
COPY bundle_host_http ./bundle_host_http
WORKDIR /build/svc_action
RUN cargo build --release --locked
FROM debian:bookworm-slim AS runtime
COPY --from=builder /build/svc_action/target/release/svc-action /app/svc-action
"""


def _build_fixture_repo(root: Path, dockerfile_text: str) -> None:
    """Builds a minimal repo tree mirroring svc_action's real path-dep chain:
    svc_action -> bundle_active_set, bundle_host_http -> egress_assertion."""
    core = root / "core"
    (core / "svc_action" / "src").mkdir(parents=True)
    (core / "bundle_active_set" / "src").mkdir(parents=True)
    (core / "bundle_host_http" / "src").mkdir(parents=True)
    (core / "egress_assertion" / "src").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)

    (core / "svc_action" / "Cargo.toml").write_text(
        CRATE_CARGO_TOML.format(
            name="svc-action",
            deps=(
                'bundle-active-set = { path = "../bundle_active_set", version = "=0.1.0" }\n'
                'bundle-host-http = { path = "../bundle_host_http", version = "=0.1.0" }\n'
            ),
        )
    )
    (core / "bundle_active_set" / "Cargo.toml").write_text(
        CRATE_CARGO_TOML.format(name="bundle-active-set", deps="")
    )
    (core / "bundle_host_http" / "Cargo.toml").write_text(
        CRATE_CARGO_TOML.format(
            name="bundle-host-http",
            deps='egress-assertion = { path = "../egress_assertion", version = "=0.1.0" }\n',
        )
    )
    (core / "egress_assertion" / "Cargo.toml").write_text(
        CRATE_CARGO_TOML.format(name="egress-assertion", deps="")
    )
    (core / "svc_action" / "Dockerfile.rust").write_text(dockerfile_text)
    (root / ".github" / "workflows" / "build-svc-action.yml").write_text(
        "env:\n"
        "  MODULE_PATH: core/svc_action\n"
        "  BUILD_CONTEXT: core\n"
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: docker/build-push-action@x\n"
        "        with:\n"
        "          context: ${{ env.BUILD_CONTEXT }}\n"
        "          file: ${{ env.MODULE_PATH }}/Dockerfile.rust\n"
    )


def _run(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestGoodFixture:
    """A Dockerfile COPYing every transitive path dependency passes."""

    def test_all_path_deps_copied_passes(self, tmp_path: Path) -> None:
        _build_fixture_repo(tmp_path, DOCKERFILE_GOOD)
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Examined 1 Dockerfile.rust file(s)" in result.stdout
        assert "OK" in result.stdout
        assert "FAIL" not in result.stdout


class TestBadFixture:
    """A missing transitive COPY (egress_assertion, 2 hops deep) fails loudly."""

    def test_missing_transitive_dep_fails(self, tmp_path: Path) -> None:
        _build_fixture_repo(tmp_path, DOCKERFILE_BAD)
        result = _run(tmp_path)
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "FAIL" in combined
        assert "egress_assertion" in combined


class TestZeroDenominator:
    """No Dockerfile.rust files anywhere is a hard failure, never a silent pass."""

    def test_empty_repo_fails(self, tmp_path: Path) -> None:
        (tmp_path / "core").mkdir()
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "0 Dockerfile.rust files found" in result.stderr


class TestTransitivePathDeps:
    """Unit-level: the BFS walk reaches 2-hop dependencies, not just direct ones."""

    def test_walks_two_hops(self, tmp_path: Path) -> None:
        _build_fixture_repo(tmp_path, DOCKERFILE_GOOD)
        crate_dir = tmp_path / "core" / "svc_action"
        deps = mod.transitive_path_deps(crate_dir)
        names = {d.name for d in deps}
        assert names == {"bundle_active_set", "bundle_host_http", "egress_assertion"}


def test_header_declared_context_used_when_no_workflow(tmp_path: Path) -> None:
    """A `# Build (context = core/` header resolves context when no workflow builds it."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    svc = tmp_path / "core" / "svc_x"
    svc.mkdir(parents=True)
    (svc / "Dockerfile.rust").write_text("# Build (context = core/, the parent)\nFROM scratch\n")
    ctx, source = mod.resolve_build_context(
        Path("core/svc_x/Dockerfile.rust"), tmp_path, tmp_path / ".github" / "workflows"
    )
    assert ctx == (tmp_path / "core").resolve()
    assert source == "dockerfile-header:context-declaration"
