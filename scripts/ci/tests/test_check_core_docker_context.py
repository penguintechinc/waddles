"""Unit tests for scripts/ci/check-core-docker-context.py.

Fixture repos exercise the destination check (a path dep COPYed, but to the wrong
container path -- the svc-streaming `/platform_keys` bug from deploy runs
43859970070/43858247593), a missing COPY, test-only module exclusion, named build
contexts, and the zero-examined failure.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).parent.parent / "check-core-docker-context.py"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module() -> Any:
    """Loads the hyphenated CI script as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "check_core_docker_context", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()

GOOD = """\
FROM rust:1 AS builder
WORKDIR /build
COPY keys ./keys
WORKDIR /build/svc
COPY svc/Cargo.toml svc/Cargo.lock ./
COPY svc/src ./src
COPY --from=proto . /proto
FROM debian AS runtime
COPY --from=builder /out/svc /app/svc
"""

# The svc-streaming shape: COPY before any WORKDIR -> lands at /keys, not /build/keys.
WRONG_DEST = GOOD.replace("WORKDIR /build\nCOPY keys ./keys\n", "COPY keys ./keys\n")
MISSING = GOOD.replace("COPY keys ./keys\n", "")


def _repo(tmp_path: Path, dockerfile: str, test_only_fixture: bool = False) -> Path:
    """Builds a fixture repo: core/svc depends on core/keys; image-inputs maps svc."""
    core = tmp_path / "core"
    (core / "keys" / "src").mkdir(parents=True)
    (core / "keys" / "Cargo.toml").write_text(
        '[package]\nname = "keys"\nversion = "0.1.0"\n'
    )
    (core / "keys" / "src" / "lib.rs").write_text("pub fn k() {}\n")
    (core / "svc" / "src").mkdir(parents=True)
    (core / "svc" / "Cargo.toml").write_text(
        '[package]\nname = "svc"\nversion = "0.1.0"\n\n[dependencies]\n'
        'keys = { path = "../keys" }\n\n[dev-dependencies]\nfx = { path = "../fx" }\n'
    )
    (core / "svc" / "Cargo.lock").write_text("")
    main = "fn main() {}\n"
    if test_only_fixture:
        main += "#[cfg(test)]\nmod t;\n"
        (core / "svc" / "src" / "t.rs").write_text(
            'const F: &str = include_str!("../../fixtures/x.json");\n'
        )
    (core / "svc" / "src" / "main.rs").write_text(main)
    (core / "svc" / "Dockerfile.rust").write_text(dockerfile)
    (tmp_path / "proto").mkdir()
    ci = tmp_path / "scripts" / "ci"
    ci.mkdir(parents=True)
    (ci / "image-inputs.yaml").write_text(
        "images:\n  svc:\n    dockerfile: core/svc/Dockerfile.rust\n    context: core\n"
        '    build_contexts: ["proto=./proto"]\n  py:\n    dockerfile: py/Dockerfile\n'
    )
    (tmp_path / "py").mkdir()
    (tmp_path / "py" / "Dockerfile").write_text("FROM python\n")
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, root: Path, *extra: str) -> int:
    """Runs main() against `root` with extra CLI args."""
    monkeypatch.setattr(sys, "argv", ["x", "--repo-root", str(root), *extra])
    return int(mod.main())


def test_good_layout_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Path dep COPYed to the sibling path the crate expects -> pass, denominator printed."""
    assert _run(monkeypatch, _repo(tmp_path, GOOD)) == 0
    out = capsys.readouterr().out
    assert "images_examined=1 crates_examined=2" in out
    assert "failing=0" in out


def test_wrong_destination_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """regression: deploy runs 43859970070/43858247593 (COPY before WORKDIR)."""
    assert _run(monkeypatch, _repo(tmp_path, WRONG_DEST)) == 1
    assert "COPYed to /keys, crate expects /build/keys" in capsys.readouterr().out


def test_missing_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Path dep never COPYed -> fail naming the expected container path."""
    assert _run(monkeypatch, _repo(tmp_path, MISSING)) == 1
    assert "core/keys not COPYed (needed at /build/keys)" in capsys.readouterr().out


def test_test_only_module_fixture_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_str! inside a #[cfg(test)] module is never compiled by the release build."""
    assert _run(monkeypatch, _repo(tmp_path, GOOD, test_only_fixture=True)) == 0


def test_override_checks_alternate_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dockerfile-override evaluates other Dockerfile text at the image's real location."""
    root = _repo(tmp_path, GOOD)
    bad = tmp_path / "bad.txt"
    bad.write_text(WRONG_DEST)
    assert _run(monkeypatch, root, "--dockerfile-override", f"svc={bad}") == 1


def test_zero_examined_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No Cargo-building image in the inputs map is a failure, never a silent pass."""
    root = _repo(tmp_path, GOOD)
    (root / "scripts" / "ci" / "image-inputs.yaml").write_text(
        "images:\n  py:\n    dockerfile: py/Dockerfile\n"
    )
    assert _run(monkeypatch, root) == 1


def test_real_repo_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo's own core Dockerfiles satisfy the gate."""
    assert _run(monkeypatch, REPO_ROOT) == 0


def test_real_pre_fix_streaming_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """regression: the pre-fix svc-streaming Dockerfile.rust is rejected."""
    fixture = (
        REPO_ROOT
        / "scripts/ci/tests/fixtures/core-docker-context/svc_streaming-pre-fix.dockerfile.txt"
    )
    assert (
        _run(
            monkeypatch, REPO_ROOT, "--dockerfile-override", f"svc-streaming={fixture}"
        )
        == 1
    )
    assert "crate expects /build/platform_keys" in capsys.readouterr().out
