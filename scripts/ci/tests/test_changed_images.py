"""Tests for scripts/ci/changed_images.py against the real image-input map.

Fixture diffs cover every image (own source, shared Rust crates, proto/wit, Cargo.lock,
Dockerfile), the shared_all invalidators, and every fail-safe path. The git-backed test
runs the real `git diff` in a throwaway repo (no mocks of git).
"""

import pathlib
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

import pytest

CI = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
import changed_images as ci

IMAP = ci.load_map(CI / "image-inputs.yaml")
ALL = frozenset(IMAP.images)
BASE = "a" * 40
HEAD = "b" * 40

RUST_PROTO = {
    "svc-ingest",
    "svc-process",
    "svc-action",
    "svc-presentation",
    "egress-proxy",
}

# path -> images that must rebuild. Every image appears in at least one row.
FIXTURES = {
    # own source
    "core/svc_ingest/src/connector/host_calls.rs": {"svc-ingest"},
    "core/svc_process/src/lib.rs": {"svc-process"},
    "core/svc_action/src/main.rs": {"svc-action"},
    "core/svc_presentation/src/main.rs": {"svc-presentation"},
    "core/egress_proxy/src/main.rs": {"egress-proxy"},
    "core/svc_streaming/src/main.rs": {"svc-streaming"},
    "core/svc_streaming/tests/smoke.rs": {"svc-streaming"},
    "core/bundle_executor/src/lib.rs": {"svc-bundles-executor"},
    "hub_api/blueprints/foo.py": {"hub-api"},
    "hub_api/app.py": {"hub-api"},
    "admin/hub_module/frontend/src/App.tsx": {"hub-webui"},
    "admin/hub_module/webui-server/server.js": {"hub-webui"},
    "migrations/run-alembic.sh": {"migrations"},
    "config/postgres/migrations/100_x.sql": {"migrations"},
    "scripts/db/bundle_x.sql": {"migrations"},
    "bundles/python/pyping/src/app.py": {"seeder"},
    "bundles/rust/ping/src/lib.rs": {"seeder"},
    "bundles/core-bundles.yaml": {"seeder"},
    "sdk/waddle-sdk-rs/src/lib.rs": {"seeder"},
    # shared dependencies
    "core/service_auth/src/lib.rs": {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "svc-presentation",
        "egress-proxy",
    },
    "core/hub_client/Cargo.lock": {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "svc-presentation",
    },
    "core/bundle_host_http/Cargo.lock": {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "egress-proxy",
    },
    "core/egress_assertion/src/lib.rs": {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "egress-proxy",
    },
    "core/bundle_host_kv/src/lib.rs": {"svc-process", "svc-action"},
    "core/bundle_host_media/src/lib.rs": {"svc-process"},
    "core/overlay_schema/src/lib.rs": {"svc-presentation"},
    "core/discord_mentions/src/lib.rs": {"svc-ingest", "svc-action"},
    "proto/waddles/hub/internal/v1/hub.proto": RUST_PROTO,
    "wit/waddle-bundle/world.wit": {"svc-bundles-executor", "seeder"},
    "hub_api/services/erasure_registry.py": {"hub-api", "seeder"},
    "hub_api/requirements.txt": {"hub-api", "seeder"},
    "alembic/versions/0088_x.py": {"hub-api", "migrations"},
    "alembic.ini": {"migrations"},
    "libs/flask_core/flask_core/x.py": {"hub-api", "migrations", "seeder"},
    ".dockerignore": {
        "hub-api",
        "hub-webui",
        "migrations",
        "seeder",
        "svc-bundles-executor",
    },
    # a Dockerfile edit rebuilds exactly its image (base-image digest bumps live here)
    "hub_api/Dockerfile": {"hub-api"},
    "admin/hub_module/Dockerfile.webui": {"hub-webui"},
    "migrations/Dockerfile": {"migrations"},
    "bundles/Dockerfile.core-bundles": {"seeder"},
    "core/bundle_executor/Dockerfile.rust": {"svc-bundles-executor"},
    "core/svc_ingest/Dockerfile.rust": {"svc-ingest"},
    "core/svc_process/Dockerfile.rust": {"svc-process"},
    "core/svc_action/Dockerfile.rust": {"svc-action"},
    "core/svc_presentation/Dockerfile.rust": {"svc-presentation"},
    "core/egress_proxy/Dockerfile.rust": {"egress-proxy"},
    "core/svc_streaming/Dockerfile.rust": {"svc-streaming"},
    # build machinery rebuilds everything
    "scripts/ci/image-inputs.yaml": ALL,
    "scripts/ci/changed_images.py": ALL,
    "scripts/ci/alpha_plan.py": ALL,
    ".github/workflows/deploy-dal2alpha.yml": ALL,
    # provably irrelevant
    "docs/ARCHITECTURE.md": set(),
    "README.md": set(),
    "k8s/helm/waddlebot/values.yaml": set(),
    "hub_api/tests/test_x.py": set(),
    "tests/e2e/x.py": set(),
    "core/svc_ingest/README.md": set(),
    "core/bundle_compiler/src/lib.rs": set(),
    ".github/ISSUE_TEMPLATE/bug.md": set(),
    ".github/workflows/rust-svc-ingest.yml": set(),
    "bundles/csharp/superpenguin-golf/Program.cs": set(),
    "website/index.html": set(),
}


def plan(
    paths: Iterable[str], base: str = BASE, head: str = HEAD, **kw: Any
) -> dict[str, ci.Decision]:
    """Run compute_plan with a fixed diff for every image."""
    bases: dict[str, str | None] = {n: base for n in IMAP.images}
    return ci.compute_plan(IMAP, bases, head, lambda _b, _h: list(paths), **kw)


def built(decisions: dict[str, ci.Decision]) -> set[str]:
    """Names of images with build=True."""
    return {n for n, d in decisions.items() if d.build}


@pytest.mark.parametrize("path,expected", sorted(FIXTURES.items()))
def test_fixture_diff(path: str, expected: set[str]) -> None:
    assert built(plan([path])) == set(expected), path


def test_every_image_is_triggered_by_some_fixture() -> None:
    covered = set()
    for exp in FIXTURES.values():
        covered |= set(exp)
    assert covered == ALL, f"images with no fixture: {ALL - covered}"
    assert len(ALL) == 11


def test_multi_path_diff_is_union() -> None:
    got = built(
        plan(["core/svc_streaming/src/a.rs", "hub_api/blueprints/x.py", "docs/a.md"])
    )
    assert got == {"svc-streaming", "hub-api"}


def test_force_all_builds_everything() -> None:
    d = plan([], force_all=True)
    assert built(d) == ALL
    assert all(x.reasons == ["force_all"] for x in d.values())


@pytest.mark.parametrize("bad", [None, "", "abc", "z" * 40, ci.ZERO_SHA])
def test_unknown_or_malformed_base_builds(bad: str | None) -> None:
    d = ci.compute_plan(IMAP, dict.fromkeys(IMAP.images, bad), HEAD, lambda _b, _h: [])
    assert built(d) == ALL


def test_one_image_with_unknown_base_builds_only_that_image() -> None:
    bases: dict[str, str | None] = {n: BASE for n in IMAP.images}
    bases["hub-webui"] = None
    d = ci.compute_plan(IMAP, bases, HEAD, lambda _b, _h: ["docs/x.md"])
    assert built(d) == {"hub-webui"}


def test_diff_failure_builds_everything() -> None:
    def boom(_b: str, _h: str) -> list[str]:
        raise RuntimeError("bad object")

    d = ci.compute_plan(IMAP, {n: BASE for n in IMAP.images}, HEAD, boom)
    assert built(d) == ALL
    assert "failed" in d["hub-api"].reasons[0]


def test_empty_diff_with_different_shas_is_suspicious() -> None:
    d = plan([])
    assert built(d) == ALL
    assert "suspicious" in d["hub-api"].reasons[0]


def test_same_sha_means_nothing_to_build() -> None:
    d = ci.compute_plan(IMAP, {n: HEAD for n in IMAP.images}, HEAD, lambda _b, _h: [])
    assert built(d) == set()


def test_unmapped_path_builds_everything() -> None:
    d = plan(["brand-new-toplevel/foo.py"])
    assert built(d) == ALL
    assert "unmapped" in d["svc-ingest"].reasons[0]


def test_unmapped_nested_path_builds_everything() -> None:
    assert built(plan(["core/brand_new_crate/src/lib.rs"])) == ALL


def test_per_image_bases_diff_independently() -> None:
    seen: list[str] = []

    def diff(b: str, _h: str) -> list[str]:
        seen.append(b)
        return ["hub_api/blueprints/x.py"] if b == "c" * 40 else ["docs/a.md"]

    bases: dict[str, str | None] = {n: "d" * 40 for n in IMAP.images}
    bases["hub-api"] = "c" * 40
    d = ci.compute_plan(IMAP, bases, HEAD, diff)
    assert built(d) == {"hub-api"}
    assert sorted(set(seen)) == ["c" * 40, "d" * 40]


@pytest.mark.parametrize(
    "pat,path,ok",
    [
        ("a/**", "a/b/c", True),
        ("a/**", "a", False),
        ("a/", "a/b", True),
        ("a/*.py", "a/b/c.py", False),
        ("a/*.py", "a/c.py", True),
        ("**/*.md", "x/y/z.md", True),
        ("**/*.md", "z.md", True),
        ("**/*.md", "z.mdx", False),
        ("package*.json", "package-lock.json", True),
        ("package*.json", "a/package.json", False),
        ("core/*/target/**", "core/x/target/release/y", True),
    ],
)
def test_glob_semantics(pat: str, path: str, ok: bool) -> None:
    assert bool(ci.glob_to_regex(pat).match(path)) is ok


def test_load_map_rejects_bad_maps(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "m.yaml"
    bad.write_text("version: 2\n")
    with pytest.raises(ci.MapError):
        ci.load_map(bad)
    bad.write_text(
        "version: 1\nlanes: {a: X}\nimages: {x: {dockerfile: d, context: '.', lane: z, inputs: [a]}}\n"
    )
    with pytest.raises(ci.MapError):
        ci.load_map(bad)
    bad.write_text(
        "version: 1\nlanes: {a: X}\nimages: {x: {dockerfile: d, context: '.', inputs: []}}\n"
    )
    with pytest.raises(ci.MapError):
        ci.load_map(bad)


def test_matrix_rows_carry_build_inputs() -> None:
    rows = {
        r["service"]: r for r in ci.matrix_for(IMAP, ["svc-streaming", "svc-ingest"])
    }
    assert rows["svc-streaming"]["target"] == "cpu"
    assert rows["svc-streaming"]["suffix"] == "-cpu"
    assert rows["svc-streaming"]["context"] == "core/svc_streaming"
    assert rows["svc-ingest"]["extra_ctx"] == "proto=./proto"
    assert rows["svc-ingest"]["context"] == "core"


def _git(repo: pathlib.Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_real_git_diff_end_to_end(tmp_path: pathlib.Path) -> None:
    """Real `git diff` in a throwaway repo, three commits, per-range verdicts."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    def commit(path: str, text: str, msg: str) -> str:
        f = tmp_path / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        _git(tmp_path, "add", path)
        _git(tmp_path, "commit", "-q", "-m", msg)
        return _git(tmp_path, "rev-parse", "HEAD")

    c0 = commit("docs/a.md", "0", "base")
    c1 = commit("docs/b.md", "1", "docs only")
    c2 = commit("core/egress_proxy/src/main.rs", "fn main(){}", "egress")
    c3 = commit("core/service_auth/Cargo.lock", "x", "shared lock")

    def run(base: str, head: str) -> set[str]:
        bases: dict[str, str | None] = {n: base for n in IMAP.images}
        return built(
            ci.compute_plan(
                IMAP, bases, head, lambda b, h: ci.git_diff_names(b, h, str(tmp_path))
            )
        )

    assert run(c0, c1) == set()
    assert run(c1, c2) == {"egress-proxy"}
    assert run(c2, c3) == {
        "svc-ingest",
        "svc-process",
        "svc-action",
        "svc-presentation",
        "egress-proxy",
    }
    assert run(c0, c3) >= {"egress-proxy", "svc-ingest"}
    assert run("f" * 40, c3) == ALL  # base object missing locally -> fail safe
